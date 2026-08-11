from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import struct
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import SCHEMA_VERSION


FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch"}
AGENT_TOOLS = {
    "Task",
    "Agent",
    "TaskOutput",
    "SendMessage",
    "spawn_agent",
    "followup_task",
    "send_message",
}
TODO_TOOLS = {
    "TodoWrite",
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "TaskGet",
    "update_plan",
}
TEST_WORDS = re.compile(r"\b(?:test|tests|pytest|jest|vitest|cargo test|go test|npm test)\b", re.I)
PATH_RE = re.compile(r'''(?<![\w.:-])(?:~?/|\.{1,2}/)[^\s"'`<>|,;:(){}\[\]]+''')
QUOTED_PATH_RE = re.compile(r'''["'`]((?:~?/|\.{1,2}/)[^"'`]{2,})["'`]''')
COMMIT_RE = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{7,40})(?![0-9a-f])", re.I)
PRIVATE_BLOCK_TYPES = {"thinking", "redacted_thinking"}
RELEVANT_ATTACHMENTS = {
    "compact_file_reference",
    "edited_text_file",
    "file",
    "task_notification",
    "task_reminder",
}
BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
SLASH_COMMANDS = {
    "/clear",
    "/compact",
    "/config",
    "/context",
    "/doctor",
    "/exit",
    "/help",
    "/hooks",
    "/init",
    "/mcp",
    "/permissions",
    "/plugin",
    "/reload-plugins",
    "/resume",
    "/skills",
    "/status",
}


