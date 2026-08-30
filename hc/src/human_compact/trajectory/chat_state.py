"""Durable, session-scoped Claude Code event ingestion for chat goals.

The global trajectory pipeline intentionally summarizes many Vault sessions.
This module is the separate state boundary for ``/bart``: one Claude session,
one append-only logical event stream, one goal tree.  Transcript files are
treated as replaceable caches (Claude may truncate or rewrite them); stable
record ids and event deduplication are the correctness boundary.
"""
from __future__ import annotations

import difflib
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

from .goals import (  # noqa: F401
    join_doc,
    link_evidence_prompts,
    normalize_sources,
    overlay_todo_store,
    promote_todos,
    render_attachments,
    split_doc,
    split_todo_store,
    strip_todo_items,
)
from .secure_io import secure_dir
from ..platform_compat import maybe_fchmod, pid_alive


SCHEMA_VERSION = 1
_TAIL_BYTES = 4096
# The three sentences the goals workspace is allowed to say about the chat it
# is attached to.  Each one is exactly what a single hook payload proves, and
# nothing further: a Stop means Claude finished a turn, not that anything was
# decided, updated or completed.
NOTICE_KINDS = ("session_stopped", "subagent_returned", "session_ended")
# Enough to catch up on what was missed, few enough that the store stays a
# file the browser can be handed on every poll.
NOTICE_LIMIT = 20
_NOTICE_DETAIL = 160
# Brainstorms are kept the way the goals are: a file beside the tree they
# argue with, holding every conversation this project has had rather than the
# one the browser happens to have on screen. The caps are what keeps that file
# a file -- a conversation nobody ever started a second one of would otherwise
# grow for as long as the project does.
BRAINSTORM_LIMIT = 40
BRAINSTORM_TURNS = 400
_BRAINSTORM_TEXT = 20000
_BRAINSTORM_TITLE = 120
BRAINSTORM_ROLES = ("you", "engelbart")
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
    todos: Path
    important: Path
    goal_context: Path
    context_snapshot: Path
    notices: Path
    brainstorms: Path
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


