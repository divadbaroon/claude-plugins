"""Build runs: a headless Claude Code session that implements picked TODO rows.

The workspace rail's Build button hands the picked rows of one goal to a fresh
``claude -p`` process running in the chat's own project directory. The prompt
it opens on is the whole goal tree this plugin injects into the chat, then the
picked rows -- each under an id the reader never sees -- and a small protocol:

* to ask about a row, print ``{"id": "...", "question": "..."}`` (under 100
  characters) and stop; the row shows the question in the rail, the reader
  answers there, and the answer goes back into the same session as
  ``{"id": "...", "answer": "..."}``;
* when a row is finished, print ``{"id": "...", "state": "DONE"}``.

Everything else the process prints is a log. Rows are ``building`` from the
moment they are submitted, ``asking`` while a question stands, ``done`` on the
DONE marker, and ``failed`` if the process ends with none of that.

Two ways the work reaches Claude, chosen by ``HC_BUILD_MODE``:

* ``headless`` (the default): a separate ``claude -p`` process in the chat's
  directory, its stream-json read on a thread. Runs the moment Build is
  pressed, in any workspace; isolated, no shared context.
* ``session``: the build is handed to the CONNECTED session --
  the one this workspace is a view of -- through the plugin's own hooks. Build
  writes the prompt to a queue in the session directory; the next hook that
  fires delivers it: the Stop hook answers ``{"decision": "block", "reason":
  <prompt>}``, which Claude Code takes as the next instruction the moment the
  current turn ends, and UserPromptSubmit carries it as context alongside the
  user's next message if the session was idle. Rows are ``queued`` until one
  of those happens, then ``building``. Claude's answers -- the protocol lines
  above -- are read back out of the session transcript at the same hooks.
  Nothing is typed into a window and no second process runs: it is the
  reader's own session, with everything it already knows -- but only once
  the hooks run a runtime that has this code.

Both fold their results into the goal state under the chat's own lock.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import chat_state as CS
from . import goals as GM
from .secure_io import atomic_write_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- the prompt

PROTOCOL = """\
# How to work

You are implementing the TODO rows listed under "The work" for the FOCUS goal,
in this repository, autonomously. Work through them in order. Keep diffs
small; run the project's tests where they exist.

Each row carries an id in square brackets. The ids are for the protocol below;
never mention them to the user in prose.

Protocol -- print these as bare JSON objects on their own line, nothing else on
the line, exactly in these shapes:

- If a row is ambiguous and you cannot proceed without the user, print
  {"id": "<row id>", "question": "<under 100 characters>"}
  and then STOP your turn immediately. Do not continue with other rows in that
  turn. The user's answer arrives as the next message, in the shape
  {"id": "<row id>", "answer": "<their words>"}. Continue from there.
- When a row is finished, print
  {"id": "<row id>", "state": "DONE"}
  and continue with the next row.

