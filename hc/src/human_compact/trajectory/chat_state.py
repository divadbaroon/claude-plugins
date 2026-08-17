"""Durable, session-scoped Claude Code event ingestion for chat goals.

The global trajectory pipeline intentionally summarizes many Vault sessions.
This module is the separate state boundary for ``/goals-ui``: one Claude session,
one append-only logical event stream, one goal tree.  Transcript files are
treated as replaceable caches (Claude may truncate or rewrite them); stable
record ids and event deduplication are the correctness boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .goals import link_evidence_prompts, promote_todos  # noqa: F401
from .secure_io import secure_dir


SCHEMA_VERSION = 1
_TAIL_BYTES = 4096
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_COMMAND_TAG_RE = re.compile(
    r"<command-(name|message|args)>[\s\S]*?</command-\1>", re.IGNORECASE
)
_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*(?:\s[^\r\n]*)?$")
_UNSET = object()
_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.RLock] = {}
_LOCK_DEPTH = threading.local()


@dataclass(frozen=True)
class ChatPaths:
    base: Path
    session_dir: Path
    manifest: Path
    events: Path
    prompts: Path
    goals: Path
    important: Path
    goal_context: Path
    lock_dir: Path


@dataclass(frozen=True)
class IngestResult:
    session_id: str
    appended: int
    total_events: int
    last_ordinal: int
    prompt_count: int
    rewound: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _absolute(path: Path) -> Path:
    """Normalize a state path without resolving through symlinks."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _state_location(root: Optional[Path] = None) -> Tuple[Path, Path]:
    """Return ``(session base, private boundary)`` for chat state."""
    if root is not None:
        base = _absolute(root)
        return base, base
    configured = os.environ.get("HC_CHAT_STATE_DIR")
    if configured:
        base = _absolute(Path(configured))
        return base, base
    vault = _absolute(Path(os.environ.get(
        "CLAUDE_VAULT_DIR", Path.home() / ".claude-vault")))
    return vault / "chat-sessions", vault


def _state_base(root: Optional[Path] = None) -> Path:
    return _state_location(root)[0]


def paths(session_id: str, root: Optional[Path] = None) -> ChatPaths:
    """Resolve files for *session_id*, rejecting path-like identifiers."""
    if not isinstance(session_id, str) or not _SESSION_RE.fullmatch(session_id):
        raise ValueError("invalid Claude session id")
    base = _state_base(root)
    session_dir = base / session_id
    return ChatPaths(
        base=base,
        session_dir=session_dir,
        manifest=session_dir / "manifest.json",
        events=session_dir / "events.jsonl",
        prompts=session_dir / "prompts.json",
        goals=session_dir / "goals.json",
        important=session_dir / "important.json",
        goal_context=session_dir / "goal_context.md",
        lock_dir=session_dir / ".lock",
    )


def _local_lock(lock_dir: Path) -> threading.RLock:
    key = str(lock_dir)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


@contextmanager
def session_lock(
    session_id: str, root: Optional[Path] = None, wait_s: float = 0
) -> Iterator[ChatPaths]:
    """Acquire a process-safe, same-thread-reentrant lock for a chat session."""
    p = paths(session_id, root)
    _, boundary = _state_location(root)
    secure_dir(p.session_dir, boundary)
    local = _local_lock(p.lock_dir)
    local.acquire()
    depths = getattr(_LOCK_DEPTH, "values", None)
    if depths is None:
        depths = _LOCK_DEPTH.values = {}
    key = str(p.lock_dir)
    outer = depths.get(key, 0) == 0
    acquired = False
    try:
        if outer:
            deadline = time.monotonic() + max(0.0, wait_s)
            while True:
                try:
                    p.lock_dir.mkdir()
                    (p.lock_dir / "owner.json").write_text(
                        json.dumps({"pid": os.getpid(), "created_at": _now()}),
                        encoding="utf-8",
                    )
                    (p.lock_dir / "owner.json").chmod(0o600)
                    acquired = True
                    break
                except FileExistsError:
                    try:
                        owner = json.loads(
                            (p.lock_dir / "owner.json").read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        owner = {}
                    if owner.get("pid") and not _pid_alive(owner["pid"]):
                        shutil.rmtree(p.lock_dir, ignore_errors=True)
                        continue
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"chat session {session_id} is locked")
                    time.sleep(0.05)
        depths[key] = depths.get(key, 0) + 1
        yield p
    finally:
        if key in depths:
            depths[key] -= 1
            if depths[key] <= 0:
                depths.pop(key, None)
                if acquired:
                    shutil.rmtree(p.lock_dir, ignore_errors=True)
        local.release()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with tmp.open("wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _default_manifest(session_id: str) -> Dict[str, Any]:
    now = _now()
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "cwd": None,
        "transcript_path": None,
        "created_at": now,
        "updated_at": now,
        "source": {"cursor": 0},
        "event_count": 0,
        "last_ordinal": 0,
        "prompt_count": 0,
        "analyzer": {
            "last_analyzed_ordinal": 0,
            "requested_ordinal": 0,
            "status": "idle",
            "error": None,
            "updated_at": now,
        },
    }


