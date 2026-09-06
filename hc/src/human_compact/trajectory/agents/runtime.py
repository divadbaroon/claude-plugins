"""Where the project lives and how it is touched.

Every read of a file, every command, every preview start and every build
the agents cause goes through one of these. ``LocalRuntime`` is this
machine: the project directory on disk, subprocess for commands, the
preview engine and the build for the rest. A sandboxed runtime -- Daytona
is the one planned -- is another class with these same methods, chosen by
``make``; nothing in the agents or on the page would change.

The methods are spans (``trace``), named for what they are: file.read,
file.write, command.exec, preview.start; the orchestrator wraps ``build``
and ``reopen`` in build.agent, whichever runtime carries them out.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import trace

KINDS = ("local", "daytona")
READ_LIMIT = 20_000
COMMAND_TIMEOUT_S = 60
# What ``discover`` looks at: the files that say what a project is and how
# it runs. Bounded so the answer fits in a prompt.
MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "Procfile",
             "Makefile", "Cargo.toml", "go.mod", "Gemfile", "composer.json",
             "README.md", "readme.md", "README", ".env.example", "docker-compose.yml",
             "Dockerfile", "index.html", "app.py", "main.py", "manage.py",
             "next.config.js", "vite.config.js", "vite.config.ts", "tsconfig.json")
DISCOVER_FILES = 60
DISCOVER_MANIFEST_CHARS = 1200


class Runtime:
    """The interface. Subclasses fill every method; this one only refuses."""

    kind = "abstract"

    def __init__(self, cwd: str = "", root: Optional[Path] = None):
        self.cwd = str(cwd or "")
        self.root = root

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "cwd": self.cwd}

    def read_file(self, relative: str, limit: int = READ_LIMIT) -> str:
        raise NotImplementedError

    def write_file(self, relative: str, text: str) -> None:
        raise NotImplementedError

    def run(self, argv: Sequence[str], timeout: float = COMMAND_TIMEOUT_S) -> Dict[str, Any]:
        raise NotImplementedError

    def discover(self, question: str = "") -> str:
        """What the project directory itself says: the answer to a question
        an agent could not settle from the conversation, when the answer
        is on disk rather than in the reader's head."""
        raise NotImplementedError

    def preview_state(self, session_id: str = "") -> Dict[str, Any]:
        raise NotImplementedError

    def build(self, session_id: str, root: Optional[Path], goal_id: str,
              row_ids: List[str], quick: bool = False) -> Dict[str, Any]:
        raise NotImplementedError

    def reopen(self, session_id: str, root: Optional[Path], goal_id: str,
               row_id: str, note: str) -> Dict[str, Any]:
        raise NotImplementedError


class LocalRuntime(Runtime):
    """This machine: the directory the chat is bound to, and the tools hc
    already has for running, previewing and building in it."""

    kind = "local"

    def _inside(self, relative: str) -> Path:
        base = Path(self.cwd or ".").resolve()
        target = (base / str(relative or "")).resolve()
        if target != base and base not in target.parents:
            raise ValueError("outside the project: %s" % relative)
        return target

    def read_file(self, relative: str, limit: int = READ_LIMIT) -> str:
        with trace.span("file.read", path=str(relative)):
            return self._inside(relative).read_text(encoding="utf-8", errors="replace")[:limit]

    def write_file(self, relative: str, text: str) -> None:
        with trace.span("file.write", path=str(relative), chars=len(text or "")):
            target = self._inside(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(text or ""), encoding="utf-8")

    def run(self, argv: Sequence[str], timeout: float = COMMAND_TIMEOUT_S) -> Dict[str, Any]:
        argv = [str(a) for a in argv]
        with trace.span("command.exec", command=" ".join(argv)[:200]) as span:
            try:
                done = subprocess.run(argv, cwd=self.cwd or None, capture_output=True,
                                      text=True, timeout=timeout)
            except FileNotFoundError:
                span["status"] = "error"
                return {"ok": False, "code": 127, "out": "", "err": "not found: " + argv[0]}
            except subprocess.TimeoutExpired:
                span["status"] = "error"
                return {"ok": False, "code": -1, "out": "", "err": "timed out"}
            span["attrs"]["code"] = done.returncode
            return {"ok": done.returncode == 0, "code": done.returncode,
                    "out": done.stdout[-8000:], "err": done.stderr[-8000:]}

    def discover(self, question: str = "") -> str:
        with trace.span("file.read", path=".", why="discover"):
            base = Path(self.cwd or ".")
            if not base.is_dir():
                return "The project directory is not there: %s" % self.cwd
            names: List[str] = []
            try:
                for entry in sorted(os.listdir(base)):
                    if entry.startswith(".") or entry in ("node_modules", "__pycache__", "venv", ".venv"):
                        continue
                    names.append(entry + ("/" if (base / entry).is_dir() else ""))
            except OSError:
                pass
            lines = ["Directory: %s" % self.cwd,
                     "Top level: " + (", ".join(names[:DISCOVER_FILES]) or "(empty)")]
            for name in MANIFESTS:
                spot = base / name
                if spot.is_file():
                    try:
                        text = spot.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    lines += ["", "--- %s ---" % name, text[:DISCOVER_MANIFEST_CHARS].rstrip()]
            try:
                from .. import preview as PV
                config = PV.read_config(self.root, self.cwd)
                profiles = [p for p in (config or {}).get("profiles") or [] if isinstance(p, dict)]
                if profiles:
                    lines += ["", "How it runs (from the preview): "
                              + "; ".join(str(p.get("command") or "") for p in profiles)]
            except Exception:  # noqa: BLE001 -- discovery is best effort
                pass
            return "\n".join(lines)

    def preview_state(self, session_id: str = "") -> Dict[str, Any]:
        from .. import preview as PV
        try:
            return PV.state(self.root, self.cwd, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "unknown", "error": str(exc)[:200]}

    def start_preview(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        from .. import preview as PV
        with trace.span("preview.start", command=str(profile.get("command") or "")):
            return PV.start(self.root, self.cwd, profile)

    def build(self, session_id: str, root: Optional[Path], goal_id: str,
              row_ids: List[str], quick: bool = False) -> Dict[str, Any]:
        from .. import build as BUILD
        return BUILD.start(session_id, root, goal_id, list(row_ids), quick=quick)

    def reopen(self, session_id: str, root: Optional[Path], goal_id: str,
               row_id: str, note: str) -> Dict[str, Any]:
        from .. import build as BUILD
        return BUILD.reopen(session_id, root, goal_id, row_id, note)


def make(kind: Optional[str] = None, cwd: str = "", root: Optional[Path] = None) -> Runtime:
    """The runtime for this install: HC_AGENT_RUNTIME, local by default.

    ``daytona`` is named so the switch exists where it will be flipped;
    until the class does, asking for it says so rather than running the
    build on this machine under a name that promises otherwise.
    """
    kind = (kind or os.environ.get("HC_AGENT_RUNTIME", "") or "local").strip().lower()
    if kind == "local":
        return LocalRuntime(cwd, root)
    if kind == "daytona":
        raise RuntimeError("DaytonaRuntime is not here yet: the Build agent runs "
                           "locally until it lands (HC_AGENT_RUNTIME=local)")
    raise ValueError("not a runtime: %r (one of %s)" % (kind, ", ".join(KINDS)))