Ask only when a real fork exists. Do not restate the rows back; do the work.
"""


def compose_prompt(session_id: str, goals: Dict[str, Any],
                   important: Dict[str, Any], prompts: List[Dict[str, Any]],
                   goal: Dict[str, Any], rows: List[Dict[str, Any]]) -> str:
    """The build session's opening message.

    The goal tree the plugin already injects into the chat, whole; the FOCUS
    goal named; the reader's own prompt for it if they wrote one; then the
    picked rows with their children, each parent under its id.
    """
    tree = CS._goal_context_text(session_id, goals, important, prompts)
    title = " ".join(str(goal.get("title") or "Untitled").split())
    lines = [tree.rstrip("\n"), "",
             "# FOCUS goal", "",
             f"{goal['id']} · {title}",
             "Work only on this goal's rows below; the tree above is orientation."]
    own = str(goal.get("prompt_md") or "").strip()
    if own:
        lines += ["", "# The user's own prompt for this goal", "", own]
    lines += ["", "# The work", ""]
    for row in rows:
        indent = "  " * int(row.get("depth") or 0)
        marker = f" [{row['id']}]" if row.get("_picked") else ""
        lines.append(f"{indent}- {row.get('text', '')}{marker}")
    lines += ["", PROTOCOL.rstrip("\n")]
    return "\n".join(lines) + "\n"


def picked_with_children(items: List[Dict[str, Any]],
                         ids: List[str]) -> List[Dict[str, Any]]:
    """The picked rows, and every row nested under a picked one, in order.

    A picked parent's children ride along without ids of their own: the parent
    is the unit of work and of the protocol. A picked child under an unpicked
    parent is its own unit.
    """
    wanted = set(ids)
    out: List[Dict[str, Any]] = []
    under: Optional[int] = None
    for row in items:
        depth = int(row.get("depth") or 0)
        if under is not None and depth > under:
            out.append(dict(row, _picked=False))
            continue
        under = None
        if row.get("id") in wanted:
            out.append(dict(row, _picked=True))
            under = depth
    return out


# -------------------------------------------------------------- the runner

_DIRECTIVE = re.compile(r"\{[^{}]*\"id\"\s*:\s*\"t[0-9a-z]+\"[^{}]*\}")


def mode() -> str:
    """How builds reach Claude: a headless process (the default -- it runs the
    moment Build is pressed, in any workspace), or the connected session
    through its hooks (HC_BUILD_MODE=session: needs the plugin runtime that
    carries this code to be the one the hooks run)."""
    value = os.environ.get("HC_BUILD_MODE", "headless").strip().lower()
    return "session" if value == "session" else "headless"


# ------------------------------------------------ the connected session's queue

def _queue_path(session_id: str, root: Optional[Path]) -> Path:
    return _builds_dir(session_id, root) / "queue.json"


def _load_queue(session_id: str, root: Optional[Path]) -> List[Dict[str, Any]]:
    try:
        value = json.loads(_queue_path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = value.get("items") if isinstance(value, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _save_queue(session_id: str, root: Optional[Path],
                items: List[Dict[str, Any]]) -> None:
    path = _queue_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"items": items})


def enqueue(session_id: str, root: Optional[Path], item: Dict[str, Any]) -> None:
    items = _load_queue(session_id, root)
    items.append(dict(item, created_at=_now()))
    _save_queue(session_id, root, items)


def pending(session_id: str, root: Optional[Path]) -> List[Dict[str, Any]]:
    return _load_queue(session_id, root)


def deliver(session_id: str, root: Optional[Path], event: str) -> str:
    """Take everything queued and word it for the hook that will carry it.

    The rows a taken build names go from queued to building here -- this is
    the moment the work actually reaches Claude. Returns "" when nothing
    waits, so the hook says nothing.
    """
    items = _load_queue(session_id, root)
    if not items:
        return ""
    _save_queue(session_id, root, [])
    parts: List[str] = []
    for item in items:
        kind = item.get("kind")
        if kind == "build":
            for row_id in item.get("row_ids") or []:
                _set_row(session_id, root, str(item.get("goal_id") or ""),
                         row_id, status="building", question="")
            parts.append(str(item.get("prompt") or ""))
        elif kind == "answer":
            _set_row(session_id, root, str(item.get("goal_id") or ""),
                     str(item.get("row_id") or ""), status="building",
                     question="")
            parts.append("The user answered your question, in the shape the "
                         "protocol asked for:\n"
                         + json.dumps({"id": item.get("row_id"),
                                       "answer": item.get("text")}))
    body = "\n\n".join(p for p in parts if p.strip())
    if not body:
        return ""
    if event == "UserPromptSubmit":
        head = ("[Engelbart] The user pressed Build in the goals workspace. "
                "This is an instruction from the user, in addition to their "
                "message below: after answering their message, do the "
                "following.\n\n")
    else:
        head = ("[Engelbart] The user pressed Build in the goals workspace. "
                "Continue with the following.\n\n")
    return head + body


# ------------------------------------------------ reading the session back

def _scan_path(session_id: str, root: Optional[Path]) -> Path:
    return _builds_dir(session_id, root) / "scan.json"


def _set_row_any(session_id: str, root: Optional[Path], row_id: str,
                 **fields) -> bool:
    """A directive names only a row; find its goal."""
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        hit = None
        for goal in goals.get("goals", []):
            for row in goal.get("todo_items") or []:
                if row.get("id") == row_id:
                    row.update(fields)
                    hit = goal
        if hit is None:
            return False
        hit["updated_at"] = GM._now()
        GM.sanitize(goals)
        return CS.save_goals(session_id, goals, important, root)


def scan_transcript(session_id: str, root: Optional[Path],
                    transcript_path: Optional[str]) -> int:
    """Read the session transcript from where the last scan stopped and fold
    every protocol line Claude printed into the rows. Returns how many."""
    if not transcript_path:
        return 0
    path = Path(str(transcript_path))
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    marker = _scan_path(session_id, root)
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    offset = int(state.get("offset") or 0) if state.get("path") == str(path) else 0
    if offset > size:
        offset = 0
    applied = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return 0
    # Only whole lines: a line still being written is read next time.
    cut = chunk.rfind("\n")
    if cut < 0:
        return 0
    lines, consumed = chunk[:cut].split("\n"), cut + 1
    for line in lines:
        line = line.strip()
        # Inside a transcript line the protocol's quotes are escaped, so
        # look for the bare key; the JSON parse below is the real test.
        if not line or "assistant" not in line or "id" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        text = _stream_text(event)
        for obj in directives(text):
            if isinstance(obj.get("question"), str):
                question = " ".join(obj["question"].split())[:100]
                if _set_row_any(session_id, root, obj["id"],
                                status="asking", question=question):
                    applied += 1
            elif obj.get("state") == "DONE":
                if _set_row_any(session_id, root, obj["id"],
                                status="done", question=""):
                    applied += 1
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(marker, {"path": str(path), "offset": offset + consumed})
    return applied


# ------------------------------------------------ is the session still there

def _alive_path(session_id: str, root: Optional[Path]) -> Path:
    return _builds_dir(session_id, root) / "session.json"


def note_hook(session_id: str, root: Optional[Path], event: str) -> None:
    """Every hook is proof of life; SessionEnd is the one that says goodbye."""
    path = _alive_path(session_id, root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    now = _now()
    state["last_hook_at"] = now
    state["last_event"] = event
    if event == "SessionEnd":
        state["ended_at"] = now
    elif event == "SessionStart" or state.get("ended_at"):
        # Resumed, or any sign of life after an end: the session is back.
        state["ended_at"] = None
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state)


def session_state(session_id: str, root: Optional[Path]) -> Dict[str, Any]:
    try:
        state = json.loads(_alive_path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    return {"last_hook_at": state.get("last_hook_at"),
            "ended_at": state.get("ended_at"),
            "queued": len(_load_queue(session_id, root)),
            "mode": mode()}


def reopen(session_id: str, root: Optional[Path]) -> Dict[str, Any]:
    """Open a terminal that resumes the connected session: claude -r <id>."""
    from . import agent_exec as AE
    cwd = _cwd_for(session_id, root)
    session_dir = CS.paths(session_id, root).session_dir
    try:
        script = AE.write_launch_script(
            session_dir, "reopen", cwd, ["claude", "-r", session_id], send=True)
        app = AE.open_terminal(script)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:200],
                "command": f"cd {cwd} && claude -r {session_id}"}
    return {"ok": True, "terminal": app, "cwd": cwd,
            "command": f"cd {cwd} && claude -r {session_id}"}

_RUNS: Dict[str, "Run"] = {}
_RUNS_GUARD = threading.Lock()


def _builds_dir(session_id: str, root: Optional[Path]) -> Path:
    return CS.paths(session_id, root).session_dir / "builds"


def _run_path(session_id: str, root: Optional[Path], goal_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", goal_id)[:60]
    return _builds_dir(session_id, root) / f"{safe}.json"


def load_run(session_id: str, root: Optional[Path], goal_id: str) -> Optional[Dict[str, Any]]:
    path = _run_path(session_id, root, goal_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_run(session_id: str, root: Optional[Path], record: Dict[str, Any]) -> None:
    path = _run_path(session_id, root, record["goal_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)


def directives(text: str) -> List[Dict[str, Any]]:
    """Every protocol object in a piece of assistant text, in order."""
    out = []
    for match in _DIRECTIVE.finditer(text or ""):
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            continue
        if isinstance(obj.get("question"), str) or obj.get("state") == "DONE":
            out.append(obj)
    return out


def _stream_text(event: Dict[str, Any]) -> str:
    """The text an assistant stream-json event carries, if any."""
    if event.get("type") == "assistant":
        message = event.get("message") or {}
        parts = message.get("content") or []
        return "".join(str(p.get("text") or "") for p in parts
                       if isinstance(p, dict) and p.get("type") == "text")
    if event.get("type") == "result":
        return str(event.get("result") or "")
    return ""


def _set_row(session_id: str, root: Optional[Path], goal_id: str,
             row_id: str, **fields) -> bool:
    """Write build state onto one row, under the chat's lock."""
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        goal = GM.by_id(goals, goal_id)
        if not goal:
            return False
        hit = False
        for row in goal.get("todo_items") or []:
            if row.get("id") == row_id:
                row.update(fields)
                hit = True
        if not hit:
            return False
        goal["updated_at"] = GM._now()
        GM.sanitize(goals)
        return CS.save_goals(session_id, goals, important, root)


