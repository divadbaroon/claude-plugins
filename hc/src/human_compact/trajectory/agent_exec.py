"""Agent execution state: Claude's temporary plan for advancing a Vault goal.

Vault goals (``goals.py``) are persistent human intent.  The task list Claude
builds with TaskCreate/TaskUpdate inside one session is execution state: it
belongs to that session, it is observed rather than authored by the user, and
it never edits the goal tree.  Those are two different things, so they live in
two different stores.  This one is a single JSON run record per Claude session,
written only by that session's hook, tagged with the Vault goal it was launched
against:

    <trajdir>/agent-runs/<claude_session_id>.json

An agent completing its own task says nothing about the human goal; promoting
anything from here into ``goals.json`` is a separate, explicit user action that
this module deliberately does not perform.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .secure_io import atomic_write_json, atomic_write_text, secure_dir

RUNS_DIRNAME = "agent-runs"
PENDING_NAME = "pending.json"
GOAL_ENV = "HC_VAULT_GOAL_ID"
PENDING_TTL_SECONDS = 45 * 60
SCHEMA_VERSION = 1
MAX_TASKS = 60
MAX_RUNS_PER_GOAL = 3
STATUSES = ("pending", "in_progress", "completed", "deleted")
TASK_TOOLS = ("taskcreate", "taskupdate", "tasklist", "taskget")
# Tools whose calls leave something behind on disk: the run's artifact.
FILE_TOOLS = ("edit", "write", "multiedit", "notebookedit")
MAX_FILES = 80
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
# Ids come from inference (g1), promotion (g1a1) and the browser
# (gmsqs8jjv33). Shape only rules out path tricks; the tree decides
# whether a goal actually exists.
_GOAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# "#3 [in_progress] Add session binding" — the TaskList/TaskGet summary line.
_ROW_RE = re.compile(
    r"^\s*#?\s*([A-Za-z0-9_.-]+)\s*\[(pending|in_progress|completed)\]\s*(.*)$")
_CREATED_RE = re.compile(r"[Tt]ask\s*#?\s*([A-Za-z0-9_.-]+)")
_OWNER_SUFFIX_RE = re.compile(
    r"\s*(?:\((?:owner|blocked by)[^)]*\)|—.*|·.*)\s*$", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runs_dir(trajdir: Path) -> Path:
    return Path(trajdir) / RUNS_DIRNAME


def _run_path(trajdir: Path, session_id: str) -> Path:
    if not isinstance(session_id, str) or not _SESSION_RE.fullmatch(session_id):
        raise ValueError("invalid Claude session id")
    return runs_dir(trajdir) / f"{session_id}.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(trajdir: Path, path: Path, value: Any) -> None:
    root = Path(trajdir).parent
    secure_dir(runs_dir(trajdir), root)
    atomic_write_json(path, value, root=root)


def load_run(trajdir: Path, session_id: str) -> Optional[Dict[str, Any]]:
    try:
        value = _read_json(_run_path(trajdir, session_id), None)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("vault_goal_id") else None


def save_run(trajdir: Path, run: Dict[str, Any]) -> Dict[str, Any]:
    run["updated_at"] = _now()
    _write(trajdir, _run_path(trajdir, run["claude_session_id"]), run)
    return run


def load_runs(trajdir: Path) -> List[Dict[str, Any]]:
    """Every recorded run, newest first."""
    directory = runs_dir(trajdir)
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return out
    for path in entries:
        if path.name == PENDING_NAME:
            continue
        value = _read_json(path, None)
        if isinstance(value, dict) and value.get("vault_goal_id") and \
                value.get("claude_session_id"):
            out.append(value)
    out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return out


# --- launching: binding a Claude session to a Vault goal ------------------

def arm(trajdir: Path, goal_id: str, goal_title: str = "",
        cwd: Optional[str] = None) -> Dict[str, Any]:
    """Claim the next Claude session for *goal_id*.

    The Goals UI runs in the browser and cannot start a terminal session, so
    "work on this goal" leaves this claim behind and the next SessionStart hook
    picks it up.  ``hc work`` does not need it: it binds through the
    environment, which cannot be claimed by the wrong session.
    """
    claim = {"schema_version": SCHEMA_VERSION, "vault_goal_id": goal_id,
             "goal_title": goal_title, "cwd": cwd,
             "created_at": _now(), "expires_at": time.time() + PENDING_TTL_SECONDS}
    _write(trajdir, runs_dir(trajdir) / PENDING_NAME, claim)
    return claim


def pending_claim(trajdir: Path) -> Optional[Dict[str, Any]]:
    claim = _read_json(runs_dir(trajdir) / PENDING_NAME, None)
    if not isinstance(claim, dict) or not claim.get("vault_goal_id"):
        return None
    if float(claim.get("expires_at") or 0) < time.time():
        return None
    return claim


def clear_claim(trajdir: Path) -> None:
    try:
        (runs_dir(trajdir) / PENDING_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def _container_dirs() -> set:
    """Directories you *pass through*, never ones you work in.

    Plenty of conversations happen at ``~`` or ``~/Desktop``, so raw majority
    vote will confidently return them. Opening a session there is not what
    "work on this goal" means, and no project marker rules them out —
    real project dirs here have no ``.git`` either.
    """
    home = Path.home()
    return {home, Path("/"), Path("/tmp"), Path(os.sep)} | {
        home / name for name in
        ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures",
         "Library", "Applications", "Public")
    }


def _cwd_votes(index: Dict[str, Any], goal: Dict[str, Any]) -> Dict[str, int]:
    votes: Dict[str, int] = {}
    for evidence_id in goal.get("evidence_ids") or []:
        record = index.get(evidence_id)
        if isinstance(record, dict) and record.get("cwd"):
            votes[str(record["cwd"])] = votes.get(str(record["cwd"]), 0) + 1
    return votes


def _best_cwd(votes: Dict[str, int]) -> Optional[str]:
    containers = _container_dirs()
    ranked = sorted(votes.items(), key=lambda row: (-row[1], -len(row[0]), row[0]))
    for candidate, _count in ranked:
        path = Path(candidate).expanduser()
        if path in containers or not path.is_dir():
            continue
        return str(path)
    return None


def goal_cwd(trajdir: Path, goals: Dict[str, Any], goal_id: str) -> Optional[str]:
    """Where this goal's work happens, from the directories its turns were typed in.

    A button has no shell and therefore no working directory, but the evidence
    index records one per turn and a goal cites those turns — so the goal knows
    its own project. Descendants are consulted before ancestors: a subgoal is
    more specific about where its work lives than the goal above it.
    """
    try:
        index = json.loads((Path(trajdir) / "evidence_index.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(index, dict):
        return None
    goal = _known_goal(goals, goal_id)
    if goal is None:
        return None

    direct = _best_cwd(_cwd_votes(index, goal))
    if direct:
        return direct

    votes: Dict[str, int] = {}
    frontier = [goal["id"]]
    while frontier:
        children = [g for g in goals.get("goals", [])
                    if g.get("parent_goal_id") in frontier]
        if not children:
            break
        for child in children:
            for cwd, count in _cwd_votes(index, child).items():
                votes[cwd] = votes.get(cwd, 0) + count
        best = _best_cwd(votes)
        if best:
            return best
        frontier = [c["id"] for c in children]

    parent = _known_goal(goals, str(goal.get("parent_goal_id") or ""))
    while parent is not None:
        best = _best_cwd(_cwd_votes(index, parent))
        if best:
            return best
        parent = _known_goal(goals, str(parent.get("parent_goal_id") or ""))
    return None


def goal_sources(goals: Dict[str, Any], goal_id: str):
    """Split a goal's attached sources into readable dirs and cited references.

    Only an existing local directory becomes ``--add-dir``: that flag widens
    what the session may touch, so it is honoured exactly as typed and never
    guessed. Repos, docs and anything that does not resolve are cited in the
    prompt so the session knows where to look.
    """
    goal = _known_goal(goals, goal_id)
    directories: List[str] = []
    references: List[str] = []
    for entry in (goal or {}).get("sources") or []:
        if isinstance(entry, dict):
            label, kind = str(entry.get("label") or "").strip(), entry.get("type")
        else:
            label, kind = str(entry or "").strip(), None
        if not label:
            continue
        if kind in (None, "local"):
            candidate = Path(label).expanduser()
            if candidate.is_dir():
                directories.append(str(candidate))
                continue
        references.append(label[:300])
    return directories, references


def launch_prompt(goals: Dict[str, Any], goal_id: str) -> str:
    """The opening message for a session launched on a goal.

    Short on purpose: the full briefing already arrives through the
    SessionStart hook, so this only has to point at the work.
    """
    goal = _known_goal(goals, goal_id)
    if not goal:
        return ""
    override = " ".join(str(goal.get("opening") or "").split())
    if override:
        return override[:400]
    # Everything else is already in the briefing; repeating it here would
    # spend the opening line describing what the session can already read.
    title = " ".join(str(goal.get("title") or "").split()).rstrip(".")
    text = f"{LAUNCH_PREFIX}{goal_id} — {title}."
    return text + " Plan first."


TERMINALS = {
    "iTerm.app": "iTerm",
    "Apple_Terminal": "Terminal",
    "WarpTerminal": "Warp",
    "ghostty": "Ghostty",
}


def terminal_app() -> str:
    """Which terminal to open, preferring the one this process came from."""
    override = os.environ.get("HC_TERMINAL")
    if override:
        return override
    return TERMINALS.get(os.environ.get("TERM_PROGRAM", ""), "Terminal")


EXPECT_BIN = "/usr/bin/expect"
LAUNCH_SETTLE_SECONDS = "2500"      # let the TUI finish drawing before typing
SUBMIT_SETTLE_SECONDS = "600"       # and let a long paste land before Return


def single_line(prompt: str) -> str:
    """Collapse a prompt to one line: a newline in it would press Enter."""
    return " ".join(str(prompt or "").split())


def _write_expect_launch(directory: Path, goal_id: str, command: List[str],
                         prompt: str, send: bool = False) -> Optional[Path]:
    """Start Claude, type the goal into its composer, hand over the terminal.

    With ``send`` the Return is sent too: the user has already confirmed the
    exact text in the Goals UI, so asking for a second keypress in a window
    they did not choose to look at is a worse guarantee, not a better one.

    Claude Code has no flag for "open with this text pre-filled but unsent":
    a prompt argument is submitted immediately. So the session is started
    under ``expect``, which types into its pty and then ``interact``s — the
    user sees the prompt sitting in the composer and presses Enter. Synthetic
    window keystrokes would need an Accessibility grant and could land in
    whatever window happened to be focused; this cannot.
    """
    if not Path(EXPECT_BIN).exists() or not prompt:
        return None
    body = single_line(prompt)
    prompt_file = directory / f"{goal_id}.prompt"
    prompt_file.write_text(body, encoding="utf-8")
    prompt_file.chmod(0o600)

    spawn = " ".join(command)          # tokens are our own, already validated
    path = directory / f"{goal_id}.exp"
    path.write_text(
        f"#!{EXPECT_BIN} -f\n"
        "# Written by hc. Types the goal into Claude and stops there —\n"
        "# starting the work is your keypress, not this script's.\n"
        "set timeout -1\n"
        f'set fh [open "{prompt_file}" r]\n'
        "set body [read $fh]\n"
        "close $fh\n"
        f"spawn -noecho {spawn}\n"
        "# Keep the TUI sized to the real window while it is ours to drive.\n"
        "trap {\n"
        "  set rows [stty rows]\n"
        "  set cols [stty columns]\n"
        "  stty rows $rows columns $cols < $spawn_out(slave,name)\n"
        "} WINCH\n"
        "expect -timeout 30 -re {.} {} timeout {}\n"
        f"after {LAUNCH_SETTLE_SECONDS}\n"
        "send -- $body\n"
        # The composer needs a moment to take a long paste before it will
        # accept a Return; sending both in the same breath can submit an
        # empty or half-typed line.
        + (f"after {SUBMIT_SETTLE_SECONDS}\nsend -- \\r\n" if send else "")
        + "interact\n",
        encoding="utf-8")
    path.chmod(0o700)
    return path


def write_launch_script(trajdir: Path, goal_id: str, cwd: str,
                        command: List[str], prompt: str = "",
                        send: bool = False) -> Path:
    """Stage the launch as a private script rather than a shell string.

    The terminal lands in the goal's project with the command *typed and
    waiting* — starting a Claude session is the user's keypress, not a
    side effect of clicking in a browser. zsh can pre-fill its line editor
    (``print -z``); other shells get the command echoed to run themselves.

    Passing a path to the terminal also keeps every goal-derived value out of
    command-line quoting: nothing is interpolated into a shell by the caller,
    and the files are owner-only inside the vault.
    """
    import shlex
    if not _GOAL_RE.fullmatch(goal_id):
        raise ValueError("invalid goal id")
    directory = runs_dir(trajdir) / "launch"
    secure_dir(directory, Path(trajdir).parent)
    line = " ".join(shlex.quote(part) for part in command)

    if send:
        # Auto-run: `hc work <goal> --start` hands the opening message to
        # Claude as an argument, which it submits itself. Typing into the TUI
        # and then sending a Return was a race against the composer for no
        # benefit once the user has asked for it to just run.
        path = directory / f"{goal_id}.sh"
        path.write_text(
            "#!/bin/sh\n"
            "# Written by hc. Runs the goal's session immediately, because\n"
            "# the run was asked for in the Goals UI.\n"
            f"cd {shlex.quote(cwd)} || exit 1\n"
            f"exec {line}\n",
            encoding="utf-8")
        path.chmod(0o700)
        return path

    driver = _write_expect_launch(directory, goal_id, command, prompt, send=send)
    if driver is not None:
        path = directory / f"{goal_id}.sh"
        path.write_text(
            "#!/bin/sh\n"
            "# Written by hc so a goal can be opened in its own project.\n"
            "# Claude starts with the goal typed in; you press Enter.\n"
            f"cd {shlex.quote(cwd)} || exit 1\n"
            f"exec {shlex.quote(EXPECT_BIN)} -f {shlex.quote(str(driver))}\n",
            encoding="utf-8")
        path.chmod(0o700)
        return path

    zdotdir = directory / "zdotdir"
    secure_dir(zdotdir, Path(trajdir).parent)
    # Sourced by the interactive zsh we start. It defers to the user's own
    # startup files, pre-fills the prompt, then removes itself from the
    # environment so nested shells behave normally.
    (zdotdir / ".zshrc").write_text(
        'HC_REAL_ZDOTDIR="${HC_REAL_ZDOTDIR:-$HOME}"\n'
        '[ -f "$HC_REAL_ZDOTDIR/.zshrc" ] && . "$HC_REAL_ZDOTDIR/.zshrc"\n'
        'unset ZDOTDIR\n'
        '[ -n "$HC_LAUNCH_CMD" ] && print -z -- "$HC_LAUNCH_CMD"\n'
        'unset HC_LAUNCH_CMD HC_REAL_ZDOTDIR\n',
        encoding="utf-8")
    (zdotdir / ".zshrc").chmod(0o600)

    path = directory / f"{goal_id}.sh"
    path.write_text(
        "#!/bin/sh\n"
        "# Written by hc so a goal can be opened in its own project.\n"
        "# Nothing runs on its own: the command is typed, you press Enter.\n"
        f"cd {shlex.quote(cwd)} || exit 1\n"
        f"HC_LAUNCH_CMD={shlex.quote(line)}\n"
        'if [ -x /bin/zsh ]; then\n'
        f'  HC_REAL_ZDOTDIR="${{ZDOTDIR:-$HOME}}" ZDOTDIR={shlex.quote(str(zdotdir))} \\\n'
        '    HC_LAUNCH_CMD="$HC_LAUNCH_CMD" exec /bin/zsh -i\n'
        "fi\n"
        'printf "\\n  run this to start the session:\\n\\n    %s\\n\\n" "$HC_LAUNCH_CMD"\n'
        'exec "${SHELL:-/bin/sh}" -i\n',
        encoding="utf-8")
    path.chmod(0o700)
    return path


# AppleScript for Terminal.app: create exactly one window and raise that one.
# `open -a` asks the app to open a document, which activates it — and macOS
# activation brings every window of that app forward, so a launch buried the
# screen in whatever terminals were already open.
_TERMINAL_SCPT = """
on run argv
  set p to item 1 of argv
  tell application "Terminal"
    set w to do script ("exec " & quoted form of p)
    set winId to id of (first window whose tabs contains w)
    set index of window id winId to 1
    return winId
  end tell
