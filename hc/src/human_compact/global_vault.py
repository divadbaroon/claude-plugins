"""Noninteractive global Vault setup and lifecycle capture.

The npm installer owns a private Python runtime, so global capture cannot
depend on ``jq`` or on that runtime being present on the user's shell PATH.
This module is called through the stable ``hc`` launcher resolved by the
installed hook adapter.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


def home_dir() -> Path:
    return Path(os.environ.get("HC_HOME", Path.home()))


def vault_root() -> Path:
    return Path(os.environ.get("CLAUDE_VAULT_DIR",
                               home_dir() / ".claude-vault"))


def projects_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECTS_DIR",
                               home_dir() / ".claude" / "projects"))


def enable_file() -> Path:
    return home_dir() / ".human-compact" / "config" / "global-vault"


def _persist_enable_state(state: str) -> Path:
    target = enable_file()
    _secure_dir(target.parent, home_dir() / ".human-compact")
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_text(state + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def enable_always_on() -> Path:
    """Persist always-on capture without modifying a shell profile."""
    return _persist_enable_state("enabled")


def disable_always_on() -> Path:
    """Persist an explicit opt-out that overrides legacy environment state."""
    return _persist_enable_state("disabled")


def is_enabled() -> bool:
    try:
        state = enable_file().read_text().strip()
    except FileNotFoundError:
        # Before setup recorded an explicit choice, preserve the historical
        # per-process opt-in used by ``claude --vault`` and old shell profiles.
        return os.environ.get("CLAUDE_VAULT") == "1"
    except OSError:
        # A present-but-unreadable choice must fail closed instead of silently
        # re-enabling capture through a legacy environment export.
        return False
    return state == "enabled"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


_SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _valid_session_id(value: str) -> bool:
    return bool(_SAFE_SESSION.fullmatch(value))


def _secure_dir(path: Path, root: Path) -> None:
    """Create and lock down ``root`` plus every directory beneath it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path
    stop = root.parent
    while current != stop:
        if current.is_symlink():
            raise RuntimeError(f"refusing symlinked state directory: {current}")
        os.chmod(current, 0o700)
        if current == root:
            break
        if current.parent == current:
            raise RuntimeError(f"state path escaped its root: {path}")
        current = current.parent


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validated_transcript(raw: str) -> Optional[Path]:
    if not raw:
        return None
    source = Path(raw)
    if source.is_symlink():
        return None
    try:
        resolved = source.resolve(strict=True)
        root = projects_root().resolve(strict=True)
    except OSError:
        return None
    if not _inside(resolved, root) or not resolved.is_file():
        return None
    return resolved


def _session_day(source: Path, timestamp: str) -> str:
    if timestamp:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone().date().isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(source.stat().st_mtime).astimezone() \
        .date().isoformat()


def _first_record(source: Path) -> Dict:
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return {}


def _atomic_copy(source: Path, destination: Path) -> None:
    _secure_dir(destination.parent, vault_root())
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    try:
        shutil.copyfile(source, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(destination: Path, value: Dict) -> None:
    _secure_dir(destination.parent, vault_root())
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(value, indent=1) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def _existing_session(root: Path, session_id: str) -> Optional[Path]:
    sessions = root / "sessions"
    if not sessions.is_dir():
        return None
    for day in sessions.iterdir():
        candidate = day / session_id
        if (day.is_dir() and candidate.is_dir() and
                (candidate / "conversation.jsonl").is_file() and
                (candidate / "metadata.json").is_file()):
            return candidate
    return None


def backfill() -> Dict[str, int]:
    """Import Claude transcripts idempotently, raising on any failed copy.

    Successful copies from a partially failed run remain valid and are skipped
    when setup is retried. Always-on capture is enabled only after this
    function returns successfully.
    """
    source_root, root = projects_root(), vault_root()
    counts = {"imported": 0, "skipped": 0}
    if not source_root.is_dir():
        return counts
    for source in sorted(source_root.glob("*/*.jsonl")):
        session_id = source.stem
        if not _valid_session_id(session_id):
            raise ValueError(f"unsafe Claude session filename: {source.name}")
        validated = _validated_transcript(str(source))
        if validated is None:
            raise ValueError(f"refusing symlinked or out-of-scope transcript: {source}")
        source = validated
        if _existing_session(root, session_id):
            counts["skipped"] += 1
            continue
        first = _first_record(source)
        timestamp = str(first.get("timestamp") or "")
        destination = (root / "sessions" /
                       _session_day(source, timestamp) / session_id)
        day_dir = destination.parent
        _secure_dir(day_dir, root)
        staging = Path(tempfile.mkdtemp(prefix=f".{session_id}.setup-",
                                        dir=str(day_dir)))
        os.chmod(staging, 0o700)
        try:
            _atomic_copy(source, staging / "conversation.jsonl")
            _atomic_json(staging / "metadata.json", {
                "session_id": session_id,
                "cwd": str(first.get("cwd") or ""),
                "started_at": timestamp,
                "transcript_path": str(source),
                "start_source": "backfill",
                "imported_at": _utc_now(),
            })
            if destination.exists():
                quarantine = destination.with_name(
                    f".{session_id}.incomplete-{os.getpid()}")
                destination.rename(quarantine)
            os.replace(staging, destination)
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
        counts["imported"] += 1
    return counts


def _debug(event: str, message: str) -> None:
    if os.environ.get("VAULT_DEBUG") != "1":
        return
    root = vault_root()
    _secure_dir(root, root)
    debug = root / "debug.log"
    with debug.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"ts": _utc_now(), "event": event,
                                 "msg": message}) + "\n")
    os.chmod(debug, 0o600)


