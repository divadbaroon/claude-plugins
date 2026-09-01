"""The middle pane: what happens if you run what you have built.

The pane is not a browser. It answers one question -- *if I run this project
right now, what do I see?* -- and a browser is only the answer when the thing
being built happens to serve HTTP. A simulation answers with its output, a
script with the file it wrote, a project nobody has installed yet with the one
command that has to be run first. So the pane is a projection of two things
that are deliberately kept apart::

    surface   web | terminal | artifact | instructions | empty
    status    unconfigured | ready | needs_user_action | starting
              | running | finished | failed | stale | not_ready

Collapsing those into one enum is what makes a preview pane grow a case per
framework. Kept apart, ``web + running`` is an embedded page, ``terminal +
running`` is live output, ``web + needs_user_action`` is a card saying which
command has to be run first, and none of them know what a Next.js is.

Three parts, and only one of them is a model.

**The detector** reads the repository and nothing else. package.json scripts,
Makefile targets, Procfile, pyproject, Cargo, go.mod, manage.py, notebooks,
a lone index.html: each is evidence for a run profile, and each profile
carries the sentence explaining what running it does. Detection never runs
anything and never asks a model. It is fast enough to do on every open, but
its answer is cached against a fingerprint of the files it read, so a project
whose build files have not changed is not re-detected at all.

**The supervisor** owns the processes. One process per working directory, its
own session (so the whole group can be signalled), stdout read on a thread
into a bounded ring, and every claim about it re-checked rather than
remembered: a URL is believed once something answers on it, a pid is believed
once it is still there. Nothing here starts on its own -- a preview that ran
`npm run dev` because a page was opened would be a preview that ran arbitrary
repository code because a page was opened. Every start is a click.

**The model** is asked two questions, and only when the deterministic side
has run out: what to do about a run that failed, and -- for the TODO being
worked on -- what to look at once the thing is up. Both are cached; neither
is on the path that draws the pane.

``not_ready`` is the ending the Show UI button is allowed to have. That
button promises something visual, so a project with nothing that serves, and
a run started for a page that never produced one, both say exactly that
rather than leaving a frame to stay empty while a spinner turns.

What the surface is, is observed rather than declared. A profile says it
expects to serve HTTP; the pane calls it web only once a URL has been printed
and something has answered on it. That one rule is what keeps the pane honest
about a dev server that died three seconds after boot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import project_store as PS
from .secure_io import atomic_write_json
from ..platform_compat import detached_popen_kwargs, kill_process_tree

SCHEMA_VERSION = 1

# How much of a process's output the pane can show. A dev server that has
# been up for an hour has printed more than anybody reads; the last screenful
# is what says what it is doing now.
LINES_KEPT = 400

# A process that has printed no URL after this long is not starting up any
# more -- it is a program that does not serve HTTP, and its output is the
# thing to show.
STARTING_GRACE_S = 12.0

# Probes are for a server on this machine that either answers or does not.
PROBE_TIMEOUT_S = 1.5

# The model calls, both of which the pane draws fine without.
EXPLAIN_TIMEOUT_S = 90
INTENT_TIMEOUT_S = 90

# Files whose contents decide how a project runs. The fingerprint is taken
# over these, so a project only gets re-detected when one of them changes.
CONFIG_FILES = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "bun.lockb", "pyproject.toml", "requirements.txt", "Pipfile", "Cargo.toml",
    "go.mod", "Makefile", "makefile", "Procfile", "docker-compose.yml",
    "docker-compose.yaml", "compose.yaml", "manage.py", "CLAUDE.md",
    "AGENTS.md",
)

MAX_CONFIG_BYTES = 200_000

# What a URL looks like when a dev server announces itself. Vite says
# "Local:", Next says "url:", Django says "Starting development server at",
# and plenty say nothing but the URL itself.
URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?[^\s\"'<>`]*")
PORT_RE = re.compile(r"(?:listening|running|serving|started).{0,24}?"
                     r"\bport\s*[:=]?\s*(\d{2,5})\b", re.I)

# Output a terminal drew for a human: cursor moves, colour, the spinner a
# bundler leaves behind. Kept out of the ring so the pane shows text.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|[\r\x00-\x08\x0b\x0c\x0e-\x1f]")

# What counts as something a run produced, when it produced no URL and no
# output worth reading: the file it wrote.
ARTIFACT_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".csv",
                     ".json", ".html", ".md", ".txt", ".parquet", ".npy")

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".next", "target", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".engelbart"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(line: str) -> str:
    return ANSI_RE.sub("", line).rstrip()


def _resolved(cwd) -> str:
    try:
        return str(Path(str(cwd)).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return str(cwd or "")


# --- where the answer is kept ------------------------------------------------

def config_path(root: Optional[Path], cwd) -> Path:
    """Beside the project's record, keyed the same way.

    Its own file rather than a key in the project record, for the reason the
    server record is its own file: the record's project section is rebuilt
    from a whitelist on every save, and how a project runs is not something
    the reader authored.
    """
    target = PS.project_path(root, cwd)
    return target.with_name(target.stem + ".run.json")


def read_config(root: Optional[Path], cwd) -> Dict[str, Any]:
    if not cwd:
        return {}
    try:
        value = json.loads(config_path(root, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_config(root: Optional[Path], cwd, value: Dict[str, Any]) -> Path:
    target = config_path(root, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = dict(value)
    record["version"] = SCHEMA_VERSION
    record["cwd"] = _resolved(cwd)
    record["saved_at"] = _now()
    atomic_write_json(target, record, root=target.parent)
    return target


def intents_path(root: Optional[Path], cwd) -> Path:
    target = PS.project_path(root, cwd)
    return target.with_name(target.stem + ".intent.json")


def read_intents(root: Optional[Path], cwd) -> Dict[str, Any]:
    """What to look at, per TODO row. Kept because it costs a model call and
    does not change while the row's words do not."""
    if not cwd:
        return {}
    try:
        value = json.loads(intents_path(root, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def intent_of(root: Optional[Path], cwd, todo_id: str,
              text: str = "") -> Dict[str, Any]:
    """The stored intent for a row, unless the row has been reworded since.

    The words are what the intent was derived from, so a row that now says
    something else has no intent -- showing the old one would send the reader
    to check a thing nobody is building any more.
    """
    held = read_intents(root, cwd).get(str(todo_id))
    if not isinstance(held, dict):
        return {}
    if text and str(held.get("todo_text") or "") != text:
        return {}
    return held


def save_intent(root: Optional[Path], cwd, todo_id: str, text: str,
                value: Dict[str, Any]) -> None:
    held = read_intents(root, cwd)
    held[str(todo_id)] = dict(value, todo_text=text, saved_at=_now())
    # A project the reader has been through has hundreds of rows; the pane
    # only ever asks about the one in front of it.
    if len(held) > 200:
        ordered = sorted(held.items(),
                         key=lambda kv: str((kv[1] or {}).get("saved_at", "")))
        held = dict(ordered[-200:])
    target = intents_path(root, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, held, root=target.parent)


def fingerprint(cwd) -> str:
    """A digest of the files that decide how this project runs.

    Only the files, and only their contents: a project whose source changed
    but whose package.json did not runs the same way it did, and re-deriving
    that is a second of somebody's life for no answer.
    """
    where = Path(_resolved(cwd))
    digest = hashlib.sha256()
    for name in CONFIG_FILES:
        spot = where / name
        try:
            if not spot.is_file() or spot.stat().st_size > MAX_CONFIG_BYTES:
                continue
            digest.update(name.encode("utf-8"))
            digest.update(spot.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:32]


# --- the detector ------------------------------------------------------------

def _json_file(spot: Path) -> Dict[str, Any]:
    try:
        value = json.loads(spot.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text_file(spot: Path, limit: int = MAX_CONFIG_BYTES) -> str:
    try:
        if spot.stat().st_size > limit:
            return ""
        return spot.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _package_manager(where: Path) -> str:
    for name, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                          ("bun.lockb", "bun")):
        if (where / name).exists():
            return manager
    return "npm"


def _profile(pid: str, name: str, kind: str, command: str, why: str,
             serves: bool = False, rank: int = 50) -> Dict[str, Any]:
    return {"id": pid, "name": name, "kind": kind, "command": command,
            "why": why, "serves": bool(serves), "rank": rank}


def _node_profiles(where: Path) -> List[Dict[str, Any]]:
    package = _json_file(where / "package.json")
    if not package:
        return []
    scripts = package.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    manager = _package_manager(where)
    run = (manager + " run ") if manager != "npm" else "npm run "
    out: List[Dict[str, Any]] = []
    named = {
        "dev": ("Development server",
                "Starts the project's development server.", True, 5),
        "start": ("Start", "Starts the project the way its package.json"
                           " says to.", True, 12),
        "serve": ("Serve", "Serves the built project.", True, 15),
        "storybook": ("Storybook", "Runs the component workshop.", True, 40),
    }
    for script, (label, why, serves, rank) in named.items():
        if isinstance(scripts.get(script), str) and scripts[script].strip():
            out.append(_profile(script, label, "web" if serves else "cli",
                                run + script, why, serves, rank))
    if isinstance(scripts.get("test"), str) and scripts["test"].strip():
        out.append(_profile("test", "Tests", "test", run + "test",
                            "Runs the project's own test suite.", False, 70))
    if not out and isinstance(package.get("main"), str) and package["main"]:
        out.append(_profile("main", "Main", "cli",
                            "node " + package["main"],
                            "Runs the entry point package.json names.",
                            False, 60))
    return out


def _make_targets(where: Path) -> List[str]:
    text = _text_file(where / "Makefile") or _text_file(where / "makefile")
    if not text:
        return []
    found = []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", line)
        if match:
            found.append(match.group(1))
    return found


def _python_profiles(where: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if (where / "manage.py").is_file():
        out.append(_profile("django", "Django server", "web",
                            "python manage.py runserver",
                            "Starts the Django development server.",
                            True, 6))
    for name in ("streamlit_app.py", "app_streamlit.py"):
        if (where / name).is_file():
            out.append(_profile("streamlit", "Streamlit app", "web",
                                f"streamlit run {name}",
                                "Serves the Streamlit app.", True, 8))
            break
    for name in ("app.py", "main.py", "server.py", "run.py"):
        if not (where / name).is_file():
            continue
        text = _text_file(where / name, 100_000)
        serves = bool(re.search(r"\b(FastAPI|Flask|uvicorn|aiohttp|"
                                r"http\.server|gradio)\b", text))
        out.append(_profile(
            Path(name).stem, name, "web" if serves else "script",
            (f"uvicorn {Path(name).stem}:app --reload"
             if "FastAPI" in text and "uvicorn" not in text else
             f"python {name}"),
            "Serves the app this file defines." if serves
            else f"Runs {name} and shows what it prints.",
            serves, 10 if serves else 30))
        break
    pyproject = _text_file(where / "pyproject.toml")
    if pyproject:
        block = re.search(r"\[project\.scripts\](.*?)(\n\[|\Z)", pyproject,
                          re.S)
        if block:
            for line in block.group(1).splitlines():
                match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=", line)
                if match:
                    out.append(_profile(
                        "script-" + match.group(1), match.group(1), "cli",
                        match.group(1),
                        "Runs the command this project installs.", False, 45))
                    break
    if (where / "tests").is_dir() or (where / "test").is_dir():
        out.append(_profile("pytest", "Tests", "test", "pytest",
                            "Runs the project's own test suite.", False, 72))
    return out


def _other_profiles(where: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    procfile = _text_file(where / "Procfile")
    for line in procfile.splitlines():
        match = re.match(r"^web\s*:\s*(.+)$", line.strip())
        if match:
            out.append(_profile("procfile-web", "Web process", "web",
                                match.group(1).strip(),
                                "Runs the web process the Procfile names.",
                                True, 14))
            break
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml"):
        if (where / name).is_file():
            out.append(_profile("compose", "Docker Compose", "web",
                                "docker compose up",
                                "Brings up the services compose defines.",
                                True, 35))
            break
    targets = _make_targets(where)
    for target in ("dev", "run", "serve", "start"):
        if target in targets:
            out.append(_profile("make-" + target, f"make {target}",
                                "web" if target in ("dev", "serve", "start")
                                else "cli",
                                "make " + target,
                                f"Runs the Makefile's {target} target.",
                                target != "run", 20))
            break
    if (where / "Cargo.toml").is_file():
        out.append(_profile("cargo", "cargo run", "cli", "cargo run",
                            "Builds and runs the crate.", False, 25))
    if (where / "go.mod").is_file():
        out.append(_profile("go", "go run", "cli", "go run ./...",
                            "Builds and runs the module.", False, 26))
    if any(where.glob("*.ipynb")):
        out.append(_profile("jupyter", "Notebook", "web",
                            "jupyter lab --no-browser",
                            "Opens the notebooks in this project.", True, 55))
    if ((where / "index.html").is_file()
            and not (where / "package.json").is_file()):
        out.append(_profile("static", "Static site", "web",
                            "python3 -m http.server 8000",
                            "Serves this directory as a static site.",
                            True, 58))
    return out


def detect(cwd) -> List[Dict[str, Any]]:
    """Every way this project can be run, best first.

    Deterministic and read-only: the evidence is the repository's own files,
    and a project that says nothing about how it runs gets an empty list
    rather than a guess. That empty list is what sends the question to the
    model, once, and only when somebody asks.
    """
    where = Path(_resolved(cwd))
    if not where.is_dir():
        return []
    found = (_node_profiles(where) + _python_profiles(where)
             + _other_profiles(where))
    seen, out = set(), []
    for item in sorted(found, key=lambda p: p["rank"]):
        if item["command"] in seen:
            continue
        seen.add(item["command"])
        out.append(item)
    return out


# --- what has to happen before it will run -----------------------------------

def blockers(cwd, profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The one step needed, when the project cannot start without it.

    Deterministic, and deliberately short: three things that stop a first run
    on most projects and that a file on disk can prove. Anything subtler is
    the failed card's job -- it has the actual error to work from, which
    beats a guess made before anything ran.
    """
    where = Path(_resolved(cwd))
    if not where.is_dir():
        return []
    command = str((profile or {}).get("command") or "")
    out: List[Dict[str, Any]] = []
    if (where / "package.json").is_file() and not (where / "node_modules").is_dir():
        manager = _package_manager(where)
        out.append({
            "id": "node_modules",
            "text": "The project's dependencies are not installed yet.",
            "command": f"{manager} install",
            "why": "package.json lists dependencies and node_modules is not"
                   " there, so the run command has nothing to run.",
            "kind": "run"})
    if (where / ".env.example").is_file() and not (where / ".env").is_file():
        out.append({
            "id": "env",
            "text": "This project expects a .env file that is not there.",
            "command": "cp .env.example .env",
            "why": "The example is checked in and the real one is not."
                   " Copy it, then fill in the values it names.",
            "kind": "manual"})
    if (command.startswith("python") or command.startswith("pytest")
            or command.startswith("uvicorn") or command.startswith("streamlit")):
        needs = ((where / "requirements.txt").is_file()
                 or (where / "pyproject.toml").is_file())
        has_env = any((where / name).is_dir()
                      for name in (".venv", "venv", "env"))
        if needs and not has_env and not os.environ.get("VIRTUAL_ENV"):
            install = ("pip install -r requirements.txt"
                       if (where / "requirements.txt").is_file()
                       else "pip install -e .")
            out.append({
                "id": "python_env",
                "text": "No virtual environment for this project's"
                        " dependencies.",
                "command": f"python3 -m venv .venv && . .venv/bin/activate"
                           f" && {install}",
                "why": "The project declares dependencies and there is no"
                       " .venv here, so the run may fail on an import.",
                "kind": "manual"})
    return out


# --- the supervisor ----------------------------------------------------------

class Proc:
    """One running preview, and the thread reading what it prints."""

    def __init__(self, cwd: str, profile: Dict[str, Any], log: Optional[Path]):
        self.cwd = cwd
        self.profile = dict(profile)
        self.log = log
        self.lines: deque = deque(maxlen=LINES_KEPT)
        self.url = ""
        self.started_at = time.time()
        self.exit_code: Optional[int] = None
        self.stopped = False
        self.error = ""
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.healthy = False
        self.embeddable = True
        self.probed_at = 0.0
        # Whether a page was the point. A run somebody started to see the
        # program's output is doing its job when it prints; a run somebody
        # pressed "Show UI" for and which never serves anything is not
        # failing -- there is simply no UI here yet, and saying that is
        # worth more than a terminal the reader did not ask for.
        self.wanted_ui = False
        self.steps: List[str] = []

    # -- lifecycle
    def spawn(self, env: Optional[Dict[str, str]] = None) -> None:
        """Start the command on a pseudo-terminal, in its own session.

        A terminal rather than a pipe, because a program that cannot see one
        buffers its output in kilobyte blocks -- the pane would show nothing
        for a minute and then everything at once, and the URL a dev server
        prints on its second line would arrive after the reader gave up. Its
        own session so the whole process group can be signalled: `npm run
        dev` is a shell that starts a node that starts a bundler, and killing
        the shell alone leaves the port taken.
        """
        shell = shutil.which("bash") or "/bin/sh"
        handle = None
        if self.log:
            try:
                self.log.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.log, "a", encoding="utf-8")
            except OSError:
                handle = None
        self._log_handle = handle
        child = dict(os.environ, **(env or {}))
        child.setdefault("TERM", "xterm-256color")
        # Python's own buffering is not the terminal's, and a simulation
        # printing a line a second is exactly what this pane is for.
        child["PYTHONUNBUFFERED"] = "1"
        try:
            import pty
            self._master, slave = pty.openpty()
        except Exception:                               # noqa: BLE001
            self._master, slave = None, None
        if self._master is None:
            self.process = subprocess.Popen(
                [shell, "-lc", self.profile["command"]], cwd=self.cwd,
                env=child, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                close_fds=True, **detached_popen_kwargs())
        else:
            self.process = subprocess.Popen(
                [shell, "-lc", self.profile["command"]], cwd=self.cwd,
                env=child, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, **detached_popen_kwargs())
            os.close(slave)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _keep(self, line: str) -> None:
        line = _clean(line)
        if not line:
            return
        self.lines.append(line)
        if not self.url:
            self._sniff(line)

    def _read(self) -> None:
        assert self.process
        handle = getattr(self, "_log_handle", None)
        try:
            if getattr(self, "_master", None) is not None:
                rest = ""
                while True:
                    try:
                        chunk = os.read(self._master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", "replace")
                    if handle:
                        try:
                            handle.write(text)
                            handle.flush()
                        except (OSError, ValueError):
                            pass
                    rest += text
                    parts = re.split(r"\r\n|\n|\r", rest)
                    rest = parts.pop()
                    for line in parts:
                        self._keep(line)
                if rest:
                    self._keep(rest)
            else:
                for raw in self.process.stdout:          # type: ignore[union-attr]
                    self._keep(raw)
                    if handle:
                        try:
                            handle.write(raw)
                            handle.flush()
                        except (OSError, ValueError):
                            pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.exit_code = self.process.wait()
            except (OSError, ValueError):
                self.exit_code = -1
            for closing in (getattr(self, "_master", None),):
                if closing is not None:
                    try:
                        os.close(closing)
                    except OSError:
                        pass
            if handle:
                try:
                    handle.close()
                except OSError:
                    pass

    def _sniff(self, line: str) -> None:
        """The address the program just announced, if it announced one."""
        match = URL_RE.search(line)
        if match:
            self.url = match.group(0).rstrip(".,);")
            return
        port = PORT_RE.search(line)
        if port:
            self.url = "http://localhost:" + port.group(1)

    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def stop(self) -> bool:
        if not self.alive():
            return False
        self.stopped = True
        assert self.process
        try:
            kill_process_tree(self.process.pid)
        except Exception:  # noqa: BLE001 - fall back to a direct terminate
            try:
                self.process.terminate()
            except OSError:
                return False
        return True

    # -- what is true about it now
    def probe(self, force: bool = False) -> None:
        """Ask the address whether anything is there, and whether the pane
        may embed it. Both are re-checked rather than remembered: a dev
        server that fell over still has its URL in the scrollback."""
        if not self.url or (not force and time.time() - self.probed_at < 2.0):
            return
        self.probed_at = time.time()
        request = urllib.request.Request(self.url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_S) as answer:
                headers = answer.headers
                self.healthy = True
        except urllib.error.HTTPError as exc:          # a 404 is still a server
            headers, self.healthy = exc.headers, True
        except Exception:                              # noqa: BLE001
            self.healthy, self.embeddable = False, self.embeddable
            return
        deny = str(headers.get("X-Frame-Options") or "").lower()
        policy = str(headers.get("Content-Security-Policy") or "").lower()
        frames = re.search(r"frame-ancestors([^;]*)", policy)
        self.embeddable = not (
            "deny" in deny or "sameorigin" in deny
            or (frames and "none" in frames.group(1)))

    def status(self) -> str:
        if self.alive():
            if self.url:
                return "running"
            if time.time() - self.started_at < STARTING_GRACE_S:
                return "starting"
            return "running"
        if self.stopped:
            return "finished"
        return "finished" if self.exit_code == 0 else "failed"

    def snapshot(self) -> Dict[str, Any]:
        self.probe()
        return {"command": self.profile.get("command", ""),
                "wanted_ui": self.wanted_ui, "steps": list(self.steps),
                "profile_id": self.profile.get("id", ""),
                "profile_name": self.profile.get("name", ""),
                "pid": self.process.pid if self.process else None,
                "url": self.url, "healthy": self.healthy,
                "embeddable": self.embeddable,
                "started_at": self.started_at,
                "seconds": round(time.time() - self.started_at, 1),
                "exit_code": self.exit_code,
                "lines": list(self.lines)}


_RUNS: Dict[str, Proc] = {}
_RUNS_LOCK = threading.Lock()


def running(cwd) -> Optional[Proc]:
    with _RUNS_LOCK:
        return _RUNS.get(_resolved(cwd))


def start(root: Optional[Path], cwd, profile: Dict[str, Any],
          session_id: str = "") -> Dict[str, Any]:
    """Run one profile, here, now, because somebody pressed Run.

    Nothing in this module calls this on its own. The command comes out of
    the project's own files and runs with the reader's environment: that is
    the point of the pane, and also why it is never automatic.
    """
    where = _resolved(cwd)
    if not Path(where).is_dir():
        return {"ok": False, "error": "no directory to run in"}
    command = str(profile.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "that profile has no command"}
    held = running(where)
    if held and held.alive():
        return {"ok": False, "error": "something is already running here",
                "run": held.snapshot()}
    # A serving profile started at all is consent to start it again when the
    # same command is still the verified one (see show_ui's auto). Written
    # before the spawn branches so every door in -- Run, Show UI, the dev
    # handoff -- says the same thing.
    if profile.get("serves"):
        _set_autostart(root, where, True)
    if session_id and dev_owns(where, profile) and command == str(
            profile.get("command") or "").strip():
        # The project's own dev script: dev_server runs it, and keeps
        # running it after this window closes. Starting a second one here
        # would take a second port and answer to nobody.
        from . import dev_server as DEV
        out = DEV.start(session_id, root, where)
        if out.get("ok"):
            return {"ok": True, "owner": "dev_server", "dev": out}
        return {"ok": False, "error": str(out.get("error")
                                          or "the dev server would not start")}
    log = None
    try:
        target = PS.project_path(root, where)
        log = target.with_name(target.stem + ".run.log")
    except Exception:                                   # noqa: BLE001
        log = None
    proc = Proc(where, profile, log)
    try:
        proc.spawn()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not start: {exc}"[:200]}
    with _RUNS_LOCK:
        _RUNS[where] = proc
    return {"ok": True, "run": proc.snapshot()}


# How long a run started for its UI is given to produce one before the pane
# stops calling it "starting". Longer than the plain grace: a cold Next build
# compiles for a while before it says anything.
UI_GRACE_S = 75.0


def _set_autostart(root: Optional[Path], cwd, value: bool) -> None:
    """Remember whether this project's UI starts on its own.

    Written where the run config lives, because it is a fact about the
    project rather than about one workspace window: Stop says "do not
    restart what I just stopped" everywhere, Start says the opposite.
    """
    config = read_config(root, cwd)
    if not config:
        return
    if bool(config.get("autostart", True)) == bool(value):
        return
    config["autostart"] = bool(value)
    write_config(root, cwd, config)


def ui_profile(config: Dict[str, Any]) -> Dict[str, Any]:
    """The profile that would put a page on screen, if this project has one.

    The primary one when it serves; otherwise the best-ranked one that does.
    A project whose only run target is a simulation has none, and that is an
    answer rather than a failure.
    """
    primary = _primary(config)
    if primary.get("serves"):
        return primary
    for item in config.get("profiles") or []:
        if isinstance(item, dict) and item.get("serves"):
            return item
    return {}


def show_ui(root: Optional[Path], cwd, session_id: str = "",
            auto: bool = False) -> Dict[str, Any]:
    """Do whatever has to happen for there to be a page to look at.

    One press, and the steps behind it are the project's own: install what
    is missing, then start the thing that serves. They are composed into one
    shell line rather than orchestrated here -- `a && b` is what a person
    would type, the terminal streams both, and a failure in the first stops
    the second without this module having to sequence anything.

    A project with nothing that serves gets told so. That is the honest end
    of this button, and pretending otherwise -- a spinner, a blank frame --
    is how a preview pane teaches people to distrust it.

    ``auto`` is the pane starting the project on its own, so the reader sees
    the thing without being asked to run it first. Bounded, not open: only a
    serving command read out of the repository's OWN files ever qualifies (a
    model's guess never starts unasked), nothing may need installing first,
    and a Stop press turns it off for the project until a Start turns it
    back on. A project whose run files changed is re-detected -- detection
    is free and read-only -- and the fresh answer is what runs.
    """
    where = _resolved(cwd)
    config = read_config(root, where)
    profile = ui_profile(config)
    if auto:
        if config.get("autostart") is False:
            return {"ok": False, "auto": True,
                    "error": "autostart is off for this project"}
        stale = bool(config.get("fingerprint")
                     and config["fingerprint"] != fingerprint(where))
        if stale or not config:
            configure(root, where, detect_only=True)
            config = read_config(root, where)
            profile = ui_profile(config)
        if (not profile or str(config.get("source") or "") == "model"
                or blockers(where, profile)):
            return {"ok": False, "auto": True,
                    "error": "not eligible to start unasked"}
    if not profile:
        return {"ok": False, "not_ready": True,
                "reason": ("nothing in this project serves a page yet — what"
                           " it has is " + (", ".join(
                               str(p.get("name") or p.get("id"))
                               for p in (config.get("profiles") or [])[:3])
                               or "no run target at all"))}
    held = running(where)
    if held and held.alive():
        return {"ok": True, "already": True, "run": held.snapshot()}
    # The steps a file on disk proves are needed, in front of the run. Only
    # the ones that are commands to run: copying a .env and filling it in is
    # not something to chain a dev server behind.
    steps = [b["command"] for b in blockers(where, profile)
             if b.get("kind") == "run" and b.get("command")]
    if not steps and session_id and dev_owns(where, profile):
        out = start(root, where, profile, session_id=session_id)
        if out.get("ok"):
            out["ui"] = True
        return out
    composed = dict(profile)
    composed["command"] = " && ".join(steps + [str(profile["command"])])
    out = start(root, where, composed)
    proc = running(where)
    if out.get("ok") and proc:
        proc.wanted_ui = True
        proc.steps = steps + [str(profile["command"])]
        out["run"] = proc.snapshot()
    out["ui"] = True
    return out


def stop(cwd, root: Optional[Path] = None, session_id: str = "") -> Dict[str, Any]:
    # A Stop press is also "and do not start this on your own again": the
    # remembered consent behind show_ui's auto ends where the reader ends
    # the run. The next manual start remembers the opposite.
    _set_autostart(root, cwd, False)
    proc = running(cwd)
    if proc:
        proc.stop()
        return {"ok": True}
    if session_id:
        from . import dev_server as DEV
        held = dev_state(session_id, root, cwd)
        if held.get("status") in ("running", "starting"):
            out = DEV.stop(session_id, root, str(_resolved(cwd)))
            return {"ok": bool(out.get("ok", True)), "owner": "dev_server"}
    return {"ok": False, "error": "nothing is running here"}


def forget(cwd) -> None:
    """Drop a finished run so the pane goes back to offering to start one."""
    where = _resolved(cwd)
    with _RUNS_LOCK:
        proc = _RUNS.get(where)
        if proc and not proc.alive():
            _RUNS.pop(where, None)


def artifacts(cwd, since: float, limit: int = 12) -> List[str]:
    """Files this run wrote: the answer, when a script is the program.

    Cheap on purpose -- one shallow walk, the noisy directories skipped --
    because it is asked every time a finished run is drawn.
    """
    where = Path(_resolved(cwd))
    out: List[str] = []
    if not where.is_dir():
        return out
    for base, dirs, files in os.walk(where):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.lower().endswith(ARTIFACT_SUFFIXES):
                continue
            spot = Path(base) / name
            try:
                if spot.stat().st_mtime <= since:
                    continue
            except OSError:
                continue
            try:
                out.append(str(spot.relative_to(where)))
            except ValueError:
                out.append(str(spot))
            if len(out) >= limit:
                return sorted(out)
    return sorted(out)


# --- the one supervisor rule -------------------------------------------------
#
# This module is not the first thing here that can run `npm run dev`.
# dev_server owns that one: it adopts a server back after the workspace
# restarts, tells our process from somebody else's on the same port, and
# knows the PATH a node started from a GUI needs. Two supervisors racing to
# start a dev server in one directory is two servers, a port fight, and each
# of them reporting the other as unknown -- so where dev_server can run a
# profile, this hands it over and reads its answer back. Everything it does
# not do (scripts, simulations, tests, python servers, anything that is not
# a node dev script) is this module's own.

def _dev_plan(cwd) -> Dict[str, Any]:
    from . import dev_server as DEV
    try:
        return DEV.detect(str(cwd)) or {}
    except Exception:                                   # noqa: BLE001
        return {}


def dev_owns(cwd, profile: Optional[Dict[str, Any]]) -> bool:
    """Whether dev_server is the one that should run this profile."""
    command = str((profile or {}).get("command") or "").strip()
    if not command:
        return False
    plan = _dev_plan(cwd)
    if not plan.get("ok"):
        return False
    theirs = " ".join(plan.get("command") or [])
    if command == theirs:
        return True
    # The detector spells it the way a person would (`npm run dev`); the plan
    # spells it the way it is executed (`/usr/local/bin/npm run dev`). Same
    # script, same project, one server.
    script = str(plan.get("script") or "")
    return bool(script) and command.split()[-1] == script and (
        command.startswith("npm ") or command.startswith("pnpm ")
        or command.startswith("yarn ") or command.startswith("bun "))


def dev_state(session_id: str, root: Optional[Path], cwd) -> Dict[str, Any]:
    from . import dev_server as DEV
    try:
        return DEV.status(str(session_id or ""), root, str(cwd)) or {}
    except Exception:                                   # noqa: BLE001
        return {}


# --- the projection the pane draws -------------------------------------------

def _primary(config: Dict[str, Any]) -> Dict[str, Any]:
    profiles = config.get("profiles")
    profiles = profiles if isinstance(profiles, list) else []
    wanted = str(config.get("primary") or "")
    for item in profiles:
        if isinstance(item, dict) and item.get("id") == wanted:
            return item
    return profiles[0] if profiles and isinstance(profiles[0], dict) else {}


def _surface(status: str, proc: Optional[Proc], produced: List[str]) -> str:
    if status == "not_ready":
        return "instructions"
    if status in ("running", "starting"):
        if proc and proc.url and proc.healthy:
            return "web"
        return "terminal"
    if status == "finished":
        return "artifact" if produced else "terminal"
    if status in ("unconfigured", "ready", "needs_user_action", "failed",
                  "stale"):
        return "instructions"
    return "empty"


def state(root: Optional[Path], cwd, intent: Optional[Dict[str, Any]] = None,
          session_id: str = "") -> Dict[str, Any]:
    """Everything the middle pane needs, in one read.

    No model, no subprocess, no writes: this is called on a sweep, so it may
    only look at what is already known -- the config file, the process this
    server started, and whether the address it announced still answers.
    """
    where = _resolved(cwd)
    if not where or not Path(where).is_dir():
        return {"ok": True, "cwd": where, "status": "unconfigured",
                "surface": "empty", "profiles": [], "blockers": [],
                "configured": False,
                "reason": "this workspace is not pointed at a directory yet"}
    config = read_config(root, where)
    profiles = config.get("profiles") if isinstance(config, dict) else []
    profiles = [p for p in (profiles or []) if isinstance(p, dict)]
    profile = _primary(config)
    proc = running(where)
    produced: List[str] = []
    stale = bool(config and config.get("fingerprint")
                 and config["fingerprint"] != fingerprint(where))

    # A dev server dev_server started -- in this window or a previous one --
    # is this project running, whoever pressed the button. Asked before our
    # own processes are, so the pane cannot offer to start a second one.
    theirs = (dev_state(session_id, root, where)
              if session_id and dev_owns(where, profile) else {})
    if theirs.get("status") in ("running", "starting") and not proc:
        return _dev_projection(where, config, profiles, profile, theirs,
                               stale, intent)

    if proc:
        status = proc.status()
        if status == "finished":
            produced = artifacts(where, proc.started_at)
        # Asked for a page and still without one: a run that was started to
        # show a UI and has been up a while with nothing serving is not
        # "running" in any sense the reader cares about. Saying so beats a
        # terminal they did not ask for and a frame that never fills.
        if (proc.wanted_ui and status in ("running", "starting")
                and not proc.url
                and time.time() - proc.started_at > UI_GRACE_S):
            status = "not_ready"
    elif not profiles:
        status = "unconfigured"
    else:
        status = "stale" if stale else "ready"

    found = blockers(where, profile) if status in ("ready", "stale") else []
    if found and status in ("ready", "stale"):
        status = "needs_user_action"

    serving = ui_profile(config)
    out: Dict[str, Any] = {
        "ok": True,
        "cwd": where,
        "configured": bool(profiles),
        # Whether there is a page to be had at all, and what to say when
        # there is not. Read by the pane's Show UI button, which is the one
        # control that promises something visual.
        "ui": {"available": bool(serving),
               "profile_id": serving.get("id", ""),
               "command": serving.get("command", ""),
               "reason": ("" if serving else
                          "nothing in this project serves a page yet")},
        "stale": stale,
        # Whether this project may start on its own when the workspace
        # opens -- the reader's last Stop/Start press, read by the pane's
        # auto request (which the server re-checks regardless).
        "autostart": config.get("autostart") is not False,
        "status": status,
        "surface": _surface(status, proc, produced),
        "profile": profile,
        "profiles": profiles,
        "blockers": found,
        "artifacts": produced,
        "detected_at": config.get("detected_at", ""),
        "verified_at": config.get("verified_at", ""),
        "verified_command": config.get("verified_command", ""),
    }
    if proc:
        out["run"] = proc.snapshot()
        out["url"] = proc.url if proc.healthy else ""
    if isinstance(intent, dict) and intent:
        out["intent"] = intent
    return out


def _dev_projection(where: str, config: Dict[str, Any],
                    profiles: List[Dict[str, Any]],
                    profile: Dict[str, Any], theirs: Dict[str, Any],
                    stale: bool, intent) -> Dict[str, Any]:
    """The pane, drawn over a run dev_server owns.

    Same two answers as any other run -- a surface and a status -- read out
    of the record that module keeps rather than out of a process of ours.
    The address is theirs too: a dev server that moved to 3001 because 3000
    was taken said so, and the port anybody expected is not evidence.
    """
    url = str(theirs.get("url") or "")
    status = "running" if (theirs.get("status") == "running" and url) else "starting"
    lines = [str(line) for line in (theirs.get("last") or [])]
    out = {
        "ok": True, "cwd": where, "configured": bool(profiles), "stale": stale,
        "status": status, "surface": "web" if url else "terminal",
        "profile": profile, "profiles": profiles, "blockers": [],
        "artifacts": [], "owner": "dev_server",
        "detected_at": config.get("detected_at", ""),
        "verified_at": config.get("verified_at", ""),
        "verified_command": config.get("verified_command", ""),
        "url": url,
        "run": {"command": str(theirs.get("command")
                               or profile.get("command") or ""),
                "profile_id": profile.get("id", ""),
                "profile_name": profile.get("name", ""),
                "pid": theirs.get("pid"), "url": url, "healthy": bool(url),
                "embeddable": True, "exit_code": None,
                "seconds": 0, "lines": lines},
    }
    if isinstance(intent, dict) and intent:
        out["intent"] = intent
    return out


def configure(root: Optional[Path], cwd, engine=None,
              detect_only: bool = False) -> Dict[str, Any]:
    """Work out how this project runs, and write the answer down.

    The detector answers first and for free. Only a project it can say
    nothing about reaches the model, and what the model is asked for is one
    command and one sentence -- not a plan, not a refactor.

    ``detect_only`` is how the pane configures itself unasked: detection
    reads the repository's own files and runs nothing, so it may happen on
    an open -- but the model call may not, so a project the files say
    nothing about is left for the button rather than billed on a page load.
    """
    where = _resolved(cwd)
    if not Path(where).is_dir():
        return {"ok": False, "error": "no directory to look in"}
    profiles = detect(where)
    source = "repository"
    if not profiles:
        if detect_only:
            return {"ok": False, "not_configured": True,
                    "error": "nothing in this project's own files names a"
                             " way to run it"}
        asked = _ask_model(where, engine)
        if not asked.get("ok"):
            return asked
        profiles, source = asked["profiles"], "model"
    config = read_config(root, where)
    kept = {str(p.get("id")): p for p in (config.get("profiles") or [])
            if isinstance(p, dict)}
    for item in profiles:
        # A profile that was verified before keeps that stamp when the same
        # command comes back: re-detection is not a reason to doubt a run
        # that was watched working.
        was = kept.get(item["id"])
        if was and was.get("command") == item["command"] and was.get("verified"):
            item["verified"] = True
            item["verified_at"] = was.get("verified_at", "")
    record = {"fingerprint": fingerprint(where), "detected_at": _now(),
              "source": source, "profiles": profiles,
              "primary": profiles[0]["id"] if profiles else "",
              "verified_at": config.get("verified_at", ""),
              "verified_command": config.get("verified_command", "")}
    write_config(root, where, record)
    return {"ok": True, "source": source, "profiles": profiles,
            "primary": record["primary"]}


def set_primary(root: Optional[Path], cwd, profile_id: str) -> Dict[str, Any]:
    config = read_config(root, cwd)
    ids = [p.get("id") for p in (config.get("profiles") or [])
           if isinstance(p, dict)]
    if profile_id not in ids:
        return {"ok": False, "error": "no such run profile"}
    config["primary"] = profile_id
    write_config(root, cwd, config)
    return {"ok": True, "primary": profile_id}


def note_verified(root: Optional[Path], cwd, command: str) -> None:
    """A run that actually came up. Written once, and read as the reason the
    pane stops offering to re-detect: a command watched working beats a
    command derived from a file."""
    config = read_config(root, cwd)
    if not config:
        return
    for item in config.get("profiles") or []:
        if isinstance(item, dict) and item.get("command") == command:
            item["verified"] = True
            item["verified_at"] = _now()
    config["verified_at"] = _now()
    config["verified_command"] = command
    write_config(root, cwd, config)


def verify_running(root: Optional[Path], cwd) -> bool:
    """Called on the sweep: stamp the profile the moment its run answers."""
    proc = running(cwd)
    if not proc or not proc.alive():
        return False
    proc.probe()
    if not (proc.url and proc.healthy):
        return False
    config = read_config(root, cwd)
    if config.get("verified_command") == proc.profile.get("command"):
        return False
    note_verified(root, cwd, str(proc.profile.get("command") or ""))
    return True


# --- the two questions worth a model -----------------------------------------

def _engine(stage: str, timeout: int, engine=None):
    from . import providers as PROVIDERS
    if engine is not None:
        return engine
    return PROVIDERS.make(os.environ.get("HC_CHAT_PROVIDER", "claude"),
                          stage, None, timeout=timeout)


def _ask_model(cwd, engine=None) -> Dict[str, Any]:
    """The one call the detector cannot answer: a project whose own files say
    nothing about how it runs."""
    from . import providers as PROVIDERS
    listing = []
    try:
        for entry in sorted(Path(cwd).iterdir())[:60]:
            listing.append(entry.name + ("/" if entry.is_dir() else ""))
    except OSError:
        pass
    prompt = (
        "You are looking at a software project and answering one question:"
        " what single command runs it, so somebody can see what it does?\n\n"
        f"Directory: {cwd}\nTop level: {', '.join(listing) or '(empty)'}\n\n"
        "Read what you need to. Answer with JSON and nothing else:\n"
        '{"command": "...", "kind": "web|cli|script|test",'
        ' "name": "short label", "why": "one sentence, plain English,'
        ' what running this does", "serves": true|false}\n'
        "serves is true only if the command starts something that listens on"
        " a port. If nothing in this project can be run, answer"
        ' {"command": ""}.')
    try:
        model = _engine("synthesize", EXPLAIN_TIMEOUT_S, engine)
        raw = (model.generate_searching(prompt, where=str(cwd))
               if hasattr(model, "generate_searching")
               else model.generate_json(prompt))
    except PROVIDERS.ProviderError as exc:
        return {"ok": False, "error": " ".join(str(exc).split())[:200]}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    value = raw if isinstance(raw, dict) else _loose_json(raw)
    command = str((value or {}).get("command") or "").strip()
    if not command:
        return {"ok": False,
                "error": "nothing here looks like something that can be run"}
    kind = str(value.get("kind") or "cli")
    return {"ok": True, "profiles": [_profile(
        "model", str(value.get("name") or "Run")[:40],
        kind if kind in ("web", "cli", "script", "test") else "cli",
        command[:400], str(value.get("why") or "")[:300],
        bool(value.get("serves")), 90)]}


def _loose_json(raw) -> Dict[str, Any]:
    from .providers import _last_json_object
    if isinstance(raw, dict):
        return raw
    try:
        return _last_json_object(str(raw))
    except Exception:                                   # noqa: BLE001
        return {}


def explain_failure(cwd, command: str, lines: List[str], exit_code,
                    engine=None) -> Dict[str, Any]:
    """What to do about a run that stopped. The error is the evidence, so
    this is asked after the fact rather than guessed before it."""
    from . import providers as PROVIDERS
    tail = "\n".join([str(line) for line in (lines or [])][-40:])
    prompt = (
        "A command was run to preview a project and it did not work. Say what"
        " went wrong in one sentence a person can act on, and give the single"
        " next command to try, if there is one.\n\n"
        f"Directory: {cwd}\nCommand: {command}\nExit code: {exit_code}\n"
        f"Last output:\n{tail}\n\n"
        "JSON only:\n"
        '{"reason": "one sentence", "command": "the next command, or empty",'
        ' "why": "one sentence on why that command"}')
    try:
        model = _engine("synthesize", EXPLAIN_TIMEOUT_S, engine)
        raw = model.generate_json(prompt)
    except PROVIDERS.ProviderError as exc:
        return {"ok": False, "error": " ".join(str(exc).split())[:200]}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    value = _loose_json(raw)
    reason = str(value.get("reason") or "").strip()
    if not reason:
        return {"ok": False, "error": "no answer came back"}
    return {"ok": True,
            "reason": reason[:300],
            "command": str(value.get("command") or "").strip()[:400],
            "why": str(value.get("why") or "").strip()[:300]}


def intent_for(cwd, goal_title: str, todo_text: str, profile: Dict[str, Any],
               engine=None) -> Dict[str, Any]:
    """What to look at in the preview, for the row being worked on.

    The configurator knows how the project runs. This knows what is worth
    seeing *for this change* -- which page, what to do on it, and what should
    happen. It is asked of the TODO, not of the repository.
    """
    from . import providers as PROVIDERS
    prompt = (
        "Somebody is working on one task in a project and is about to look at"
        " the running program to check it.\n\n"
        f"Project directory: {cwd}\n"
        f"Goal: {goal_title or '(none)'}\n"
        f"Task: {todo_text}\n"
        f"The project runs with: {profile.get('command', '(unknown)')}\n\n"
        "Say where to look and what should happen. JSON only:\n"
        '{"entrypoint": "a path or route, or empty",'
        ' "scenario": ["step", "step"],'
        ' "expected": "one sentence: what should be true if this worked"}\n'
        "At most four steps, each a short imperative. If the task is not"
        " something you can see by using the program, answer"
        ' {"scenario": [], "expected": ""}.')
    try:
        model = _engine("synthesize", INTENT_TIMEOUT_S, engine)
        raw = model.generate_json(prompt)
    except PROVIDERS.ProviderError as exc:
        return {"ok": False, "error": " ".join(str(exc).split())[:200]}
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    value = _loose_json(raw)
    steps = value.get("scenario")
    steps = [str(s)[:200] for s in steps[:4]] if isinstance(steps, list) else []
    expected = str(value.get("expected") or "").strip()[:300]
    if not steps and not expected:
        return {"ok": False, "error": "nothing to look for here"}
    return {"ok": True, "entrypoint": str(value.get("entrypoint") or "")[:200],
            "scenario": steps, "expected": expected}


# --- what the TODO agent is handed, and hands back ---------------------------

def contract(goal: Dict[str, Any], rows: List[Dict[str, Any]],
             config: Dict[str, Any], intent: Optional[Dict[str, Any]] = None
             ) -> Dict[str, Any]:
    """The task contract: what a build is for, and how it will be checked.

    The build agent gets this instead of being told to work it out. It is a
    structured thing on purpose -- what comes back is a result, not a
    narrative, so the chat beside it can read what happened without reading
    the build's transcript.
    """
    profile = _primary(config)
    return {
        "goal": {"id": str(goal.get("id") or ""),
                 "title": str(goal.get("title") or "")},
        "todos": [{"id": str(r.get("id") or ""),
                   "text": str(r.get("text") or "")} for r in rows],
        "execution": {"run_profile": profile.get("id", ""),
                      "command": profile.get("command", ""),
                      "cwd": str(config.get("cwd") or "")},
        "preview": {"surface": "web" if profile.get("serves") else "terminal",
                    "entrypoint": (intent or {}).get("entrypoint", "")},
        "verification": {"expected": (intent or {}).get("expected", ""),
                         "scenario": (intent or {}).get("scenario", [])},
    }