def _now_ms() -> str:
    """A timestamp a browser can compare against its own page-load moment.

    ``_now()`` rounds down to the second, which places a notice written a
    fraction of a second *after* a page opened a fraction of a second
    *before* it.  The banner filters on "newer than this page", so that
    rounding does not make it late -- it makes it never appear.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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
        todos=session_dir / "todos.json",
        important=session_dir / "important.json",
        goal_context=session_dir / "goal_context.md",
        context_snapshot=session_dir / "context_snapshot.json",
        notices=session_dir / "notices.json",
        brainstorms=session_dir / "brainstorms.json",
        lock_dir=session_dir / ".lock",
    )


def _local_lock(lock_dir: Path) -> threading.RLock:
    key = str(lock_dir)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _pid_alive(pid: Any) -> bool:
    return pid_alive(pid)


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
            maybe_fchmod(handle.fileno(), 0o600)
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


def mark_goals_ui_invoked(session_id: str, root: Optional[Path] = None) -> None:
    """Record that this chat opened its goal workspace, keeping the first time.

    Every session's history is ingested, but analysis and context injection
    belong only to the chats whose owner asked for them by running /bart.
    Opening the workspace again is also how a disabled chat is turned back on.
    """
    with session_lock(session_id, root, wait_s=5) as p:
        manifest = load_manifest(session_id, root)
        opened = bool(manifest.get("goals_ui_invoked_at"))
        disabled = bool(manifest.get("goals_ui_disabled_at"))
        if opened and not disabled:
            return
        if not opened:
            manifest["goals_ui_invoked_at"] = _now()
        manifest.pop("goals_ui_disabled_at", None)
        manifest["updated_at"] = _now()
        _atomic_json(p.manifest, manifest)


def _project_home(where) -> str:
    """One spelling of a project's home, for every writer and every reader.

    Two chats that name the same directory differently -- one through a
    symlink, one through the checkout it was cloned into -- do not meet in
    the middle on their own, and a project whose chats disagree about its
    address has as many trees as it has spellings.
    """
    said = str(where or "").strip()
    if not said:
        return ""
    try:
        from . import project_store as PS
        return PS.repo_home(said)
    except Exception:  # noqa: BLE001 - a path git cannot read is still a path
        try:
            return str(Path(said).expanduser().resolve())
        except (OSError, RuntimeError):
            return str(Path(said).expanduser())


def _project_tree_session(home: str, root: Optional[Path],
                          unless: str = "") -> str:
    """Which chat's store holds a project's tree.

    A project's goals live in one chat's directory -- the first one that was
    ever worked in there. Every chat bound afterwards reads and writes that
    same store, which is what makes joining a project show the project rather
    than an empty page. Preferring a store that actually has goals over the
    merely oldest matters: a chat opened, abandoned and never written in
    would otherwise become the project's tree forever.
    """
    base = _state_base(root)
    target = _project_home(home)
    best, best_score = "", None
    try:
        entries = sorted(base.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return ""
    for entry in entries:
        if not entry.is_dir() or entry.name == unless:
            continue
        try:
            manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        where = manifest.get("project_home") or manifest.get("cwd") or ""
        try:
            same = bool(where) and _project_home(where) == target
        except (OSError, RuntimeError, TypeError):
            same = False
        if not same:
            continue
        # How much of the project's work this store actually holds, and when
        # it was last touched. Ordering the directories instead picked by
        # name, and a session id is a UUID -- so the winner was whichever
        # random string sorted first, which is how a seven-goal store beat a
        # hundred-goal one.
        try:
            held = json.loads((entry / "goals.json").read_text(encoding="utf-8"))
            count = len(held.get("goals") or []) if isinstance(held, dict) else 0
        except (OSError, ValueError):
            count = 0
        try:
            when = (entry / "goals.json").stat().st_mtime
        except OSError:
            when = 0.0
        score = (count, when)
        if best_score is None or score > best_score:
            best, best_score = entry.name, score
    return best


def tree_session(session_id: str, root: Optional[Path] = None) -> str:
    """The session whose store this chat's goals actually live in.

    Itself, until this chat belongs to a project; from then on whichever
    store that project says is its tree. The project is asked rather than
    the chat, because two chats working it out for themselves can disagree
    -- and did, one reading a seven-goal store while the other read a
    hundred-goal one, both naming the same project.
    """
    try:
        manifest = load_manifest(session_id, root)
    except (OSError, ValueError, TypeError):
        return session_id
    # The binding only, never the raw cwd: reading a project's tree because
    # of the directory a chat happens to sit in is the implicit rule this
    # replaced, and it would show an unbound chat the goals it is about to be
    # asked which project it belongs to. Migration writes project_home from
    # the cwd, so a chat that predates binding still joins.
    home = str(manifest.get("project_home") or "")
    held = ""
    if home:
        try:
            from . import project_store as PS
            held = PS.tree_session(root, home)
            if not held:
                # Nobody has named it yet: the store holding the project's
                # work is named now, once, for every chat that follows.
                held = PS.set_tree_session(
                    root, home,
                    _project_tree_session(home, root) or session_id)
        except Exception:  # noqa: BLE001 - a read must never fail over this
            held = ""
    held = held or str(manifest.get("project_tree") or "")
    if not held or held == session_id:
        return session_id
    try:
        paths(held, root)
    except (ValueError, TypeError):
        return session_id
    return held


def bind_project(session_id: str, home, root: Optional[Path] = None) -> str:
    """Tie this chat to one project, permanently, until it is tied to another.

    The directory a chat was started in used to decide its project on its
    own, which made every chat in a folder the same project and left nothing
    to choose. The binding is recorded on the chat instead: it is what the
    onboarding asks for, it survives a resume, and re-binding moves the chat
    rather than refusing -- a chat put in the wrong project should be
    correctable without being abandoned.
    """
    where = str(home or "").strip()
    if not where:
        raise ValueError("a project needs somewhere to be")
    resolved = _project_home(where)
    with session_lock(session_id, root, wait_s=5) as p:
        manifest = load_manifest(session_id, root)
        manifest["project_home"] = resolved
        # Decided here rather than on every read: a project's tree must not
        # change hands under a chat that is in the middle of reading it.
        shared = _project_tree_session(resolved, root, unless=session_id)
        manifest["project_tree"] = shared or session_id
        manifest["project_bound_at"] = _now()
        manifest["updated_at"] = _now()
        _atomic_json(p.manifest, manifest)
    return resolved


def unbind_project(session_id: str, root: Optional[Path] = None) -> bool:
    """Cut a chat loose from the project it was in.

    Used when the project itself is forgotten: a chat left naming a record
    that is gone reads an empty tree and is never asked about it, which is
    the worst of both. Unbound, it goes through onboarding again and the
    reader says where it belongs.
    """
    try:
        with session_lock(session_id, root, wait_s=5) as p:
            manifest = load_manifest(session_id, root)
            if not any(manifest.get(k) for k in
                       ("project_home", "project_tree", "project_bound_at")):
                return False
            for key in ("project_home", "project_tree", "project_bound_at",
                        "project_bound_by"):
                manifest.pop(key, None)
            manifest["updated_at"] = _now()
            _atomic_json(p.manifest, manifest)
        return True
    except (OSError, ValueError, TypeError, TimeoutError):
        return False


def chats_in_project(home, root: Optional[Path] = None) -> List[str]:
    """Every chat that says it belongs to this project."""
    where = _project_home(home)
    out: List[str] = []
    if not where:
        return out
    try:
        entries = sorted(_state_base(root).iterdir(), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            manifest = json.loads(
                (entry / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        said = manifest.get("project_home")
        if isinstance(said, str) and said and _project_home(said) == where:
            out.append(entry.name)
    return out


def mark_project_migrated(session_id: str, root: Optional[Path] = None) -> None:
    """Record that a chat predating the binding was taken as already bound.

    Written without a project_home: the chat keeps naming whatever directory
    it named before, and only stops being asked about it.
    """
    with session_lock(session_id, root, wait_s=5) as p:
        manifest = load_manifest(session_id, root)
        already = bool(manifest.get("project_bound_at"))
        if already and manifest.get("project_home"):
            return
        # Filling in, not re-binding: the first migration wrote only the
        # moment, so a chat declared already-in-a-project had no project to
        # be in -- it kept reading its own store, and one directory served
        # two trees. The moment stands; what it was missing is added.
        if not already:
            manifest["project_bound_at"] = _now()
            manifest["project_bound_by"] = "migration"
        home = str(manifest.get("project_home") or manifest.get("cwd") or "")
        if not home:
            if not already:
                manifest["updated_at"] = _now()
                _atomic_json(p.manifest, manifest)
            return
        manifest["project_home"] = _project_home(home)
        manifest["updated_at"] = _now()
        _atomic_json(p.manifest, manifest)


def needs_project_onboarding(session_id: str, root: Optional[Path] = None) -> bool:
    """Whether to ask this chat which project it is for.

    A chat the hooks have seen always has a manifest -- SessionStart writes
    one before anything else -- so "a manifest without a binding" is exactly
    a chat that has never been asked. A scope with no manifest at all is not
    a chat awaiting onboarding: it is a workspace opened directly on a
    directory, and asking it would be asking nobody.
    """
    try:
        p = paths(session_id, root)
    except (ValueError, TypeError):
        return False
    if not p.manifest.is_file():
        return False
    return not project_bound(session_id, root)


def bound_project(session_id: str, root: Optional[Path] = None) -> str:
    """Where this chat's project lives, or "" when it was never bound."""
    try:
        return str(load_manifest(session_id, root).get("project_home") or "")
    except (OSError, ValueError, TypeError):
        return ""