def _rows_in(session_id: str, root: Optional[Path], goal_id: str,
             status: str) -> List[str]:
    goals, _ = CS.load_goals(session_id, root)
    goal = GM.by_id(goals, goal_id)
    return [row["id"] for row in (goal or {}).get("todo_items") or []
            if row.get("status") == status]


# A run that died on the provider's side, not on the work: retried by
# resuming the same session, so nothing already done is lost.
_TRANSIENT = re.compile(
    r"(?i)(api error: 5\d\d|overloaded|rate.?limit|internal server error"
    r"|connection (reset|refused|error)|timed? ?out)")
RETRY_LIMIT = 2
RETRY_DELAY_S = 8.0


class Run:
    """One goal's build process, and the thread that reads what it prints."""

    def __init__(self, session_id: str, root: Optional[Path], goal_id: str,
                 cwd: str, claude_session: str):
        self.session_id = session_id
        self.root = root
        self.goal_id = goal_id
        self.cwd = cwd
        self.claude_session = claude_session
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.asked: Optional[str] = None
        self.error = ""
        self.retries = 0
        # Rows the reader pulled back while this run was out: whatever the
        # process still says about them is not folded in, and a stop the
        # reader asked for is recorded as that, not as a failure.
        self.cancelled: set = set()
        self.stopped = False

    def record(self, **extra) -> Dict[str, Any]:
        rec = load_run(self.session_id, self.root, self.goal_id) or {}
        rec.update({
            "goal_id": self.goal_id,
            "claude_session_id": self.claude_session,
            "cwd": self.cwd,
            "pid": self.process.pid if self.process else None,
            "updated_at": _now(),
        })
        rec.setdefault("started_at", rec.get("updated_at"))
        rec.update(extra)
        _save_run(self.session_id, self.root, rec)
        return rec

    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def _command(self, message: str, resume: bool) -> List[str]:
        command = ["claude", "-p", message, "--output-format", "stream-json",
                   "--verbose", "--permission-mode",
                   os.environ.get("HC_BUILD_PERMISSION_MODE", "acceptEdits")]
        if resume:
            command += ["--resume", self.claude_session]
        else:
            command += ["--session-id", self.claude_session]
        model = os.environ.get("HC_BUILD_MODEL")
        if model:
            command += ["--model", model]
        return command

    def spawn(self, message: str, resume: bool) -> None:
        from .providers import subscription_env
        # On the reader's subscription, not an API key the server happened
        # to inherit (see providers.subscription_env).
        env = subscription_env()
        # A build is not the reader's chat: keep the always-on hooks from
        # treating it as one, and from nesting another analyzer inside it.
        env["HC_CHAT_INFERENCE"] = "1"
        env.pop("CLAUDE_VAULT", None)
        env.pop("CLAUDECODE", None)
        log_dir = _builds_dir(self.session_id, self.root)
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / f"{self.goal_id}.log", "a", encoding="utf-8")
        self.process = subprocess.Popen(
            self._command(message, resume), cwd=self.cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
            text=True, close_fds=True, start_new_session=True)
        self.asked = None
        self.record(status="running", last_message=message[-400:])
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "result" and event.get("is_error"):
                    self.error = str(event.get("result") or "build failed")[:300]
                self._fold(_stream_text(event))
        finally:
            code = self.process.wait()
            self._finish(code)

    def stop(self) -> bool:
        """End the process: the reader pulled back everything it was doing.
        The reader thread sees the exit and finishes the run as cancelled."""
        if not self.alive():
            return False
        self.stopped = True
        assert self.process
        try:
            os.killpg(os.getpgid(self.process.pid), 15)
        except (OSError, ProcessLookupError):
            try:
                self.process.terminate()
            except OSError:
                return False
        return True

    def _fold(self, text: str) -> None:
        for obj in directives(text):
            row_id = obj["id"]
            if row_id in self.cancelled:
                continue
            if isinstance(obj.get("question"), str):
                question = " ".join(obj["question"].split())[:100]
                _set_row(self.session_id, self.root, self.goal_id, row_id,
                         status="asking", question=question)
                self.asked = row_id
            elif obj.get("state") == "DONE":
                _set_row(self.session_id, self.root, self.goal_id, row_id,
                         status="done", question="")

    def _finish(self, code: int) -> None:
        # A provider-side death (a 500, an overload, a dropped connection)
        # is not a verdict on the rows: resume the same session and keep
        # going, up to the retry limit, with rows left as building.
        if (not self.stopped and (code or self.error)
                and self.retries < RETRY_LIMIT
                and _TRANSIENT.search(self.error or "")
                and _rows_in(self.session_id, self.root, self.goal_id,
                             "building")):
            self.retries += 1
            self.error = ""
            self.record(status="retrying", retry=self.retries)
            import time as _time
            _time.sleep(RETRY_DELAY_S * self.retries)
            try:
                self.spawn("The previous turn ended on a transient API "
                           "error. Continue working through the rows; "
                           "re-print any protocol lines that did not land.",
                           resume=True)
                return
            except (OSError, RuntimeError):
                pass  # fall through to the honest failure below
        # Rows still building when the process ends -- with no question
        # standing -- did not get done. Say so rather than leaving them
        # "building" forever. A run that stopped to ask leaves the rest
        # building: the answer resumes it.
        waiting = bool(_rows_in(self.session_id, self.root, self.goal_id, "asking"))
        if not waiting:
            for row_id in _rows_in(self.session_id, self.root, self.goal_id, "building"):
                _set_row(self.session_id, self.root, self.goal_id, row_id,
                         status="failed" if (code or self.error) else "done")
        self.record(status="waiting" if waiting
                    else "cancelled" if self.stopped
                    else ("failed" if code else "idle"),
                    exit_code=code, error=self.error)
        # Rows that queued up behind this run go out now -- unless the run
        # stopped on a question, whose answer resumes this same session
        # first; they stay queued and leave with the resumed run's finish.
        if not waiting:
            held = _pop_later(self.session_id, self.root, self.goal_id)
            if held:
                start(self.session_id, self.root, self.goal_id, held)