end run
"""

# Raising the window that is already running the session, rather than
# starting a second one beside it.
_RAISE_WINDOW = """
on run argv
  set winId to (item 1 of argv) as integer
  tell application "Terminal"
    set index of window id winId to 1
  end tell
  tell application "System Events"
    tell process "Terminal"
      perform action "AXRaise" of (first window whose value of attribute "AXMain" is true)
    end tell
  end tell
end run
"""


def raise_window(window_id: str) -> bool:
    """Bring one Terminal window forward. False when it is gone."""
    import subprocess
    import sys
    if sys.platform != "darwin" or not str(window_id or "").strip():
        return False
    done = subprocess.run(["osascript", "-", str(window_id)],
                          input=_RAISE_WINDOW, capture_output=True,
                          text=True, timeout=10)
    if done.returncode != 0:
        return False
    subprocess.run(["osascript", "-e",
                    'tell application "Terminal" to activate'],
                   capture_output=True, text=True, timeout=10)
    return True


def remember_window(trajdir: Path, goal_id: str, window_id: str) -> None:
    """Park the window id until the session it opened binds to a run."""
    if not _GOAL_RE.fullmatch(goal_id) or not str(window_id or "").strip():
        return
    directory = runs_dir(trajdir) / "launch"
    secure_dir(directory, Path(trajdir).parent)
    path = directory / f"{goal_id}.window"
    atomic_write_text(path, str(window_id).strip(), root=Path(trajdir).parent)
    path.chmod(0o600)


def take_window(trajdir: Path, goal_id: str) -> str:
    """Read and consume a parked window id, if the launcher left one."""
    path = runs_dir(trajdir) / "launch" / f"{goal_id}.window"
    try:
        value = path.read_text().strip()
    except OSError:
        return ""
    path.unlink(missing_ok=True)
    return value


_RAISE_ONE = """
tell application "System Events"
  tell process "Terminal"
    if (count of windows) > 0 then
      perform action "AXRaise" of window 1
    end if
  end tell