def project_bound(session_id: str, root: Optional[Path] = None) -> bool:
    """Whether the reader has said which project this chat is for.

    False is what sends a chat through onboarding, so it is answered from
    the binding alone -- never from the directory, which every chat has and
    which therefore could never tell a bound chat from a new one.

    Answered from the moment of binding rather than from its target: a chat
    can be past onboarding while still naming the directory it started in,
    and reading the home instead would call that chat new forever.
    """
    try:
        manifest = load_manifest(session_id, root)
    except (OSError, ValueError, TypeError):
        return False
    return bool(manifest.get("project_bound_at") or manifest.get("project_home"))


def open_workspace_for(cwd, root: Optional[Path] = None) -> str:
    """Start a workspace of this vault's own, for a directory, and name it.

    Everything here is keyed by the chat a workspace serves, and a project
    nobody has worked in yet has none -- which used to make it unopenable,
    which made creating one a dead end. So one is made: a session this
    vault minted rather than one Claude started, with the directory on it.

    It holds goals, TODO rows and builds like any other, and a build of its
    rows runs in that directory and leaves Claude's own session behind. The
    difference is only where it came from, which is worth recording.
    """
    import uuid
    session_id = "hcws-" + uuid.uuid4().hex[:24]
    here = str(Path(str(cwd)).expanduser())
    with session_lock(session_id, root, wait_s=5) as p:
        manifest = _default_manifest(session_id)
        manifest["cwd"] = here
        manifest["origin"] = "workspace"
        manifest["goals_ui_invoked_at"] = _now()
        p.session_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(p.manifest, manifest)
    return session_id


