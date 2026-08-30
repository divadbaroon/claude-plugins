"""Dev servers: the project's own ``npm run dev``, started from the workspace.

A goal whose work is a web interface has something the goal tree cannot show:
a page. The project it lives in already knows how to serve that page -- it has
a ``dev`` script -- so the workspace needs no bundler, no port of its own and
no opinion about frameworks. It needs to know whether that script is running,
and to start it when it is not.

What this does and what it refuses to do:

* it runs one script -- ``dev``, or ``start`` where there is no ``dev`` -- out
  of the project's own package.json, and it installs nothing. A project whose
  ``node_modules`` is missing is reported, never repaired: ``npm install``
  runs lifecycle scripts from the whole dependency tree, and the reader asked
  for a preview, not for that;
* it never guesses the address. Next moves to 3001 when 3000 is taken and says
  so on stdout, and so does every other dev server worth running, so the URL
  reported here is the one the process printed -- not the one we expected;
* it does not start a second server over one already up. A record of our own
  is adopted back after a workspace restart, and a port answering with no
  record of ours is reported as somebody else's and left alone. The reader can
  still say start anyway, and the framework picks its own free port.

The process outlives the workspace on purpose. A dev server belongs to the
project rather than to this window: closing the workspace and opening it again
finds the same server and says so. It stops when the reader stops it.

Its group, not its pid: ``npm run dev`` is a wrapper whose real server is a
grandchild, so the run is spawned into a session of its own and signalled by
process group. Killing the wrapper alone would leave the port held by an
orphan nobody can see.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import chat_state as CS
from .secure_io import atomic_write_json, secure_dir
from ..platform_compat import kill_process_tree, pid_alive

# The scripts a preview is allowed to run, best first. Nothing else in
# package.json is reachable from here: "dev" and "start" are the two names
# that mean "serve this project", and a button that could run any script
# would be a remote shell with a friendly label.
DEV_SCRIPTS = ("dev", "start")

# Dependency -> what to call it, and where that framework serves by default.
# The default is only ever a hint for "is something already on that port?";
# the address we report always comes from the process itself.
FRAMEWORKS = (
    ("next", "Next.js", 3000),
    ("nuxt", "Nuxt", 3000),
    ("@remix-run/dev", "Remix", 3000),
    ("@angular/cli", "Angular", 4200),
    ("astro", "Astro", 4321),
    ("@sveltejs/kit", "SvelteKit", 5173),
    ("react-scripts", "Create React App", 3000),
    ("vite", "Vite", 5173),
)

# Lockfile -> the package manager that wrote it, and how it runs a script.
LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm", ["pnpm", "run"]),
    ("yarn.lock", "yarn", ["yarn"]),
    ("bun.lockb", "bun", ["bun", "run"]),
    ("bun.lock", "bun", ["bun", "run"]),
    ("package-lock.json", "npm", ["npm", "run"]),
)
DEFAULT_MANAGER = ("npm", ["npm", "run"])

# The address a dev server prints, in any of the shapes they print it:
# "- Local: http://localhost:3000", "  ➜  Local:   http://127.0.0.1:5173/",
# "ready - started server on http://[::1]:3000".
_URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::(\d{2,5}))?\b")
# A port pinned in the script itself, which is where a project that does not
# want the framework default puts it: "next dev -p 4000", "--port=4000",
# "PORT=4000 next dev".
_SCRIPT_PORT_RE = re.compile(
    r"(?:^|\s)(?:-p|--port)[=\s]+(\d{2,5})\b|(?:^|\s)PORT=(\d{2,5})\b")

LOG_LIMIT = 512_000        # what the log file is allowed to grow to
LOG_TAIL = 64_000          # how much of it the workspace reads back
LOG_LINES = 300            # and how many lines of that it is shown
START_GRACE_S = 90.0       # a compile this long without an address has failed
PROBE_TIMEOUT_S = 0.35


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------- where it lives


def _dev_dir(session_id: str, root: Optional[Path]) -> Path:
    return CS.paths(session_id, root).session_dir / "dev"


def _key(cwd: str) -> str:
    """One directory, one dev server -- whichever goal asked for it.

    Two goals under the same project share a server, because the project has
    one. The name keeps the directory readable and a digest for identity, so
    two checkouts of the same repository never collide.
    """
    import hashlib
    resolved = str(Path(cwd).expanduser().resolve())
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", Path(resolved).name)[:40] or "project"
    return f"{slug}-{hashlib.sha1(resolved.encode()).hexdigest()[:12]}"


def _record_path(session_id: str, root: Optional[Path], cwd: str) -> Path:
    return _dev_dir(session_id, root) / f"{_key(cwd)}.json"


def log_path(session_id: str, root: Optional[Path], cwd: str) -> Path:
    return _dev_dir(session_id, root) / f"{_key(cwd)}.log"


def load_record(session_id: str, root: Optional[Path],
                cwd: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(
            _record_path(session_id, root, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_record(session_id: str, root: Optional[Path], cwd: str,
                 record: Dict[str, Any]) -> Dict[str, Any]:
    path = _record_path(session_id, root, cwd)
    secure_dir(path.parent, root)
    atomic_write_json(path, record)
    return record


# ------------------------------------------------------- what the project is


def _read_package(cwd: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _manager(cwd: Path, package: Dict[str, Any]):
    """Which package manager runs the script.

    The declared one wins: ``packageManager: "pnpm@9"`` is the project saying
    so outright. Otherwise the lockfile says it, and a project with neither
    gets npm, which is what a bare package.json means.
    """
    declared = str(package.get("packageManager") or "").strip()
    name = declared.split("@")[0].strip().lower()
    for _lock, manager, argv in LOCKFILES:
        if name == manager:
            return manager, list(argv)
    for lockfile, manager, argv in LOCKFILES:
        if (cwd / lockfile).is_file():
            return manager, list(argv)
    return DEFAULT_MANAGER[0], list(DEFAULT_MANAGER[1])


def _framework(package: Dict[str, Any]):
    deps = {}
    for field in ("dependencies", "devDependencies"):
        value = package.get(field)
        if isinstance(value, dict):
            deps.update(value)
    for key, label, port in FRAMEWORKS:
        if key in deps:
            return label, port
    return "", 0


def _version_key(name: str):
    parts = re.findall(r"\d+", name)
    return tuple(int(p) for p in parts[:3]) or (0,)


def _search_path() -> str:
    """PATH, plus the places node installs itself when nobody logged in.

    A workspace opened from a hook inherits whatever environment the Claude
    Code process had, and a GUI-launched one has never read a shell profile:
    npm is on disk and not on PATH. These are appended rather than prepended
    -- the reader's own PATH decides which node runs, and this only decides
    whether one is found at all.
    """
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    home = Path.home()
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
             str(home / ".volta" / "bin"), str(home / ".bun" / "bin"),
             str(home / ".local" / "bin")]
    versions = home / ".nvm" / "versions" / "node"
    try:
        installed = sorted((d for d in versions.iterdir() if (d / "bin").is_dir()),
                           key=lambda d: _version_key(d.name))
        if installed:
            extra.append(str(installed[-1] / "bin"))
    except OSError:
        pass
    for candidate in extra:
        if candidate not in parts and Path(candidate).is_dir():
            parts.append(candidate)
    return os.pathsep.join(parts)


def _which(binary: str) -> str:
    import shutil
    return shutil.which(binary, path=_search_path()) or ""


def _script_port(text: str) -> int:
    found = _SCRIPT_PORT_RE.search(str(text or ""))
    if not found:
        return 0
    port = found.group(1) or found.group(2)
    try:
        return int(port)
    except (TypeError, ValueError):
        return 0


def detect(cwd: str) -> Dict[str, Any]:
    """What this directory is, and what would serve it.

    Every refusal names what is missing, because the panel prints it: an
    answer of "cannot" that does not say why is the same as no answer.
    """
    directory = Path(str(cwd or "")).expanduser()
    if not directory.is_dir():
        return {"ok": False, "error": "that project directory no longer exists"}
    package = _read_package(directory)
    if package is None:
        return {"ok": False,
                "error": "no readable package.json here — nothing to serve"}
    scripts = package.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    script = next((s for s in DEV_SCRIPTS if str(scripts.get(s) or "").strip()), "")
    if not script:
        return {"ok": False,
                "error": "package.json has no dev or start script"}
    if not (directory / "node_modules").is_dir():
        return {"ok": False,
                "error": "dependencies are not installed — run your install "
                         "command in this project first"}
    manager, argv = _manager(directory, package)
    if not _which(manager):
        return {"ok": False,
                "error": f"{manager} is not on PATH for this workspace"}
    label, default_port = _framework(package)
    return {"ok": True,
            "cwd": str(directory.resolve()),
            "manager": manager,
            "script": script,
            "command": argv + [script],
            "framework": label,
            "port_hint": _script_port(scripts.get(script)) or default_port}


# --------------------------------------------------------------- is it alive


def _pid_alive(pid) -> bool:
    return pid_alive(pid)


def _answers(port) -> bool:
    """Whether anything at all is listening there, without speaking to it."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not 1 <= port <= 65535:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _command_of(pid) -> str:
    try:
        done = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                              capture_output=True, text=True, timeout=2.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _identity(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Confirm a recorded pid is still the server we started.

    A pid outlives its process and is handed out again, so aliveness is not
    identity -- this is the same care ``ui._server_identity`` takes before it
    signals a workspace. Three things have to agree: the pid is alive, it
    still leads the group we put it in (every run gets a session of its own,
    so the leader's group is its own pid), and what it is running still looks
    like the command we spawned. Anything less and we leave it alone: the
    cost of being wrong here is killing a stranger's process group.
    """
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    if not _pid_alive(pid):
        return None
    # Confirm the pid still leads its own process group before signalling it, so
    # a recycled pid cannot make us kill a stranger's group. Windows has no
    # process groups; there kill_process_tree is scoped to this pid's own tree.
    if hasattr(os, "getpgid"):
        try:
            if os.getpgid(int(pid)) != int(pid):
                return None
        except (OSError, TypeError, ValueError):
            return None
    command = _command_of(pid)
    manager = str(record.get("manager") or "")
    if command and manager and manager not in command and "node" not in command:
        return None
    return record


# ------------------------------------------------------------------- the run


_RUNS: Dict[str, "Server"] = {}
_RUNS_GUARD = threading.Lock()


class Server:
    """One project's dev server, and the thread that reads what it prints."""

    def __init__(self, session_id: str, root: Optional[Path], cwd: str,
                 plan: Dict[str, Any]):
        self.session_id = session_id
        self.root = root
        self.cwd = cwd
        self.plan = plan
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.url = ""
        self.started_at = 0.0

    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = _search_path()
        # A dev server that opens a browser tab from under the reader is a
        # dev server nobody asked to be interrupted by; create-react-app and
        # friends do it unless told not to.
        env["BROWSER"] = "none"
        # Colour is escape codes in a log the workspace prints as text.
        env["FORCE_COLOR"] = "0"
        env["NO_COLOR"] = "1"
        # The project's own process, not a piece of the plugin: nothing here
        # should look to it like it is running inside a captured chat.
        for name in ("CLAUDE_VAULT", "CLAUDECODE", "HC_CHAT_INFERENCE"):
            env.pop(name, None)
        return env

    def spawn(self) -> Dict[str, Any]:
        directory = _dev_dir(self.session_id, self.root)
        secure_dir(directory, self.root)
        path = log_path(self.session_id, self.root, self.cwd)
        # Each start opens its own log: the last failure's compile errors
        # under this one's first lines would read as this one's.
        path.write_text(f"$ {' '.join(self.plan['command'])}\n", encoding="utf-8")
        self.process = subprocess.Popen(
            self.plan["command"], cwd=self.cwd, env=self._env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, close_fds=True,
            **detached_popen_kwargs())
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        return self.record(status="starting", url="", error="", exit_code=None)

    def record(self, **extra) -> Dict[str, Any]:
        rec = load_record(self.session_id, self.root, self.cwd) or {}
        rec.update({
            "cwd": self.cwd,
            "manager": self.plan.get("manager"),
            "script": self.plan.get("script"),
            "framework": self.plan.get("framework"),
            "command": self.plan.get("command"),
            "port_hint": self.plan.get("port_hint"),
            "pid": self.process.pid if self.process else None,
            "updated_at": _now(),
        })
        rec.setdefault("started_at", rec["updated_at"])
        rec.update(extra)
        return _save_record(self.session_id, self.root, self.cwd, rec)

    def _note_url(self, line: str) -> None:
        found = _URL_RE.search(line)
        if not found:
            return
        port = found.group(1) or ("443" if line.startswith("https") else "80")
        # Reported on loopback whatever the process called itself: 0.0.0.0 is
        # not an address a browser should be handed, and ::1 is one some
        # browsers resolve differently than the server bound.
        self.url = f"http://127.0.0.1:{port}/"
        self.record(status="running", url=self.url, port=int(port))

    def _read(self) -> None:
        path = log_path(self.session_id, self.root, self.cwd)
        try:
            for line in self.process.stdout:  # type: ignore[union-attr]
                try:
                    with open(path, "a", encoding="utf-8") as handle:
                        handle.write(line)
                    if path.stat().st_size > LOG_LIMIT:
                        _trim(path)
                except OSError:
                    pass
                if not self.url:
                    self._note_url(line)
        except (OSError, ValueError):
            pass
        finally:
            # The pipe is ours to close: the process may outlive this read
            # (a stop signals it, and the wait below is what notices), and a
            # workspace that starts servers all afternoon must not leak a
            # descriptor for each one.
            try:
                self.process.stdout.close()   # type: ignore[union-attr]
            except (OSError, AttributeError):
                pass
        code = self.process.wait() if self.process else None
        with _RUNS_GUARD:
            if _RUNS.get(_key(self.cwd)) is self:
                _RUNS.pop(_key(self.cwd), None)
        # A server the reader stopped exits non-zero on the signal; that is
        # not a failure to report as one. Anything else that ends without
        # ever printing an address ended badly, and the log says how.
        stopped = (load_record(self.session_id, self.root, self.cwd) or {}
                   ).get("stopping")
        error = ""
        if not stopped and not self.url:
            error = "the dev server exited before it served anything"
        self.record(status="stopped", exit_code=code, error=error,
                    stopping=False, url="")


def _trim(path: Path) -> None:
    """Keep the tail. A dev server left up all afternoon writes forever."""
    try:
        data = path.read_bytes()[-(LOG_LIMIT // 2):]
        path.write_bytes(data[data.find(b"\n") + 1:])
    except OSError:
        pass


def _server_for(cwd: str) -> Optional[Server]:
    with _RUNS_GUARD:
        return _RUNS.get(_key(cwd))


# ------------------------------------------------------------- what it tells


def _tail(session_id: str, root: Optional[Path], cwd: str,
          lines: int = LOG_LINES) -> List[str]:
    path = log_path(session_id, root, cwd)
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_TAIL))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [ln.rstrip() for ln in text.splitlines()][-lines:]


def status(session_id: str, root: Optional[Path], cwd: str) -> Dict[str, Any]:
    """Where this project's dev server stands, from disk rather than memory.

    A workspace restarted since the server started has no ``Server`` object
    for it, and the reader would be shown a Start button for something that
    is already up. So the record is what answers, and the process behind it
    is confirmed before it is believed.
    """
    plan = detect(cwd)
    out: Dict[str, Any] = {"ok": True, "cwd": str(cwd or ""),
                           "status": "stopped", "url": "", "pid": None,
                           "can_start": bool(plan.get("ok"))}
    if plan.get("ok"):
        out.update({"manager": plan["manager"], "script": plan["script"],
                    "framework": plan["framework"],
                    "command": " ".join(plan["command"]),
                    "port_hint": plan["port_hint"]})
    else:
        out["error"] = plan.get("error", "")
        return out

    record = load_record(session_id, root, cwd) or {}
    live = _server_for(cwd)
    if live and live.alive():
        running = True
    else:
        running = _identity(record) is not None and \
            str(record.get("status")) in ("starting", "running")
    if running:
        url = str((live.url if live else "") or record.get("url") or "")
        started = record.get("started_at") or ""
        if url and _answers(_port_of(url)):
            out.update({"status": "running", "url": url,
                        "pid": record.get("pid"), "started_at": started})
        else:
            # Up, but with nothing to show yet: still compiling, or wedged.
            # The second is worth saying rather than spinning forever.
            waited = time.time() - (live.started_at if live else 0.0)
            out.update({"status": "starting", "pid": record.get("pid"),
                        "started_at": started})
            if live and waited > START_GRACE_S:
                out["error"] = ("no address after "
                                f"{int(waited)}s — see the log")
        out["last"] = _tail(session_id, root, cwd, 3)
        return out

    # Not ours, and not running. Something else may still hold the port this
    # project would want, which is worth saying before a second one starts
    # up beside it on a port the reader is not looking at.
    out["exit_code"] = record.get("exit_code")
    if record.get("error"):
        out["error"] = record["error"]
    out["last"] = _tail(session_id, root, cwd, 3)
    hint = plan.get("port_hint") or 0
    if hint and _answers(hint):
        out["status"] = "in_use"
        out["other_url"] = f"http://127.0.0.1:{int(hint)}/"
    return out


def _port_of(url: str) -> int:
    found = re.search(r":(\d{2,5})", str(url or ""))
    return int(found.group(1)) if found else 0


def start(session_id: str, root: Optional[Path], cwd: str,
          force: bool = False) -> Dict[str, Any]:
    """Run the project's dev script, unless something already is.

    ``force`` is the reader answering the one question this cannot answer for
    them: the port their project usually takes is busy, and only they know
    whether that is their own server in another terminal or an unrelated one.
    Forced, the framework picks a free port and reports the one it got.
    """
    current = status(session_id, root, cwd)
    if not current.get("can_start"):
        return {"ok": False, "error": current.get("error", "nothing to serve"),
                **current}
    if current["status"] in ("running", "starting"):
        return {"ok": True, "started": False, **current}
    if current["status"] == "in_use" and not force:
        return {"ok": True, "started": False, **current}
    plan = detect(cwd)
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error", "")}
    server = Server(session_id, root, plan["cwd"], plan)
    with _RUNS_GUARD:
        existing = _RUNS.get(_key(cwd))
        if existing and existing.alive():
            return {"ok": True, "started": False, **status(session_id, root, cwd)}
        _RUNS[_key(cwd)] = server
    try:
        server.spawn()
    except (OSError, ValueError) as exc:
        with _RUNS_GUARD:
            _RUNS.pop(_key(cwd), None)
        return {"ok": False,
                "error": f"{plan['manager']} would not start: {str(exc)[:160]}"}
    # Its pid and its group are recorded before anything is awaited: a
    # workspace that dies in the next second must still be able to find this
    # process again rather than leave it holding a port anonymously.
    return {"ok": True, "started": True, **status(session_id, root, cwd)}


def stop(session_id: str, root: Optional[Path], cwd: str,
         timeout: float = 8.0) -> Dict[str, Any]:
    """Stop the server we started -- the whole group of it.

    Signalled by group because ``npm run dev`` is a wrapper: the thing holding
    the port is its child, and terminating the wrapper alone leaves an orphan
    serving a stale build on a port nothing can now release.
    """
    record = load_record(session_id, root, cwd) or {}
    live = _server_for(cwd)
    pid = (live.process.pid if (live and live.process) else record.get("pid"))
    if not _pid_alive(pid) or (live is None and _identity(record) is None):
        _save_record(session_id, root, cwd,
                     {**record, "status": "stopped", "url": "",
                      "stopping": False, "updated_at": _now()})
        return {"ok": True, "stopped": False, **status(session_id, root, cwd)}
    # Recorded before the signal, so the reader's stop is not read back as a
    # crash when the process ends non-zero on SIGTERM, as it should.
    _save_record(session_id, root, cwd, {**record, "stopping": True})
    try:
        kill_process_tree(int(pid))
    except (TypeError, ValueError):
        return {"ok": False, "error": "the dev server could not be signalled",
                **status(session_id, root, cwd)}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.05)
    else:
        kill_process_tree(int(pid), force=True)
    with _RUNS_GUARD:
        if _RUNS.get(_key(cwd)) is live:
            _RUNS.pop(_key(cwd), None)
    _save_record(session_id, root, cwd,
                 {**(load_record(session_id, root, cwd) or record),
                  "status": "stopped", "url": "", "stopping": False,
                  "updated_at": _now()})
    return {"ok": True, "stopped": True, **status(session_id, root, cwd)}


def log(session_id: str, root: Optional[Path], cwd: str) -> Dict[str, Any]:
    """The whole tail, for the reader who opened the log.

    Kept apart from ``status`` for the reason the build log is: the panel
    polls state every second and a half, and the compile output of a Next
    project is not something to send back on every one of those.
    """
    return {"ok": True, "cwd": str(cwd or ""),
            "lines": _tail(session_id, root, cwd),
            "status": status(session_id, root, cwd)}
