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

Nothing here touches the reader's own session: the build is a separate
process, and its only channel back is the stream-json it prints, read on a
thread and folded into the goal state under the chat's own lock.
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
        env = os.environ.copy()
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

    def _fold(self, text: str) -> None:
        for obj in directives(text):
            row_id = obj["id"]
            if isinstance(obj.get("question"), str):
                question = " ".join(obj["question"].split())[:100]
                _set_row(self.session_id, self.root, self.goal_id, row_id,
                         status="asking", question=question)
                self.asked = row_id
            elif obj.get("state") == "DONE":
                _set_row(self.session_id, self.root, self.goal_id, row_id,
                         status="done", question="")

    def _finish(self, code: int) -> None:
        # Rows still building when the process ends -- with no question
        # standing -- did not get done. Say so rather than leaving them
        # "building" forever. A run that stopped to ask leaves the rest
        # building: the answer resumes it.
        waiting = bool(_rows_in(self.session_id, self.root, self.goal_id, "asking"))
        if not waiting:
            for row_id in _rows_in(self.session_id, self.root, self.goal_id, "building"):
                _set_row(self.session_id, self.root, self.goal_id, row_id,
                         status="failed" if (code or self.error) else "done")
        self.record(status="waiting" if waiting else ("failed" if code else "idle"),
                    exit_code=code, error=self.error)


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
        return {"ok": False, "error": "a build is already running for this goal"}
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
        for row in items:
            if row["id"] in ids:
                row["status"] = "building"
                row["question"] = ""
        goal["updated_at"] = GM._now()
        GM.sanitize(goals)
        prompts = CS.load_prompts(session_id, root)
        prompt = compose_prompt(session_id, goals, important, prompts, goal, rows)
        if not CS.save_goals(session_id, goals, important, root):
            return {"ok": False, "error": "goal state changed; try again"}
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


def status(session_id: str, root: Optional[Path], goal_id: str) -> Dict[str, Any]:
    run = _run_for(session_id, root, goal_id)
    record = load_run(session_id, root, goal_id) or {}
    return {"ok": True, "running": bool(run and run.alive()),
            "record": record}
