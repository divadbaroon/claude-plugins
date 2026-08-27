"""Setup: the conversation that stands where the blank goals screen was.

A chat opened cold -- `npx engelbart-cli`, then `/bart`, with nothing said
in it yet -- has no transcript to read a goal out of. The workspace used to
open on an empty tree, which is indistinguishable from a project with
nothing in it, and said nothing about writing one. Setup is what opens
instead: the reader describes the work in their own words, answers a couple
of rounds of questions, approves a plan, picks what to start on, and edits
the TODO rows before anything is saved.

Nothing here decides *what* to ask -- the model does, on the reader's own
Claude subscription, through the ordinary provider. This module is the
contract around it: the prompt that says what may come back, the coercion
of whatever does, and the shape the approved conversation takes as goals.

Every value on the way in is model output on its way into the reader's own
document, so all of it is bounded and none of it is trusted.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List

from . import goals as GM

# The one thing the reader has to know at a blank screen: describing it
# badly is enough to start.
OPEN = ("Tell me what you're working on in your own words. I'll ask a few"
        " questions, then write up a plan for you to approve.")

# What the reader is told when they answered nothing at all -- said as their
# turn rather than dropped, because the model's next card should know the
# question went unanswered rather than never having been put.
SKIPPED = "skip -- decide for me"

MAX_SAY = 1200
MAX_TITLE = 200
MAX_LABEL = 200
MAX_WHY = 300
MAX_LINE_KEY = 40
MAX_LINE_VALUE = 600
MAX_TODO = 300
MAX_TURNS = 40          # of transcript carried into the next call
MAX_TURN_TEXT = 2000
MAX_QUESTIONS = 6
MAX_OPTIONS = 8
MAX_PLAN_LINES = 8
MAX_GOALS = 8
MAX_TODOS = 20

CARDS = ("questions", "plan", "goals", "todos", "none")
KINDS = ("radio", "check", "text")


# --- the prompt --------------------------------------------------------------
#
# One call answers the whole of "what happens next": what to say, and which
# card -- if any -- to put under it. Asking for the card by name rather than
# inferring it from the prose keeps the rail out of the business of reading
# the model's mind, and lets a reply that is only prose be exactly that.

FORM = [
    "You are setting up a new project in Engelbart with someone who has just",
    "installed it. They have written nothing down yet. Your job is to end up",
    "with a plan they approve, one goal they want to start on, and the TODO",
    "rows for it -- and to get there in as few rounds as the work allows.",
    "",
    "Reply with ONE JSON object and nothing else:",
    "",
    '  {"say": "<what you say to them, plain prose>",',
    '   "card": "questions" | "plan" | "goals" | "todos" | "none",',
    '   "questions": {"eyebrow": "<two or three words>",',
    '                 "items": [{"id": "<short slug>",',
    '                            "type": "radio" | "check" | "text",',
    '                            "title": "<the question>",',
    '                            "subtitle": "<optional, e.g. pick any>",',
    '                            "options": ["<for radio and check>"],',
    '                            "placeholder": "<for text>"}]},',
    '   "plan": {"head": "<one line: what this project is>",',
    '            "lines": [{"k": "the work", "v": "..."},',
    '                      {"k": "in place", "v": "..."},',
    '                      {"k": "constraint", "v": "..."},',
    '                      {"k": "done means", "v": "..."}]},',
    '   "goals": [{"label": "<an outcome, not a task>",',
    '              "why": "<why this one is worth starting on>"}],',
    '   "todos": ["<one row of work>"]}',
    "",
    "Only the key for the card you name is read; leave the others out.",
    "",
    "The order this normally goes in: questions, questions again if the",
    "answers opened something, then plan, then goals, then todos. Do not",
    "skip ahead to a plan you cannot write from what they have told you, and",
    "do not ask a third round of questions to avoid writing one.",
    "",
    "Questions are for what changes your proposal, never for what you could",
    "assume. Offer radio and check options where the answers are known and a",
    "text box where they are not. Three questions is a lot; two is usually",
    "enough.",
    "",
    "A goal is an outcome someone could tell you they had reached. A TODO row",
    "is one piece of work, in the imperative, that a coding agent could pick",
    "up and finish. Neither is a phase, a heading or a category.",
    "",
    "Nothing you propose is saved until they approve it, so propose the thing",
    "you actually think rather than the safe version of it.",
]


def compose(transcript, extra=()) -> List[str]:
    """The prompt for the next card: the form, then what has been said.

    Bounded from the oldest end. The next card is drawn from the last few
    turns and the plan they approved; an opening message forty turns back
    is not worth the deadline it costs to carry.
    """
    lines = list(FORM) + ["", "# The conversation so far", ""]
    rows = [r for r in (transcript or []) if isinstance(r, dict)]
    for row in rows[-MAX_TURNS:]:
        who = "them" if str(row.get("role") or "") == "you" else "you"
        text = str(row.get("text") or "").strip()[:MAX_TURN_TEXT]
        if text:
            lines += ["%s: %s" % (who, text), ""]
    lines += list(extra)
    return lines


# --- what comes back ---------------------------------------------------------

def _one(value, cap: int) -> str:
    return " ".join(str(value or "").split())[:cap]


def _question_id(seen) -> str:
    while True:
        made = "q" + secrets.token_hex(3)
        if made not in seen:
            return made


def _normalize_questions(value) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    raw = value.get("items")
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        title = _one(row.get("title"), MAX_TITLE)
        if not title:
            continue
        options = [_one(o, MAX_LABEL) for o in row.get("options") or []
                   if isinstance(o, (str, int, float)) and _one(o, MAX_LABEL)]
        kind = str(row.get("type") or "").strip().lower()
        # A kind nobody can draw, and a choice with nothing to choose from,
        # are both answered the same way: give them a box to type in. A
        # question the reader cannot answer is worse than an open one.
        if kind not in KINDS or (kind != "text" and not options):
            kind = "text"
        qid = _one(row.get("id"), 40)
        if not qid or qid in seen:
            qid = _question_id(seen)
        seen.add(qid)
        out.append({"id": qid, "type": kind, "title": title,
                    "subtitle": _one(row.get("subtitle"), 80),
                    "options": options[:MAX_OPTIONS] if kind != "text" else [],
                    "placeholder": _one(row.get("placeholder"), MAX_TITLE)})
        if len(out) >= MAX_QUESTIONS:
            break
    return {"eyebrow": _one(value.get("eyebrow"), 40) or "a few questions",
            "items": out}


def _normalize_plan(value) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    lines = []
    for row in value.get("lines") or []:
        if not isinstance(row, dict):
            continue
        key = _one(row.get("k"), MAX_LINE_KEY)
        val = _one(row.get("v"), MAX_LINE_VALUE)
        # Half a line says nothing: a label with no answer under it reads as
        # a question the plan failed to settle.
        if key and val:
            lines.append({"k": key, "v": val})
        if len(lines) >= MAX_PLAN_LINES:
            break
    return {"head": _one(value.get("head"), MAX_TITLE), "lines": lines}


def _normalize_goals(value) -> List[Dict[str, str]]:
    out = []
    for row in value if isinstance(value, list) else []:
        # Same rule as a TODO row: what somebody wrote, or a dict carrying
        # it. Nothing else becomes a goal in the reader's tree.
        if isinstance(row, dict):
            label, why = (_one(row.get("label"), MAX_LABEL),
                          _one(row.get("why"), MAX_WHY))
        elif isinstance(row, str):
            label, why = _one(row, MAX_LABEL), ""
        else:
            continue
        if not label:
            continue
        out.append({"label": label, "why": why})
        if len(out) >= MAX_GOALS:
            break
    return out


def _normalize_todos(value) -> List[str]:
    out = []
    for row in value if isinstance(value, list) else []:
        # A row is what somebody wrote or a dict that carries it. A bare
        # number is neither, and "7" as a line of work in the reader's own
        # document is worse than one row fewer.
        if isinstance(row, dict):
            text = _one(row.get("text"), MAX_TODO)
        elif isinstance(row, str):
            text = _one(row, MAX_TODO)
        else:
            continue
        if not text:
            continue
        out.append(text)
        if len(out) >= MAX_TODOS:
            break
    return out


def normalize_card(value) -> Dict[str, Any]:
    """Whatever came back, as the one shape the rail draws.

    A card the reader cannot act on is not a card: questions with nothing
    left in them, and a plan with neither a head nor a line, both come back
    as ``none`` so the reply stands as prose alone rather than opening an
    empty box under it.
    """
    value = value if isinstance(value, dict) else {}
    card = str(value.get("card") or "").strip().lower()
    if card not in CARDS:
        card = "none"
    out: Dict[str, Any] = {"say": _one(value.get("say"), MAX_SAY),
                           "card": card, "questions": {"eyebrow": "",
                                                       "items": []},
                           "plan": {"head": "", "lines": []},
                           "goals": [], "todos": []}
    if card == "questions":
        held = _normalize_questions(value.get("questions"))
        out["questions"] = held
        if not held["items"]:
            out["card"] = "none"
    elif card == "plan":
        held = _normalize_plan(value.get("plan"))
        out["plan"] = held
        if not held["head"] and not held["lines"]:
            out["card"] = "none"
    elif card == "goals":
        out["goals"] = _normalize_goals(value.get("goals"))
        if not out["goals"]:
            out["card"] = "none"
    elif card == "todos":
        out["todos"] = _normalize_todos(value.get("todos"))
        if not out["todos"]:
            out["card"] = "none"
    return out


def answers_as_said(questions, answers) -> str:
    """What the reader picked, written as the turn they took.

    Put back into the transcript in their own voice rather than as a
    structure, because the next call reads the conversation and a question
    answered is a thing they said.
    """
    questions = questions if isinstance(questions, dict) else {}
    answers = answers if isinstance(answers, dict) else {}
    said = []
    for row in questions.get("items") or []:
        if not isinstance(row, dict):
            continue
        got = answers.get(row.get("id"))
        if isinstance(got, list):
            got = " · ".join(_one(g, MAX_LABEL) for g in got if _one(g, 1))
        got = _one(got, MAX_LINE_VALUE)
        if not got:
            continue
        said.append("%s: %s" % (_one(row.get("title"), MAX_TITLE), got))
    return "\n".join(said) if said else SKIPPED


# --- the approved conversation, as a project ---------------------------------

def to_goals(offered, chosen, todos) -> List[Dict[str, Any]]:
    """Every goal that was offered, with the rows under the one picked.

    The goals not chosen are kept and left alone -- they were proposed and
    approved together, and dropping the two the reader did not start on
    would make the approval mean less than it said. Only the chosen one is
    in progress; only the chosen one has rows.

    *chosen* is a label rather than an index because the reader may have
    typed their own, which is then a goal of its own alongside the offer.
    """
    chosen = _one(chosen, MAX_LABEL)
    rows = [_one(t, MAX_TODO) for t in todos or []]
    rows = [t for t in rows if t][:MAX_TODOS]
    doc: Dict[str, Any] = {"goals": []}
    labels = []
    for row in _normalize_goals(offered):
        labels.append(row["label"])
        doc["goals"].append(_goal(doc, row["label"], row["why"],
                                  rows if row["label"] == chosen else []))
    if chosen and chosen not in labels:
        doc["goals"].append(_goal(doc, chosen, "", rows))
    return doc["goals"]


def _goal(doc, title, why, rows) -> Dict[str, Any]:
    # Ids are allocated against the tree as it grows: next_goal_id reads
    # what is already in it, so each goal must be in the document before
    # the next one asks for a name.
    goal = GM.new_goal(GM.next_goal_id(doc), title[:MAX_LABEL], origin="user")
    goal["description"] = why
    # No status is what the rail means by "not yet sent to a build", and
    # setup has run nothing: every row it writes is the reader's to send.
    goal["todo_items"] = [{"id": GM.todo_id(), "text": text, "depth": 0,
                           "status": "", "note": "", "answer": "",
                           "question": "", "history": []} for text in rows]
    goal["todos_md"] = GM.render_todos(goal["todo_items"])
    goal["status"] = "in_progress" if rows else "active"
    return goal


def to_project(name, plan) -> Dict[str, Any]:
    """What the project record holds when setup is done.

    The plan's head is the objective -- one line, the thing every chat in
    the project reads -- and the lines under it become the description,
    which is where the reader looks for what they agreed the work was.
    """
    plan = _normalize_plan(plan)
    body = "\n".join("%s: %s" % (row["k"], row["v"]) for row in plan["lines"])
    return {"name": _one(name, 80), "objective": plan["head"],
            "description": body}


# --- what setup leaves behind ------------------------------------------------
#
# The setup conversation happens on a web page, not in a Claude Code chat.
# There is therefore no chat to bind: the project is made from the name the
# reader typed while the goals were being written, and its goal tree lives in
# a workspace this vault minted. A chat joins it later, by opening it, the
# same way it would join any project it did not start.

def commit(root, name, plan, offered, chosen, todos) -> Dict[str, Any]:
    """Make the project, write its tree, attach it to nothing.

    Refused before anything is created when there is nothing to save, when
    the name cannot become a folder, and when the name is already a
    project's -- two projects with one name are two answers to "which one
    did I mean", and the reader is told rather than handed the first.
    """
    from . import chat_state as CS
    from . import project_store as PS

    made = to_goals(offered, chosen, todos)
    held = to_project(name, plan)
    if not made and not held["objective"]:
        return {"ok": False, "error": "nothing to save yet"}
    if not held["name"]:
        return {"ok": False, "error": "name this project first"}
    if PS.workspace_home(root, held["name"]) is None:
        return {"ok": False, "error": "that name cannot become a folder"}
    if PS.project_named(root, held["name"]) is not None:
        return {"ok": False,
                "error": "a project is already called that; pick another name"}

    cwd = PS.create_named(root, held["name"])
    if not cwd:
        return {"ok": False, "error": "that name cannot become a folder"}
    # A session of this vault's own rather than one Claude started: it holds
    # the tree, and no conversation is bound to it. Written in the same save
    # as the name and the objective -- the project section is rebuilt from a
    # whitelist on every write, so naming the store in a second save of its
    # own is a second chance to drop one of the two.
    session_id = CS.open_workspace_for(cwd, root)
    held["tree_session"] = session_id
    PS.save_project(root, cwd, held)
    if made:
        goals = {"goals": made}
        GM.sanitize(goals)
        CS.save_goals(session_id, goals, {"items": []}, root)
    return {"ok": True, "cwd": cwd, "name": held["name"],
            "tree_session": session_id, "goals": len(made)}
