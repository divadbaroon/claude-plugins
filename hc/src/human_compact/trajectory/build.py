"""Build runs: a headless Claude Code session that implements picked TODO rows.

The workspace rail's Build button hands the picked rows of one goal to a fresh
``claude -p`` process running in the chat's own project directory. The prompt
it opens on is the whole goal tree this plugin injects into the chat, then the
picked rows -- each under an id the reader never sees -- and a small protocol:

* to ask about a row, print ``{"id": "...", "question": "..."}`` (under 100
  characters) and stop; the row shows the question in the rail, the reader
  answers there, and the answer goes back into the same session as
  ``{"id": "...", "answer": "..."}``;
* when a row is finished, print ``{"id": "...", "state": "DONE"}``;
* before starting on the rows, print ``{"estimate": {"tokens": N, "minutes":
  N}}`` -- what the whole build will spend and how long it will take. The
  rail's watch line says "calculating" until that line lands, then counts down
  from it. It is asked of the build session itself, which already holds the
  context, rather than of a second process handed a copy of it. Builds do
  not always say (one in four did, and one of those took three minutes to),
  and what a build says before its first tool call can reach the stream as
  a thinking block rather than text -- so the estimate is read out of both,
  and after ESTIMATE_GRACE_S with no word the rail is given a stand-in from
  this chat's measured runs (``cost``) until the build's own arrives.

Two things reach a running build from the reader, both through the same
door: the process is ended and its session resumed on a message, since a
``claude -p`` process reads no input once started (``Run.redirect``).

* a row deleted from the build while it is being worked on: the session is
  told which rows are gone -- print nothing for them, not even their state
  -- and reminded of the rows still its own;
* a note typed under a building row (Enter on the row opens the pane): the
  session is told what the reader added, and carries on.

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

A headless run also says what it is doing while it does it -- each tool call
and the first line of what it says between them -- so that "building" is not
the whole of what the reader gets to know. The rail reads the last line back
with the state, opens the rest on request, and can put the run in a terminal
window: following its log while it runs, resuming its session once it stops.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

- FIRST, as the opening line of your very first reply -- before you read,
  search or run anything -- print your estimate for the whole build (every
  row under "The work" together) as
  {"estimate": {"tokens": <integer>, "minutes": <integer>}}
  where tokens is what you expect this session to spend in total (everything
  it reads, writes and re-reads, cache included) and minutes is how long the
  work will take. Size it from the rows and the tree alone; a rough number
  now is worth more than an exact one later, and the rail shows nothing until
  it lands. Put it in the same reply as your first tool call, as plain reply
  text, not inside a code block. If your estimate changes materially as you
  work, print it again with the new numbers.
- If a row is ambiguous and you cannot proceed without the user, print
  {"id": "<row id>", "question": "<under 100 characters>"}
  and then STOP your turn immediately. Do not continue with other rows in that
  turn. The user's answer arrives as the next message, in the shape
  {"id": "<row id>", "answer": "<their words>"}. Continue from there.
- When a row is finished, print
  {"id": "<row id>", "state": "DONE"}
  and continue with the next row.
- The user may reopen a row you already finished, in the shape
  {"id": "<row id>", "reopened": "<what was wrong with your work>"}.
  Your work on that row did not satisfy them. Fix what they name -- do not
  redo the row from nothing -- and print its DONE marker again.

Ask only when a real fork exists. Do not restate the rows back; do the work.
"""


def project_lines(session_id: str, root: Optional[Path]) -> List[str]:
    """What the reader has written about the project the build runs in.

    The goal tree names goals, not the place they are for: a session opening
    on it alone knows the work and not the repository. Everything here is
    already in the workspace -- the directory the chat's manifest recorded and
    the project record kept beside it -- so nothing is invented when nothing
    was written, and a chat with no directory on record contributes nothing.
    """
    from . import project_store as PS
    cwd = str(CS.load_manifest(session_id, root).get("cwd") or "").strip()
    if not cwd:
        return []
    record = PS.load_project(root, cwd)
    name = str(record.get("name") or "").strip() or Path(cwd).name
    lines = ["# Project", "", f"{name} · {cwd}"]
    objective = " ".join(str(record.get("objective") or "").split())
    if objective:
        lines.append(f"Objective: {objective}")
    description = str(record.get("description") or "").strip()
    if description and description != objective:
        lines += ["", description]
    # Pointers away from the project, as a short index: the same six the goal
    # tree allows itself, and for the same reason.
    for source in GM.normalize_sources(record.get("sources"))[:6]:
        lines.append(f"- SOURCE ({source['type']}): {source['label']}")
    return lines


