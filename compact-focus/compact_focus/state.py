from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from . import SCHEMA_VERSION


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_component(value: str, fallback: str = "unknown") -> str:
    cleaned = _SAFE.sub("-", value or "").strip(".-")[:96]
    return cleaned or fallback


def project_id(cwd: str) -> str:
    canonical = os.path.realpath(os.path.expanduser(cwd or os.getcwd()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def state_root() -> Path:
    configured = (
        os.environ.get("COMPACT_FOCUS_STATE_DIR")
        or os.environ.get("PLUGIN_DATA")
        or os.environ.get("CLAUDE_PLUGIN_DATA")
        or os.path.join(Path.home(), ".claude", "compact-focus")
    )
    return Path(configured).expanduser().resolve()


def atomic_write_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


@contextlib.contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[bool]:
    """Cross-process advisory lock; yields False for a busy nonblocking lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)
        handle = os.fdopen(fd, "a+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt  # pragma: no cover - exercised on Windows CI

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
                acquired = True
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt  # pragma: no cover

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def append_jsonl(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with file_lock(path.with_suffix(path.suffix + ".lock")) as acquired:
        if not acquired:  # blocking locks always acquire; defensive only
            raise RuntimeError(f"could not lock {path}")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class StatePaths:
    base: Path
    session_id: str
    project_id: str
    cwd: str

    @classmethod
    def from_hook(cls, payload: Dict[str, Any]) -> "StatePaths":
        sid = safe_component(str(payload.get("session_id") or "unknown"))
        cwd = str(payload.get("cwd") or os.getcwd())
        return cls(state_root(), sid, project_id(cwd), os.path.realpath(cwd))

    @classmethod
    def explicit(cls, session_id: str, cwd: Optional[str] = None) -> "StatePaths":
        resolved_cwd = os.path.realpath(cwd or os.getcwd())
        return cls(state_root(), safe_component(session_id), project_id(resolved_cwd), resolved_cwd)

    @property
    def session(self) -> Path:
        return self.base / "sessions" / self.session_id

    @property
    def project(self) -> Path:
        return self.base / "projects" / self.project_id

    @property
    def cycles(self) -> Path:
        return self.session / "cycles"

    @property
    def events(self) -> Path:
        return self.session / "events.jsonl"

    @property
    def lock(self) -> Path:
        return self.session / ".session.lock"

    def ensure(self) -> None:
        self.cycles.mkdir(parents=True, exist_ok=True)
        (self.project / "policy").mkdir(parents=True, exist_ok=True)
        for directory in (
            self.base,
            self.base / "sessions",
            self.base / "projects",
            self.session,
            self.cycles,
            self.project,
            self.project / "policy",
        ):
            if directory.exists():
                with contextlib.suppress(OSError):
                    os.chmod(directory, 0o700)
        atomic_write_json(
            self.session / "identity.json",
            {
                "schema_version": SCHEMA_VERSION,
                "session_id": self.session_id,
                "project_id": self.project_id,
                "cwd": self.cwd,
            },
        )
        atomic_write_text(self.project / "recent-session", self.session_id + "\n")

    def cycle(self, cycle_id: str) -> Path:
        return self.cycles / safe_component(cycle_id, "cycle")

    def latest_cycle_id(self) -> Optional[str]:
        try:
            value = (self.session / "latest-cycle").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return safe_component(value) if value else None

    def set_latest_cycle(self, cycle_id: str) -> None:
        atomic_write_text(self.session / "latest-cycle", safe_component(cycle_id) + "\n")

    def record(self, event: str, **fields: Any) -> None:
        row: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": utc_now(),
            "event": event,
            "session_id": self.session_id,
            "project_id": self.project_id,
        }
        row.update(fields)
        append_jsonl(self.events, row)


def cycle_id(source_hash: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"c-{stamp}-{safe_component(source_hash[:10], 'trace')}"