def disable_goals_ui(session_id: str, root: Optional[Path] = None) -> None:
    """Stop injecting and analyzing this chat until /bart is run again."""
    with session_lock(session_id, root, wait_s=5) as p:
        manifest = load_manifest(session_id, root)
        manifest["goals_ui_disabled_at"] = _now()
        manifest["updated_at"] = _now()
        _atomic_json(p.manifest, manifest)
        # Claude is told nothing while this is off, so whatever it "last saw"
        # stops being a base a later diff can honestly be taken against.
        p.context_snapshot.unlink(missing_ok=True)


def goals_ui_invoked(session_id: str, root: Optional[Path] = None) -> bool:
    """True once /bart has opened this chat's workspace at least once.

    The public "has this chat ever opted in" query, which a disable does not
    revoke; ``goals_ui_active`` is the narrower "may we act right now" gate.
    """
    return bool(load_manifest(session_id, root).get("goals_ui_invoked_at"))


def goals_ui_active(session_id: str, root: Optional[Path] = None) -> bool:
    """True while this chat's goals may be analyzed and injected.

    One /bart is the whole opt-in: it does not expire when the browser
    tab closes or the workspace server exits, because the goals the user
    wrote there outlive the window that wrote them.
    """
    manifest = load_manifest(session_id, root)
    return bool(manifest.get("goals_ui_invoked_at")) and not manifest.get(
        "goals_ui_disabled_at"
    )


def _notice_detail(text: Any) -> str:
    """One scannable line, from whatever the hook happened to carry."""
    return " ".join(str(text or "").split())[:_NOTICE_DETAIL]


