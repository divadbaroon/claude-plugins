"""The project goes up on its own, four seconds after the last edit.

A button that has to be remembered is a button that will be forgotten, and
the one session nobody presses it is the session the work is lost. So every
write the workspace accepts arms a timer, and the next write disarms it and
arms another: a burst of typing sends once, when the typing stops, rather
than once per keystroke.

Four seconds is deliberately short. ``hc_sync_project`` upserts the whole
project and prunes what its payload lacks, so a send is a full snapshot
however little changed -- the debounce is what keeps that from happening
per keystroke, and the ceiling on the traffic is the pause between edits,
not the length of the edit.

Nothing here raises. A send that cannot happen -- no config, no session,
no network -- is not a failed edit: the edit is already on disk, the button
is still there, and the reason is kept for the panel to read.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional, Set

# Long enough that a sentence being typed is one send, short enough that
# closing the laptop straight after a thought does not lose it.
DEFAULT_DELAY = 4.0

# The operations that change what a send would carry. Everything absent
# from this set -- reads, status checks, opening a panel, the account work
# in settings -- leaves the timer alone, so looking at the workspace never
# sends it anywhere.
WRITE_OPS = frozenset({
    # One goal, edited.
    "rename_goal", "set_status", "set_priority", "set_notes", "set_sources",
    "set_opening", "set_description", "toggle_todo", "set_relevance",
    "add_todo", "set_understanding",
    # The tree's shape, and what hangs off it.
    "add_goal", "attach_prompt", "detach_prompt", "generate_prompt",
    # Which chats the project is built from.
    "link_chat", "unlink_chat",
    # The project's own record.
    "set_project_objective", "set_project_meta", "project_setup",
    "new_project",
    # The canvas's own save. The web UI does not dispatch a rename or a
    # typed note as an operation above: it posts the whole tree back to
    # /api/import on every edit. That is the workspace's main write path,
    # and without this name it was the one path that never sent.
    "import_goals",
    # Work done to a goal by the build, which writes the goal back the same
    # way a hand would: a run that finishes is an edit nobody typed.
    "build_todos", "answer_todo", "cancel_todos", "reopen_todo",
    # The Archive's two writes. The permanent delete erases its own rows up
    # there by name -- the sync cannot, since it reads an absent goal as one
    # it cannot see -- but the project around it changed and should follow.
    "purge_goal", "restore_goal",
})

_GUARD = threading.Lock()
_TIMERS: Dict[str, threading.Timer] = {}
# Keys with a send in the air, and keys whose send finished into a write
# that arrived while it was flying.
_SENDING: Set[str] = set()
_AGAIN: Set[str] = set()
_LAST: Dict[str, Dict[str, Any]] = {}


def delay() -> float:
    """Seconds of quiet before the send. ``0`` or less turns it off."""
    raw = str(os.environ.get("HC_AUTOSYNC_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_DELAY
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_DELAY


def enabled() -> bool:
    return delay() > 0


def _key(root, cwd) -> str:
    return f"{root or ''}\x00{cwd or ''}"


def schedule(root, cwd, _delay: Optional[float] = None) -> bool:
    """Arm the send, and disarm whatever was armed for this project."""
    if not cwd:
        return False
    wait = delay() if _delay is None else _delay
    if wait <= 0:
        return False
    key = _key(root, cwd)
    with _GUARD:
        held = _TIMERS.pop(key, None)
        if held is not None:
            held.cancel()
        timer = threading.Timer(wait, _fire, (root, cwd, key))
        # Daemon: a pending send must not keep the workspace from closing.
        # The edit it would have carried is on disk, and the next session
        # sends it.
        timer.daemon = True
        _TIMERS[key] = timer
        timer.start()
    return True


def cancel(root, cwd) -> bool:
    """Disarm without sending. Used when something else is about to send
    the same project, so the two do not prune each other's rows."""
    key = _key(root, cwd)
    with _GUARD:
        held = _TIMERS.pop(key, None)
    if held is None:
        return False
    held.cancel()
    return True


class hold:  # noqa: N801 - a context manager reads as a verb here
    """Claim this project's send while something else does it by hand.

    ``hc_sync_project`` prunes the rows its payload lacks, so two sends of
    one project overlapping is not merely wasteful -- the slower one can
    delete what the faster one just wrote. The button takes this, and the
    timer respects it.
    """

    def __init__(self, root, cwd):
        self.key = _key(root, cwd)
        self.root = root
        self.cwd = cwd

    def __enter__(self):
        cancel(self.root, self.cwd)
        with _GUARD:
            _SENDING.add(self.key)
        return self

    def __exit__(self, *_exc):
        with _GUARD:
            _SENDING.discard(self.key)
            again = self.key in _AGAIN
            _AGAIN.discard(self.key)
        # An edit that arrived while the button was sending is not carried by
        # that send: it was on disk after the snapshot was built. Arm again.
        if again:
            schedule(self.root, self.cwd)
        return False


def _note(key: str, value: Dict[str, Any]) -> Dict[str, Any]:
    with _GUARD:
        _LAST[key] = value
    return value


def _fire(root, cwd, key: str) -> None:
    with _GUARD:
        # ``Timer.cancel`` cannot stop a callback that has already begun.
        # If an edit replaced this timer while its callback was waiting for
        # the guard, it is stale and must leave the replacement armed.
        if _TIMERS.get(key) is not threading.current_thread():
            return
        _TIMERS.pop(key, None)
        if key in _SENDING:
            # Something is already sending this project. Remember that a
            # write arrived after it started, and send again once it lands.
            _AGAIN.add(key)
            return
        _SENDING.add(key)
    try:
        _send(root, cwd, key)
    finally:
        with _GUARD:
            _SENDING.discard(key)
            again = key in _AGAIN
            _AGAIN.discard(key)
    if again:
        schedule(root, cwd)


def _send(root, cwd, key: str) -> Dict[str, Any]:
    from . import supabase_client as SB
    try:
        state = SB.status(root)
    except Exception as exc:  # noqa: BLE001 - a vault that will not read
        return _note(key, {"ok": False, "at": time.time(),
                           "error": str(exc)[:200]})
    # Not connected is not an error to report on every edit: there is
    # nowhere to send, the panel already says so, and the edit is safe on
    # disk either way.
    if not state.get("configured") or not state.get("signed_in"):
        return _note(key, {"ok": False, "at": time.time(), "waiting": True,
                           "error": "not signed in to Supabase"})
    try:
        result = SB.sync_project(root, cwd)
    except Exception as exc:  # noqa: BLE001 - every failure is a sentence
        return _note(key, {"ok": False, "at": time.time(),
                           "error": str(exc)[:200]})
    return _note(key, {"ok": True, "at": time.time(),
                       "sent": result.get("sent") or {}})


def state(root, cwd) -> Dict[str, Any]:
    """What the panel says about the sending it did not have to ask for."""
    key = _key(root, cwd)
    with _GUARD:
        last = dict(_LAST.get(key) or {})
        pending = key in _TIMERS
        sending = key in _SENDING
    return {"enabled": enabled(), "seconds": delay(), "pending": pending,
            "sending": sending, "last": last}