end tell
"""


def open_terminal(script: Path, app: Optional[str] = None,
                  foreground: bool = True, opened_window=None) -> str:
    """Open *script* in one terminal window. Returns the app it used.

    ``opened_window`` is filled with the Terminal window id when there is
    one, so a later "open the conversation" can raise that same window
    instead of starting a second session beside it.
    """
    import subprocess
    import sys
    opened_window = opened_window if opened_window is not None else []
    app = app or terminal_app()
    if sys.platform != "darwin":
        raise RuntimeError("one-click launch currently supports macOS only")
    if app == "Terminal":
        # One window, made frontmost within Terminal, without asking macOS to
        # raise the app's other windows unless the caller wants focus.
        result = subprocess.run(
            ["osascript", "-", str(script)], input=_TERMINAL_SCPT,
            capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            opened_window[:] = [result.stdout.strip()]
            if foreground:
                # Raise just this window. AXRaise needs Accessibility
                # permission; when that is not granted it fails silently and
                # the window sits behind the browser, so fall back to
                # activating the app — noisier, but visible beats hidden.
                raised = subprocess.run(["osascript", "-e", _RAISE_ONE],
                                        capture_output=True, text=True,
                                        timeout=10)
                if raised.returncode != 0:
                    subprocess.run(
                        ["osascript", "-e",
                         'tell application "Terminal" to activate'],
                        capture_output=True, text=True, timeout=10)
            return app
        # Fall through: a scripting-disabled Terminal is still openable.
    result = subprocess.run(["open", "-a", app, str(script)],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or f"could not open {app}").strip()[:200])
    return app


MAX_ACTIVITY = 60


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def describe_call(call: Dict[str, Any]) -> str:
    """One short phrase for a tool call, or nothing worth reporting.

    Paths and commands only — never file contents, which is the same line the
    run store already draws.
    """
    name = str(call.get("tool_name") or call.get("name") or "")
    args = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
    target = str(args.get("file_path") or args.get("path") or "").strip()
    tail = Path(target).name if target else ""
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return ("edited " + tail) if tail else "edited a file"
    if name in ("Read", "NotebookRead"):
        return ("read " + tail) if tail else "read a file"
    if name in ("Grep", "Glob"):
        return "searched the project"
    if name == "Bash":
        command = " ".join(str(args.get("command") or "").split())[:60]
        return ("ran " + command) if command else "ran a command"
    if name in ("WebFetch", "WebSearch"):
        return "looked something up"
    return ""


def note_activity(run: Dict[str, Any], kind: str, text: str) -> bool:
    """Append one line to the run's activity, newest last. Bounded."""
    text = " ".join(str(text or "").split())[:200]
    if not text:
        return False
    log = run.setdefault("activity", [])
    if log and log[-1].get("kind") == kind and log[-1].get("text") == text:
        return False
    log.append({"at": _now(), "kind": kind, "text": text})
    del log[:-MAX_ACTIVITY]
    return True