# A headless run takes one process per goal, and that process reads nothing
# once started -- so rows picked while one is out wait their turn rather than
# being turned away. They are marked "queued", remembered here, and started
# the moment the running build ends.

def _later_path(session_id: str, root: Optional[Path]) -> Path:
    return _builds_dir(session_id, root) / "later.json"


def _load_later(session_id: str, root: Optional[Path]) -> Dict[str, List[str]]:
    try:
        value = json.loads(_later_path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _push_later(session_id: str, root: Optional[Path], goal_id: str,
                ids: List[str]) -> None:
    later = _load_later(session_id, root)
    held = [i for i in later.get(goal_id) or [] if isinstance(i, str)]
    later[goal_id] = held + [i for i in ids if i not in held]
    path = _later_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, later)


def _pop_later(session_id: str, root: Optional[Path], goal_id: str) -> List[str]:
    later = _load_later(session_id, root)
    ids = [i for i in later.pop(goal_id, []) if isinstance(i, str)]
    path = _later_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, later)
    return ids


def _cwd_for(session_id: str, root: Optional[Path]) -> str:
    manifest = CS.load_manifest(session_id, root)
    cwd = str(manifest.get("cwd") or "").strip()
    if cwd and Path(cwd).is_dir():
        return cwd
    return os.getcwd()


