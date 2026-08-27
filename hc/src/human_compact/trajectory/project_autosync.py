"""Keeping Supabase current without anyone having to remember to.

Sending the project was a button. A button is a thing to forget, and every
minute between the last press and the next one is a minute where the remote
says something that stopped being true -- which for a shared workspace means
a collaborator reading a stale tree and not knowing it.

So: the workspace marks itself dirty when something meaningful changes, and
this decides when that becomes a request. The whole of the policy:

* QUIET. A sync happens once changes have stopped for a few seconds. Typing
  a note is dozens of writes and one thought; the remote wants the thought.
* CEILING. Changes that never stop would otherwise never sync, so a project
  dirty for MAX_DIRTY_SECONDS goes up regardless. The clock restarts from
  each successful send, not from the first change ever made.
* ONE AT A TIME. Every send happens on this module's single pump thread, so
  two syncs of one project cannot overlap and a burst of changes collapses
  into the one request that carries all of them. There is no queue of
  pending payloads: the payload is built when the request is made, from
  disk, so the newest one is always the whole truth.
* DIRTY UNTIL PROVEN SENT. A failed sync leaves the project dirty. Nothing
  is ever dropped because the network was out -- the retry carries it, and
  if the process dies first the next one starts dirty because the button's
  own snapshot is rebuilt from disk anyway.
* BOUNDARIES SKIP THE WAIT. A build finishing and a workspace closing are
  moments where a few seconds of debounce is exactly the wrong thing to
  spend. Those call ``flush_soon`` and ``drain``.

REVISIONS, NOT FLAGS. A boolean "dirty" loses a change that arrives while a
sync is in flight: the send clears the flag, and the write that landed
mid-flight is never sent again. So dirty is a comparison -- ``revision`` is
bumped by every change, ``synced_revision`` records what the last successful
send actually carried, and a change during a send simply leaves the first
number ahead of the second.

WHAT IS NOT HERE. No second persistence path: this calls the same
``supabase_client.sync_project`` the button does, which builds the same
snapshot and calls the same ``hc_sync_project``. This module decides *when*
and nothing else.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

# Long enough that a burst of edits is one send, short enough that a reader
# who stops typing and looks at another window sees it land.
QUIET_SECONDS = 4.0
# A project changing without pause still goes up this often.
MAX_DIRTY_SECONDS = 30.0
# Network trouble: try again soon, and back off so an outage is not a flood.
RETRY_SCHEDULE = (5.0, 15.0, 45.0, 120.0)
# Not-signed-in, not-configured, migration-missing: real failures that no
# amount of retrying fixes in the next minute. Stay dirty, ask again rarely.
COLD_SECONDS = 300.0
# The pump never sleeps longer than this, so a change made while it waits on
# nothing in particular is still picked up promptly.
MAX_WAIT = 60.0

# Substrings of the messages supabase_client raises for trouble that passes
# on its own. Everything else is treated as cold -- the safe direction: a
# wrong guess here costs a slower retry, and the opposite wrong guess costs
# a request every five seconds for as long as the workspace is open.
_TRANSIENT_MARKERS = (
    "could not reach supabase",
    "supabase returned 5",
    "timed out",
    "temporarily",
    "connection reset",
    "try again",
)

_LOCK = threading.RLock()
_CV = threading.Condition(_LOCK)
_STATES: Dict[Tuple[str, str], "_Project"] = {}
_PUMP: Optional[threading.Thread] = None
_STOP = False

# Seams, so the tests can drive this with a clock they control and a sync
# that does not need a network. Nothing else reassigns them.
_now: Callable[[], float] = time.monotonic
_sync: Optional[Callable[[Any, str], Dict[str, Any]]] = None


class _Project:
    """One project's place in the schedule."""

    __slots__ = ("root", "cwd", "revision", "synced_revision", "dirty_since",
                 "last_change", "due_now", "in_flight", "attempt",
                 "next_attempt_at", "last_error", "last_ok_at", "last_reason",
                 "sends")

    def __init__(self, root, cwd: str) -> None:
        self.root = root
        self.cwd = cwd
        self.revision = 0
        self.synced_revision = 0
        self.dirty_since: Optional[float] = None
        self.last_change = 0.0
        self.due_now = False
        self.in_flight = False
        self.attempt = 0
        self.next_attempt_at = 0.0
        self.last_error = ""
        self.last_ok_at: Optional[float] = None
        self.last_reason = ""
        self.sends = 0

    @property
    def dirty(self) -> bool:
        return self.revision > self.synced_revision

    def due_at(self) -> Optional[float]:
        """When this project should next be sent, or None if it should not.

        The earlier of "changes have settled" and "it has been dirty long
        enough", never before a backoff that is still running.
        """
        if not self.dirty or self.in_flight:
            return None
        if self.due_now:
            return self.next_attempt_at
        quiet = self.last_change + QUIET_SECONDS
        ceiling = (self.dirty_since or self.last_change) + MAX_DIRTY_SECONDS
        return max(min(quiet, ceiling), self.next_attempt_at)