def compose_prompt(session_id: str, goals: Dict[str, Any],
                   important: Dict[str, Any], prompts: List[Dict[str, Any]],
                   goal: Dict[str, Any], rows: List[Dict[str, Any]],
                   root: Optional[Path] = None) -> str:
    """The build session's opening message.

    The project it runs in; the goal tree the plugin already injects into the
    chat, whole; the FOCUS goal named; the reader's own prompt for it if they
    wrote one; then the picked rows with their children, each parent under its
    id. This is also what the rail's Prompt tab prints, so that what the
    reader is shown and what the build opens on are one string.
    """
    tree = CS._goal_context_text(session_id, goals, important, prompts)
    title = " ".join(str(goal.get("title") or "Untitled").split())
    head = project_lines(session_id, root)
    lines = ([] if not head else head + [""]) + [
             tree.rstrip("\n"), "",
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
        # A row the reader reopened carries every earlier run's verdict and
        # what they said was wrong with it. The work is to fix THAT, not to
        # do the row again from nothing.
        for n, past in enumerate(GM.normalize_history(row.get("history")), 1):
            note = " ".join(str(past.get("note") or "").split())
            lines.append(f"{indent}  (run {n} ended {past['state']}; the user"
                         f" reopened it: \"{note}\")")
    # Screenshots pasted into the rows going out: each "[attachment #N]" a
    # row cites, resolved to the file it names, so the session can open it.
    shots = GM.render_attachments(rows).rstrip("\n")
    if shots:
        lines += ["", "# Attachments", "",
                  "Files the rows above cite; read each one for the row"
                  " that names it.", shots]
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


def preview(session_id: str, root: Optional[Path], goals: Dict[str, Any],
            important: Dict[str, Any], goal: Dict[str, Any],
            ids: List[str]) -> str:
    """The prompt a build of these rows would open on, composed and not sent.

    The whole of what the rail's Prompt tab prints, so what a build carries
    is read before it goes rather than only after. With nothing picked it
    previews the rows a Build still has to do, since that is the build the
    reader is about to ask for.
    """
    items = [row for row in (goal.get("todo_items") or [])
             if str(row.get("text") or "").strip()]
    picked = [i for i in ids if isinstance(i, str)]
    if not picked:
        picked = [str(row.get("id") or "") for row in items
                  if str(row.get("status") or "") not in ("done", "building")]
    rows = picked_with_children(items, picked)
    return compose_prompt(session_id, goals, important,
                          CS.load_prompts(session_id, root), goal, rows,
                          root=root)


def preview_context_tokens(session_id: str, root: Optional[Path],
                           goals: Dict[str, Any], important: Dict[str, Any],
                           goal: Dict[str, Any]) -> int:
    """What this goal's build opens on before a single row is added to it.

    The same string as `preview`, composed with no rows: the project, the goal
    tree, the FOCUS heading, the reader's own prompt and the protocol. The rail
    prices each row against this, so a row's corner and the Prompt tab's own
    count are measurements of one string rather than two guesses about it --
    `cost`'s context_tokens is the goal-context file alone and reads low beside
    the tab.
    """
    text = compose_prompt(session_id, goals, important,
                          CS.load_prompts(session_id, root), goal, [],
                          root=root)
    return len(text) // CHARS_PER_TOKEN


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
        elif kind == "reopen":
            _set_row(session_id, root, str(item.get("goal_id") or ""),
                     str(item.get("row_id") or ""), status="building",
                     question="")
            parts.append(
                "The user reopened a row you had already marked DONE: your"
                " work on it did not satisfy them. Read what they said, fix"
                " it, and print the DONE marker again when it is right.\n"
                + json.dumps({"id": item.get("row_id"),
                              "reopened": item.get("text")}))
        elif kind == "withdraw":
            parts.append(withdraw_message(
                session_id, root, str(item.get("goal_id") or ""),
                [r for r in item.get("row_ids") or [] if isinstance(r, str)]))
        elif kind == "note":
            parts.append(note_message(
                session_id, root, str(item.get("goal_id") or ""),
                str(item.get("row_id") or ""), str(item.get("text") or "")))
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


def _safe_goal(goal_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(goal_id or ""))[:60]


def _run_path(session_id: str, root: Optional[Path], goal_id: str) -> Path:
    return _builds_dir(session_id, root) / f"{_safe_goal(goal_id)}.json"


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


# ------------------------------------------------------ what the run is doing
#
# A row that says "building" and nothing else is a build the reader has to take
# on faith. So a run says what it is doing while it does it: every tool call it
# makes and the first line of what it says between them, one short phrase each,
# newest last. It is written twice, for the two ways a reader watches work:
#
# * a bounded JSON list the workspace reads back -- the rail prints the last
#   line under the rows and the whole list when the reader opens the log;
# * a plain-text file a terminal can follow, for the reader who would rather
#   watch it in the window they already work in (see ``watch``).
#
# Neither carries file contents or more than the first line of the model's
# prose: this is the shape of the work, not the work.

ACTIVITY_KEEP = 200
WATCH_LOG_LIMIT = 512_000


def _activity_path(session_id: str, root: Optional[Path], goal_id: str) -> Path:
    return _builds_dir(session_id, root) / f"{_safe_goal(goal_id)}.activity.json"


def watch_log(session_id: str, root: Optional[Path], goal_id: str) -> Path:
    return _builds_dir(session_id, root) / f"{_safe_goal(goal_id)}.watch.log"


def load_activity(session_id: str, root: Optional[Path],
                  goal_id: str) -> List[Dict[str, Any]]:
    try:
        value = json.loads(_activity_path(session_id, root, goal_id)
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    lines = value.get("lines") if isinstance(value, dict) else None
    return [l for l in lines if isinstance(l, dict)] if isinstance(lines, list) else []


def note_activity(session_id: str, root: Optional[Path], goal_id: str,
                  kind: str, text: str) -> bool:
    """One line onto this goal's build log, in both copies. Bounded."""
    text = " ".join(str(text or "").split())[:200]
    if not text:
        return False
    lines = load_activity(session_id, root, goal_id)
    if lines and lines[-1].get("kind") == kind and lines[-1].get("text") == text:
        return False
    at = _now()
    lines.append({"at": at, "kind": kind, "text": text})
    path = _activity_path(session_id, root, goal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"lines": lines[-ACTIVITY_KEEP:]})
    try:
        with watch_log(session_id, root, goal_id).open("a", encoding="utf-8") as fh:
            fh.write(f"{at[11:19]}  {text}\n")
    except OSError:
        pass
    return True


def _open_watch_log(session_id: str, root: Optional[Path], goal_id: str) -> None:
    """A fresh build starts the followable log again once it has grown large:
    the terminal is watching this build, and scrolling past a megabyte of the
    last one to reach it is not watching."""
    path = watch_log(session_id, root, goal_id)
    try:
        if path.stat().st_size > WATCH_LOG_LIMIT:
            path.write_text("", encoding="utf-8")
    except OSError:
        pass


# A protocol line is not something to report as work: the rail already shows
# what it did to the row.
_JSON_LINE = re.compile(r"^\s*\{.*\}\s*$")


def _said(text: str) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or _JSON_LINE.match(line):
            continue
        return line
    return ""


def _tool_phrase(name: str, args: Dict[str, Any]) -> str:
    """One phrase for a tool call -- paths and commands, never contents."""
    from . import agent_exec as AE
    phrase = AE.describe_call({"tool_name": name, "tool_input": args})
    if phrase:
        return phrase
    name = " ".join(str(name or "").split())[:40]
    return ("used " + name) if name else ""


def stream_activity(event: Dict[str, Any]) -> List[Tuple[str, str]]:
    """What one stream-json event is worth saying, as (kind, phrase) pairs."""
    out: List[Tuple[str, str]] = []
    if event.get("type") != "assistant":
        return out
    message = event.get("message") or {}
    for part in message.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            said = _said(part.get("text"))
            if said:
                out.append(("say", said))
        elif part.get("type") == "tool_use":
            args = part.get("input")
            phrase = _tool_phrase(str(part.get("name") or ""),
                                  args if isinstance(args, dict) else {})
            if phrase:
                out.append(("tool", phrase))
    return out


# ------------------------------------------------------ what a build costs
#
# The rail prints a number beside every row it has not built yet: roughly what
# building that row will spend. It is made of two parts, and only one of them
# is a guess.
#
# * The context is exact. Every build opens on the goal tree this plugin keeps
#   for the chat, and that text is already on disk (goal_context.md). Its
#   characters over four is the usual rule of thumb for tokens; nothing here
#   can do better, since only the model's own tokenizer knows.
# * The work -- what Claude then reads, writes, and re-reads to do the row --
#   cannot be known before it happens. So it is measured rather than assumed:
#   a headless run reports its own usage when it ends, and the last few runs
#   of this chat are kept below. The median run, per row it built, is what the
#   next row is estimated at. Until a chat has built anything there is nothing
#   to measure and DEFAULT_ROW_TOKENS stands in -- which is why the rail marks
#   the number "~" and says where it came from.
#
# Nothing is sent anywhere: the samples are this chat's own runs, in its own
# session directory.

USAGE_KEEP = 20
DEFAULT_ROW_TOKENS = 30000
DEFAULT_ROW_CHARS = 80
CHARS_PER_TOKEN = 4
# How long a row takes is measured the same way, from the same samples: the
# median seconds one row of this chat's finished runs took. Until a chat has
# built anything this stands in, and the rail says the number is a guess.
DEFAULT_ROW_SECONDS = 240


def _usage_tokens(event: Dict[str, Any]) -> int:
    """Everything one stream-json ``result`` event says it spent.

    Cache reads and writes count: they are billed, and on a build they are
    most of the traffic. A CLI that reports no usage contributes nothing
    rather than a zero -- the caller only records positive totals.
    """
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens"):
        try:
            value = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        total += max(0, value)
    return total


def _usage_path(session_id: str, root: Optional[Path]) -> Path:
    return _builds_dir(session_id, root) / "usage.json"


def _load_usage(session_id: str, root: Optional[Path]) -> List[Dict[str, Any]]:
    try:
        value = json.loads(_usage_path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    runs = value.get("runs") if isinstance(value, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def record_usage(session_id: str, root: Optional[Path], rows: int,
                 chars: int, tokens: int, seconds: float = 0.0) -> None:
    """One finished run: how many rows it built, how long they were, what it
    spent, and how long it took. Only the last USAGE_KEEP are kept; older runs
    say less about how this chat works now."""
    if rows <= 0 or tokens <= 0:
        return
    runs = _load_usage(session_id, root)
    runs.append({"rows": int(rows), "chars": max(0, int(chars)),
                 "tokens": int(tokens), "seconds": int(max(0.0, seconds)),
                 "at": _now()})
    path = _usage_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"runs": runs[-USAGE_KEEP:]})


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def cost(session_id: str, root: Optional[Path]) -> Dict[str, Any]:
    """What the rail needs to price a row: the context every build carries,
    and what a row of work has cost in this chat so far."""
    try:
        context_chars = CS.paths(session_id, root).goal_context.stat().st_size
    except OSError:
        context_chars = 0
    runs = _load_usage(session_id, root)
    per_row = [r["tokens"] / r["rows"] for r in runs
               if isinstance(r.get("tokens"), int) and r["tokens"] > 0
               and isinstance(r.get("rows"), int) and r["rows"] > 0]
    chars = [r["chars"] / r["rows"] for r in runs
             if isinstance(r.get("chars"), int) and r["chars"] > 0
             and isinstance(r.get("rows"), int) and r["rows"] > 0]
    secs = [r["seconds"] / r["rows"] for r in runs
            if isinstance(r.get("seconds"), int) and r["seconds"] > 0
            and isinstance(r.get("rows"), int) and r["rows"] > 0]
    return {
        "context_tokens": int(context_chars // CHARS_PER_TOKEN),
        "row_tokens": int(_median(per_row)) if per_row else DEFAULT_ROW_TOKENS,
        "row_chars": int(_median(chars)) if chars else DEFAULT_ROW_CHARS,
        "samples": len(per_row),
        "row_seconds": int(_median(secs)) if secs else DEFAULT_ROW_SECONDS,
        "time_samples": len(secs),
    }


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


# The build's own word on what it will cost: one object with one nested
# object, so the row directive's pattern (which bars braces inside) is not
# the one to find it with.
_ESTIMATE = re.compile(r"\{\s*\"estimate\"\s*:\s*\{[^{}]*\}\s*\}")


_ESTIMATE_NUMBER = re.compile(r"^\s*([0-9][0-9,_]*(?:\.[0-9]+)?)\s*([kKmM])?\s*$")


def _estimate_number(value: Any) -> Optional[int]:
    """A count as a build writes it: a number, or a string like "600k",
    "1.2M" or "600,000". None for anything else (a boolean included)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    match = _ESTIMATE_NUMBER.match(str(value or ""))
    if not match:
        return None
    number = float(match.group(1).replace(",", "").replace("_", ""))
    scale = {"k": 1_000, "m": 1_000_000}.get((match.group(2) or "").lower(), 1)
    return max(0, int(number * scale))


def estimate_in(text: str) -> Optional[Dict[str, int]]:
    """The last estimate in a piece of assistant text, or None.

    ``{"tokens": N, "minutes": N}`` with both whole and non-negative; a line
    that names only one of them still counts, with the other at zero. The
    last one wins: a build that re-estimates as it goes means the later
    number. A number written as a string -- "600k", "1.2M", "600,000" -- is
    read as the number it names, since a build that wrote it that way meant
    it, and the line it sits on is otherwise lost.
    """
    found: Optional[Dict[str, int]] = None
    for match in _ESTIMATE.finditer(text or ""):
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        inner = obj.get("estimate") if isinstance(obj, dict) else None
        if not isinstance(inner, dict):
            continue
        out: Dict[str, int] = {}
        for key in ("tokens", "minutes"):
            number = _estimate_number(inner.get(key))
            if number is not None:
                out[key] = number
        if out:
            found = {"tokens": out.get("tokens", 0),
                     "minutes": out.get("minutes", 0)}
    return found


# How long the rail waits on a running build's own estimate before it is
# handed a stand-in: what this chat's finished runs have cost per row
# (``cost``), or the defaults where nothing has been measured yet. Seconds,
# not minutes -- a line that says "calculating" for longer than that is a
# line that has stopped meaning anything. The build's own word, when it
# comes, replaces the stand-in.
ESTIMATE_GRACE_S = 15


def fallback_estimate(session_id: str, root: Optional[Path],
                      rows: int) -> Dict[str, Any]:
    """What a build of ``rows`` rows is likely to cost, from what this chat's
    earlier builds cost -- the stand-in for a build that has not said."""
    priced = cost(session_id, root)
    rows = max(1, int(rows or 1))
    tokens = int(priced["context_tokens"]) + rows * int(priced["row_tokens"])
    minutes = max(1, -(-rows * int(priced["row_seconds"]) // 60))
    return {"tokens": tokens, "minutes": minutes,
            "source": "measured" if priced["time_samples"] else "default"}


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


def _stream_thinking(event: Dict[str, Any]) -> str:
    """What an assistant event thought rather than said. Read for the
    estimate only: what a build writes before its first tool call has been
    seen to reach the stream as a thinking block, and the number in it is
    the number it meant. Row directives are never taken from here -- a
    build thinking "DONE" is not a build saying it."""
    if event.get("type") != "assistant":
        return ""
    message = event.get("message") or {}
    parts = message.get("content") or []
    return "".join(str(p.get("thinking") or "") for p in parts
                   if isinstance(p, dict) and p.get("type") == "thinking")


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
# How old a build's process must be before a word for it (Run.redirect) ends
# it: younger, and claude has not written the session a resume needs.
REDIRECT_MIN_AGE_S = 4.0


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
        # What this build is, for the cost record: the rows it was asked for
        # and how much text they carried, then what the CLI says it spent.
        # Tokens accumulate across a resume -- the question and its answer are
        # one build -- and are banked once, when the run is finally done.
        self.picked: List[str] = []
        self.picked_chars = 0
        self.tokens = 0
        # And how long it took, the same way: the clock runs while a process
        # of this build is up, across a resume, and is banked with the tokens.
        # It is what the rail's "about N left" is made of.
        self.seconds = 0.0
        self.spawned_at = 0.0
        # A message the reader sent a running build -- a row deleted from
        # under it, a note added to one -- waits here while the process is
        # ended, and the session is resumed on it the moment it has.
        self.redirect_with = ""

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
        # The model and effort the reader chose in Settings, or the shell's
        # word where they chose nothing (HC_BUILD_MODEL, HC_BUILD_EFFORT).
        chosen = load_settings(self.session_id, self.root)
        model = chosen.get("model") or os.environ.get("HC_BUILD_MODEL", "")
        if model:
            command += ["--model", model]
        effort = chosen.get("effort") or os.environ.get("HC_BUILD_EFFORT", "")
        if effort in EFFORTS:
            command += ["--effort", effort]
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
        if not resume:
            _open_watch_log(self.session_id, self.root, self.goal_id)
        self.process = subprocess.Popen(
            self._command(message, resume), cwd=self.cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
            text=True, close_fds=True, start_new_session=True)
        self.asked = None
        self.spawned_at = time.time()
        # The clock the rail shows starts again with every turn of the build,
        # a resume included: what it is timing is the work happening now, and
        # an hour spent waiting for the reader's answer is not that. How many
        # rows are out is a fresh run's to say -- a resume may be a Run that
        # only knows the session, not what was picked into it.
        fresh: Dict[str, Any] = {"started_at": _now()}
        if not resume:
            fresh["rows"] = len(self.picked) or 1
            # A new build has not said what it will cost yet; the last
            # one's word must not stand in for it.
            fresh["estimate"] = None
        self.record(status="running", last_message=message[-400:], **fresh)
        rows = len(self.picked) or 1
        self._say("start", "carrying on" if resume else
                  "started on %d row%s" % (rows, "" if rows == 1 else "s"))
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _say(self, kind: str, text: str) -> None:
        note_activity(self.session_id, self.root, self.goal_id, kind, text)

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
                if event.get("type") == "result":
                    if event.get("is_error"):
                        self.error = str(event.get("result") or "build failed")[:300]
                    self.tokens += _usage_tokens(event)
                for kind, phrase in stream_activity(event):
                    self._say(kind, phrase)
                self._fold(_stream_text(event))
                self._estimate(_stream_thinking(event))
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

    def redirect(self, message: str) -> bool:
        """Tell a running build something: end the process and resume its
        session on ``message``. The process reads no input once started, so
        this is the one way a word reaches it mid-work; what the session had
        done stays done, since the resume picks its transcript back up. The
        reader thread does the resuming, in _finish, once the exit lands.
        False when there is no live process to tell."""
        if not self.alive():
            return False
        # A process only just started has not written its session yet, and
        # a resume of a session that is not there fails outright: a word
        # that comes in the first seconds waits for them to pass.
        young = REDIRECT_MIN_AGE_S - (time.time() - self.spawned_at)
        if young > 0:
            time.sleep(young)
            if not self.alive():
                return False
        self.redirect_with = message
        if not self.stop():
            self.redirect_with = ""
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
        self._estimate(text)

    def _estimate(self, text: str) -> None:
        # The build's estimate of itself goes on the run record, where the
        # rail reads its countdown from; and into the log, so what the line
        # said is in plain words beside everything else the build did.
        estimate = estimate_in(text)
        if estimate:
            self.record(estimate=dict(estimate, at=_now(), source="build"))
            self._say("say", "estimated about %d min and %s tokens for the build"
                      % (estimate["minutes"], f"{estimate['tokens']:,}"))

    def _bank(self) -> None:
        """Bank what this build spent: one sample for the estimate, and -- when
        the build was one row and one row only -- the real number onto that
        row, where the rail prints it in place of its guess."""
        spent, self.tokens = self.tokens, 0
        took, self.seconds = self.seconds, 0.0
        # A build the reader cut short spent what it spent, but it says
        # nothing about what a row costs: it is not a sample of one.
        if spent <= 0 or not self.picked or self.stopped or self.cancelled:
            return
        record_usage(self.session_id, self.root, len(self.picked),
                     self.picked_chars, spent, took)
        if len(self.picked) == 1:
            _set_row(self.session_id, self.root, self.goal_id,
                     self.picked[0], tokens=spent)

    def _finish(self, code: int) -> None:
        if self.spawned_at:
            self.seconds += max(0.0, time.time() - self.spawned_at)
            self.spawned_at = 0.0
        # Ended to be told something (see redirect): not a verdict on the
        # rows, which stay building; the session goes on from where its
        # transcript stopped, with the reader's word as its next message.
        if self.redirect_with:
            message, self.redirect_with = self.redirect_with, ""
            self.stopped = False
            self.error = ""
            self.record(status="redirecting")
            try:
                self.spawn(message, resume=True)
                return
            except (OSError, RuntimeError) as exc:
                self.error = "could not resume the build: %s" % str(exc)[:120]
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
            self._say("end", "the API dropped the turn; picking it back up")
            time.sleep(RETRY_DELAY_S * self.retries)
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
        spent = {"tokens": self.tokens} if self.tokens else {}
        ended = ("waiting" if waiting else "cancelled" if self.stopped
                 else ("failed" if code else "idle"))
        self.record(status=ended, exit_code=code, error=self.error, **spent)
        self._say("end", {"waiting": "waiting on your answer",
                          "cancelled": "you stopped the build",
                          "failed": "the build stopped: "
                                    + (self.error or "no reason given"),
                          "idle": "the build finished"}[ended])
        # A run that stopped on a question is not over: its answer resumes
        # this same session and spends more. The cost is banked when nothing
        # is left waiting, and the counter starts again from there.
        if not waiting:
            self._bank()
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


def _goal_cwd(goals, goal_id) -> str:
    """The directory a goal's work belongs in, inherited from above it.

    A goal made under another project carries that project's directory; a
    subgoal of it carries none of its own and belongs to the same place, so
    the answer is the nearest one on the way up. Empty means the ordinary
    case: the project this chat was started in.
    """
    seen = set()
    at = str(goal_id or "")
    while at and at not in seen:
        seen.add(at)
        goal = GM.by_id(goals, at)
        if not goal:
            return ""
        here = str(goal.get("project_cwd") or "").strip()
        if here:
            return here
        at = str(goal.get("parent_goal_id") or "")
    return ""


def _cwd_for(session_id: str, root: Optional[Path], goals=None,
             goal_id: str = "") -> str:
    """Where a build runs.

    The chat's own directory, unless the goal says otherwise: a project
    made in the workspace has a directory of its own, and work on its goals
    belongs there rather than wherever this chat happens to have started.
    A directory that has gone away is not used -- a build in a path that no
    longer exists fails in a way nobody can read.
    """
    if goals is not None and goal_id:
        wanted = _goal_cwd(goals, goal_id)
        if wanted and Path(wanted).expanduser().is_dir():
            return str(Path(wanted).expanduser())
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
        prompt = compose_prompt(session_id, goals, important, prompts, goal,
                                rows, root=root)
        if not CS.save_goals(session_id, goals, important, root):
            return {"ok": False, "error": "goal state changed; try again"}
    if mode() == "session":
        enqueue(session_id, root, {"kind": "build", "goal_id": goal_id,
                                   "row_ids": ids, "prompt": prompt})
        return {"ok": True, "queued": True, "mode": "session", "rows": ids,
                "prompt": prompt}
    run = Run(session_id, root, goal_id,
              _cwd_for(session_id, root, goals, goal_id),
              str(uuid.uuid4()))
    # What this build is, for the cost it will report when it ends: the rows
    # picked, and the text they and their children carry.
    run.picked = list(ids)
    run.picked_chars = sum(len(str(row.get("text") or "")) for row in rows)
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
    # What the reader told the build while it waited on this question -- a
    # row deleted, a note added -- goes ahead of the answer, in the same
    # message: the answer is what resumes the session, so it is the ride.
    held = _pop_pending(session_id, root, goal_id)
    if held:
        message = "\n\n".join(held + [
            "And the user answered your question, in the shape the protocol"
            " asked for:\n" + message])
    try:
        run.spawn(message, resume=True)
    except (FileNotFoundError, OSError) as exc:
        _set_row(session_id, root, goal_id, row_id, status="asking",
                 question="(could not resume the build: %s)" % str(exc)[:60])
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "resumed": True, "row": row_id}


# ------------------------------------------ a word for a build mid-work
#
# A ``claude -p`` process reads nothing once it has started, so what the
# reader sends a running build goes in through Run.redirect: the process is
# ended and its session resumed on the message, with everything the session
# had done still in its transcript. Two things are sent this way.
#
# Deleting a row the build is working on (the corner, or Escape): the build
# is told which rows are gone, that it is to print nothing for them -- not
# even their state, since they are no longer on the list -- and which rows
# are still its own, so "do the others" is a list and not a memory test.
#
# A note typed under a building row (Enter on the row opens the pane, the
# same one Claude's own questions use): the build is told what the reader
# added and carries on. Not a new row, and not a reopen: the row is out
# already, and this is the reader talking to the session about it.
#
# When no process is up because the run stopped on another row's question,
# the word waits on the run record and rides in with the answer.


def _row_lines(goal: Dict[str, Any], ids: List[str]) -> List[str]:
    """The rows named, as the prompt writes them: '- text [id]', a picked
    row's children nested under it without ids of their own."""
    rows = picked_with_children(goal.get("todo_items") or [], ids)
    lines: List[str] = []
    for row in rows:
        indent = "  " * int(row.get("depth") or 0)
        marker = f" [{row['id']}]" if row.get("_picked") else ""
        lines.append(f"{indent}- {row.get('text', '')}{marker}")
    return lines


def _goal_for_message(session_id: str, root: Optional[Path],
                      goal_id: str) -> Dict[str, Any]:
    goals, _ = CS.load_goals(session_id, root)
    return GM.by_id(goals, goal_id) or {"todo_items": []}


def withdraw_message(session_id: str, root: Optional[Path], goal_id: str,
                     gone: List[str]) -> str:
    """What a build is told when rows are deleted from under it."""
    goal = _goal_for_message(session_id, root, goal_id)
    items = goal.get("todo_items") or []
    kept = [row["id"] for row in items
            if row.get("id") not in gone
            and str(row.get("status") or "") in ("building", "asking", "queued")]
    lines = ["[Engelbart] The user deleted these TODO rows from the build"
             " while you were working on them:", ""]
    lines += _row_lines(goal, gone) or ["- (rows no longer on the list)"]
    lines += ["",
              "Do not work on them, and print no protocol lines for them --"
              " no DONE, no question, nothing that names their ids. They are"
              " gone from the list, and their state is not yours to report.",
              ""]
    if kept:
        lines += ["The rows still yours, in order:", ""]
        lines += _row_lines(goal, kept)
        lines += ["", "Continue with those, same protocol as your opening"
                      " message: print {\"id\": \"<row id>\", \"state\":"
                      " \"DONE\"} when each is finished."]
    else:
        lines += ["Nothing else was picked for this build: stop here."]
    return "\n".join(lines)


def note_message(session_id: str, root: Optional[Path], goal_id: str,
                 row_id: str, text: str) -> str:
    """What a build is told when the reader adds context to a row it is on."""
    goal = _goal_for_message(session_id, root, goal_id)
    text = " ".join(str(text or "").split())
    lines = ["[Engelbart] The user added context to a TODO row you are"
             " working on, from the goals workspace:", ""]
    lines += _row_lines(goal, [row_id]) or [f"- [{row_id}]"]
    lines += [f"  ↳ \"{text}\"", "",
              "Take it into account for that row. Nothing else changes: the"
              " same rows are yours, in the same order, under the same"
              " protocol as your opening message. Continue."]
    return "\n".join(lines)


def _push_pending(session_id: str, root: Optional[Path], goal_id: str,
                  message: str) -> None:
    record = load_run(session_id, root, goal_id) or {"goal_id": goal_id}
    held = [m for m in record.get("pending") or [] if isinstance(m, str)]
    record["pending"] = held + [message]
    _save_run(session_id, root, record)


def _pop_pending(session_id: str, root: Optional[Path],
                 goal_id: str) -> List[str]:
    record = load_run(session_id, root, goal_id)
    if not record:
        return []
    held = [m for m in record.get("pending") or [] if isinstance(m, str)]
    if held:
        record["pending"] = []
        _save_run(session_id, root, record)
    return held


NOTABLE = ("building",)


def note(session_id: str, root: Optional[Path], goal_id: str, row_id: str,
         text: str) -> Dict[str, Any]:
    """The reader's note on a row the build is working on: into the build's
    session, now if a process is up, with the next answer if the run is
    waiting on a question."""
    text = " ".join(str(text or "").split())
    if not text:
        return {"ok": False, "error": "write the note first"}
    goals, _ = CS.load_goals(session_id, root)
    goal = GM.by_id(goals, goal_id)
    if not goal:
        return {"ok": False, "error": "goal not found"}
    row = next((r for r in goal.get("todo_items") or []
                if r.get("id") == row_id), None)
    if row is None:
        return {"ok": False, "error": "that TODO is not on this goal"}
    status = str(row.get("status") or "")
    session_mode = mode() == "session"
    if status not in NOTABLE and not (session_mode and status == "queued"):
        return {"ok": False,
                "error": "only a TODO the build is working on can take a note"}
    label = " ".join(str(row.get("text") or "").split())[:60]
    note_activity(session_id, root, goal_id, "say",
                  "you added to “%s”: %s" % (label, text))
    if session_mode:
        enqueue(session_id, root, {"kind": "note", "goal_id": goal_id,
                                   "row_id": row_id, "text": text})
        return {"ok": True, "queued": True, "mode": "session", "row": row_id}
    message = note_message(session_id, root, goal_id, row_id, text)
    run = _run_for(session_id, root, goal_id)
    if run is not None and run.alive():
        if run.redirect(message):
            return {"ok": True, "redirected": True, "row": row_id}
    record = load_run(session_id, root, goal_id) or {}
    if record.get("status") == "waiting" or (run is not None and run.alive()):
        # Stopped on another row's question: the answer carries the note.
        _push_pending(session_id, root, goal_id, message)
        return {"ok": True, "pending": True, "row": row_id}
    return {"ok": False, "error": "the build is not running"}


# ---------------------------------------------------------------- reopening
#
# A row comes back done and the reader disagrees: "no, you did a bad job".
# Reopening is not a second Build of the same row -- it is the same row, on
# its next run, told what was wrong with the last one. The run that ended
# keeps its verdict and the reader's note in the row's `history`, which is
# what the rail stacks under the row and what compose_prompt reads back out.
#
# Where the note goes depends on what is still around: the session that did
# run 1 is resumed with it where it can be, since it is the only thing that
# knows what it actually wrote; failing that a fresh run opens on a prompt
# carrying the whole history.

REOPENABLE = ("done",)


def reopen(session_id: str, root: Optional[Path], goal_id: str,
           row_id: str, note: str) -> Dict[str, Any]:
    """Send a finished row back out with what the reader says went wrong."""
    note = " ".join(str(note or "").split())
    if not note:
        return {"ok": False, "error": "say what went wrong first"}
    live = _run_for(session_id, root, goal_id)
    if live and live.alive():
        return {"ok": False, "error": "the build is still running"}
    session_mode = mode() == "session"
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        goal = GM.by_id(goals, goal_id)
        if not goal:
            return {"ok": False, "error": "goal not found"}
        items = goal.get("todo_items") or []
        row = next((r for r in items if r.get("id") == row_id), None)
        if row is None:
            return {"ok": False, "error": "that TODO is not on this goal"}
        if row.get("status") not in REOPENABLE:
            return {"ok": False, "error": "only a finished TODO can be reopened"}
        history = GM.normalize_history(row.get("history"))
        history.append({"state": row["status"], "note": note})
        row["history"] = history[-GM.MAX_HISTORY:]
        row["status"] = "queued" if session_mode else "building"
        row["question"] = ""
        # A goal called finished on this row's account is working again.
        if goal.get("status") in ("active", "completed"):
            goal["status"] = "in_progress"
        goal["updated_at"] = GM._now()
        GM.sanitize(goals)
        prompts = CS.load_prompts(session_id, root)
        rows = picked_with_children(items, [row_id])
        prompt = compose_prompt(session_id, goals, important, prompts, goal, rows)
        if not CS.save_goals(session_id, goals, important, root):
            return {"ok": False, "error": "goal state changed; try again"}
    if session_mode:
        enqueue(session_id, root, {"kind": "reopen", "goal_id": goal_id,
                                   "row_id": row_id, "text": note})
        return {"ok": True, "queued": True, "mode": "session", "row": row_id}
    record = load_run(session_id, root, goal_id)
    claude_session = (record or {}).get("claude_session_id")
    resume = bool(claude_session)
    run = Run(session_id, root, goal_id,
              str((record or {}).get("cwd") or _cwd_for(session_id, root)),
              str(claude_session or uuid.uuid4()))
    try:
        run.spawn(json.dumps({"id": row_id, "reopened": note}) if resume
                  else prompt, resume=resume)
    except (FileNotFoundError, OSError) as exc:
        # Back to done, with the note still on the record: nothing is lost
        # and the reader can try again.
        _set_row(session_id, root, goal_id, row_id, status="done", question="")
        return {"ok": False, "error": str(exc)[:200]}
    with _RUNS_GUARD:
        _RUNS[f"{session_id}:{goal_id}"] = run
    return {"ok": True, "reopened": True, "resumed": resume, "row": row_id,
            "run": len(GM.normalize_history(row.get("history"))) + 1}


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
                          CS.load_prompts(session_id, root), goal, rows,
                          root=root)


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
    # Rows the build had actually taken up -- building, or asking -- are
    # told about, the way the reader would put it to a terminal: not that
    # one; the others, which are these. A row that only waited its turn
    # (queued) never reached the build, and there is nothing to tell.
    told = [i for i in cancelled if were[i] in ("building", "asking")]
    if mode() == "session":
        if told:
            enqueue(session_id, root, {"kind": "withdraw", "goal_id": goal_id,
                                       "row_ids": told})
            out["queued"] = True
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
        elif told:
            # Mid-work: the process is ended and its session resumed on the
            # word, since the process itself reads nothing (see redirect).
            note_activity(session_id, root, goal_id, "say",
                          "you deleted %d row%s from the build; telling it"
                          % (len(told), "" if len(told) == 1 else "s"))
            out["redirected"] = run.redirect(
                withdraw_message(session_id, root, goal_id, told))
        return out
    # No process: the run ended, on a question if anything is still building.
    if told and still:
        claude_session = (run.claude_session if run else
                          (record or {}).get("claude_session_id"))
        if not claude_session:
            return out
        message = withdraw_message(session_id, root, goal_id, told)
        withdrew = [i for i in cancelled if were[i] == "asking"]
        if not withdrew:
            # A building row deleted while another row's question stands:
            # the answer to that question is what resumes the session, and
            # it carries this word with it.
            _push_pending(session_id, root, goal_id, message)
            out["pending"] = True
            return out
        if run is None:
            run = Run(session_id, root, goal_id,
                      str((record or {}).get("cwd") or _cwd_for(session_id, root)),
                      claude_session)
            run.cancelled.update(cancelled)
            with _RUNS_GUARD:
                _RUNS[f"{session_id}:{goal_id}"] = run
        note_activity(session_id, root, goal_id, "say",
                      "you withdrew the question; telling the build")
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
            "record": record,
            "run": live(session_id, root).get(goal_id),
            "activity": load_activity(session_id, root, goal_id)}