def _run_for(session_id: str, root: Optional[Path], goal_id: str) -> Optional[Run]:
    with _RUNS_GUARD:
        return _RUNS.get(f"{session_id}:{goal_id}")


def start(session_id: str, root: Optional[Path], goal_id: str,
          row_ids: List[str]) -> Dict[str, Any]:
    """Submit rows: mark them building, compose the prompt, spawn the run."""
    ids = [i for i in row_ids if isinstance(i, str)]
    if not ids:
        return {"ok": False, "error": "pick at least one TODO"}
    live = _run_for(session_id, root, goal_id)
    if live and live.alive():
        # The running process cannot take more work; these rows go next.
        with CS.session_lock(session_id, root, wait_s=5):
            goals, important = CS.load_goals(session_id, root)
            goal = GM.by_id(goals, goal_id)
            if not goal:
                return {"ok": False, "error": "goal not found"}
            known = {row["id"] for row in goal.get("todo_items") or []}
            ids = [i for i in ids if i in known]
            if not ids:
                return {"ok": False, "error": "those TODOs are not on this goal"}
            for row in goal.get("todo_items") or []:
                if row["id"] in ids:
                    row["status"] = "queued"
                    row["question"] = ""
            goal["updated_at"] = GM._now()
            GM.sanitize(goals)
            if not CS.save_goals(session_id, goals, important, root):
                return {"ok": False, "error": "goal state changed; try again"}
        _push_later(session_id, root, goal_id, ids)
        return {"ok": True, "queued": True, "after_run": True, "rows": ids}
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        goal = GM.by_id(goals, goal_id)
        if not goal:
            return {"ok": False, "error": "goal not found"}
        items = goal.get("todo_items") or []
        known = {row["id"] for row in items}
        ids = [i for i in ids if i in known]
        if not ids:
            return {"ok": False, "error": "those TODOs are not on this goal"}
        rows = picked_with_children(items, ids)
        first = "queued" if mode() == "session" else "building"
        for row in items:
            if row["id"] in ids:
                row["status"] = first
                row["question"] = ""
        # Rows handed to Claude are work begun on the goal.
        if goal.get("status") == "active":
            goal["status"] = "in_progress"
        goal["updated_at"] = GM._now()
        GM.sanitize(goals)
        prompts = CS.load_prompts(session_id, root)
        prompt = compose_prompt(session_id, goals, important, prompts, goal, rows)
        if not CS.save_goals(session_id, goals, important, root):
            return {"ok": False, "error": "goal state changed; try again"}
    if mode() == "session":
        enqueue(session_id, root, {"kind": "build", "goal_id": goal_id,
                                   "row_ids": ids, "prompt": prompt})
        return {"ok": True, "queued": True, "mode": "session", "rows": ids,
                "prompt": prompt}
    run = Run(session_id, root, goal_id, _cwd_for(session_id, root),
              str(uuid.uuid4()))
    try:
        run.spawn(prompt, resume=False)
    except FileNotFoundError:
        _revert(session_id, root, goal_id, ids)
        return {"ok": False, "error": "claude CLI not found on PATH"}
    except OSError as exc:
        _revert(session_id, root, goal_id, ids)
        return {"ok": False, "error": str(exc)[:200]}
    with _RUNS_GUARD:
        _RUNS[f"{session_id}:{goal_id}"] = run
    return {"ok": True, "started": True, "rows": ids,
            "claude_session_id": run.claude_session, "cwd": run.cwd,
            "prompt": prompt}