def _key(root, cwd: str) -> Tuple[str, str]:
    return (str(root) if root is not None else "", str(cwd))


def _state(root, cwd: str) -> Optional[_Project]:
    """The record for one project, made on first sight.

    A project with no directory is not a project: the personal workspace in
    global scope has goals but nowhere to put them, and marking it dirty
    would schedule a send that can only ever fail.
    """
    if not cwd:
        return None
    key = _key(root, cwd)
    state = _STATES.get(key)
    if state is None:
        state = _Project(root, str(cwd))
        _STATES[key] = state
    return state


def mark_dirty(root, cwd: str, reason: str = "") -> None:
    """Something worth sending changed. Cheap on purpose.

    Called from the middle of a save, on the request thread, possibly many
    times a second -- so it takes a lock, bumps two numbers and wakes the
    pump. No file is read here and no decision is made; deciding is the
    pump's job precisely so that this one stays free.
    """
    with _CV:
        state = _state(root, cwd)
        if state is None:
            return
        now = _now()
        if not state.dirty:
            state.dirty_since = now
        # A backoff that is running is deliberately NOT cleared here. During
        # an outage the reader keeps working, and letting each edit reset the
        # retry clock would turn the backoff into a request every few
        # seconds -- exactly the flood it exists to prevent. The change is
        # not lost; it goes out with the retry, which carries everything
        # since the last successful send anyway.
        state.revision += 1
        state.last_change = now
        if reason:
            state.last_reason = reason
        _ensure_pump()
        _CV.notify_all()


def flush_soon(root, cwd: str, reason: str = "") -> None:
    """A boundary: send as soon as the pump can, without the quiet wait.

    For the moments where waiting is wrong rather than merely slow -- a
    build that just finished, a project being handed over. It does not
    block: the caller is usually a build thread finishing up, and a network
    call is not that thread's business.
    """
    with _CV:
        state = _state(root, cwd)
        if state is None:
            return
        now = _now()
        if not state.dirty:
            # Nothing local changed, but the boundary is still worth
            # honouring: the last attempt may have failed, or a previous
            # process may have left the remote behind.
            state.revision += 1
            state.dirty_since = now
        state.last_change = now
        state.due_now = True
        state.next_attempt_at = min(state.next_attempt_at, now)
        if reason:
            state.last_reason = reason
        _ensure_pump()
        _CV.notify_all()


def note_external_sync(root, cwd: str) -> None:
    """Someone sent this project by other means -- the button, a script.

    The schedule believes it, because the payload that went is built from
    the same disk this one would have read. Without this the manual button
    would be followed four seconds later by an automatic send of exactly
    the same rows.
    """
    with _CV:
        state = _state(root, cwd)
        if state is None:
            return
        now = _now()
        state.synced_revision = state.revision
        state.dirty_since = None
        state.due_now = False
        state.attempt = 0
        state.next_attempt_at = 0.0
        state.last_error = ""
        state.last_ok_at = now
        _CV.notify_all()


def _classify(error: str) -> bool:
    """True when this failure is worth trying again shortly."""
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _send(state: _Project, boundary: bool = False) -> Tuple[bool, str]:
    """One sync, outside the lock. Returns ``(ok, error)``.

    Every failure is caught. This runs on a daemon thread that must outlive
    anything a misconfigured project can raise: a pump that dies takes
    autosave with it and says nothing.

    A *boundary* send asks for the file provenance as well. The ordinary
    scheduled send leaves that to its own slower clock -- goals are what a
    collaborator is reading, and walking the working tree every few seconds
    to keep file sizes fresh would cost far more than it is worth.
    """
    sync = _sync
    if sync is None:
        from . import supabase_client as SB
        if not SB.configured(state.root):
            return False, "Supabase is not configured"
        sync = SB.sync_project
    try:
        result = sync(state.root, state.cwd, **({"files": True}
                                                if boundary else {}))
    except Exception as exc:                             # noqa: BLE001
        return False, str(exc)[:200] or exc.__class__.__name__
    if isinstance(result, dict) and result.get("ok") is False:
        return False, str(result.get("error") or "sync refused")[:200]
    return True, ""


def _settle(state: _Project, sent_revision: int, ok: bool, error: str,
            now: float) -> None:
    """Fold one attempt's outcome back into the schedule."""
    state.in_flight = False
    if ok:
        state.sends += 1
        state.attempt = 0
        state.last_error = ""
        state.last_ok_at = now
        state.due_now = False
        state.next_attempt_at = 0.0
        # What went up is what the payload was built from. A change that
        # landed while it was in flight leaves revision ahead, and the
        # project simply stays dirty -- with its ceiling measured from this
        # send rather than from the change that started it all.
        state.synced_revision = sent_revision
        state.dirty_since = now if state.dirty else None
        return
    state.last_error = error
    index = min(state.attempt, len(RETRY_SCHEDULE) - 1)
    delay = RETRY_SCHEDULE[index] if _classify(error) else COLD_SECONDS
    state.attempt += 1
    state.next_attempt_at = now + delay
    # Deliberately no change to synced_revision: the project is still
    # dirty, and the next attempt carries this change along with anything
    # that has happened since.