def _live_base(root: Path, session_id: str) -> Path:
    existing = _existing_session(root, session_id)
    if existing:
        return existing
    return root / "sessions" / datetime.now().astimezone().date().isoformat() / session_id


def _snapshot(transcript: str, destination: Path, event: str) -> None:
    source = _validated_transcript(transcript)
    if source is None:
        _debug(event, "transcript unavailable")
        return
    _atomic_copy(source, destination)
    _debug(event, "snapshot -> " + destination.name)


def _counter(base: Path, increment: bool = False) -> int:
    path = base / ".compaction-counter"
    try:
        current = int(path.read_text().strip())
    except (OSError, ValueError):
        current = 0
    if increment:
        current += 1
        path.write_text(str(current) + "\n")
        os.chmod(path, 0o600)
    return current


def _start_worker(root: Path, session_id: str, now: str) -> None:
    queue = root / "trajectory" / "queue"
    _secure_dir(queue, root)
    marker = queue / session_id
    marker.write_text(now + "\n")
    os.chmod(marker, 0o600)
    log_path = root / "trajectory" / "worker.log"
    _secure_dir(log_path.parent, root)
    child_env = os.environ.copy()
    with log_path.open("a", encoding="utf-8") as log:
        os.chmod(log_path, 0o600)
        subprocess.Popen(
            [sys.executable, "-m", "human_compact.cli", "worker"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, env=child_env,
        )


def handle_hook(payload: Dict, output=None) -> None:
    """Handle one Claude lifecycle event. Every missing-input path is inert."""
    if not is_enabled():
        return
    output = output or sys.stdout
    event = str(payload.get("hook_event_name") or "")
    session_id = str(payload.get("session_id") or "")
    if not event or not _valid_session_id(session_id):
        _debug(event or "?", "missing event or session_id")
        return
    root = vault_root()
    base = _live_base(root, session_id)
    _secure_dir(base, root)
    now, transcript = _utc_now(), str(payload.get("transcript_path") or "")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if event == "SessionStart":
        metadata = base / "metadata.json"
        if not metadata.exists():
            _atomic_json(metadata, {
                "session_id": session_id,
                "cwd": str(payload.get("cwd") or ""),
                "started_at": now,
                "transcript_path": transcript,
                "start_source": str(payload.get("source") or ""),
            })
        _snapshot(transcript, base / "conversation.jsonl", event)
        goal_context = root / "trajectory" / "goal_context.md"
        if goal_context.is_file():
            response = {"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": goal_context.read_text(),
            }}
            output.write(json.dumps(response) + "\n")
    elif event == "PreCompact":
        number = _counter(base, increment=True)
        trigger = re.sub(r"[^A-Za-z0-9_-]+", "-",
                         str(payload.get("trigger") or "unknown"))[:40] or "unknown"
        destination = (base / "snapshots" /
                       f"pre-compact-{number:03d}-{stamp}-{trigger}.jsonl")
        _snapshot(transcript, destination, event)
        _snapshot(transcript, base / "conversation.jsonl", event)
    elif event == "PostCompact":
        if "compact_summary" in payload:
            number = _counter(base)
            destination = (base / "compactions" /
                           f"summary-{number:03d}-{stamp}.json")
            _atomic_json(destination, {
                "ts": now, "trigger": payload.get("trigger"),
                "compact_summary": payload.get("compact_summary"),
            })
    elif event == "SessionEnd":
        _snapshot(transcript, base / "conversation.jsonl", event)
        ends = base / "ends.jsonl"
        with ends.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"ended_at": now,
                                     "reason": payload.get("reason", "unknown")}) + "\n")
        os.chmod(ends, 0o600)
        _start_worker(root, session_id, now)