def _revert(session_id, root, goal_id, ids):
    for row_id in ids:
        _set_row(session_id, root, goal_id, row_id, status="", question="")


def answer(session_id: str, root: Optional[Path], goal_id: str,
           row_id: str, text: str) -> Dict[str, Any]:
    """The reader's answer to a standing question: back into the same session."""
    text = " ".join(str(text or "").split())
    if not text:
        return {"ok": False, "error": "write an answer first"}
    if mode() == "session":
        if not _set_row(session_id, root, goal_id, row_id, status="queued",
                        question=""):
            return {"ok": False, "error": "that TODO is not on this goal"}
        enqueue(session_id, root, {"kind": "answer", "goal_id": goal_id,
                                   "row_id": row_id, "text": text})
        return {"ok": True, "queued": True, "mode": "session", "row": row_id}
    run = _run_for(session_id, root, goal_id)
    record = load_run(session_id, root, goal_id)
    claude_session = (run.claude_session if run else
                      (record or {}).get("claude_session_id"))
    if not claude_session:
        return {"ok": False, "error": "no build session to answer into"}
    if run and run.alive():
        return {"ok": False, "error": "the build is still running"}
    if not _set_row(session_id, root, goal_id, row_id, status="building",
                    question=""):
        return {"ok": False, "error": "that TODO is not on this goal"}
    if run is None:
        run = Run(session_id, root, goal_id,
                  str((record or {}).get("cwd") or _cwd_for(session_id, root)),
                  claude_session)
        with _RUNS_GUARD:
            _RUNS[f"{session_id}:{goal_id}"] = run
    message = json.dumps({"id": row_id, "answer": text})
    try:
        run.spawn(message, resume=True)
    except (FileNotFoundError, OSError) as exc:
        _set_row(session_id, root, goal_id, row_id, status="asking",
                 question="(could not resume the build: %s)" % str(exc)[:60])
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "resumed": True, "row": row_id}


