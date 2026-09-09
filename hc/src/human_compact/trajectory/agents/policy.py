"""The trigger policy: which events are meaningful transitions.

The Overseer is asked on a meaningful transition and on nothing else.
That is a static list, not a judgement call, so it can be read here and
tested; and the event log is complete either way -- a minor event is
recorded, it just goes no further.

Also here: reading the reader's words for the two intents that change
which agent answers a Bart message. They are conservative on purpose. A
message that merely mentions an option is a message for Chat; one that
asks for options is a request to brainstorm.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# --- event types -------------------------------------------------------------

# What the reader does.
BART_MESSAGE = "bart.message"
BUILD_REQUESTED = "todo.build_requested"
PLAN_REQUESTED = "plan.requested"
TODO_ADDED = "todo.added"
TODO_TEXT_EDITED = "todo.text_edited"
TODO_DONE_TOGGLED = "todo.done_toggled"
TODO_REMOVED = "todo.removed"
GOAL_ADDED = "goal.added"
NOTES_EDITED = "notes.edited"
CHAT_SAVED = "chat.saved"
PREVIEW_OP = "preview.op"

# What the machinery reports.
BUILD_STARTED = "build.started"
BUILD_COMPLETED = "build.completed"
BUILD_FAILED = "build.failed"
BUILD_QUESTION = "build.question"
VERIFY_STARTED = "verify.started"
VERIFY_PASSED = "verify.passed"
VERIFY_FAILED = "verify.failed"
VERIFY_ESCALATED = "verify.escalated"
DISCOVERED = "discover.done"
CONTEXT_UPDATED = "context.updated"
OVERSEER_ROUTED = "overseer.routed"

# What an agent said or found.
CHAT_REPLIED = "chat.replied"
CHAT_NEEDS_HUMAN = "chat.needs_human"
CHAT_NEEDS_DISCOVERY = "chat.needs_discovery"
BRAINSTORM_REPLIED = "brainstorm.replied"
PATH_PLANNED = "path.planned"
REPAIR_REQUESTED = "build.repair_requested"

# The transitions the Overseer is asked about. Everything else is recorded
# and left alone.
MEANINGFUL = frozenset({
    BART_MESSAGE, BUILD_REQUESTED, PLAN_REQUESTED,
    BUILD_COMPLETED, BUILD_FAILED, BUILD_QUESTION,
    VERIFY_PASSED, VERIFY_FAILED,
    CHAT_NEEDS_HUMAN, CHAT_NEEDS_DISCOVERY,
})


def is_meaningful(event: Dict[str, Any]) -> bool:
    return isinstance(event, dict) and event.get("type") in MEANINGFUL


# The goal page's operations, as the events they are. build_todos is the
# one that routes; it goes through the orchestrator rather than being
# noted after the fact (see ui._apply_goal_page_op).
OP_EVENTS = {
    "add_goal": GOAL_ADDED,
    "set_notes": NOTES_EDITED,
    "add_todo_row": TODO_ADDED,
    "insert_todo_row": TODO_ADDED,
    "set_todo_text": TODO_TEXT_EDITED,
    "set_todo_done": TODO_DONE_TOGGLED,
    "remove_todo_row": TODO_REMOVED,
    "build_todos": BUILD_REQUESTED,
}


def op_event(op: str) -> Optional[str]:
    return OP_EVENTS.get(str(op or ""))


# --- reading the reader ------------------------------------------------------

_BRAINSTORM = re.compile(
    r"(?i)\b(brainstorm\w*|what are (my|the|some) (options|choices|alternatives)"
    r"|(give|show|list) me (some |a few |the )?(options|alternatives|ideas|approaches)"
    r"|(other|different|alternative) (ways|approaches|options)"
    r"|weigh (the )?(options|alternatives|trade-?offs)"
    r"|which (way|approach|option) (should|would)"
    r"|help me (choose|decide|pick))\b")

_PLAN = re.compile(
    r"(?i)\b(plan (this|it|the (work|piece|steps)) out|break (this|it) (down|into)"
    r"|what are the steps|lay out the (steps|rows|work|plan)"
    r"|help me plan|plan (this|the project|our next steps)|let['’]?s plan"
    r"|(write|draft|propose) (the |a )?(plan|todo rows|rows|steps) for)\b")


def wants_brainstorm(text: str) -> bool:
    """The reader asked, in so many words, for options or ideas."""
    return bool(_BRAINSTORM.search(str(text or "")))


def wants_plan(text: str) -> bool:
    """The reader asked for the piece to be planned or broken down."""
    return bool(_PLAN.search(str(text or "")))


def last_user_text(transcript) -> str:
    """The reader's most recent turn in a page transcript."""
    for turn in reversed(list(transcript or [])):
        if isinstance(turn, dict) and str(turn.get("role") or "") in ("you", "user"):
            return str(turn.get("text") or "")
    return ""


def bart_intent(text: str) -> Tuple[str, str]:
    """Where a Bart message goes by its words alone: (action, reason)."""
    if wants_brainstorm(text):
        return "brainstorm", "the reader asked for options"
    if wants_plan(text):
        return "replan", "the reader asked for the piece to be planned"
    return "chat", "an ordinary message: answered, not brainstormed"