def _known_goal(goals: Optional[Dict[str, Any]], goal_id: str) -> Optional[Dict[str, Any]]:
    for goal in (goals or {}).get("goals", []):
        if isinstance(goal, dict) and goal.get("id") == goal_id:
            return goal
    return None


def _git_branch(cwd: Optional[str]) -> Optional[str]:
    """Read the checked-out branch without shelling out from a hook."""
    if not cwd:
        return None
    try:
        current = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    for directory in [current, *current.parents]:
        head = directory / ".git" / "HEAD"
        try:
            text = head.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            continue
        if text.startswith("ref: refs/heads/"):
            return text.split("refs/heads/", 1)[1][:120]
        return text[:40] or None
    return None


def bind(trajdir: Path, session_id: str, goal_id: str, goals=None,
         cwd: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Create the ``vault_goal_id ↔ claude_session_id`` record."""
    if not _GOAL_RE.fullmatch(str(goal_id or "")):
        return None
    goal = _known_goal(goals, goal_id)
    if goals is not None and goal is None:
        return None                      # never bind to a goal that vanished
    existing = load_run(trajdir, session_id)
    if existing:
        return existing
    run = {
        "schema_version": SCHEMA_VERSION,
        "claude_session_id": session_id,
        "vault_goal_id": goal_id,
        "goal_title": str((goal or {}).get("title") or "")[:120],
        "status": "running",
        "cwd": cwd,
        "git_branch": _git_branch(cwd),
        "started_at": _now(),
        "finished_at": None,
        "end_reason": None,
        "git_head_before": git_head(cwd),
        "git_head_after": None,
        "user_prompt": "",
        "summary": "",
        "tasks": [],
        "files": [],
    }
    return save_run(trajdir, run)


def _resolve_binding(trajdir: Path, session_id: str, payload: Dict[str, Any],
                     goals: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Bind through the launcher's environment, else through a UI claim.

    Unbound sessions are the common case, so this answers "no" before reading
    the goal tree: every hook of every ordinary session passes through here.
    """
    env_goal = str(os.environ.get(GOAL_ENV) or "").strip()
    claim = None if env_goal else pending_claim(trajdir)
    if claim and str(payload.get("hook_event_name") or "") != "SessionStart":
        return None          # a claim binds a new session, never a live one
    if not env_goal and not claim:
        return None
    if goals is None:
        from . import goals as GM
        goals = GM.load(trajdir)[0]
    cwd = payload.get("cwd")
    if env_goal:
        run = bind(trajdir, session_id, env_goal, goals, cwd)
        window = take_window(trajdir, env_goal)
        if run is not None and window and not run.get("terminal_window"):
            run["terminal_window"] = window
            save_run(trajdir, run)
        return run
    run = bind(trajdir, session_id, str(claim["vault_goal_id"]), goals,
               cwd or claim.get("cwd"))
    if run:
        clear_claim(trajdir)             # one claim binds exactly one session
    return run


# --- observing: TaskCreate / TaskUpdate / TaskList / TaskGet --------------

def _response_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [str(b.get("text")) for b in value
                 if isinstance(b, dict) and b.get("text")]
        if parts:
            return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result"):
            nested = value.get(key)
            if isinstance(nested, str) and nested:
                return nested
            if isinstance(nested, list):
                text = _response_text(nested)
                if text:
                    return text
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _task(run: Dict[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
    for task in run["tasks"]:
        if task.get("task_id") == task_id:
            return task
    return None


def _upsert(run: Dict[str, Any], task_id: str, **fields) -> Optional[Dict[str, Any]]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    task = _task(run, task_id)
    if task is None:
        if len(run["tasks"]) >= MAX_TASKS:
            return None
        task = {
            "task_id": task_id,
            "subject": "",
            "description": "",
            "status": "pending",
            "activeForm": "",
            "owner": "",
            "blocks": [],
            "blockedBy": [],
            "source": "agent",
            "vault_goal_id": run["vault_goal_id"],
            "claude_session_id": run["claude_session_id"],
            "created_at": _now(),
            "updated_at": _now(),
        }
        run["tasks"].append(task)
    changed = False
    for key, value in fields.items():
        if value in (None, "", [], {}) or task.get(key) == value:
            continue
        if key in ("blocks", "blockedBy"):
            merged = list(dict.fromkeys(list(task.get(key) or []) + list(value)))
            if merged == task.get(key):
                continue
            task[key] = merged
        else:
            task[key] = value
        changed = True
    if changed:
        task["updated_at"] = _now()
    return task


def _field(payload: Dict[str, Any], *names):
    """First present key among close-but-different spellings."""
    for name in names:
        value = payload.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _status(value: Any) -> str:
    value = str(value or "").strip()
    return value if value in STATUSES else ""


def _clean_subject(text: str) -> str:
    return _OWNER_SUFFIX_RE.sub("", str(text or "").strip())[:200]


def _apply_listing(run: Dict[str, Any], text: str) -> bool:
    """Reconcile against a TaskList/TaskGet summary — the agent's own view."""
    touched = False
    for line in str(text or "").splitlines():
        match = _ROW_RE.match(line)
        if not match:
            continue
        task_id, status, rest = match.groups()
        if _upsert(run, task_id, status=status,
                   subject=_clean_subject(rest)) is not None:
            touched = True
    return touched


def observe_tool_call(run: Dict[str, Any], call: Dict[str, Any]) -> bool:
    """Fold one Task* tool call into the run's plan. True when it changed."""
    name = str(call.get("tool_name") or call.get("name") or "")
    if name.lower() not in TASK_TOOLS:
        return False
    payload = call.get("tool_input")
    if not isinstance(payload, dict):
        payload = call.get("input") if isinstance(call.get("input"), dict) else {}
    response = call.get("tool_response", call.get("response"))
    text = _response_text(response) if response is not None else ""
    before = json.dumps(run["tasks"], sort_keys=True)

    lowered = name.lower()
    if lowered == "taskcreate":
        # The assigned id is never in the input; it comes back in the result as
        # {"task": {"id", "subject"}}. Fall back to the visible text only when
        # the structured form is absent.
        task_id = ""
        if isinstance(response, dict):
            task = response.get("task")
            if isinstance(task, dict):
                task_id = str(task.get("id") or "")
            if not task_id:
                task_id = str(response.get("id") or response.get("taskId") or "")
        if not task_id:
            found = _CREATED_RE.search(text)
            task_id = found.group(1) if found else ""
        if not task_id:
            # An unparseable create still belongs on the plan; a later TaskList
            # reconciles it under its real id.
            task_id = f"new-{len(run['tasks']) + 1}"
        _upsert(run, task_id,
                subject=str(payload.get("subject") or "")[:200],
                description=str(payload.get("description") or "")[:1000],
                activeForm=str(_field(payload, "activeForm", "active_form") or "")[:120],
                status="pending")
    elif lowered == "taskupdate":
        # A streamed input is the raw shape the model emitted: Claude Code
        # repairs id/task_id -> taskId and active_form -> activeForm before
        # executing, but the repair is not always what an observer sees.
        _upsert(run, str(_field(payload, "taskId", "id", "task_id") or ""),
                status=_status(payload.get("status")),
                subject=str(payload.get("subject") or "")[:200],
                description=str(payload.get("description") or "")[:1000],
                activeForm=str(_field(payload, "activeForm", "active_form") or "")[:120],
                owner=str(payload.get("owner") or "")[:80],
                blocks=[str(x) for x in
                        (_field(payload, "addBlocks", "add_blocks") or [])][:20],
                blockedBy=[str(x) for x in
                           (_field(payload, "addBlockedBy", "add_blocked_by") or [])][:20])
    elif lowered in ("tasklist", "taskget"):
        _apply_listing(run, text)

    return json.dumps(run["tasks"], sort_keys=True) != before


def observe_file_call(run: Dict[str, Any], call: Dict[str, Any]) -> bool:
    """Record a file this session wrote. Paths only — never contents.

    The artifact of a run is what it left on disk. Most of these projects are
    not git repositories, so a commit range would show nothing; the tool calls
    themselves are the reliable record of what changed.
    """
    name = str(call.get("tool_name") or call.get("name") or "").lower()
    if name not in FILE_TOOLS:
        return False
    payload = call.get("tool_input")
    if not isinstance(payload, dict):
        payload = call.get("input") if isinstance(call.get("input"), dict) else {}
    path = _field(payload, "file_path", "path", "notebook_path")
    if not isinstance(path, str) or not path.strip():
        return False
    files = run.setdefault("files", [])
    for entry in files:
        if entry.get("path") == path:
            entry["edits"] = int(entry.get("edits") or 0) + 1
            entry["last_at"] = _now()
            return True
    if len(files) >= MAX_FILES:
        return False
    files.append({"path": path, "edits": 1, "tool": name,
                  "first_at": _now(), "last_at": _now()})
    return True


def git_head(cwd: Optional[str]) -> Optional[str]:
    """The current commit, read from disk rather than shelled out for."""
    if not cwd:
        return None
    try:
        root = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    for directory in [root, *root.parents]:
        git = directory / ".git"
        if not git.is_dir():
            continue
        try:
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not head.startswith("ref: "):
            return head[:40] or None
        ref = head[5:].strip()
        try:
            return (git / ref).read_text(encoding="utf-8").strip()[:40] or None
        except OSError:
            pass
        try:
            for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:40]
        except OSError:
            return None
        return None
    return None


def observe_hook(payload: Dict[str, Any], trajdir: Optional[Path] = None,
                 goals: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Fold one Claude Code hook payload into this session's execution record.

    Returns the run when the session is bound to a Vault goal, else ``None``.
    Sessions that were not launched against a goal are the normal case and
    leave no trace here at all.
    """
    if trajdir is None:
        from . import state
        trajdir = state.trajdir()
    trajdir = Path(trajdir)
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    if not _SESSION_RE.fullmatch(session_id):
        return None
    event = str(payload.get("hook_event_name") or "")

    run = load_run(trajdir, session_id)
    if run is None:
        run = _resolve_binding(trajdir, session_id, payload, goals)
        if run is None:
            return None
    run.setdefault("tasks", [])
    dirty = False

    if not run.get("cwd") and payload.get("cwd"):
        run["cwd"] = payload["cwd"]
        run["git_branch"] = run.get("git_branch") or _git_branch(run["cwd"])
        dirty = True

    if event == "UserPromptSubmit":
        # One branch, because this is an if/elif chain: a second
        # UserPromptSubmit arm below never ran when a prompt was present, so
        # answering the session never cleared "waiting on you".
        prompt = str(payload.get("prompt") or "").strip()
        if prompt and not run.get("user_prompt"):
            run["user_prompt"] = prompt[:1000]
            dirty = True
        if run.get("awaiting_user"):
            run["awaiting_user"] = False
            dirty = True
    elif event == "PostToolBatch":
        batch = []
        for call in payload.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            if observe_tool_call(run, call):
                dirty = True
            if observe_file_call(run, call):
                dirty = True
            note = describe_call(call)
            if note:
                batch.append(note)
        if batch and note_activity(run, "did", "; ".join(batch[:3])):
            dirty = True
    elif event in ("TaskCreated", "TaskCompleted"):
        # The event may carry the task flat or nested; reading only the flat
        # spelling silently records nothing, which is what 15 runs with zero
        # tasks looked like. Try both rather than assume one.
        nested = payload.get("task")
        nested = nested if isinstance(nested, dict) else {}
        subject = str(_field(payload, "task_subject", "subject")
                      or nested.get("subject") or "").strip()
        task_id = str(_field(payload, "task_id", "taskId", "id")
                      or nested.get("id") or "").strip()
        if task_id or subject:
            if not task_id:
                existing = next((t for t in run["tasks"]
                                 if t.get("subject") == subject), None)
                task_id = existing["task_id"] if existing else f"evt-{len(run['tasks']) + 1}"
            before = json.dumps(run["tasks"], sort_keys=True)
            _upsert(run, task_id, subject=subject[:200],
                    description=str(_field(payload, "task_description",
                                           "description")
                                    or nested.get("description") or "")[:1000],
                    status="completed" if event == "TaskCompleted" else "pending")
            dirty = dirty or json.dumps(run["tasks"], sort_keys=True) != before
            if subject:
                note_activity(run, "task",
                              ("finished: " if event == "TaskCompleted"
                               else "planned: ") + subject[:120])
    elif event == "Stop":
        # A stop is the session handing the turn back: it has either finished
        # a stretch of work or is asking something. That is true whether or
        # not the payload carries the message — gating the whole branch on
        # last_assistant_message meant a waiting session read as running.
        summary = str(payload.get("last_assistant_message") or "").strip()
        if summary and summary != run.get("summary"):
            run["summary"] = summary[:1200]
        note_activity(run, "turn",
                      _first_line(summary)[:160] if summary
                      else "handed the turn back \u2014 waiting on you")
        run["awaiting_user"] = True
        dirty = True

    elif event == "SessionEnd":
        run["git_head_after"] = git_head(run.get("cwd"))
        run["status"] = "finished"
        run["finished_at"] = _now()
        run["end_reason"] = str(payload.get("reason") or "unknown")[:60]
        dirty = True

    return save_run(trajdir, run) if dirty else run


# --- reading: UI payload and session context ------------------------------

def _counts(run: Dict[str, Any]) -> Dict[str, int]:
    tally = {status: 0 for status in STATUSES}
    for task in run.get("tasks", []):
        status = task.get("status")
        if status in tally:
            tally[status] += 1
    return tally


def plans(trajdir: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Per-goal execution plans for ``/api/state``, newest run first."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for run in load_runs(trajdir):
        bucket = out.setdefault(str(run["vault_goal_id"]), [])
        if len(bucket) >= MAX_RUNS_PER_GOAL:
            continue
        bucket.append({
            "session_id": run["claude_session_id"],
            "goal_id": run["vault_goal_id"],
            "status": run.get("status", "running"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "updated_at": run.get("updated_at"),
            "git_branch": run.get("git_branch"),
            "user_prompt": str(run.get("user_prompt") or "")[:400],
            "summary": str(run.get("summary") or "")[:400],
            "counts": _counts(run),
            # What the run changed, so REVIEW can show it from the state the
            # page loads at boot rather than waiting on a second fetch.
            "files": [{"path": str(f.get("path") or ""),
                       "edits": int(f.get("edits") or 0)}
                      for f in (run.get("files") or [])][:40],
            "tasks": [{
                "task_id": task.get("task_id"),
                "subject": task.get("subject") or task.get("description", "")[:80],
                "status": task.get("status", "pending"),
                "activeForm": task.get("activeForm", ""),
                "owner": task.get("owner", ""),
                "blockedBy": task.get("blockedBy", []),
                "source": "agent",
            } for task in run.get("tasks", [])[:MAX_TASKS]],
        })
    return out


# The opening line this launcher types into a new session. It is machine
# authored, so it is filtered back out of the evidence the goal model reads —
# otherwise each launch quotes its own prompt into the next briefing.
LAUNCH_PREFIX = "Work on my Vault goal "
MAX_BRIEFING_CHARS = 6000
BRIEF_ITEMS = 5


def _linked_prompts(trajdir: Path, goal: Dict[str, Any],
                    limit: int = BRIEF_ITEMS) -> List[str]:
    """The user's own words for this goal, verbatim and most recent last.

    Across this vault's goals the inference cites user turns over assistant
    turns roughly ten to one — asked what explains a goal, it picks what the
    user said. These are the highest-value tokens in the briefing: intent
    ages well, while a week-old assistant claim about the code may already
    be false.
    """
    from . import goals as GM
    wanted = [pid for pid in (goal.get("prompt_ids") or [])
              if isinstance(pid, str)]
    if not wanted:
        return []
    known = {p["id"]: p for p in GM.evidence_prompts(trajdir)}
    rows = [known[pid] for pid in wanted if pid in known]
    rows.sort(key=lambda p: int(p.get("ordinal") or 0))
    return [" ".join(str(p.get("text") or "").split())[:400] for p in rows[-limit:]]


def _source_extractions(trajdir: Path, goal: Dict[str, Any],
                        limit: int = 3) -> List[Dict[str, Any]]:
    """Distilled records of the conversations this goal draws evidence from.

    The transcripts themselves run to hundreds of thousands of tokens and
    would be compacted on arrival into something worse than this. Extraction
    already did that work, at roughly a thousand tokens per conversation.
    """
    prefixes: List[str] = []
    for evidence_id in goal.get("evidence_ids") or []:
        prefix = str(evidence_id).split("#", 1)[0]
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    out: List[Dict[str, Any]] = []
    directory = Path(trajdir) / "conversations"
    for prefix in prefixes[:limit]:
        try:
            matches = sorted(directory.glob(f"{prefix}*.json"))
        except OSError:
            continue
        for path in matches[:1]:
            value = _read_json(path, None)
            extracted = (value or {}).get("extracted") if isinstance(value, dict) else None
            if isinstance(extracted, dict):
                out.append(extracted)
    return out


def _digest(extractions: List[Dict[str, Any]], key: str,
            limit: int = BRIEF_ITEMS) -> List[str]:
    """Merge one field across source conversations, first mention wins."""
    seen: List[str] = []
    for extraction in extractions:
        for item in extraction.get(key) or []:
            text = " ".join(str(item).split())[:180]
            if text and text not in seen:
                seen.append(text)
            if len(seen) >= limit:
                return seen
    return seen


def _ancestry(goals: Dict[str, Any], goal: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The chain from the top-level goal down to this one."""
    chain = [goal]
    seen = {goal.get("id")}
    parent = _known_goal(goals, str(goal.get("parent_goal_id") or ""))
    while parent is not None and parent.get("id") not in seen:
        chain.append(parent)
        seen.add(parent.get("id"))
        parent = _known_goal(goals, str(parent.get("parent_goal_id") or ""))
    chain.reverse()
    return chain


def goal_context(trajdir: Path, goals: Dict[str, Any], goal_id: str) -> str:
    """The prompt text a session launched on this goal opens with."""
    parts = prompt_sections(trajdir, goals, goal_id)
    if not parts:
        return ""
    lines = list(parts["intro"])
    for section in parts["sections"]:
        lines.extend(["", f"## {section['n']}. {section['title']}", ""])
        lines.extend(section["lines"])
    if parts["footer"]:
        lines.extend(["", "---"] + parts["footer"])
    return "\n".join(lines)[:MAX_BRIEFING_CHARS] + "\n"


def prompt_sections(trajdir: Path, goals: Dict[str, Any], goal_id: str):
    """The prompt as structure: intro, numbered sections, closing caveat.

    Written to be read top-down: what this is, where the goal sits, what the
    user actually said, then the context to work from. Sections are numbered
    as they are emitted so an empty one leaves no gap — a briefing that says
    "4. (none)" teaches the reader to skim.
    """
    goal = _known_goal(goals, goal_id)
    if not goal:
        return None

    chain = _ancestry(goals, goal)
    status = str(goal.get("status", "active")).replace("_", " ")
    head = [
        "# Your assignment for this session",
        "",
        "Everything below is context the user's Vault assembled for ONE goal.",
        "Read it, then work only on the FOCUS goal named in section 1.",
        "",
        "Their goals are long-lived intent. The tasks you create with",
        "TaskCreate/TaskUpdate are your own plan for this session: they are",
        "recorded against the goal and shown in the user's Goals UI, but they",
        "never change it. Finishing your task is not finishing their goal —",
        "leave the goal's status and its subgoals' statuses alone.",
    ]

    sections: List[tuple] = []

    where = []
    for depth, node in enumerate(chain):
        marker = ("GRAND GOAL" if depth == 0 and len(chain) > 1 else
                  "FOCUS" if node is goal else "PARENT")
        indent = "  " * depth
        label = (indent + marker).ljust(14) + f"{node['id']:<5} · {node.get('title', '')}"
        if node is goal:
            label += f" [{status}]"
        where.append(label)
    if len(chain) > 1:
        where.append("")
        where.append("Work the FOCUS goal. The goals above it are orientation, not scope.")
    description = " ".join(str(goal.get("description") or "").split())[:400]
    notes = " ".join(str(goal.get("notes") or "").split())[:600]
    if description:
        where.extend(["", f"What finishing it means: {description}"])
    if notes:
        where.append(f"The user's notes: {notes}")
    if str(goal.get("priority") or "normal") != "normal":
        where.append(f"Priority: {goal['priority']}")
    children = [g for g in goals.get("goals", [])
                if g.get("parent_goal_id") == goal["id"]]
    if children:
        where.append("")
        where.append("It breaks down into (the user's own subgoals — do not restatus them):")
        for child in children[:12]:
            child_status = str(child.get("status", "active")).replace("_", " ")
            where.append(f"  - {child.get('title', '')[:120]} [{child_status}]")
    sections.append(("WHERE THIS SITS", where))

    quotes = _linked_prompts(trajdir, goal)
    if quotes:
        sections.append(("WHAT THE USER ASKED FOR, IN THEIR WORDS",
                         [f'  - "{quote}"' for quote in quotes]))

    directories, references = goal_sources(goals, goal_id)
    if directories or references:
        attached = [f"  - {d} (readable this session)" for d in directories]
        attached += [f"  - {r}" for r in references]
        sections.append(("CONTEXT THE USER ATTACHED", attached))

    extractions = _source_extractions(trajdir, goal)
    for title, key in (
        ("ALREADY DECIDED — settled, do not relitigate without reason", "decisions"),
        ("ALREADY BUILT", "artifacts_or_outputs"),
        ("PROBLEMS HIT BEFORE", "blockers"),
        ("STILL OPEN", "unresolved_questions"),
    ):
        items = _digest(extractions, key)
        if items:
            sections.append((title, [f"  - {item}" for item in items]))

    history = [r for r in load_runs(trajdir)
               if r.get("vault_goal_id") == goal_id and r.get("tasks")]
    if history:
        rows = []
        for run in history[:3]:
            tally = _counts(run)
            summary = " ".join(str(run.get("summary") or "").split())[:220]
            rows.append(
                f"  - {str(run.get('started_at') or '')[:10]} "
                f"session {str(run['claude_session_id'])[:8]} "
                f"({run.get('status', 'running')}): {len(run['tasks'])} tasks, "
                f"{tally['completed']} completed"
                + (f" — {summary}" if summary else ""))
        sections.append(("EARLIER CLAUDE SESSIONS ON THIS GOAL", rows))

    footer = [
        "The recalled sections came from earlier conversations and may have",
        "gone stale. Check the code before relying on any of them.",
    ] if extractions else []
    return {
        "intro": head,
        "sections": [{"n": number, "title": title, "lines": body}
                     for number, (title, body) in enumerate(sections, start=1)],
        "footer": footer,
    }


# Commands whose whole purpose is to check something. Anything else a session
# ran is work, not verification, and claiming otherwise would be worse than
# saying nothing.
_CHECKS = (
    "npm test", "npm run test", "npm run build", "npm run lint", "yarn test",
    "pnpm test", "pytest", "python -m unittest", "python -m pytest", "tox",
    "go test", "cargo test", "cargo check", "make test", "make check",
    "mvn test", "gradle test", "rspec", "jest", "vitest", "mypy", "ruff",
    "eslint", "tsc", "node --test", "node --check",
)


def _elapsed(run: Dict[str, Any]) -> str:
    start, end = run.get("started_at"), run.get("finished_at")
    try:
        began = datetime.fromisoformat(str(start))
        ended = (datetime.fromisoformat(str(end)) if end
                 else datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return ""
    seconds = max(0, int((ended - began).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def run_state(run: Dict[str, Any]) -> str:
    """running / waiting on you / finished / failed — in that order of truth."""
    if run.get("status") == "finished":
        reason = str(run.get("end_reason") or "")
        return "failed" if reason in ("error", "crash") else "finished"
    if run.get("awaiting_user"):
        return "waiting"
    return "running"


def _checks_run(run: Dict[str, Any]) -> List[str]:
    seen, out = set(), []
    for entry in run.get("activity") or []:
        text = str(entry.get("text") or "")
        if not text.startswith("ran "):
            continue
        command = text[4:].strip()
        if any(command.startswith(check) for check in _CHECKS):
            if command not in seen:
                seen.add(command)
                out.append(command)
    return out


def _subgoal_progress(goals: Dict[str, Any], goal_id: str) -> Dict[str, int]:
    children = [g for g in goals.get("goals", [])
                if g.get("parent_goal_id") == goal_id]
    done = sum(1 for g in children if g.get("status") == "completed")
    return {"done": done, "total": len(children)}


def review(trajdir: Path, goals: Dict[str, Any], goal_id: str) -> Dict[str, Any]:
    """What each session on this goal left behind, and how to go look at it.

    Deliberately no copies of the user's files: this reports paths, counts and
    the command that shows the real thing in its real place.
    """
    goal = _known_goal(goals, goal_id)
    if not goal:
        return {"ok": False, "runs": []}
    rows = []
    for run in load_runs(trajdir):
        if run.get("vault_goal_id") != goal_id:
            continue
        cwd = run.get("cwd")
        files = sorted(run.get("files") or [],
                       key=lambda f: (-int(f.get("edits") or 0), f.get("path", "")))
        listed = []
        for entry in files[:40]:
            path = str(entry.get("path") or "")
            shown = path
            if cwd and path.startswith(str(cwd).rstrip("/") + "/"):
                shown = path[len(str(cwd).rstrip("/")) + 1:]
            listed.append({"path": shown, "full": path,
                           "edits": int(entry.get("edits") or 0)})
        before, after = run.get("git_head_before"), run.get("git_head_after")
        how = []
        if cwd and (Path(cwd) / ".git").is_dir():
            if before and after and before != after:
                how.append({"what": "commits made during this session",
                            "command": f"git -C {cwd} log --oneline {before[:8]}..{after[:8]}"})
                how.append({"what": "everything this session changed",
                            "command": f"git -C {cwd} diff {before[:8]}..{after[:8]}"})
            else:
                how.append({"what": "uncommitted changes",
                            "command": f"git -C {cwd} diff"})
        elif cwd:
            how.append({"what": "open the project", "command": f"open {cwd}"})
        if listed and cwd:
            how.append({"what": "the file it touched most",
                        "command": f"open {listed[0]['full']}"})
        rows.append({
            "session_id": run.get("claude_session_id"),
            "live_window": bool(run.get("terminal_window")),
            "status": run.get("status"), "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"), "cwd": cwd,
            "git_branch": run.get("git_branch"),
            "committed": bool(before and after and before != after),
            "summary": str(run.get("summary") or "")[:600],
            "counts": _counts(run), "task_total": len(run.get("tasks") or []),
            "files": listed, "files_total": len(files), "how": how,
            # What the reader is actually asking: where is this, what did it
            # do, and does it need me?
            "state": run_state(run),
            "session_id": run.get("claude_session_id"),
            "live_window": bool(run.get("terminal_window")),
            "elapsed": _elapsed(run),
            "did": [{"at": str(e.get("at") or "")[11:19],
                     "kind": str(e.get("kind") or ""),
                     "text": str(e.get("text") or "")}
                    for e in (run.get("activity") or [])][-25:],
            "attention": (str(run.get("summary") or "")[:600]
                          if run.get("awaiting_user") else ""),
            "checked": _checks_run(run),
            "tasks": {"done": _counts(run).get("completed", 0),
                      "total": len(run.get("tasks") or [])},
            "subgoals": _subgoal_progress(goals, goal_id),
            "resume": ("claude -r " + str(run.get("claude_session_id"))
                       if run.get("claude_session_id") else ""),
        })
    known = _digest(_source_extractions(trajdir, goal), "artifacts_or_outputs")
    return {"ok": True, "goal_id": goal_id, "runs": rows[:5], "known": known}