# ------------------------------------------------------------ pulling back
#
# The reader can take a row back from the build at any point short of done:
# queued, building, asking, or failed, it returns to the active band with no
# status. What "stop building it" means depends on where the row was:
#
# * waiting behind a headless run (later.json): it is dropped from the wait;
# * queued for the connected session (queue.json): it leaves the queued
#   build -- the prompt is recomposed around the rows that remain, and the
#   build itself is dropped when none do;
# * out with a live headless process: the process cannot be told to skip a
#   row, so the row's later verdicts are ignored and the process is ended
#   only when nothing it was doing is still wanted;
# * a question the reader withdraws rather than answers, with other rows of
#   the same run still building: the session is resumed and told to move on.

CANCELLABLE = ("queued", "building", "asking", "failed")


def _drop_later(session_id: str, root: Optional[Path], goal_id: str,
                ids: List[str]) -> None:
    later = _load_later(session_id, root)
    if goal_id not in later:
        return
    held = [i for i in later.get(goal_id) or []
            if isinstance(i, str) and i not in ids]
    if held:
        later[goal_id] = held
    else:
        later.pop(goal_id, None)
    path = _later_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, later)


def _recompose(session_id: str, root: Optional[Path], goal_id: str,
               ids: List[str]) -> str:
    goals, important = CS.load_goals(session_id, root)
    goal = GM.by_id(goals, goal_id)
    if not goal:
        return ""
    rows = picked_with_children(goal.get("todo_items") or [], ids)
    return compose_prompt(session_id, goals, important,
                          CS.load_prompts(session_id, root), goal, rows)