def load_notices(session_id: str, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """What this chat's session has done lately, oldest first.

    Read without the session lock, like ``load_manifest`` and
    ``load_context_snapshot``: the file is only ever replaced whole
    (``os.replace``), so a reader sees one version or the other and never a
    torn one -- and this is on the path of an HTTP handler that already holds
    the lock, where a second timed acquisition could only add a way to fail.
    """
    value = _read_json(paths(session_id, root).notices, [])
    if not isinstance(value, list):
        return []
    return [row for row in value
            if isinstance(row, dict) and row.get("id")
            and row.get("kind") in NOTICE_KINDS]


def add_notice(
    session_id: str,
    kind: str,
    detail: str = "",
    root: Optional[Path] = None,
    wait_s: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Record something the open workspace should tell the reader about.

    An unknown *kind* is dropped rather than stored: the browser draws one of
    three fixed sentences, and a kind nobody wrote a sentence for would reach
    the reader as a blank banner.  ``wait_s`` is short on the hook path -- a
    banner may never be the reason Claude waits.
    """
    if kind not in NOTICE_KINDS:
        return None
    row = {
        "id": os.urandom(8).hex(),
        "kind": kind,
        "at": _now_ms(),
        "detail": _notice_detail(detail),
    }
    with session_lock(session_id, root, wait_s=wait_s) as p:
        rows = load_notices(session_id, root)
        rows.append(row)
        # A workspace opened now wants the last few minutes, not the session.
        _atomic_json(p.notices, rows[-NOTICE_LIMIT:])
    return row


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

    ``goals-ui`` and ``hc-ui`` are the pre-rename spellings and still appear
    in transcripts recorded before each rename, so every name is recognized.
    A prompt that only ever opened the workspace is not a goal.
    """
    lowered = str(text or "").strip().lower()
    for name in ("bart", "goals-ui", "hc-ui"):
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


def _hook_notice(
    payload: Dict[str, Any], hook_event: str
) -> Optional[Tuple[str, str]]:
    """What this hook proves, in the reader's terms, or nothing.

    Deliberately narrow.  A hook fires with a payload and no wider knowledge:
    a ``Stop`` means the turn ended, and says nothing about whether goals
    moved, tasks closed or work succeeded.  The banner may only repeat what
    is in the payload.
    """
    if hook_event == "Stop":
        return "session_stopped", _notice_detail(
            payload.get("last_assistant_message"))
    if hook_event == "SubagentStop":
        # agent_type is what the matcher filters on and what the reader would
        # recognize; agent_id is the fallback when a payload omits the name.
        who = _notice_detail(
            payload.get("agent_type") or payload.get("agent_id"))
        said = _notice_detail(payload.get("last_assistant_message"))
        if who and said:
            return "subagent_returned", _notice_detail(f"{who}: {said}")
        return "subagent_returned", who or said
    if hook_event == "SessionEnd":
        return "session_ended", _notice_detail(payload.get("reason"))
    return None


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

    notice = _hook_notice(payload, hook_event)
    if notice is not None:
        try:
            # Already inside this session's lock, so the wait is nominal --
            # and a banner may never be what costs a hook its ingest.
            add_notice(session_id, notice[0], notice[1], root, wait_s=0.5)
        except (OSError, ValueError, TypeError):
            pass

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
    p = paths(tree_session(session_id, root), root)
    goals = _read_json(p.goals, {"version": 1, "goals": []})
    important = _read_json(p.important, {"items": []})
    if not isinstance(goals, dict):
        goals = {"version": 1, "goals": []}
    if not isinstance(important, dict):
        important = {"items": []}
    goals.setdefault("goals", [])
    important.setdefault("items", [])
    # The rail's rows live in their own file, apart from everything the notes
    # travel in; a store from before the split still carries them inline and
    # is read as it is, to be rewritten split on its next save.
    todos = _read_json(p.todos, {})
    overlay_todo_store(goals, todos.get("todos")
                       if isinstance(todos, dict) else {})
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
            description = " ".join(str(goal.get("description") or "").split())
            priority = str(goal.get("priority") or "normal")
            if description:
                lines.append(f"{indent}  - DESCRIPTION: {description}")
            # The goal's markdown document, whole. Squashing it to one capped
            # line used to throw away the bullets that carry the actual state;
            # only sections nobody has written in are left out.
            written = [(title, body)
                       for title, body in split_doc(goal.get("notes"))
                       if body.strip()]
            if written:
                lines.append(f"{indent}  - USER NOTES:")
                lines.extend(f"{indent}    {line}".rstrip()
                             for line in join_doc(written).splitlines())
            # The rail's list, kept apart from the notes so an edit to one
            # never touches the other -- and injected the same way, whole.
            todos_md = str(goal.get("todos_md") or "").strip("\n")
            if todos_md.strip():
                lines.append(f"{indent}  - TODOS:")
                lines.extend(f"{indent}    {line}".rstrip()
                             for line in todos_md.splitlines())
                # The files those rows cite by "[attachment #N]", so a
                # marker in the list above is never a dangling reference.
                shots = render_attachments(goal.get("todo_items"))
                if shots:
                    lines.append(f"{indent}  - ATTACHMENTS:")
                    lines.extend(f"{indent}    {line}".rstrip()
                                 for line in shots.splitlines())
            if priority != "normal":
                lines.append(f"{indent}  - PRIORITY: {priority}")
            # What the user attached as background for this goal, named by
            # kind so the reader knows whether a label is a checkout, a repo
            # or a URL before deciding to go read it. Normalized on the way
            # out because nothing between the browser and here guarantees the
            # stored rows are typed. Six is a list, not a manifest: sources
            # are pointers away from the goal, so they stay a short index
            # while everything the user wrote here is rendered whole.
            for source in normalize_sources(goal.get("sources"))[:6]:
                lines.append(f"{indent}  - SOURCE ({source['type']}): "
                             f"{source['label']}")
        for todo in goal.get("todos", []):
            if details and isinstance(todo, dict) and not todo.get("done"):
                lines.append(f"{indent}  - TODO: {todo.get('text', '')}")
        # No arbitrary head-of-list here: the evidence a user linked to a
        # goal, and the lines they marked important, are the goal.
        for prompt_id in goal.get("prompt_ids", []):
            prompt = prompt_map.get(prompt_id)
            if details and prompt:
                text = " ".join(str(prompt.get("text") or "").split())
                lines.append(f"{indent}  - USER PROMPT: {text}")
        for item_id in goal.get("important_item_ids", []):
            item = item_map.get(item_id)
            if details and item:
                lines.append(f"{indent}  - IMPORTANT: {str(item.get('text') or '')}")
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
    # Deliberately unbounded. Claude Code caps a single additionalContext at
    # 10,000 characters and degrades to a file plus a preview past that, so a
    # second budget here could only silently drop the user's own words.
    return "\n".join(lines) + "\n"


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
    # Written where the tree lives, and locked there too: two chats bound to
    # one project are two writers of one file, and a lock taken on the chat
    # rather than on the store would not keep them apart.
    session_id = tree_session(session_id, root)
    with session_lock(session_id, root, wait_s=5) as p:
        current_goals, current_important = load_goals(session_id, root)
        if expected_revision is not None and expected_revision != _revision_of(
            current_goals, current_important
        ):
            return False
        prompts = load_prompts(session_id, root)
        goals = link_evidence_prompts(_ensure_prompt_ids(goals), prompts)
        goals["generated_at"] = _now()
        # goals.json is the tree and its notes; the rows go to todos.json --
        # a TODO is never stored in, derived from, or parsed out of the notes.
        _atomic_json(p.goals, strip_todo_items(goals))
        _atomic_json(p.todos, {"version": 1, "todos": split_todo_store(goals)})
        _atomic_json(p.important, important)
        text = _goal_context_text(session_id, goals, important, prompts)
        _atomic_write(p.goal_context, text.encode("utf-8"))
    # The project's own file -- one per directory, holding the goals of every
    # chat started in it -- is a snapshot of what was just written, so it is
    # refreshed here rather than by each of the many callers. Outside the
    # lock, and imported here rather than at the top: it reads this module.
    from . import project_store
    project_store.refresh_for_session(session_id, root)
    return True


def _brainstorm_title(messages: List[Dict[str, Any]]) -> str:
    """What to call a conversation, taken from the first thing they said.

    Never from the model's half: every brainstorm opens on the same
    invitation, so titling by the first turn would name every conversation
    the same thing.
    """
    for row in messages:
        if row.get("role") == "you":
            said = " ".join(str(row.get("text") or "").split())
            if said:
                return said[:_BRAINSTORM_TITLE]
    return ""


def _brainstorm_turns(value: Any) -> List[Dict[str, Any]]:
    """The conversation, kept to the shape the panel draws.

    A turn is a role this screen has and words under it; anything else came
    from a hand-edited file or a browser that had drifted, and is dropped
    rather than stored for the panel to trip over later.
    """
    out: List[Dict[str, Any]] = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        text = str(row.get("text") or "")
        if role not in BRAINSTORM_ROLES or not text.strip():
            continue
        out.append({"role": role, "text": text[:_BRAINSTORM_TEXT]})
    # Bounded from the oldest end, like every other read of a conversation
    # here: what a reader comes back to is the end of it.
    return out[-BRAINSTORM_TURNS:]


def load_brainstorms(
    session_id: str, root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Every brainstorm held against this chat's tree, newest first.

    Read where the goals are read -- ``tree_session`` -- so the conversations
    a project has had are the project's rather than one chat's, the same way
    its goals are. Read without the lock for the reason ``load_notices`` is:
    the file is only ever replaced whole.
    """
    p = paths(tree_session(session_id, root), root)
    value = _read_json(p.brainstorms, {})
    rows = value.get("chats") if isinstance(value, dict) else None
    out: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not str(row.get("id") or ""):
            continue
        turns = _brainstorm_turns(row.get("messages"))
        if not turns:
            continue
        out.append({
            "id": str(row.get("id")),
            "title": str(row.get("title") or "")[:_BRAINSTORM_TITLE]
                     or _brainstorm_title(turns),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "messages": turns,
        })
    out.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return out


def save_brainstorm(
    session_id: str,
    chat_id: str,
    messages: Any,
    root: Optional[Path] = None,
    wait_s: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """Write one brainstorm down, replacing the one it is a longer version of.

    Called after each round rather than at the end of one, because there is
    no end: the reader closes the tab. A conversation with nothing said in it
    is not stored -- opening the screen and leaving is not a brainstorm.

    ``chat_id`` names the conversation being extended; an empty one, or one
    the file has never seen, starts a new record and the id it was given is
    the caller's handle on it from then on.
    """
    turns = _brainstorm_turns(messages)
    if not turns:
        return None
    session_id = tree_session(session_id, root)
    with session_lock(session_id, root, wait_s=wait_s) as p:
        held = load_brainstorms(session_id, root)
        wanted = str(chat_id or "")
        row = next((r for r in held if r["id"] == wanted), None) if wanted else None
        now = _now_ms()
        if row is None:
            row = {"id": wanted or os.urandom(8).hex(), "created_at": now}
        else:
            held.remove(row)
        row["messages"] = turns
        row["title"] = _brainstorm_title(turns)
        row["updated_at"] = now
        # The one just written goes to the front before the sort rather than
        # relying on it: two saves inside the same millisecond carry the same
        # stamp, and a stable sort would leave the older of them first.
        held.insert(0, row)
        held.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        _atomic_json(p.brainstorms,
                     {"version": 1, "chats": held[:BRAINSTORM_LIMIT]})
    return dict(row)


def _project_key(cwd: Path) -> str:
    # Claude encodes an absolute project path by replacing all non-word path
    # punctuation (including spaces) with hyphens.
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(cwd))


def load_context_snapshot(
    session_id: str, root: Optional[Path] = None
) -> Dict[str, Any]:
    """What this chat's model was last shown, or ``{}`` if nothing or unusable."""
    value = _read_json(paths(session_id, root).context_snapshot, {})
    return value if isinstance(value, dict) else {}


def save_context_snapshot(
    session_id: str,
    text: str,
    root: Optional[Path] = None,
    wait_s: float = 5.0,
) -> Dict[str, Any]:
    """Remember the exact text just handed to the model.

    This records what was *rendered*, not what was delivered: Claude Code may
    still drop or compact the injection, in which case the next diff is taken
    against text the model no longer has. SessionStart's full re-send after
    compaction and the mirror file are the recovery, not a delivery receipt.

    ``wait_s`` is short on the injection path -- see the caller. Raising here
    is the safe failure: the snapshot does not advance, so the change this
    render described is still pending and the next one restates it.
    """
    snapshot = {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
        "at": _now(),
    }
    with session_lock(session_id, root, wait_s=wait_s) as p:
        _atomic_json(p.context_snapshot, snapshot)
    return snapshot


def clear_context_snapshot(session_id: str, root: Optional[Path] = None) -> None:
    """Forget it, so the next injection sends the whole document again."""
    with session_lock(session_id, root, wait_s=5) as p:
        p.context_snapshot.unlink(missing_ok=True)


def mirror_goal_context(
    session_id: str,
    transcript_path: Optional[str] = None,
    cwd: Optional[str] = None,
    root: Optional[Path] = None,
) -> Optional[Path]:
    """Copy this chat's goal document beside Claude's own transcript.

    Context arrives as a system reminder the user never sees.  The mirror is
    the readable half of that bargain: an ordinary markdown file, in the
    directory the session already lives in, that can be opened, diffed and
    deleted.  Best effort by construction -- a hook must not fail over a copy.
    """
    try:
        text = paths(tree_session(session_id, root),
                     root).goal_context.read_text(encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return None
    try:
        if transcript_path:
            project = _absolute(Path(str(transcript_path))).parent
        elif cwd:
            project = (Path.home() / ".claude" / "projects"
                       / _project_key(_absolute(Path(str(cwd)))))
            # Only ever sit beside a project directory Claude itself made.
            # Guessing one into existence would scatter files under homes
            # that never ran this session.
            if not project.is_dir():
                return None
        else:
            return None
        target_dir = project / "goals-ui"
        target = target_dir / f"{session_id}.md"
        if target_dir.is_symlink():
            return None
        try:
            if target.read_text(encoding="utf-8") == text:
                return target
        except (OSError, ValueError):
            pass
        if not target_dir.is_dir():
            # Only tighten a directory we are the ones creating; re-chmoding
            # one Claude or the user already owns is not ours to do.
            target_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(target_dir, 0o700)
        _atomic_write(target, text.encode("utf-8"))
        return target
    except (OSError, ValueError, TypeError):
        return None


def render_context_injection(
    session_id: str,
    mode: str,
    transcript_path: Optional[str] = None,
    cwd: Optional[str] = None,
    root: Optional[Path] = None,
    remember: bool = True,
    snapshot_wait_s: float = 5.0,
) -> str:
    """Render the goal document for one injection point.

    ``full`` states the whole document; ``delta`` states only what changed
    since the model last saw it, which is the difference between paying for
    the goal tree once a conversation and paying for it once a message.
    ``remember=False`` renders for a reader with its own separate context (a
    subagent) without claiming the main conversation has seen anything.
    ``snapshot_wait_s`` bounds how long recording the render may wait on the
    session lock; a hook on the model's own turn cannot afford the default.
    """
    p = paths(session_id, root)
    # The document comes from wherever this chat's tree lives; the snapshot
    # of what THIS chat was last told stays its own, because two chats on one
    # project have seen different amounts of it.
    held = paths(tree_session(session_id, root), root)
    try:
        current = held.goal_context.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    if not current.strip():
        return ""
    location = mirror_goal_context(session_id, transcript_path, cwd, root) \
        or held.goal_context
    full = (f"# Goals for this Claude chat (full file: {location})\n\n"
            f"{current}")

    def keep(text: str) -> str:
        if remember:
            save_context_snapshot(session_id, current, root, snapshot_wait_s)
        return text

    if mode != "delta":
        return keep(full)
    snapshot = load_context_snapshot(session_id, root)
    previous = snapshot.get("text")
    if not isinstance(previous, str) or not snapshot.get("sha256"):
        return keep(full)
    if snapshot["sha256"] == hashlib.sha256(current.encode("utf-8")).hexdigest():
        return ""
    diff = "\n".join(difflib.unified_diff(
        previous.splitlines(),
        current.splitlines(),
        fromfile="goals (as you last saw them)",
        tofile="goals (now)",
        lineterm="",
        n=1,
    ))
    delta = (f"# Goals for this chat changed since your last message "
             f"(full file: {location})\n\n{diff}\n")
    # A diff of a document that was rewritten end to end is longer than the
    # document; there is nothing to be gained by sending it twice over.
    return keep(delta if len(delta) < len(full) else full)