def _due(now: float) -> Optional[_Project]:
    ready = [s for s in _STATES.values()
             if (due := s.due_at()) is not None and due <= now]
    # Oldest dirt first, so a project that has been waiting cannot be
    # starved by one that keeps being touched.
    return min(ready, key=lambda s: s.dirty_since or 0.0) if ready else None


def _sleep_for(now: float) -> float:
    deadlines = [due for due in (s.due_at() for s in _STATES.values())
                 if due is not None]
    if not deadlines:
        return MAX_WAIT
    return max(0.0, min(MAX_WAIT, min(deadlines) - now))


def _pump_loop() -> None:
    while True:
        with _CV:
            if _STOP:
                return
            now = _now()
            state = _due(now)
            if state is None:
                _CV.wait(_sleep_for(now))
                continue
            state.in_flight = True
            boundary = state.due_now
            state.due_now = False
            sent_revision = state.revision
        ok, error = _send(state, boundary)
        with _CV:
            _settle(state, sent_revision, ok, error, _now())
            _CV.notify_all()


def _ensure_pump() -> None:
    """Start the pump on first use, and never a second one.

    Called with the lock held. A workspace that never changes anything
    never starts a thread at all.
    """
    global _PUMP, _STOP
    if _PUMP is not None and _PUMP.is_alive():
        return
    _STOP = False
    _PUMP = threading.Thread(target=_pump_loop, name="hc-project-autosync",
                             daemon=True)
    _PUMP.start()


def drain(timeout: float = 8.0) -> Dict[str, Any]:
    """Send everything still dirty, now, and stop.

    For the close of a workspace, where the pump's schedule is no longer
    something there is time for. The pump is stopped first so that this
    thread is the only one sending -- the no-overlap promise holds through
    shutdown as much as during it -- and each project gets one attempt,
    because a process on its way out is not the place for a retry loop.
    """
    stop(join=timeout / 2 if timeout else 0.0)
    deadline = _now() + max(0.0, timeout)
    sent, failed = [], []
    with _CV:
        pending = [s for s in _STATES.values() if s.dirty]
    for state in pending:
        if _now() >= deadline:
            break
        with _CV:
            if state.in_flight:
                continue
            state.in_flight = True
            sent_revision = state.revision
        # Closing is a boundary: whatever else this send is, it is the last
        # one, so it carries everything.
        ok, error = _send(state, boundary=True)
        with _CV:
            _settle(state, sent_revision, ok, error, _now())
        (sent if ok else failed).append(state.cwd)
    return {"ok": not failed, "sent": sent, "failed": failed}


def stop(join: float = 1.0) -> None:
    """Stop the pump. Dirt is kept: the schedule ends, the state does not."""
    global _STOP, _PUMP
    with _CV:
        _STOP = True
        _CV.notify_all()
        pump = _PUMP
    if pump is not None and join and pump is not threading.current_thread():
        pump.join(timeout=join)
    with _CV:
        if _PUMP is pump:
            _PUMP = None


def status(root=None, cwd: str = "") -> Dict[str, Any]:
    """What the schedule thinks, for the pane and for the tests."""
    with _CV:
        if cwd:
            state = _STATES.get(_key(root, cwd))
            states = [state] if state else []
        else:
            states = list(_STATES.values())
        now = _now()
        return {"projects": [{
            "cwd": s.cwd,
            "dirty": s.dirty,
            "in_flight": s.in_flight,
            "revision": s.revision,
            "synced_revision": s.synced_revision,
            "sends": s.sends,
            "attempt": s.attempt,
            "due_in": (None if s.due_at() is None
                       else max(0.0, s.due_at() - now)),
            "last_error": s.last_error,
            "last_reason": s.last_reason,
        } for s in states]}


def reset() -> None:
    """Forget every project. Tests only -- nothing in the workspace calls it."""
    stop(join=1.0)
    with _CV:
        _STATES.clear()


def project_of(session_id: str, root=None) -> str:
    """The directory a chat works in, for callers that hold only a session.

    The same answer the workspace's own header gives: what the chat was
    bound to, else the directory it was started in.
    """
    import json

    from . import chat_state as CS
    try:
        bound = CS.bound_project(str(session_id), root)
        if bound:
            return bound
    except (OSError, ValueError, TypeError):
        pass
    # Read from the file rather than through load_manifest, for the same
    # reason the workspace's own header does: that loader hands back a blank
    # default when the manifest's session_id disagrees with the directory it
    # sits in, which a seeded or copied workspace's does -- and the directory
    # a chat was started in is still the directory it was started in.
    try:
        path = CS.paths(str(session_id), root).manifest
        value = json.loads(path.read_text(encoding="utf-8"))
        return str(value.get("cwd") or "") if isinstance(value, dict) else ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