def load_manifest(session_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    p = paths(session_id, root)
    value = _read_json(p.manifest, _default_manifest(session_id))
    if not isinstance(value, dict) or value.get("session_id") not in (None, session_id):
        return _default_manifest(session_id)
    value.setdefault("schema_version", SCHEMA_VERSION)
    value["session_id"] = session_id
    value.setdefault("source", {"cursor": 0})
    value.setdefault("analyzer", _default_manifest(session_id)["analyzer"])
    return value


def load_events(session_id: str, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = paths(session_id, root)
    out: List[Dict[str, Any]] = []
    try:
        with p.events.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("id"):
                    out.append(value)
    except OSError:
        pass
    return out


def load_prompts(session_id: str, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    value = _read_json(paths(session_id, root).prompts, {"prompts": []})
    if isinstance(value, list):  # tolerate the earliest development shape
        prompts = value
    else:
        prompts = value.get("prompts", []) if isinstance(value, dict) else []
    return [
        p for p in prompts
        if (isinstance(p, dict) and p.get("role") == "user"
            and not _is_goals_ui_launcher(str(p.get("text") or ""))
            and not _is_command_prompt(str(p.get("text") or "")))
    ]


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    bits: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "thinking") and isinstance(
            block.get("text") or block.get("thinking"), str
        ):
            bits.append(block.get("text") or block.get("thinking"))
        elif block.get("type") == "tool_result":
            nested = block.get("content")
            if isinstance(nested, str):
                bits.append(nested)
            elif isinstance(nested, list):
                bits.extend(
                    str(item.get("text"))
                    for item in nested
                    if isinstance(item, dict) and item.get("text")
                )
    return "\n".join(bit for bit in bits if bit)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _record_id(record: Dict[str, Any], raw: bytes, suffix: str) -> str:
    stable = record.get("uuid")
    if stable:
        return f"event:{stable}:{suffix}"
    digest = hashlib.sha256(raw + b"\0" + suffix.encode()).hexdigest()[:24]
    return f"event:sha256:{digest}"


def _human_origin(record: Dict[str, Any]) -> bool:
    origin = record.get("origin")
    if isinstance(origin, dict):
        origin = origin.get("kind")
    prompt_source = record.get("promptSource")
    if isinstance(prompt_source, dict):
        prompt_source = prompt_source.get("kind")
    if origin and origin != "human":
        return False
    if prompt_source and prompt_source not in ("typed", "pasted", "human"):
        return False
    return not (
        record.get("isMeta")
        or record.get("isSidechain")
        or record.get("sourceToolAssistantUUID")
        or record.get("toolUseResult") is not None
    )


def _is_goals_ui_launcher(text: str) -> bool:
    """Keep the command that opens the workspace out of its own goal model.

    ``hc-ui`` is the pre-rename spelling and still appears in transcripts
    recorded before the rename, so both names are recognized.
    """
    lowered = str(text or "").strip().lower()
    for name in ("goals-ui", "hc-ui"):
        if (lowered in (f"/{name}", f"\\{name}", name)
                or lowered.startswith(f"/{name} ")
                or re.search(
                    rf"^\s*<command-name>\s*/?{re.escape(name)}\s*</command-name>",
                    lowered)):
            return True
    return False


def _is_command_prompt(text: str) -> bool:
    """Identify Claude slash-command records that are not human messages.

    Claude persists built-in commands such as ``/compact`` as XML-like user
    records. They remain useful in the event stream, but showing them in the
    prompt picker conflates a UI action with authored conversation content.
    """
    stripped = str(text or "").strip()
    if _SLASH_COMMAND_RE.fullmatch(stripped):
        return True
    if not re.search(r"<command-name>", stripped, re.IGNORECASE):
        return False
    remainder = _COMMAND_TAG_RE.sub("", stripped)
    return not remainder.strip()


def _base_event(
    record: Dict[str, Any], source: Dict[str, Any], event_id: str
) -> Dict[str, Any]:
    return {
        "id": event_id,
        "timestamp": record.get("timestamp"),
        "source_uuid": record.get("uuid"),
        "parent_uuid": record.get("parentUuid"),
        "sidechain": bool(record.get("isSidechain")),
        "source": source,
    }


def _normalize_record(
    record: Dict[str, Any], raw: bytes, source: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Project one Claude JSONL record into goal-relevant, traceable events."""
    typ = record.get("type")
    out: List[Dict[str, Any]] = []

    if typ == "user":
        message = record.get("message") or {}
        content = message.get("content")
        if record.get("isCompactSummary"):
            text = _text_content(content)
            if text:
                event = _base_event(record, source, _record_id(record, raw, "summary"))
                event.update(
                    kind="compact_summary",
                    role="system",
                    text=text,
                    usable_for_goals=True,
                )
                out.append(event)
            return out

        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        ):
            for index, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                text = _text_content([block])
                if not text and record.get("toolUseResult") is not None:
                    text = _json_text(record.get("toolUseResult"))
                event_id = (
                    f"result:{tool_use_id}"
                    if tool_use_id
                    else _record_id(record, raw, f"result:{index}")
                )
                event = _base_event(record, source, event_id)
                event.update(
                    kind="tool_result",
                    role="tool",
                    text=text,
                    tool_use_id=tool_use_id,
                    is_error=bool(block.get("is_error")),
                    usable_for_goals=True,
                )
                out.append(event)
            return out

        text = _text_content(content)
        if not text:
            return out
        if _human_origin(record):
            prompt_id = record.get("promptId")
            event_id = f"prompt:{prompt_id}" if prompt_id else _record_id(record, raw, "prompt")
            kind = "human_prompt"
            usable = not (_is_goals_ui_launcher(text) or _is_command_prompt(text))
        elif record.get("isMeta"):
            event_id = _record_id(record, raw, "context")
            kind, usable = "context", False
        else:
            event_id = _record_id(record, raw, "notification")
            kind, usable = "task_notification", True
        event = _base_event(record, source, event_id)
        event.update(kind=kind, role="user", text=text, usable_for_goals=usable)
        out.append(event)
        return out

    if typ == "assistant":
        message = record.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return out
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                event = _base_event(
                    record, source, _record_id(record, raw, f"text:{index}")
                )
                event.update(
                    kind="assistant_message",
                    role="assistant",
                    text=block["text"],
                    usable_for_goals=True,
                )
                out.append(event)
            elif block_type == "tool_use":
                tool_name = str(block.get("name") or "")
                tool_use_id = block.get("id")
                lowered = tool_name.lower()
                kind = (
                    "plan_update"
                    if "plan" in lowered or lowered in ("todowrite", "update_plan")
                    else "tool_use"
                )
                event = _base_event(
                    record,
                    source,
                    f"tool:{tool_use_id}"
                    if tool_use_id
                    else _record_id(record, raw, f"tool:{index}"),
                )
                event.update(
                    kind=kind,
                    role="assistant",
                    text=_json_text(block.get("input") or {}),
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    usable_for_goals=True,
                )
                out.append(event)
            elif block_type == "thinking":
                # Preserve record coverage without feeding private scratch-work
                # into goal inference. Visible plans/progress and actions above
                # remain available as first-class events.
                event = _base_event(
                    record, source, _record_id(record, raw, f"thinking:{index}")
                )
                event.update(
                    kind="assistant_thinking",
                    role="assistant",
                    text="",
                    redacted=True,
                    usable_for_goals=False,
                )
                out.append(event)
        return out

    if typ == "queue-operation" and record.get("operation") == "enqueue":
        text = record.get("content")
        if isinstance(text, str) and text:
            event = _base_event(record, source, _record_id(record, raw, "queued"))
            event.update(
                kind="queued_prompt",
                role="user",
                text=text,
                usable_for_goals=not _is_goals_ui_launcher(text),
            )
            out.append(event)
        return out

    if typ == "system":
        subtype = record.get("subtype") or "system"
        if subtype == "local_command":
            text = str(record.get("content") or "")
        else:
            selected = {
                key: record.get(key)
                for key in (
                    "subtype",
                    "durationMs",
                    "messageCount",
                    "stopReason",
                    "preventedContinuation",
                    "hookErrors",
                    "hookInfos",
                )
                if record.get(key) not in (None, "", [], {})
            }
            text = _json_text(selected)
        event = _base_event(record, source, _record_id(record, raw, f"system:{subtype}"))
        event.update(
            kind=f"system_{subtype}",
            role="system",
            text=text,
            usable_for_goals=subtype in ("local_command", "stop_hook_summary"),
        )
        out.append(event)
        return out

    if typ == "attachment":
        attachment = record.get("attachment") or {}
        attachment_type = attachment.get("type") if isinstance(attachment, dict) else None
        if attachment_type in ("task_reminder", "edited_text_file", "plan_mode_exit"):
            event = _base_event(
                record, source, _record_id(record, raw, f"attachment:{attachment_type}")
            )
            event.update(
                kind=f"attachment_{attachment_type}",
                role="system",
                text=_json_text(attachment),
                usable_for_goals=True,
            )
            out.append(event)
    return out


def _first_record_cwd(records: Iterable[Dict[str, Any]]) -> Optional[str]:
    return next(
        (
            str(record["cwd"])
            for record in records
            if isinstance(record, dict) and record.get("cwd")
        ),
        None,
    )


def _tail_fingerprint(path: Path, cursor: int) -> Tuple[int, str]:
    start = max(0, cursor - _TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(cursor - start)
    return start, hashlib.sha256(data).hexdigest()


def _can_resume(path: Path, source: Dict[str, Any], size: int) -> bool:
    cursor = int(source.get("cursor") or 0)
    if cursor == 0:
        return True
    if size < cursor or not source.get("tail_sha256"):
        return False
    try:
        start = int(source.get("tail_start") or 0)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(cursor - start)
        return hashlib.sha256(data).hexdigest() == source["tail_sha256"]
    except OSError:
        return False


def _event_aliases(event: Dict[str, Any]) -> Iterable[str]:
    for key in ("id", "canonical_id"):
        value = event.get(key)
        if value:
            yield str(value)


def _merge_events(
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    synthetic_match_after: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    aliases: Dict[str, Dict[str, Any]] = {}
    for event in existing:
        aliases.update((alias, event) for alias in _event_aliases(event))
    appended = 0
    for new in incoming:
        prior = aliases.get(new["id"])
        if (
            prior is None
            and new.get("source", {}).get("synthetic")
            and synthetic_match_after is not None
        ):
            boundary = int(new.get("source", {}).get("after_ordinal") or 0)
            for candidate in reversed(existing[-20:]):
                ordinal = int(candidate.get("ordinal") or 0)
                if ordinal < max(1, boundary - 5):
                    break
                if (
                    ordinal > synthetic_match_after
                    and not candidate.get("source", {}).get("synthetic")
                    and candidate.get("kind") == new.get("kind")
                    and candidate.get("text") == new.get("text")
                ):
                    prior = candidate
                    break
        if prior is None and not new.get("source", {}).get("synthetic"):
            # Hook boundaries can arrive before Claude flushes the same visible
            # message to JSONL. Upgrade that provisional event in place so UI
            # prompt ids and goal evidence links never change underneath users.
            for candidate in reversed(existing[-20:]):
                if (
                    candidate.get("source", {}).get("synthetic")
                    and candidate.get("kind") == new.get("kind")
                    and candidate.get("text") == new.get("text")
                    and not candidate.get("canonical_id")
                ):
                    prior = candidate
                    break
        if prior is not None:
            if (
                prior.get("source", {}).get("synthetic")
                and not new.get("source", {}).get("synthetic")
                and prior.get("id") == new.get("id")
            ):
                ordinal = prior.get("ordinal")
                prior.update(new)
                prior["ordinal"] = ordinal
            if (
                prior.get("id") != new["id"]
                and prior.get("source", {}).get("synthetic")
                and not new.get("source", {}).get("synthetic")
            ):
                prior["canonical_id"] = new["id"]
                ordinal = prior.get("ordinal")
                stable_id = prior["id"]
                prior.update(new)
                prior["id"] = stable_id
                prior["ordinal"] = ordinal
                aliases[new["id"]] = prior
            continue
        new["ordinal"] = max((int(e.get("ordinal") or 0) for e in existing), default=0) + 1
        existing.append(new)
        aliases.update((alias, new) for alias in _event_aliases(new))
        appended += 1
    return existing, appended


def _assignable_prompts(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prompts = []
    for event in events:
        if event.get("kind") != "human_prompt" or not event.get("text"):
            continue
        text = str(event["text"]).strip()
        if (not event.get("usable_for_goals", True)
                or _is_goals_ui_launcher(text) or _is_command_prompt(text)):
            continue
        prompts.append(
            {
                "id": event["id"],
                "role": "user",
                "text": text,
                "created_at": event.get("timestamp"),
                "ordinal": int(event.get("ordinal") or 0),
            }
        )
    return prompts


def _write_events(path: Path, events: Iterable[Dict[str, Any]]) -> None:
    data = b"".join(
        (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        for event in events
    )
    _atomic_write(path, data)


def _persist_stream(
    p: ChatPaths,
    manifest: Dict[str, Any],
    events: List[Dict[str, Any]],
    prompts: List[Dict[str, Any]],
) -> None:
    last = max((int(e.get("ordinal") or 0) for e in events), default=0)
    manifest.update(
        schema_version=SCHEMA_VERSION,
        updated_at=_now(),
        event_count=len(events),
        last_ordinal=last,
        prompt_count=len(prompts),
    )
    analyzer = manifest.setdefault("analyzer", {})
    analyzer.setdefault("last_analyzed_ordinal", 0)
    analyzer["requested_ordinal"] = max(
        int(analyzer.get("requested_ordinal") or 0), last
    )
    if last > int(analyzer.get("last_analyzed_ordinal") or 0) and analyzer.get(
        "status"
    ) not in ("running", "error"):
        analyzer["status"] = "pending"
    analyzer["updated_at"] = _now()
    _write_events(p.events, events)
    _atomic_json(
        p.prompts,
        {"schema_version": SCHEMA_VERSION, "prompts": prompts},
    )
    _atomic_json(p.manifest, manifest)


def ingest_transcript(
    session_id: str,
    transcript_path: Path,
    cwd: Optional[str] = None,
    root: Optional[Path] = None,
) -> IngestResult:
    """Incrementally ingest complete JSONL records, replaying safely on rewrite."""
    transcript = Path(transcript_path).expanduser().resolve()
    stat = transcript.stat()
    with session_lock(session_id, root, wait_s=5) as p:
        manifest = load_manifest(session_id, root)
        old_source = manifest.get("source") or {}
        resume = _can_resume(transcript, old_source, stat.st_size)
        start = int(old_source.get("cursor") or 0) if resume else 0
        rewound = bool(start == 0 and int(old_source.get("cursor") or 0))
        with transcript.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read()
        newline = chunk.rfind(b"\n")
        complete = chunk[: newline + 1] if newline >= 0 else b""
        cursor = start + len(complete)
        incoming: List[Dict[str, Any]] = []
        parsed_records: List[Dict[str, Any]] = []
        relative = 0
        for raw_line in complete.splitlines(keepends=True):
            stripped = raw_line.rstrip(b"\r\n")
            source = {
                "type": "claude_jsonl",
                "start": start + relative,
                "end": start + relative + len(raw_line),
            }
            relative += len(raw_line)
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(record, dict):
                parsed_records.append(record)
                incoming.extend(_normalize_record(record, stripped, source))
        events, appended = _merge_events(load_events(session_id, root), incoming)
        prompts = _assignable_prompts(events)
        tail_start, tail_sha = _tail_fingerprint(transcript, cursor)
        manifest["cwd"] = cwd or manifest.get("cwd") or _first_record_cwd(parsed_records)
        manifest["transcript_path"] = str(transcript)
        manifest["source"] = {
            "path": str(transcript),
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "cursor": cursor,
            "tail_start": tail_start,
            "tail_sha256": tail_sha,
        }
        _persist_stream(p, manifest, events, prompts)
        return IngestResult(
            session_id=session_id,
            appended=appended,
            total_events=len(events),
            last_ordinal=int(manifest["last_ordinal"]),
            prompt_count=len(prompts),
            rewound=rewound,
        )


def _synthetic_event(
    session_id: str,
    kind: str,
    role: str,
    text: str,
    cursor: int,
    hook_event: str,
) -> Dict[str, Any]:
    digest = hashlib.sha256(
        f"{session_id}\0{kind}\0{cursor}\0{text}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "id": f"hook:{digest}",
        "timestamp": _now(),
        "kind": kind,
        "role": role,
        "text": text,
        "source_uuid": None,
        "parent_uuid": None,
        "sidechain": False,
        "usable_for_goals": True,
        "source": {
            "type": "hook",
            "hook_event": hook_event,
            "synthetic": True,
            "after_ordinal": cursor,
        },
    }


def _hook_response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text = _text_content(value)
        return text if text else _json_text(value)
    return _json_text(value) if value is not None else ""


def _post_tool_batch_events(
    session_id: str, payload: Dict[str, Any], after_ordinal: int
) -> List[Dict[str, Any]]:
    """Normalize PostToolBatch before equivalent transcript records flush."""
    out: List[Dict[str, Any]] = []
    for index, call in enumerate(payload.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("tool_name") or call.get("name") or "")
        tool_use_id = call.get("tool_use_id") or call.get("id")
        lowered = tool_name.lower()
        kind = (
            "plan_update"
            if "plan" in lowered or lowered in ("todowrite", "update_plan")
            else "tool_use"
        )
        tool = _synthetic_event(
            session_id,
            kind,
            "assistant",
            _json_text(call.get("tool_input") or call.get("input") or {}),
            after_ordinal,
            "PostToolBatch",
        )
        tool["id"] = (
            f"tool:{tool_use_id}"
            if tool_use_id
            else f"{tool['id']}:call:{index}"
        )
        tool.update(tool_name=tool_name, tool_use_id=tool_use_id)
        out.append(tool)

        response = call.get("tool_response", call.get("response"))
        if response is None and call.get("error") is None:
            continue
        result = _synthetic_event(
            session_id,
            "tool_result",
            "tool",
            _hook_response_text(
                response if response is not None else call.get("error")
            ),
            after_ordinal,
            "PostToolBatch",
        )
        result["id"] = (
            f"result:{tool_use_id}"
            if tool_use_id
            else f"{result['id']}:result:{index}"
        )
        response_error = response.get("is_error") if isinstance(response, dict) else False
        response_failed = (
            response.get("success") is False if isinstance(response, dict) else False
        )
        result.update(
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            is_error=bool(
                call.get("is_error")
                or call.get("error")
                or response_error
                or response_failed
            ),
        )
        out.append(result)
    return out


def ingest_hook(payload: Dict[str, Any], root: Optional[Path] = None) -> IngestResult:
    """Ingest a Claude hook payload and any transcript bytes already flushed.

    ``UserPromptSubmit.prompt`` and ``Stop.last_assistant_message`` close the
    two known transcript-lag windows.  They are later upgraded in place when
    their canonical JSONL records arrive.
    """
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    paths(session_id, root)  # validate before touching disk
    with session_lock(session_id, root, wait_s=5):
        return _ingest_hook_locked(payload, session_id, root)


def _ingest_hook_locked(
    payload: Dict[str, Any], session_id: str, root: Optional[Path]
) -> IngestResult:
    baseline_ordinal = int(load_manifest(session_id, root).get("last_ordinal") or 0)
    transcript = payload.get("transcript_path")
    if transcript and Path(str(transcript)).expanduser().is_file():
        result = ingest_transcript(
            session_id, Path(str(transcript)), cwd=payload.get("cwd"), root=root
        )
    else:
        with session_lock(session_id, root, wait_s=5) as p:
            manifest = load_manifest(session_id, root)
            if payload.get("cwd"):
                manifest["cwd"] = payload["cwd"]
            if transcript:
                manifest["transcript_path"] = str(Path(str(transcript)).expanduser())
            events = load_events(session_id, root)
            prompts = _assignable_prompts(events)
            _persist_stream(p, manifest, events, prompts)
            result = IngestResult(
                session_id, 0, len(events), int(manifest["last_ordinal"]), len(prompts)
            )

    hook_event = str(payload.get("hook_event_name") or "")
    boundary: Optional[Dict[str, Any]] = None
    boundaries: List[Dict[str, Any]] = []
    if hook_event == "UserPromptSubmit" and isinstance(payload.get("prompt"), str):
        text = payload["prompt"].strip()
        if text:
            boundary = _synthetic_event(
                session_id,
                "human_prompt",
                "user",
                text,
                result.last_ordinal,
                hook_event,
            )
            boundary["usable_for_goals"] = not (
                _is_goals_ui_launcher(text) or _is_command_prompt(text)
            )
    elif hook_event == "Stop" and isinstance(payload.get("last_assistant_message"), str):
        text = payload["last_assistant_message"].strip()
        if text:
            boundary = _synthetic_event(
                session_id,
                "assistant_message",
                "assistant",
                text,
                result.last_ordinal,
                hook_event,
            )
    elif hook_event in ("TaskCreated", "TaskCompleted"):
        subject = str(payload.get("task_subject") or "").strip()
        description = str(payload.get("task_description") or "").strip()
        if subject:
            text = subject + ("\n" + description if description else "")
            boundary = _synthetic_event(
                session_id,
                "task_completed" if hook_event == "TaskCompleted" else "task_created",
                "system",
                text,
                result.last_ordinal,
                hook_event,
            )
    elif hook_event == "PostToolBatch":
        boundaries = _post_tool_batch_events(
            session_id, payload, result.last_ordinal
        )
    if boundary is not None:
        boundaries.append(boundary)
    if not boundaries:
        return result

    with session_lock(session_id, root, wait_s=5) as p:
        manifest = load_manifest(session_id, root)
        events, appended = _merge_events(
            load_events(session_id, root),
            boundaries,
            synthetic_match_after=baseline_ordinal,
        )
        prompts = _assignable_prompts(events)
        _persist_stream(p, manifest, events, prompts)
        return IngestResult(
            session_id,
            appended,
            len(events),
            int(manifest["last_ordinal"]),
            len(prompts),
            result.rewound,
        )


def new_events_since(
    session_id: str, last_analyzed_ordinal: int, root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Return events with ordinal strictly greater than the analyzer cursor."""
    cursor = int(last_analyzed_ordinal or 0)
    return [
        event
        for event in load_events(session_id, root)
        if int(event.get("ordinal") or 0) > cursor
    ]


def get_analyzer_state(
    session_id: str, root: Optional[Path] = None
) -> Dict[str, Any]:
    state = load_manifest(session_id, root).get("analyzer") or {}
    default = _default_manifest(session_id)["analyzer"]
    return {**default, **state}


def set_analyzer_state(
    session_id: str,
    *,
    last_analyzed_ordinal: Optional[int] = None,
    status: Optional[str] = None,
    error: Any = _UNSET,
    requested_ordinal: Optional[int] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Atomically update analysis metadata without overwriting omitted fields."""
    if status is not None and status not in ("idle", "pending", "running", "error"):
        raise ValueError("invalid analyzer status")
    with session_lock(session_id, root, wait_s=5) as p:
        manifest = load_manifest(session_id, root)
        analyzer = manifest.setdefault("analyzer", {})
        if last_analyzed_ordinal is not None:
            analyzer["last_analyzed_ordinal"] = max(0, int(last_analyzed_ordinal))
        if requested_ordinal is not None:
            analyzer["requested_ordinal"] = max(
                int(analyzer.get("requested_ordinal") or 0), int(requested_ordinal)
            )
        if status is not None:
            analyzer["status"] = status
        if error is not _UNSET:
            analyzer["error"] = None if error is None else str(error)[:4000]
        if (
            status is None
            and int(analyzer.get("requested_ordinal") or 0)
            <= int(analyzer.get("last_analyzed_ordinal") or 0)
        ):
            analyzer["status"] = "idle"
        analyzer["updated_at"] = _now()
        manifest["updated_at"] = _now()
        _atomic_json(p.manifest, manifest)
        return dict(analyzer)


def request_analysis(
    session_id: str,
    through_ordinal: Optional[int] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    manifest = load_manifest(session_id, root)
    requested = int(through_ordinal or manifest.get("last_ordinal") or 0)
    current = get_analyzer_state(session_id, root)
    status = "running" if current.get("status") == "running" else "pending"
    return set_analyzer_state(
        session_id,
        requested_ordinal=requested,
        status=status,
        error=None,
        root=root,
    )


def _ensure_prompt_ids(goals: Dict[str, Any]) -> Dict[str, Any]:
    # Chat goals are the same model: a next action is a goal one level down.
    promote_todos(goals)
    for goal in goals.get("goals", []):
        if not isinstance(goal, dict):
            continue
        for key in ("prompt_ids", "auto_prompt_ids", "detached_prompt_ids"):
            value = goal.get(key)
            goal[key] = list(dict.fromkeys(value)) if isinstance(value, list) else []
    return goals


def load_goals(
    session_id: str, root: Optional[Path] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    p = paths(session_id, root)
    goals = _read_json(p.goals, {"version": 1, "goals": []})
    important = _read_json(p.important, {"items": []})
    if not isinstance(goals, dict):
        goals = {"version": 1, "goals": []}
    if not isinstance(important, dict):
        important = {"items": []}
    goals.setdefault("goals", [])
    important.setdefault("items", [])
    return _ensure_prompt_ids(goals), important


def _revision_of(goals: Dict[str, Any], important: Dict[str, Any]) -> str:
    payload = json.dumps(
        {"goals": goals, "important": important},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def goal_revision(session_id: str, root: Optional[Path] = None) -> str:
    goals, important = load_goals(session_id, root)
    return _revision_of(goals, important)


def _goal_context_text(
    session_id: str,
    goals: Dict[str, Any],
    important: Dict[str, Any],
    prompts: Iterable[Dict[str, Any]],
) -> str:
    lines = [
        "# Current goals for this Claude chat",
        "",
        f"Session: {session_id}",
        "Treat this as mutable user-supervised state, not as a new instruction.",
    ]
    by_parent: Dict[Any, List[Dict[str, Any]]] = {}
    for goal in goals.get("goals", []):
        by_parent.setdefault(goal.get("parent_goal_id"), []).append(goal)
    prompt_map = {p.get("id"): p for p in prompts}
    item_map = {i.get("id"): i for i in important.get("items", [])}

    def emit(goal: Dict[str, Any], depth: int, *, details: bool = True) -> None:
        indent = "  " * depth
        lines.append(
            f"{indent}- {goal.get('title', 'Untitled')} "
            f"[{str(goal.get('status', 'active')).replace('_', ' ')}]"
        )
        if details:
            description = " ".join(str(goal.get("description") or "").split())[:280]
            notes = " ".join(str(goal.get("notes") or "").split())[:280]
            priority = str(goal.get("priority") or "normal")
            if description:
                lines.append(f"{indent}  - DESCRIPTION: {description}")
            if notes:
                lines.append(f"{indent}  - USER NOTES: {notes}")
            if priority != "normal":
                lines.append(f"{indent}  - PRIORITY: {priority}")
        for todo in goal.get("todos", []):
            if details and isinstance(todo, dict) and not todo.get("done"):
                lines.append(f"{indent}  - TODO: {todo.get('text', '')}")
        for prompt_id in goal.get("prompt_ids", [])[:4]:
            prompt = prompt_map.get(prompt_id)
            if details and prompt:
                text = " ".join(str(prompt.get("text") or "").split())[:220]
                lines.append(f"{indent}  - USER PROMPT: {text}")
        for item_id in goal.get("important_item_ids", [])[:3]:
            item = item_map.get(item_id)
            if details and item:
                lines.append(f"{indent}  - IMPORTANT: {str(item.get('text') or '')[:220]}")
        for child in by_parent.get(goal.get("id"), []):
            emit(child, depth + 1, details=details)

    roots = [
        goal
        for goal in by_parent.get(None, [])
        if goal.get("status") in ("active", "in_progress")
    ]
    if not roots:
        lines.extend(("", "No active goals have been inferred or added yet."))
    else:
        lines.append("")
        for goal in roots:
            emit(goal, 0)
    inactive = [
        goal
        for goal in by_parent.get(None, [])
        if goal.get("status") in ("completed", "abandoned")
    ]
    if inactive:
        inactive.sort(key=lambda goal: str(goal.get("updated_at") or ""), reverse=True)
        lines.extend(("", "Recent inactive goals:"))
        for goal in inactive[:8]:
            emit(goal, 0, details=False)
    return "\n".join(lines)[:8000] + "\n"


def write_goal_context(
    session_id: str,
    goals: Optional[Dict[str, Any]] = None,
    important: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> str:
    with session_lock(session_id, root, wait_s=5) as p:
        if goals is None or important is None:
            loaded_goals, loaded_important = load_goals(session_id, root)
            goals = loaded_goals if goals is None else goals
            important = loaded_important if important is None else important
        text = _goal_context_text(
            session_id, goals, important, load_prompts(session_id, root)
        )
        _atomic_write(p.goal_context, text.encode("utf-8"))
        return text


def save_goals(
    session_id: str,
    goals: Dict[str, Any],
    important: Dict[str, Any],
    root: Optional[Path] = None,
    expected_revision: Optional[str] = None,
) -> bool:
    """Atomically save scoped goals and refresh their cached agent context."""
    with session_lock(session_id, root, wait_s=5) as p:
        current_goals, current_important = load_goals(session_id, root)
        if expected_revision is not None and expected_revision != _revision_of(
            current_goals, current_important
        ):
            return False
        prompts = load_prompts(session_id, root)
        goals = link_evidence_prompts(_ensure_prompt_ids(goals), prompts)
        goals["generated_at"] = _now()
        _atomic_json(p.goals, goals)
        _atomic_json(p.important, important)
        text = _goal_context_text(session_id, goals, important, prompts)
        _atomic_write(p.goal_context, text.encode("utf-8"))
        return True