def _drop_queued(session_id: str, root: Optional[Path], goal_id: str,
                 ids: List[str]) -> None:
    items = _load_queue(session_id, root)
    if not items:
        return
    kept: List[Dict[str, Any]] = []
    changed = False
    for item in items:
        if str(item.get("goal_id") or "") != goal_id:
            kept.append(item)
            continue
        kind = item.get("kind")
        if kind == "answer":
            if str(item.get("row_id") or "") in ids:
                changed = True
                continue
            kept.append(item)
        elif kind == "build":
            before = [r for r in item.get("row_ids") or [] if isinstance(r, str)]
            after = [r for r in before if r not in ids]
            if len(after) == len(before):
                kept.append(item)
                continue
            changed = True
            if after:
                kept.append(dict(item, row_ids=after,
                                 prompt=_recompose(session_id, root, goal_id, after)
                                 or item.get("prompt")))
        else:
            kept.append(item)
    if changed:
        _save_queue(session_id, root, kept)


def cancel(session_id: str, root: Optional[Path], goal_id: str,
           row_ids: List[str]) -> Dict[str, Any]:
    """Take rows back from the build: they return to active, unbuilt."""
    ids = [i for i in row_ids if isinstance(i, str)]
    if not ids:
        return {"ok": False, "error": "name at least one TODO"}
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        goal = GM.by_id(goals, goal_id)
        if not goal:
            return {"ok": False, "error": "goal not found"}
        items = goal.get("todo_items") or []
        known = {row["id"] for row in items}
        ids = [i for i in ids if i in known]
        if not ids:
            return {"ok": False, "error": "those TODOs are not on this goal"}
        were: Dict[str, str] = {}
        for row in items:
            if row["id"] in ids and row.get("status") in CANCELLABLE:
                were[row["id"]] = str(row.get("status"))
                row["status"] = ""
                row["question"] = ""
        if not were:
            return {"ok": True, "cancelled": []}
        goal["updated_at"] = GM._now()
        GM.sanitize(goals)
        if not CS.save_goals(session_id, goals, important, root):
            return {"ok": False, "error": "goal state changed; try again"}
    cancelled = list(were)
    _drop_later(session_id, root, goal_id, cancelled)
    _drop_queued(session_id, root, goal_id, cancelled)
    out: Dict[str, Any] = {"ok": True, "cancelled": cancelled}
    if mode() == "session":
        return out
    run = _run_for(session_id, root, goal_id)
    record = load_run(session_id, root, goal_id)
    if run is None and not record:
        return out
    if run is not None:
        run.cancelled.update(cancelled)
    still = (_rows_in(session_id, root, goal_id, "building")
             + _rows_in(session_id, root, goal_id, "asking"))
    if run is not None and run.alive():
        if not still:
            out["stopped"] = run.stop()
        return out
    # No process: the run ended, on a question if anything is still building.
    withdrew = [i for i in cancelled if were[i] == "asking"]
    if withdrew and still:
        claude_session = (run.claude_session if run else
                          (record or {}).get("claude_session_id"))
        if not claude_session:
            return out
        if run is None:
            run = Run(session_id, root, goal_id,
                      str((record or {}).get("cwd") or _cwd_for(session_id, root)),
                      claude_session)
            run.cancelled.update(cancelled)
            with _RUNS_GUARD:
                _RUNS[f"{session_id}:{goal_id}"] = run
        message = ("The user withdrew these rows from the build: "
                   + ", ".join(withdrew)
                   + ". Do not work on them and print no protocol lines "
                     "for them. Continue with the remaining rows.")
        try:
            run.spawn(message, resume=True)
            out["resumed"] = True
        except (FileNotFoundError, OSError) as exc:
            out["error"] = str(exc)[:200]
    elif not still and (run is not None or (record or {}).get("status") == "waiting"):
        # Nothing of this run is wanted any more: rows waiting behind it go.
        held = _pop_later(session_id, root, goal_id)
        if held:
            start(session_id, root, goal_id, held)
    return out


def status(session_id: str, root: Optional[Path], goal_id: str) -> Dict[str, Any]:
    run = _run_for(session_id, root, goal_id)
    record = load_run(session_id, root, goal_id) or {}
    return {"ok": True, "running": bool(run and run.alive()),
            "record": record}