def _estimate_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_estimate_chars(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            total += _estimate_chars(item)
        return total
    return 0


def _decode_base64(value: str) -> Optional[bytes]:
    compact = "".join(value.split())
    if not compact or len(compact) % 4 or not BASE64_RE.fullmatch(compact):
        return None
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None


def _image_dimensions(payload: bytes, media_type: str) -> Optional[Tuple[int, int]]:
    try:
        if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
            return struct.unpack(">II", payload[16:24])
        if payload[:6] in {b"GIF87a", b"GIF89a"} and len(payload) >= 10:
            return struct.unpack("<HH", payload[6:10])
        if payload.startswith(b"\xff\xd8"):
            offset = 2
            while offset + 9 <= len(payload):
                if payload[offset] != 0xFF:
                    offset += 1
                    continue
                marker = payload[offset + 1]
                offset += 2
                if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                    continue
                if offset + 2 > len(payload):
                    break
                size = int.from_bytes(payload[offset : offset + 2], "big")
                if size < 2 or offset + size > len(payload):
                    break
                if marker in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }:
                    height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
                    width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
                    return width, height
                offset += size
        if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP" and len(payload) >= 30:
            chunk = payload[12:16]
            if chunk == b"VP8X":
                width = 1 + int.from_bytes(payload[24:27], "little")
                height = 1 + int.from_bytes(payload[27:30], "little")
                return width, height
            if chunk == b"VP8 " and payload[23:26] == b"\x9d\x01\x2a":
                width = int.from_bytes(payload[26:28], "little") & 0x3FFF
                height = int.from_bytes(payload[28:30], "little") & 0x3FFF
                return width, height
            if chunk == b"VP8L" and len(payload) >= 25:
                bits = int.from_bytes(payload[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (IndexError, struct.error, ValueError):
        return None
    return None


def _visual_tokens(width: int, height: int, maximum: int = 1568) -> int:
    """Approximate Claude's standard-tier resize and 28px patch count."""
    if width <= 0 or height <= 0:
        return 0
    raw = math.ceil(width / 28) * math.ceil(height / 28)
    return min(raw, maximum)


def _binary_descriptor(source: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    safe = {key: value for key, value in source.items() if key != "data"}
    raw = source.get("data")
    if not isinstance(raw, str):
        return safe, 0
    payload = _decode_base64(raw)
    media_type = str(
        source.get("media_type")
        or source.get("mimeType")
        or source.get("mime_type")
        or "application/octet-stream"
    )
    byte_count = len(payload) if payload is not None else max(0, len(raw) * 3 // 4)
    digest_input = payload if payload is not None else raw.encode("utf-8", errors="replace")
    descriptor: Dict[str, Any] = {
        "payload_omitted": True,
        "bytes": byte_count,
        "sha256": hashlib.sha256(digest_input).hexdigest(),
    }
    tokens = 0
    if media_type.startswith("image/") and payload is not None:
        dimensions = _image_dimensions(payload, media_type)
        if dimensions:
            descriptor["width"], descriptor["height"] = dimensions
            tokens = _visual_tokens(*dimensions)
            descriptor["visual_tokens_estimate"] = tokens
    safe.update(descriptor)
    return safe, tokens


def _sanitize_value(value: Any) -> Tuple[Any, int]:
    """Remove opaque binary payloads while retaining bounded provenance metadata."""
    if isinstance(value, list):
        cleaned: List[Any] = []
        media_tokens = 0
        for item in value:
            safe, tokens = _sanitize_value(item)
            cleaned.append(safe)
            media_tokens += tokens
        return cleaned, media_tokens
    if not isinstance(value, dict):
        return value, 0
    if (
        value.get("type") in {"base64", "image", "audio"}
        and isinstance(value.get("data"), str)
    ):
        return _binary_descriptor(value)
    cleaned: Dict[str, Any] = {}
    media_tokens = 0
    for key, item in value.items():
        if (
            isinstance(item, str)
            and item.startswith("data:")
            and ";base64," in item[:200]
        ):
            header, encoded = item.split(",", 1)
            payload = _decode_base64(encoded)
            raw = payload if payload is not None else encoded.encode("utf-8", errors="replace")
            cleaned[key] = {
                "payload_omitted": True,
                "media_type": header[5:].split(";", 1)[0] or "application/octet-stream",
                "bytes": len(payload) if payload is not None else max(0, len(encoded) * 3 // 4),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            continue
        if key in {"data", "blob", "base64"} and isinstance(item, str) and len(item) > 1000:
            payload = _decode_base64(item)
            raw = payload if payload is not None else item.encode("utf-8", errors="replace")
            cleaned[key] = {
                "payload_omitted": True,
                "bytes": len(payload) if payload is not None else len(item),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            continue
        safe, tokens = _sanitize_value(item)
        cleaned[key] = safe
        media_tokens += tokens
    return cleaned, media_tokens


def _input_audit(records: Sequence[Tuple[Dict[str, Any], int, int, int]]) -> Dict[str, Any]:
    private_blocks = 0
    media_blocks = 0
    attachment_counts: Dict[str, int] = {}
    for entry, _start, _end, _line in records:
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type in PRIVATE_BLOCK_TYPES:
                    private_blocks += 1
                elif block_type in {"image", "document"}:
                    media_blocks += 1
        attachment = entry.get("attachment")
        if isinstance(attachment, dict):
            kind = str(attachment.get("type") or "unknown")
            attachment_counts[kind] = attachment_counts.get(kind, 0) + 1
    return {
        "private_reasoning_blocks_excluded": private_blocks,
        "media_or_document_blocks_metadata_only": media_blocks,
        "attachment_counts": dict(sorted(attachment_counts.items())),
        "attachment_types_itemized": sorted(RELEVANT_ATTACHMENTS),
    }


def _json_text(value: Any, limit: int = 12000) -> Tuple[str, bool]:
    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            rendered = repr(value)
    if len(rendered) <= limit:
        return rendered, False
    return rendered[:limit] + "\n… [truncated; restore from transcript byte range]", True


def _tool_class(name: str) -> str:
    if name in FILE_TOOLS:
        return "file_changes"
    if name in AGENT_TOOLS:
        return "subagents"
    if name in TODO_TOOLS:
        return "todos"
    return "other"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", "")).strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ).strip()


def _is_human_prompt(entry: Dict[str, Any]) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    ):
        return False
    text = _message_text(content)
    if text and not text.startswith("<") and not re.match(r"^/compact(?:\s|$)", text, re.I):
        return True
    return bool(
        isinstance(content, list)
        and any(
            isinstance(block, dict)
            and str(block.get("type") or "") in {"image", "document"}
            for block in content
        )
    )


def _stable_id(
    prefix: str,
    entry: Dict[str, Any],
    block_index: int,
    start: int,
    content: Any,
) -> str:
    try:
        content_digest = hashlib.sha1(
            json.dumps(content, sort_keys=True, ensure_ascii=False, default=repr).encode("utf-8")
        ).hexdigest()[:12]
    except (TypeError, ValueError):
        content_digest = hashlib.sha1(repr(content).encode("utf-8")).hexdigest()[:12]
    identity = f"{entry.get('uuid', '')}:{block_index}:{start}:{content_digest}"
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _artifacts(text: str) -> Dict[str, List[str]]:
    candidates = PATH_RE.findall(text) + QUOTED_PATH_RE.findall(text)
    paths = sorted(
        {
            cleaned
            for hit in candidates
            if (cleaned := hit.rstrip(".,:;)]}")) not in SLASH_COMMANDS
        }
    )[:30]
    commits = sorted({hit.lower() for hit in COMMIT_RE.findall(text)})[:20]
    return {
        "paths": paths,
        "commits": commits,
        "mentions_tests": ["yes"] if TEST_WORDS.search(text) else [],
    }


def _source(
    entry: Dict[str, Any],
    block_index: int,
    kind: str,
    role: str,
    text_value: Any,
    start: int,
    end: int,
    tool_name: str = "",
    tokens_override: Optional[int] = None,
) -> Dict[str, Any]:
    safe_value, media_tokens = _sanitize_value(text_value)
    full_chars = _estimate_chars(safe_value)
    text, truncated = _json_text(safe_value)
    klass = _tool_class(tool_name) if tool_name else "other"
    tokens_estimate = tokens_override if tokens_override is not None else max(1, full_chars // 4)
    if media_tokens:
        tokens_estimate = max(tokens_estimate, media_tokens)
    return {
        "id": _stable_id("s", entry, block_index, start, text_value),
        "kind": kind,
        "role": role,
        "tool_name": tool_name or None,
        "class": klass,
        "text": text,
        "truncated": truncated,
        "tokens_estimate": max(1, tokens_estimate),
        "entry_uuid": entry.get("uuid"),
        "timestamp": entry.get("timestamp") or entry.get("created_at"),
        "byte_range": [start, end],
        "artifacts": _artifacts(text),
    }


def _entry_sources(
    entry: Dict[str, Any],
    start: int,
    end: int,
    tool_names: Optional[Dict[str, str]] = None,
    prior_backups: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    message = entry.get("message") or {}
    content = message.get("content")
    role = str(message.get("role") or entry.get("type") or "unknown")
    sources: List[Dict[str, Any]] = []

    if entry.get("isCompactSummary"):
        text = _message_text(content)
        if text:
            sources.append(_source(entry, 0, "compact_summary", "system", text, start, end))
        return sources

    if isinstance(content, str):
        if content.strip() and not (role == "user" and not _is_human_prompt(entry)):
            if _is_human_prompt(entry):
                kind = "user_prompt"
            elif role == "assistant":
                kind = "assistant_text"
            else:
                kind = "message"
            sources.append(_source(entry, 0, kind, role, content, start, end))
    elif isinstance(content, list):
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "block")
            if block_type in PRIVATE_BLOCK_TYPES:
                continue
            if block_type == "text" and block.get("text"):
                if _is_human_prompt(entry):
                    kind = "user_prompt"
                elif role == "assistant":
                    kind = "assistant_text"
                elif role == "user":
                    continue
                else:
                    kind = "message"
                sources.append(_source(entry, index, kind, role, block["text"], start, end))
            elif block_type == "tool_use":
                name = str(block.get("name") or "tool")
                body = {"name": name, "input": block.get("input", {})}
                sources.append(_source(entry, index, "tool_call", role, body, start, end, name))
            elif block_type == "tool_result":
                body = block.get("content", "")
                tool_use_id = str(block.get("tool_use_id") or "")
                tool_name = (tool_names or {}).get(tool_use_id, "")
                sources.append(
                    _source(entry, index, "tool_result", role, body, start, end, tool_name)
                )
            elif block_type in {"image", "document"}:
                safe_block, media_tokens = _sanitize_value(block)
                sources.append(
                    _source(
                        entry,
                        index,
                        block_type,
                        role,
                        safe_block,
                        start,
                        end,
                        tokens_override=media_tokens or None,
                    )
                )
            else:
                sources.append(
                    _source(entry, index, "content_block", role, block, start, end)
                )

    if entry.get("type") == "file-history-snapshot":
        tracked = ((entry.get("snapshot") or {}).get("trackedFileBackups") or {})
        changed = {
            path: metadata
            for path, metadata in tracked.items()
            if (prior_backups or {}).get(path) != metadata
        }
        removed = sorted(set(prior_backups or {}) - set(tracked))
        if changed or removed:
            backups = []
            for path, metadata in sorted(changed.items()):
                safe_metadata = metadata if isinstance(metadata, dict) else {"value": metadata}
                backups.append(
                    {
                        "path": path,
                        "backup_file": safe_metadata.get("backupFileName"),
                        "backup_time": safe_metadata.get("backupTime"),
                        "version": safe_metadata.get("version"),
                    }
                )
            sources.append(
                _source(
                    entry,
                    0,
                    "file_snapshot",
                    "system",
                    {"changed_backups": backups, "removed_paths": removed},
                    start,
                    end,
                    "Edit",
                )
            )
    attachment = entry.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") in RELEVANT_ATTACHMENTS:
        attachment_type = str(attachment.get("type"))
        if attachment_type != "task_reminder" or attachment.get("content") or attachment.get("itemCount"):
            tool_name = "TodoWrite" if attachment_type == "task_reminder" else ""
            sources.append(
                _source(
                    entry,
                    0,
                    f"attachment_{attachment_type}",
                    "system",
                    attachment,
                    start,
                    end,
                    tool_name,
                )
            )
    return sources


def _infer_window(records: Sequence[Tuple[Dict[str, Any], int, int, int]], status: Optional[Dict[str, Any]]) -> Tuple[Optional[int], str]:
    for env_name in ("COMPACT_FOCUS_WINDOW", "CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
        raw = os.environ.get(env_name)
        if raw and raw.isdigit() and int(raw) > 0:
            return int(raw), f"env:{env_name}"
    if isinstance(status, dict):
        candidates = [
            (status.get("context_window") or {}).get("context_window_size"),
            status.get("context_window_size"),
            (status.get("context_window") or {}).get("max_tokens"),
        ]
        for value in candidates:
            if isinstance(value, (int, float)) and value > 0:
                return int(value), "statusline"
    models = {
        str((entry.get("message") or {}).get("model") or "").lower()
        for entry, _start, _end, _line in records
    }
    joined = " ".join(models)
    if "1m" in joined:
        return 1_000_000, "model-marker"
    if not os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT") and (
        "fable-5" in joined or "fable_5" in joined or "sonnet-5" in joined or "sonnet_5" in joined
    ):
        return 1_000_000, "native-model-window"
    return None, "unknown"


def _latest_usage(records: Sequence[Tuple[Dict[str, Any], int, int, int]]) -> Optional[int]:
    found: Optional[int] = None
    for entry, _start, _end, _line in records:
        usage = (entry.get("message") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        values = [usage.get("input_tokens", 0), usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0)]
        if all(isinstance(value, (int, float)) for value in values):
            found = int(sum(values))
    return found


def build_trace(transcript: Path, status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from .host import HOST_CODEX, detect_host

    if detect_host(transcript=transcript) == HOST_CODEX:
        from .codex_trace import build_codex_trace

        return build_codex_trace(transcript, status)

    records: List[Tuple[Dict[str, Any], int, int, int]] = []
    position = 0
    with transcript.open("rb") as handle:
        for line_number, raw in enumerate(handle):
            end = position + len(raw)
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                position = end
                continue
            if isinstance(entry, dict):
                records.append((entry, position, end, line_number))
            position = end

    boundary_index: Optional[int] = None
    for index, (entry, _start, _end, _line) in enumerate(records):
        if entry.get("isCompactSummary"):
            boundary_index = index
    active = records[boundary_index:] if boundary_index is not None else records
    tool_names: Dict[str, str] = {}
    for entry, _start, _end, _line in active:
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                tool_names[str(block["id"])] = str(block.get("name") or "tool")

    episodes: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    prior_backups: Dict[str, Any] = {}

    def start_episode(kind: str, entry: Dict[str, Any], start: int) -> Dict[str, Any]:
        uuid = str(entry.get("uuid") or "")
        digest = hashlib.sha1(f"{uuid}:{start}".encode("utf-8")).hexdigest()[:12]
        return {"id": f"e-{digest}", "kind": kind, "sources": [], "byte_range": [start, start]}

    for offset, (entry, start, end, _line) in enumerate(active):
        if offset == 0 and boundary_index is not None:
            current = start_episode("compact_summary", entry, start)
            episodes.append(current)
        elif _is_human_prompt(entry):
            current = start_episode("turn", entry, start)
            episodes.append(current)
        elif current is None:
            current = start_episode("preamble", entry, start)
            episodes.append(current)
        sources = _entry_sources(entry, start, end, tool_names, prior_backups)
        if entry.get("type") == "file-history-snapshot":
            tracked = ((entry.get("snapshot") or {}).get("trackedFileBackups") or {})
            if isinstance(tracked, dict):
                prior_backups = dict(tracked)
        if current is not None and sources:
            current["sources"].extend(sources)
            current["byte_range"][1] = end

    episodes = [episode for episode in episodes if episode["sources"]]
    for index, episode in enumerate(episodes, 1):
        episode_identity = ":".join(source["id"] for source in episode["sources"])
        episode["id"] = "e-" + hashlib.sha1(episode_identity.encode("utf-8")).hexdigest()[:12]
        episode["ordinal"] = index
        episode["tokens_estimate"] = sum(source["tokens_estimate"] for source in episode["sources"])
        prompt = next((source["text"] for source in episode["sources"] if source["kind"] == "user_prompt"), "")
        if not prompt and episode["kind"] == "compact_summary":
            prompt = "Previous compact summary"
        episode["title"] = " ".join(prompt.split())[:180] or f"Conversation evidence {index}"
        classes: Dict[str, int] = {}
        for source in episode["sources"]:
            classes[source["class"]] = classes.get(source["class"], 0) + source["tokens_estimate"]
        episode["classes"] = classes

    source_hash = hashlib.sha256()
    for episode in episodes:
        source_hash.update(str(episode.get("kind") or "").encode("utf-8"))
        for source in episode.get("sources", []):
            source_hash.update(str(source.get("id") or "").encode("utf-8"))
            source_hash.update(str(source.get("kind") or "").encode("utf-8"))
    digest = source_hash.hexdigest()
    window, window_source = _infer_window(records, status)
    used_tokens = _latest_usage(active)
    visible_tokens = sum(episode["tokens_estimate"] for episode in episodes)
    for episode in episodes:
        episode["window_pct_estimate"] = (
            round(episode["tokens_estimate"] * 100.0 / window, 2) if window else None
        )
        episode["used_context_pct_estimate"] = (
            round(episode["tokens_estimate"] * 100.0 / used_tokens, 2)
            if used_tokens
            else None
        )
    unattributed = max(0, (used_tokens or 0) - visible_tokens)
    warnings = [
        "Per-episode token shares are estimates reconstructed from transcript evidence.",
        "System prompts, tool schemas, startup instructions, and host-side micro-compaction are not attributable to episodes.",
    ]
    if boundary_index is None:
        warnings.append("No prior compaction boundary was found; the trace begins at session start.")
    if window is None:
        warnings.append("Context-window size is unknown; percentage estimates are unavailable.")
    input_audit = _input_audit(active)
    if input_audit["private_reasoning_blocks_excluded"]:
        warnings.append(
            "Private thinking blocks are intentionally excluded; prior-turn thinking is not reusable transcript evidence."
        )
    if input_audit["media_or_document_blocks_metadata_only"]:
        warnings.append(
            "Binary image/document payloads are represented by dimensions, byte counts, and digests—not copied into proposal or recovery files."
        )

    session_id = next((str(entry.get("sessionId")) for entry, *_ in records if entry.get("sessionId")), "")
    cwd = next((str(entry.get("cwd")) for entry, *_ in reversed(records) if entry.get("cwd")), "")
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": "claude",
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": str(transcript),
        "source_hash": digest,
        "snapshot_bytes": position,
        "boundary": {
            "found": boundary_index is not None,
            "line": records[boundary_index][3] if boundary_index is not None else None,
            "byte": records[boundary_index][1] if boundary_index is not None else 0,
        },
        "context": {
            "window_tokens": window,
            "window_source": window_source,
            "used_tokens_observed": used_tokens,
            "used_pct_observed": round(used_tokens * 100.0 / window, 1) if used_tokens and window else None,
            "visible_tokens_estimate": visible_tokens,
            "unattributed_tokens_estimate": unattributed,
            "visible_share_of_used_pct_estimate": (
                round(visible_tokens * 100.0 / used_tokens, 2) if used_tokens else None
            ),
        },
        "episodes": episodes,
        "input_audit": input_audit,
        "warnings": warnings,
    }


def evidence_text(
    trace: Dict[str, Any],
    per_source_limit: int = 1800,
    max_chars: int = 160000,
) -> str:
    """Bounded but complete episode index for the proposal model."""
    source_count = sum(len(episode.get("sources", [])) for episode in trace.get("episodes", []))
    episode_count = len(trace.get("episodes", []))
    structural_estimate = source_count * 105 + episode_count * 240
    available = max(0, max_chars - structural_estimate)
    effective_limit = min(
        per_source_limit,
        max(80, available // max(1, source_count)),
    )
    lines: List[str] = []
    for episode_index, episode in enumerate(trace.get("episodes", []), 1):
        if episode.get("window_pct_estimate") is not None:
            share = f"~{episode['window_pct_estimate']}% window"
        elif episode.get("used_context_pct_estimate") is not None:
            share = f"~{episode['used_context_pct_estimate']}% used-context"
        else:
            share = "share unavailable"
        lines.append(
            f"\nEPISODE {episode['id']} · ordinal {episode.get('ordinal', episode_index)} · "
            f"{share} · {episode['title']}"
        )
        for source in episode.get("sources", []):
            text = " ".join(str(source.get("text") or "").split())
            if len(text) > effective_limit:
                text = text[:effective_limit] + "…"
            lines.append(
                f"  SOURCE {source['id']} [{source['kind']}/{source['class']}] {text}"
            )
        artifacts = sorted(
            {
                value
                for source in episode.get("sources", [])
                for key in ("paths", "commits")
                for value in source.get("artifacts", {}).get(key, [])
            }
        )
        if artifacts:
            lines.append("  ARTIFACTS " + " · ".join(artifacts[:30]))
    return "\n".join(lines).strip()
