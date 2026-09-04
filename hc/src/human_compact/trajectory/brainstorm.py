"""Brainstorming: setup's conversation, reopened inside a project that exists.

Setup is the conversation for someone who has nothing. This is the same
conversation for someone who already has a tree and wants to think out loud
against it -- what to do next, what the work actually is, which of three
readings of a half-formed idea is the one worth writing down.

Two things make it a different screen rather than a second copy of setup.

The first is that there is no order. Setup steps a stranger through four
cards because the reader has never seen the tool and a plan written from one
sentence is a guess; here the reader already has goals on screen, so the
model may ask a question, propose nothing, or go straight to rows, and which
of those is right is a judgement about the conversation rather than a step
count. What setup enforces with ``stage_of``, this leaves alone.

The second is that nothing is written *into the tree* until it is asked for.
A brainstorm that quietly appended goals to a tree the reader has been
keeping by hand would be the opposite of thinking out loud, so the model may
only OFFER -- "I think I have enough to write the goals, shall I?" -- and the
reader answers yes, or answers no and says what is missing. The conversation
itself is another matter: it is written down after every round, beside the
goals it argues with, by ``chat_state.save_brainstorm``. Losing an hour of
thinking to a closed tab is not restraint.

Everything the cards are made of is setup's: the four question shapes, the
coercion of each, the caps. They are imported rather than restated, so the
two screens cannot drift into asking questions in two different ways.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from . import goals as GM
from . import setup_chat as SC

# Which cards this screen draws. `focus` and `offer` are its own; the rest
# are setup's, in the same shapes, so the modal that draws a question here
# is the modal that draws one there.
CARDS = ("questions", "focus", "goals", "todos", "offer", "none")
MODES = ("explore", "discriminate", "deepen", "commit")

# What may be offered. Anything else the model names is not an offer.
OFFERS = ("goals", "todos")

MAX_GOALS = 6
MAX_SUBGOALS = 6
MAX_FOCUS_OPTIONS = 6
MAX_WORKING_ROWS = 5

# The project, as much of it as is worth carrying. A tree of forty goals
# with every row under each is most of a context window and none of it is
# read closely; what a brainstorm needs is the shape of the work and where
# it has got to.
MAX_CTX_GOALS = 40
MAX_CTX_ROWS = 6
MAX_CTX_CHARS = 6000

# Over this, the digest is condensed once by the model and the condensation
# is what every later brainstorm -- in this chat and in any other chat of
# the same project -- is given instead. Under it, the digest is already
# short enough that condensing would cost more than it saves.
CONDENSE_OVER = 2400
MAX_CONDENSED = 1600

# How the reader's yes and no are put back into the transcript. Written as
# their turn rather than as a flag, because the next call reads the
# conversation and an offer accepted is a thing they said.
YES = "Yes -- generate them."
NO = "Not yet."


FORM = [
    "You are brainstorming with someone inside a project they already have.",
    "Their goals and TODO rows are below. They are thinking out loud: they",
    "want to work out what the next piece of work actually is, not to be",
    "walked through a setup.",
    "",
    "Reply with ONE JSON object and nothing else:",
    "",
    '  {"say": "<what you say to them, plain prose>",',
    '   "card": "questions" | "focus" | "goals" | "todos" | "offer"',
    '           | "none",',
    '   "questions": {"eyebrow": "<two or three words>",',
    '                 "items": [{"id": "<short slug>",',
    '                            "type": "mcq" | "select_all" | "free"',
    '                                    | "open",',
    '                            "title": "<the question>",',
    '                            "subtitle": "<optional, e.g. pick any>",',
    '                            "options": [{"label": "<the choice>",',
    '                                        "why": "<optional: what it',
    '                                                buys them>"}],',
    '                            "placeholder": "<free and open>"}]},',
    '   "focus": {"title": "<what you are asking them to choose between>",',
    '             "options": [{"label": "<one reading of the work>",',
    '                          "why": "<why this one>"}]},',
    '   "goals": [{"label": "<an outcome, not a task>",',
    '              "why": "<why this one is worth having>",',
    '              "subgoals": ["<a piece of it>"]}],',
    '   "subgoals": [{"label": "<a piece of the work>",',
    '                 "todos": ["<one row of work in that piece>"]}],',
    '   "todos": ["<or, where it does not break down, just the rows>"],',
    '   "offer": "goals" | "todos",',
    '   "mode": "explore" | "discriminate" | "deepen" | "commit",',
    '   "working_direction": {',
    '     "title": "<the direction taking shape, or empty while exploring>",',
    '     "summary": "<the concrete capability, user, input and output>",',
    '     "why": ["<evidence this fits the project and person>"],',
    '     "unclear": ["<a consequential behavioral question still open>"],',
    '     "alternatives": [{"label": "<a route considered>",',
    '                       "reason": "<why it is inactive or deferred>"}]} }',
    "",
    "Only the key for the card you name is read; leave the others out.",
    "",
    "There is NO fixed order here. Ask a question, say nothing but prose,",
    "or go straight to rows -- whichever the conversation actually calls",
    "for. Do not walk them through a sequence they did not ask for.",
    "",
    "The conversation DOES change mode: Explore -> Discriminate -> Deepen ->",
    "Commit. Explore only long enough to expose genuinely different routes.",
    "Discriminate when their preferences separate them. Deepen once one route",
    "has a center of gravity. Commit when its user, behavior, input, output,",
    "first visible result, and locus of problem solving are clear enough to",
    "write down. Return the current `mode` and `working_direction` every turn.",
    "",
    "Evidence of convergence includes returning to one idea, preferring it,",
    "elaborating its behavior or implementation, rejecting neighbors, asking",
    "narrower questions, or finding an asset that makes it tractable. Once",
    "several signals accumulate, stop inventing unrelated directions by",
    "default. Say what the center of gravity is and WHY it coheres with their",
    "goals, available assets, prior work, and where they must make consequential",
    "decisions. That is grounded assurance, not praise. Reopen exploration only",
    "when they ask or evidence undermines the direction.",
    "",
    "Questions narrow with the mode. Explore distinguishes different projects.",
    "Discriminate distinguishes versions of one project. Deepen and Commit use",
    "behavioral scenarios: who acts, what goes in, what they see, edge cases,",
    "and what behavior would make the result wrong. Late divergence branches",
    "within the idea through what-if scenarios, not away from it.",
    "",
    "Keep rejected or deferred routes in `alternatives`; do not resurface them",
    "as live siblings. New research normally sharpens the working direction",
    "instead of reopening the space. Judge difficulty by the locus of problem",
    "solving, not the sophistication of infrastructure a library or agent can",
    "carry. What reaches goals or TODOs must crystallize the visible working",
    "direction, never surprise them with a clever replacement project.",
    "",
    "Questions are for what changes what you would propose, never for what",
    "you could assume. Pick the shape by what you are asking for:",
    "",
    "  mcq         one answer out of several you can name",
    "  select_all  any number of them -- say so in the subtitle",
    "  free        one line they have to write; give a placeholder",
    "  open        a paragraph: the story, the constraint nobody wrote down",
    "",
    "An option may carry a `why`. Use it when the options are proposals of",
    "yours rather than facts of theirs, so they are choosing between",
    "arguments instead of guessing what you meant.",
    "",
    "`focus` is for when the work could be read two or three ways and which",
    "one they mean decides everything after it. They pick one and may add a",
    "line of their own; use both.",
    "",
    "Nothing you write is saved unless they ask for it. So when you think",
    "you have enough to write the goals or the rows, do NOT write them --",
    "send an `offer` card naming which, and say in one sentence what you",
    "would write. They answer yes, and you write it on the next reply; or",
    "they answer no and tell you what you are missing.",
    "",
    "A goal is an outcome someone could tell you they had reached. A TODO",
    "row is one piece of work, in the imperative, that a coding agent could",
    "pick up and finish. Neither is a phase, a heading or a category.",
    "",
    "The project already has goals. Do not propose one it already has, and",
    "do not restate its tree back at them -- they can see it.",
]


# --- the project, as the model sees it ---------------------------------------

def _status(value) -> str:
    said = SC._one(value, 20)
    return said or "active"


def digest(goals, project=None) -> str:
    """The project's goals and rows, flattened into something readable.

    The tree, its statuses, and how far the rows under each goal have got.
    Rows are counted before they are listed: "3 done, 2 building, 4 not
    sent" is most of what a brainstorm needs to know about a goal, and the
    handful of rows after it is the detail.
    """
    project = project if isinstance(project, dict) else {}
    rows = [g for g in (goals or {}).get("goals") or [] if isinstance(g, dict)]
    lines: List[str] = ["# The project", ""]
    for key in ("name", "objective", "description"):
        said = SC._long(project.get(key), 600)
        if said:
            lines += ["%s: %s" % (key, said)]
    lines += ["", "# Its goals", ""]
    if not rows:
        lines += ["(none yet)"]
    kids: Dict[str, List[Dict[str, Any]]] = {}
    for goal in rows:
        kids.setdefault(str(goal.get("parent_goal_id") or ""), []).append(goal)
    seen = 0

    def emit(parent: str, depth: int) -> None:
        nonlocal seen
        for goal in kids.get(parent, []):
            if seen >= MAX_CTX_GOALS:
                return
            seen += 1
            lines.append("%s- [%s] %s" % ("  " * depth, _status(goal.get("status")),
                                          SC._one(goal.get("title"), SC.MAX_LABEL)))
            items = [r for r in goal.get("todo_items") or [] if isinstance(r, dict)]
            if items:
                tally: Dict[str, int] = {}
                for row in items:
                    key = (_status(row.get("status")) if row.get("status")
                           else "not sent")
                    tally[key] = tally.get(key, 0) + 1
                lines.append("%s  rows: %s" % (
                    "  " * depth,
                    ", ".join("%d %s" % (n, k) for k, n in sorted(tally.items()))))
                for row in items[:MAX_CTX_ROWS]:
                    lines.append("%s  · %s" % (
                        "  " * depth, SC._one(row.get("text"), SC.MAX_TODO)))
                if len(items) > MAX_CTX_ROWS:
                    lines.append("%s  · … %d more"
                                 % ("  " * depth, len(items) - MAX_CTX_ROWS))
            emit(str(goal.get("id") or ""), depth + 1)

    emit("", 0)
    return "\n".join(lines)[:MAX_CTX_CHARS]


CONDENSE = [
    "Below is a summary of somebody's project: its goals, how far each has",
    "got, and the work under them. It is about to be given to you again, at",
    "the top of every turn of a brainstorming conversation with them, so it",
    "needs to be shorter.",
    "",
    "Write the shortest thing that would still let you brainstorm about this",
    "project well: what it is, what is done, what is in flight, and what is",
    "untouched. Keep the goals' own words where they carry meaning. Drop",
    "individual TODO rows unless one of them is the whole of a goal.",
    "",
    "Reply with ONE JSON object and nothing else:",
    "",
    '  {"context": "<the shortened summary, plain text with newlines>"}',
]


def _cache_path(root, cwd) -> Path:
    from . import project_store as PS
    path = PS.project_path(root, cwd)
    return path.with_name(path.stem + ".brainstorm.json")


def _key(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def load_cache(root, cwd) -> Dict[str, Any]:
    try:
        value = json.loads(_cache_path(root, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_cache(root, cwd, key: str, text: str) -> bool:
    path = _cache_path(root, cwd)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "context": text,
                                    "at": int(time.time())}),
                        encoding="utf-8")
    except OSError:
        # A cache that cannot be written is a cache miss next time, which
        # costs tokens and nothing else. Not worth failing a brainstorm for.
        return False
    return True


def project_context(root, cwd, raw, engine=None) -> str:
    """The project, short enough to send on every turn of every brainstorm.

    A tree that renders under ``CONDENSE_OVER`` is already short: it is sent
    as it stands. A larger one is condensed ONCE, by the same model on the
    same account, and the condensation is written beside the project record
    -- keyed by a digest of the tree it was made from, so it is reused by
    every later brainstorm of this project until the tree actually changes,
    and thrown away the moment it does.

    Never fatal. A model that cannot be reached, or that answers with
    nothing, leaves the reader with the raw digest truncated rather than
    with no brainstorm at all.
    """
    raw = str(raw or "")
    if len(raw) <= CONDENSE_OVER or not cwd:
        return raw
    key = _key(raw)
    held = load_cache(root, cwd)
    if held.get("key") == key and SC._long(held.get("context"), MAX_CONDENSED):
        return SC._long(held.get("context"), MAX_CONDENSED)
    from . import providers as PROVIDERS
    try:
        engine = engine or PROVIDERS.make(
            os.environ.get("HC_CHAT_PROVIDER", "claude"), "synthesize",
            SC.setup_model(root), timeout=SC.SETUP_TIMEOUT_SECONDS)
        got = engine.generate_json(
            "\n".join(CONDENSE) + "\n\n" + raw + "\n")
    except Exception:                                    # noqa: BLE001
        return raw[:CONDENSE_OVER]
    said = ""
    if isinstance(got, dict):
        said = SC._long(got.get("context") or got.get("say"), MAX_CONDENSED)
    if not said:
        return raw[:CONDENSE_OVER]
    save_cache(root, cwd, key, said)
    return said


# --- what comes back ---------------------------------------------------------

def _goals_with_kids(value) -> List[Dict[str, Any]]:
    """Goals, each carrying the pieces it breaks into.

    Setup has two readers of this shape -- one for the goals card, which
    drops subgoals, and one for the focus options, which keeps them but
    stops at three. A brainstorm wants both halves and neither cap, so it
    reads the shape here and borrows the caps from setup.
    """
    out = []
    for row in value if isinstance(value, list) else []:
        if isinstance(row, dict):
            label = SC._one(row.get("label") or row.get("title"), SC.MAX_LABEL)
            why = SC._one(row.get("why"), SC.MAX_WHY)
            kids = []
            for kid in row.get("subgoals") or []:
                said = SC._one(kid.get("label") if isinstance(kid, dict) else kid,
                               SC.MAX_LABEL)
                if said:
                    kids.append({"label": said})
                if len(kids) >= MAX_SUBGOALS:
                    break
        elif isinstance(row, str):
            label, why, kids = SC._one(row, SC.MAX_LABEL), "", []
        else:
            continue
        if not label:
            continue
        out.append({"label": label, "why": why, "subgoals": kids})
        if len(out) >= MAX_GOALS:
            break
    return out


def _normalize_focus(value) -> Dict[str, Any]:
    """The choice, and what is being chosen between.

    One option is not a choice: a focus card with a single reading on it is
    the model telling the reader what it decided, which is what prose is
    for. Refused back to `none` rather than drawn.
    """
    value = value if isinstance(value, dict) else {}
    if isinstance(value.get("options"), list):
        options = SC._candidates(value.get("options"))
    else:
        options = SC._candidates(value)
    return {"title": SC._one(value.get("title"), SC.MAX_TITLE)
                     or "What should we focus on?",
            "options": options[:MAX_FOCUS_OPTIONS]}


def normalize_working_direction(value) -> Dict[str, Any]:
    """The project hypothesis the conversation edits in public."""
    value = value if isinstance(value, dict) else {}
    out = {
        "title": SC._one(value.get("title"), SC.MAX_LABEL),
        "summary": SC._long(value.get("summary"), 600),
        "why": [], "unclear": [], "alternatives": [],
    }
    for key in ("why", "unclear"):
        for row in value.get(key) if isinstance(value.get(key), list) else []:
            said = SC._one(row, 280)
            if said:
                out[key].append(said)
            if len(out[key]) >= MAX_WORKING_ROWS:
                break
    for row in value.get("alternatives") if isinstance(value.get("alternatives"), list) else []:
        if not isinstance(row, dict):
            continue
        label = SC._one(row.get("label"), SC.MAX_LABEL)
        if label:
            out["alternatives"].append({
                "label": label, "reason": SC._one(row.get("reason"), 280)})
        if len(out["alternatives"]) >= MAX_WORKING_ROWS:
            break
    return out


def has_working_direction(value) -> bool:
    held = normalize_working_direction(value)
    return bool(held["title"] or held["summary"] or held["why"]
                or held["unclear"] or held["alternatives"])


def _named(value) -> Dict[str, Any]:
    """Whatever came back, with the envelope put back around it.

    The same problem setup has: told to send one card, the model sometimes
    sends the payload at the top level with no `card` naming it. Read by
    shape rather than by hope -- there is no due card here to guess toward,
    so the keys that are present are the whole of the evidence.
    """
    if isinstance(value, list):
        rows = [r for r in value if isinstance(r, dict)]
        if rows and any("label" in r or "title" in r for r in rows):
            return {"card": "goals", "goals": value}
        return {"card": "todos", "todos": value}
    if not isinstance(value, dict):
        return {}
    if value.get("card"):
        return value
    for name in ("questions", "focus", "goals", "todos", "subgoals", "offer"):
        if value.get(name):
            return dict(value, card="todos" if name == "subgoals" else name)
    if "items" in value:
        return {"card": "questions", "questions": value}
    return value


def normalize_card(value) -> Dict[str, Any]:
    """Whatever came back, as the one shape the panel draws.

    A card the reader cannot act on is not a card: an empty question set, a
    focus with one option on it, an offer of something that is not goals or
    rows -- all come back as ``none``, so the reply stands as prose rather
    than opening an empty box under it.
    """
    value = _named(value)
    value = value if isinstance(value, dict) else {}
    card = str(value.get("card") or "").strip().lower()
    if card not in CARDS:
        card = "none"
    out: Dict[str, Any] = {
        "say": SC._one(value.get("say"), SC.MAX_SAY), "card": card,
        "questions": {"eyebrow": "", "items": []},
        "focus": {"title": "", "options": []},
        "goals": [], "todos": [], "subgoals": [], "offer": "",
        "mode": (str(value.get("mode") or "").strip().lower()
                 if str(value.get("mode") or "").strip().lower() in MODES
                 else "explore"),
        "working_direction": normalize_working_direction(
            value.get("working_direction"))}
    if card == "questions":
        held = SC._normalize_questions(value.get("questions"))
        out["questions"] = held
        if not held["items"]:
            out["card"] = "none"
    elif card == "focus":
        held = _normalize_focus(value.get("focus"))
        out["focus"] = held
        if len(held["options"]) < 2:
            out["card"] = "none"
    elif card == "goals":
        out["goals"] = _goals_with_kids(value.get("goals"))
        if not out["goals"]:
            out["card"] = "none"
    elif card == "todos":
        out["subgoals"] = SC._normalize_subgoals(value.get("subgoals"))
        out["todos"] = SC._normalize_todos(value.get("todos"))
        if not out["subgoals"] and not out["todos"]:
            out["card"] = "none"
    elif card == "offer":
        said = str(value.get("offer") or "").strip().lower()
        out["offer"] = said if said in OFFERS else ""
        if not out["offer"]:
            out["card"] = "none"
    if not out["say"] and out["card"] == "none":
        out["say"] = ""
    return out


# --- asking ------------------------------------------------------------------

def compose(transcript, context="", extra=()) -> List[str]:
    """The prompt: the form, the project, then what has been said.

    Bounded from the oldest end, like every other read of a conversation
    here. The project goes above the transcript rather than inside it: it is
    the ground the whole conversation stands on, not a turn in it.
    """
    lines = list(FORM)
    said = str(context or "").strip()
    if said:
        lines += ["", said]
    lines += ["", "# The conversation so far", ""]
    rows = [r for r in (transcript or []) if isinstance(r, dict)]
    for row in rows[-SC.MAX_TURNS:]:
        who = "them" if str(row.get("role") or "") == "you" else "you"
        text = str(row.get("text") or "").strip()[:SC.MAX_TURN_TEXT]
        if text:
            lines += ["%s: %s" % (who, text), ""]
    lines += list(extra)
    return lines


def ask(transcript, context="", engine=None, root=None, extra=(),
        mode="", working_direction=None) -> Dict[str, Any]:
    """One round: the conversation and the project out, one card back.

    No stage check and no discard. Setup refuses a card out of turn because
    its four steps are what stop a plan being written from one sentence;
    here the reader has the tree in front of them and the model's judgement
    about what to send is the thing being asked for.
    """
    from . import providers as PROVIDERS
    try:
        engine = engine or PROVIDERS.make(
            os.environ.get("HC_CHAT_PROVIDER", "claude"), "synthesize",
            SC.setup_model(root), timeout=SC.SETUP_TIMEOUT_SECONDS)
        current = normalize_working_direction(working_direction)
        state = []
        if has_working_direction(current):
            state = ["", "# The working direction visible now", "",
                     "Mode: " + (mode if mode in MODES else "explore"),
                     json.dumps(current, ensure_ascii=False)]
        raw = engine.generate_json(
            "\n".join(compose(transcript, context, tuple(state) + tuple(extra))) + "\n")
    except PROVIDERS.ProviderError as exc:
        return {"ok": False,
                "error": " ".join(str(exc).split())[:200] + SC.credit_note()}
    except Exception as exc:                             # noqa: BLE001
        return SC.unexpected(exc)
    card = normalize_card(raw)
    # A malformed or older model reply must not erase the project hypothesis
    # already visible to the reader. It may revise it, never lose it by omission.
    if not has_working_direction(card.get("working_direction")) and has_working_direction(working_direction):
        card["working_direction"] = normalize_working_direction(working_direction)
    raw_mode = raw.get("mode") if isinstance(raw, dict) else None
    if card.get("mode") == "explore" and mode in MODES and not raw_mode:
        card["mode"] = mode
    if not card["say"] and card["card"] == "none":
        return {"ok": False, "error": "the model answered with nothing"}
    return dict(card, ok=True)


# --- writing what they approved into the tree they already have --------------

def _child(doc, parent, title) -> Dict[str, Any]:
    goal = GM.new_goal(GM.child_goal_id(doc, parent), SC._one(title, SC.MAX_LABEL),
                       parent, origin="user")
    doc["goals"].append(goal)
    return goal


def apply_goals(doc, proposals) -> int:
    """The approved goals, appended to the tree as the reader's own.

    Root goals, with their pieces under them, marked ``origin="user"``:
    the reader read each one and said yes, which is exactly what that
    origin means everywhere else in this workspace.
    """
    made = 0
    for row in _goals_with_kids(proposals):
        goal = GM.new_goal(GM.next_goal_id(doc), row["label"], None,
                           origin="user")
        goal["description"] = row["why"]
        doc["goals"].append(goal)
        made += 1
        for kid in row["subgoals"]:
            _child(doc, goal["id"], kid["label"])
            made += 1
    return made


def apply_todos(doc, goal_id, todos, subgoals=()) -> int:
    """The approved rows, hung on a goal that already exists.

    Where the work broke into pieces they become child goals of it, each
    carrying its own rows -- the shape the rail already draws. Flat rows go
    straight onto the goal named. Rows arrive with no status, which is what
    the rail means by "not sent to a build yet": approving a brainstorm
    writes work down, it does not start any.
    """
    goal = GM.by_id(doc, goal_id)
    if goal is None:
        return 0
    made = 0
    for piece in SC._normalize_subgoals(subgoals):
        kid = _child(doc, goal["id"], piece["label"])
        for text in piece["todos"]:
            if GM.add_todo_row(kid, text) is not None:
                made += 1
        kid["todos_md"] = GM.render_todos(kid.get("todo_items") or [])
    for text in SC._normalize_todos(todos):
        if GM.add_todo_row(goal, text) is not None:
            made += 1
    goal["todos_md"] = GM.render_todos(goal.get("todo_items") or [])
    return made