# --------------------------------------------------- what the rail is shown
#
# The workspace polls one state for the whole chat, so what every build of it
# is doing rides along there: a line each, small enough to send every few
# seconds. The log itself is asked for only when the reader opens it.


def _stamp(value: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _span(start: Any, end: Any) -> Optional[int]:
    """How long a build has been at it: to now while it runs, and to where it
    stopped once it has -- a finished build whose clock keeps running reads as
    one that never ended."""
    began = _stamp(start)
    if began is None:
        return None
    until = _stamp(end) or datetime.now(timezone.utc)
    return max(0, int((until - began).total_seconds()))


def _estimate_of(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What the build said it would cost, as the rail is shown it."""
    value = record.get("estimate")
    if not isinstance(value, dict):
        return None
    try:
        tokens = max(0, int(value.get("tokens") or 0))
        minutes = max(0, int(value.get("minutes") or 0))
    except (TypeError, ValueError):
        return None
    if not tokens and not minutes:
        return None
    return {"tokens": tokens, "minutes": minutes,
            "at": str(value.get("at") or ""),
            "source": str(value.get("source") or "build")}


def live(session_id: str, root: Optional[Path]) -> Dict[str, Any]:
    """Every build this chat has a record of, by goal: where it stands, how
    long it has been at it, the last thing it did, and -- while it is running
    -- roughly how much longer it has, from the build's own estimate of
    itself. Until the build has printed one there is no number to count down
    from, and the rail says it is calculating.
    """
    out: Dict[str, Any] = {}
    try:
        files = sorted(_builds_dir(session_id, root).glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # The builds directory holds the chat's own bookkeeping beside the run
        # records; only a run record names the goal it is for.
        if not isinstance(record, dict) or not isinstance(record.get("goal_id"), str):
            continue
        goal_id = record["goal_id"]
        run = _run_for(session_id, root, goal_id)
        running = bool(run and run.alive())
        lines = load_activity(session_id, root, goal_id)
        elapsed = _span(record.get("started_at"),
                        None if running else record.get("updated_at"))
        rows = max(1, int(record.get("rows") or 1))
        estimate = _estimate_of(record)
        if (running and not estimate and elapsed is not None
                and elapsed >= ESTIMATE_GRACE_S):
            # The build has had its chance to say; the rail gets the chat's
            # own measure of a build this size instead, marked as such.
            estimate = fallback_estimate(session_id, root, rows)
        try:
            spent = max(0, int(record.get("tokens") or 0))
        except (TypeError, ValueError):
            spent = 0
        out[goal_id] = {
            "status": "running" if running else str(record.get("status") or ""),
            "running": running,
            "started_at": record.get("started_at"),
            "updated_at": record.get("updated_at"),
            "rows": rows,
            "elapsed_s": elapsed,
            # What is left of the build's own estimate. It can reach zero and
            # stay there: an estimate that has run out is a truer thing to
            # show than one that keeps moving away. None until the build has
            # printed one, and None again once it has stopped.
            "eta_s": (max(0, estimate["minutes"] * 60 - elapsed)
                      if running and estimate and elapsed is not None
                      else None),
            "estimate": estimate,
            # What it actually spent, once it has stopped and said.
            "tokens": spent,
            "last": lines[-1] if lines else None,
            "lines": len(lines),
            "can_open": bool(record.get("claude_session_id")),
            "error": str(record.get("error") or "")[:200],
        }
    return out


def watch(session_id: str, root: Optional[Path], goal_id: str) -> Dict[str, Any]:
    """Open a terminal on this goal's build.

    While it runs there is nothing to type into -- the process is headless and
    reads no input -- so the terminal follows its log, which is what the rail
    shows, unabridged and live. Once it has stopped, the session it ran under
    is the useful thing instead: the terminal resumes it, and the reader picks
    up the build's own context where it left off.
    """
    from . import agent_exec as AE
    record = load_run(session_id, root, goal_id) or {}
    run = _run_for(session_id, root, goal_id)
    running = bool(run and run.alive())
    cwd = str(record.get("cwd") or "") or _cwd_for(session_id, root)
    if running:
        log = watch_log(session_id, root, goal_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        if not log.exists():
            log.write_text("", encoding="utf-8")
        command = ["tail", "-n", "200", "-f", str(log)]
    else:
        claude_session = str(record.get("claude_session_id") or "")
        if not claude_session:
            return {"ok": False, "error": "this goal has no build to open"}
        command = ["claude", "-r", claude_session]
    line = "cd %s && %s" % (cwd, " ".join(command))
    try:
        script = AE.write_launch_script(
            CS.paths(session_id, root).session_dir,
            "build-" + (_safe_goal(goal_id)[:40] or "run"), cwd, command,
            send=True)
        app = AE.open_terminal(script)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:200], "command": line}
    return {"ok": True, "terminal": app, "cwd": cwd, "following": running,
            "command": line}


# ------------------------------------------- which model, at what effort
#
# The rail's Settings has a Builds tab: the model a build runs on and the
# effort it runs at, both handed to ``claude -p`` as ``--model`` and
# ``--effort``. One file for the whole vault, since it is a choice about the
# reader's builds and not about one chat. Nothing chosen means the CLI's own
# defaults, which is what every build got before there was a choice.
#
# The models on offer are read off the installed Claude Code binary itself:
# it names every model it knows, and a new release names the new ones -- so
# the list keeps up with the CLI rather than with this file. The aliases
# ("opus", "sonnet"...) always resolve to the latest of their family and are
# offered first; the ids found in the binary follow, newest first. The scan
# is a few seconds over a few hundred megabytes, so it is kept beside the
# settings until the binary changes.

EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
# An id is the family and then numbers only -- "claude-opus-4-1",
# "claude-sonnet-4-5-20250929" -- ending where anything else begins: the
# binary also holds "claude-opus-4-6-fast", "claude-fable-5.md" and other
# things that start like an id and are not one.
_MODEL_BYTES = re.compile(
    rb"claude-(?:fable|opus|sonnet|haiku)(?:-[0-9]+)+(?![0-9A-Za-z.\-_])")
_MODEL_FAMILY = {name: rank for rank, name in enumerate(MODEL_ALIASES)}


def _settings_path(session_id: str, root: Optional[Path]) -> Path:
    return CS.paths(session_id, root).base / "build-settings.json"


def _clean_settings(value: Any) -> Dict[str, str]:
    out = {"model": "", "effort": ""}
    if not isinstance(value, dict):
        return out
    model = str(value.get("model") or "").strip()
    if _MODEL_ID.match(model):
        out["model"] = model
    effort = str(value.get("effort") or "").strip().lower()
    if effort in EFFORTS:
        out["effort"] = effort
    return out


def load_settings(session_id: str, root: Optional[Path]) -> Dict[str, str]:
    """The reader's choice of model and effort for builds; "" means the
    CLI's own default for either."""
    try:
        value = json.loads(_settings_path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"model": "", "effort": ""}
    return _clean_settings(value)


def save_settings(session_id: str, root: Optional[Path],
                  patch: Dict[str, Any]) -> Dict[str, Any]:
    """Set either or both; a key not in the patch is left as it was. A
    model that is not a plausible id, or an effort the CLI does not know,
    is refused rather than silently dropped."""
    if not isinstance(patch, dict):
        return {"ok": False, "error": "nothing to set"}
    current = load_settings(session_id, root)
    if "model" in patch:
        model = str(patch.get("model") or "").strip()
        if model and not _MODEL_ID.match(model):
            return {"ok": False, "error": "that is not a model id"}
        current["model"] = model
    if "effort" in patch:
        effort = str(patch.get("effort") or "").strip().lower()
        if effort and effort not in EFFORTS:
            return {"ok": False,
                    "error": "effort is one of " + ", ".join(EFFORTS)}
        current["effort"] = effort
    path = _settings_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, current)
    return {"ok": True, "settings": current}


def _cli_binary() -> Optional[Path]:
    """The Claude Code binary itself, past any shim on PATH: the first
    candidate big enough to be a program rather than a wrapper script."""
    import shutil
    home = Path.home()
    # HC_CLAUDE_BINARY names the binary outright, for an install the search
    # below would not find -- and is then the only place looked.
    named = os.environ.get("HC_CLAUDE_BINARY", "").strip()
    candidates = [named] if named else [
        shutil.which("claude"),
        str(home / ".local" / "bin" / "claude"),
        str(home / ".claude" / "local" / "claude")]
    seen: List[Path] = []
    for name in candidates:
        if not name:
            continue
        try:
            real = Path(name).resolve()
            size = real.stat().st_size
        except OSError:
            continue
        if real in seen:
            continue
        seen.append(real)
        if size >= 1_000_000:
            return real
    return None


def _model_sort_key(name: str) -> Tuple[int, List[int], int, str]:
    body = name[len("claude-"):]
    family, _, rest = body.partition("-")
    numbers = [int(n) for n in re.findall(r"[0-9]+", rest)]
    dated = 1 if any(n >= 20000000 for n in numbers) else 0
    version = [n for n in numbers if n < 20000000]
    # Newest first: the sort is ascending, so the version is negated. The
    # sentinel puts "opus-4" after "opus-4-0", not ahead of "opus-4-8".
    return (_MODEL_FAMILY.get(family, 9), [-n for n in version] + [1], dated, name)


def scan_models(path: Path) -> List[str]:
    """Every model id the binary at ``path`` names, newest first within each
    family. Variants that are not models to choose (``-fast``, ``-v1``) are
    left out."""
    found: Dict[str, int] = {}
    tail = b""
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    break
                data = tail + chunk
                for match in _MODEL_BYTES.finditer(data):
                    name = match.group(0).decode("ascii").rstrip(".-")
                    found[name] = found.get(name, 0) + 1
                tail = data[-96:]
    except OSError:
        return []
    names = [n for n in found
             if not n.endswith("-fast") and "-v1" not in n
             and re.search(r"-[0-9]", n)]
    return sorted(names, key=_model_sort_key)


def _cli_version(binary: Path) -> str:
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", binary.name):
        return binary.name
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True,
                             text=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"[0-9]+\.[0-9]+\.[0-9]+", out.stdout or "")
    return match.group(0) if match else ""


def _models_cache_path(session_id: str, root: Optional[Path]) -> Path:
    return CS.paths(session_id, root).base / "build-models.json"


def models(session_id: str, root: Optional[Path]) -> Dict[str, Any]:
    """What the Builds tab offers: the aliases, the ids the installed CLI
    names, the efforts, and what is chosen now."""
    out: Dict[str, Any] = {"ok": True, "aliases": list(MODEL_ALIASES),
                           "models": [], "efforts": list(EFFORTS),
                           "source": None,
                           "settings": load_settings(session_id, root)}
    binary = _cli_binary()
    if binary is None:
        return out
    try:
        stamp = binary.stat()
    except OSError:
        return out
    key = {"path": str(binary), "size": stamp.st_size,
           "mtime": int(stamp.st_mtime)}
    cache_path = _models_cache_path(session_id, root)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    if (isinstance(cached, dict) and cached.get("key") == key
            and isinstance(cached.get("models"), list)):
        out["models"] = [m for m in cached["models"] if isinstance(m, str)]
        out["source"] = {"path": key["path"],
                         "version": str(cached.get("version") or ""),
                         "scanned_at": str(cached.get("scanned_at") or "")}
        return out
    found = scan_models(binary)
    version = _cli_version(binary)
    scanned_at = _now()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(cache_path, {"key": key, "models": found,
                                       "version": version,
                                       "scanned_at": scanned_at})
    except OSError:
        pass
    out["models"] = found
    out["source"] = {"path": key["path"], "version": version,
                     "scanned_at": scanned_at}
    return out
