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

import json
import os
import secrets
import shutil
from pathlib import Path
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
MAX_PLAN = 2400
MAX_UNSURE = 6
MAX_GOALS = 8
MAX_TODOS = 20
MAX_SUBGOALS = 6
MAX_FOCUS = 3
MAX_CHAT_TURNS = 60
MAX_CHAT_TURN = 1200

CARDS = ("questions", "plan", "goals", "todos", "none")

# The four ways a question can be put, named for what the reader does
# rather than for the control that does it. An option may carry the reason
# it is worth choosing, which is what lets one shape ask both "which is
# true" and "which of these proposals is right" -- the second is the first
# with an argument under each row, not a different act.
MCQ = "mcq"                 # one of several
SELECT_ALL = "select_all"   # any of several
FREE = "free"               # one line
PARA = "open"               # a paragraph
KINDS = (MCQ, SELECT_ALL, FREE, PARA)
CHOICES = (MCQ, SELECT_ALL)         # kinds that carry options
WRITTEN = (FREE, PARA)              # kinds the reader types into

# Sonnet, named rather than inherited: the setup conversation is the first
# thing a new reader sees and the one place a weaker model shows immediately
# -- it has to hear a half-described project and ask the question that
# changes what it proposes.
SETUP_MODEL = "sonnet"


def setup_model(root=None) -> str:
    """Which sonnet to ask for, on whichever account this install runs on.

    A reader Engelbart connected is billed against the key we issued them,
    and their settings pin `enforceAvailableModels` to the list that key is
    allowed -- so asking for the bare alias is asking for a model the
    gateway may not answer to. When the account names its own sonnet, use
    that one; otherwise the alias, which is what an unconnected reader's own
    subscription understands.

    Nothing here reads the key itself. The npm device flow keeps the account
    record under ``~/.human-compact`` and starts setup with the issued key in
    its environment; this reads only that record's optional model names.
    """
    try:
        managed = Path(os.environ.get("HUMAN_COMPACT_HOME")
                       or Path.home() / ".human-compact")
        value = json.loads((managed / "auth.json").read_text(encoding="utf-8"))
        claude = value.get("claude") if isinstance(value, dict) else {}
        models = claude.get("models") if isinstance(claude, dict) else []
    except (OSError, ValueError):
        return SETUP_MODEL
    # `.get` answers None for a key that is absent, and a record written by a
    # CLI that had nothing to say about models is the ordinary case, not the
    # broken one. Iterating that None crashed setup for every reader whose
    # account did not name its models -- and the crash surfaced as a bare
    # TypeError, which is how it went unread for so long.
    if not isinstance(models, list):
        return SETUP_MODEL
    for name in models:
        if isinstance(name, str) and SETUP_MODEL in name:
            return name
    return SETUP_MODEL


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
    '                            "type": "mcq" | "select_all" | "free"',
    '                                    | "open",',
    '                            "title": "<the question>",',
    '                            "subtitle": "<optional, e.g. pick any>",',
    '                            "options": [{"label": "<the choice>",',
    '                                        "why": "<optional: what it',
    '                                                buys them>"}],',
    '                            "placeholder": "<free and open>"}]},',
    '   "plan": {"description": "<a short paragraph or two: what you think',
    '                            they are doing and what done looks like>",',
    '            "unsure": ["<something you could not settle from what they',
    '                        said, in their terms>"]},',
    '   "goals": [{"label": "<an outcome, not a task>",',
    '              "why": "<why this one is worth starting on>"}],',
    '   "subgoals": [{"label": "<a piece of the chosen goal>",',
    '                 "todos": ["<one row of work in that piece>"]}],',
    '   "todos": ["<or, where it does not break down, just the rows>"]}',
    "",
    "Only the key for the card you name is read; leave the others out.",
    "",
    "The plan is prose and doubts, not a form. Say what you think the work",
    "is in a couple of short paragraphs they could argue with, then list what",
    "you could not settle -- that list is what tells them whether you",
    "understood them or guessed, so write the real gaps and not none.",
    "",
    "The order this normally goes in: questions, questions again if the",
    "answers opened something, then plan, then goals, then todos. Do not",
    "skip ahead to a plan you cannot write from what they have told you, and",
    "do not ask a third round of questions to avoid writing one.",
    "",
    "Questions are for what changes your proposal, never for what you could",
    "assume. Pick the kind by what you are actually asking for:",
    "",
    "  mcq         one answer out of several you can name",
    "  select_all  any number of them -- say so in the subtitle",
    "  free        one line they have to write; give a placeholder",
    "  open        a paragraph: the story, the constraint nobody wrote down",
    "",
    "An option may carry a `why`. Use it when the options are proposals of",
    "yours rather than facts of theirs -- \"which of these is the right one",
    "to start on\" is an mcq whose rows each say what that choice buys them,",
    "so they are choosing between arguments instead of guessing what you",
    "meant. Leave `why` out when the answer is simply something they know.",
    "",
    "How many is your judgement, not a rule. Two or three in a round reads",
    "as a conversation; six reads as a form and people abandon forms. If one",
    "question would change everything you propose, ask it alone. If you",
    "genuinely need another round after this one, take it -- but do not take",
    "one to put off writing a plan you could already write.",
    "",
    "A goal is an outcome someone could tell you they had reached. A TODO row",
    "is one piece of work, in the imperative, that a coding agent could pick",
    "up and finish. Neither is a phase, a heading or a category.",
    "",
    "On the todos card, break the chosen goal into its pieces and put the",
    "rows under the piece they belong to -- two to four pieces is usually",
    "the shape of it, and a list of twelve rows in one heap is a list nobody",
    "reads. A piece is still an outcome, smaller. Where the work genuinely",
    "does not break down, send the rows flat instead and say so.",
    "",
    "Nothing you propose is saved until they approve it, so propose the thing",
    "you actually think rather than the safe version of it.",
]


# Reading a conversation instead of asking about one.
#
# Somebody who runs /bart in a chat they have been working in all afternoon
# has no project and no goals, but they are not starting from nothing: the
# transcript IS the description, and asking them to type one would be asking
# them to repeat themselves. So the questions and the plan are skipped, and
# what comes back is three things worth focusing on -- each with its tree
# already written, so choosing costs nothing and shows something at once.

FROM_CHAT = [
    "Below is a conversation somebody has been having with a coding agent.",
    "They have just opened Engelbart on it and have no project yet. Read what",
    "they were actually doing and offer THREE things worth focusing on next.",
    "",
    "Reply with ONE JSON object and nothing else:",
    "",
    '  {"focus": [{"label": "<an outcome, not a task>",',
    '              "why": "<why this one, in what they were doing>",',
    '              "subgoals": ["<a piece of it>"]}]}',
    "",
    "Each one carries its own tree, because the reader will see it the",
    "moment they choose and waiting again would be the third wait. Two to",
    "four pieces each; a piece is an outcome, smaller.",
    "",
    "Read what the conversation was FOR, not what it touched. A file they",
    "opened once is not a goal. Where they said what they were trying to do,",
    "use their words -- they will recognise them and know you read it.",
    "",
    "Three that differ. Offering one thing three ways is offering one thing.",
]


def compose_chat(events) -> List[str]:
    """The prompt for the focus options: the form, then the conversation.

    Bounded from the oldest end, like every other read of a transcript
    here: what they were doing lately is what they are doing.
    """
    lines = list(FROM_CHAT) + ["", "# The conversation", ""]
    rows = [e for e in (events or []) if isinstance(e, dict)]
    for row in rows[-MAX_CHAT_TURNS:]:
        who = str(row.get("role") or row.get("kind") or "").strip() or "?"
        text = " ".join(str(row.get("text") or "").split())[:MAX_CHAT_TURN]
        if text:
            lines += ["%s: %s" % (who, text), ""]
    return lines


def normalize_focus(value) -> List[Dict[str, Any]]:
    """The three, coerced. A tree is what makes choosing cheap, but a focus
    without one is still a focus -- refusing it would leave the reader two
    options where the model found three."""
    if isinstance(value, dict):
        value = value.get("focus") or value.get("goals") or []
    out = []
    for row in value if isinstance(value, list) else []:
        if isinstance(row, dict):
            label = _one(row.get("label") or row.get("title"), MAX_LABEL)
            why = _one(row.get("why"), MAX_WHY)
            kids = [{"label": _one(k.get("label") if isinstance(k, dict) else k,
                                   MAX_LABEL)}
                    for k in row.get("subgoals") or []]
            kids = [k for k in kids if k["label"]][:MAX_SUBGOALS]
        elif isinstance(row, str):
            label, why, kids = _one(row, MAX_LABEL), "", []
        else:
            continue
        if not label:
            continue
        out.append({"label": label, "why": why, "subgoals": kids})
        if len(out) >= MAX_FOCUS:
            break
    return out


# How long someone is made to watch "generating" before being told anything.
# The provider default is three minutes, which is right for an agent turn
# nobody is sitting in front of and wrong for the first card of setup: a
# reader watching a spinner concludes the tool is broken long before it
# concludes anything itself.
SETUP_TIMEOUT_SECONDS = 90

# The statuses the deployment uses for "this key buys nothing".
SPENT_STATUS = ("exhausted", "revoked", "blocked")
SPEND_EPSILON = 0.0001


def credit_note(root=None) -> str:
    """Whether this install spends pooled credit, and whether any is left.

    Read before blaming the reader's machine. An install wired to Engelbart
    spends a shared key through a gateway, and when that key is done the
    account record says so -- so a failure here can say which account setup
    was drawing on rather than leaving them to guess.

    Deliberately not a refusal. A spent pool is not a dead end: setup drops
    the key and runs on the reader's own Claude account, which works. Turning
    "no credit" into an error would block the setups that recover by
    themselves, so this only ever adds a sentence to a failure that already
    happened.
    """
    try:
        managed = Path(os.environ.get("HUMAN_COMPACT_HOME")
                       or Path.home() / ".human-compact")
        value = json.loads((managed / "auth.json").read_text(encoding="utf-8"))
        claude = value.get("claude") if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return ""
    # No record, or one that names no gateway: this machine is not spending
    # pooled credit and nothing about the pool is worth saying.
    if not isinstance(claude, dict) or not claude.get("baseUrl"):
        return ""
    status = claude.get("status")
    try:
        budget = float(claude.get("budgetUsd") or 0)
        spend = float(claude.get("spendUsd") or 0)
    except (TypeError, ValueError):
        budget = spend = 0.0
    if status:
        spent = status in SPENT_STATUS
    else:
        spent = budget > 0 and spend >= budget - SPEND_EPSILON
    if not spent:
        return ""
    return (f" Your Engelbart Claude credit is used up (${spend:.2f} of "
            f"${budget:.2f}), so setup falls back to your own Claude account "
            "-- check that Claude Code is signed in to it.")


def unexpected(exc) -> Dict[str, Any]:
    """Report what actually went wrong, rather than guessing at PATH.

    The guess was wrong in the case that mattered most. A reader whose Claude
    credit had run out was told to check whether the CLI was on their PATH --
    it was, and the real answer was on the other side of a bare ``except``.
    They went looking for a broken install instead of a spent budget.

    ``ProviderError`` already carries every failure this code anticipated,
    including the CLI genuinely being absent, so anything arriving here is by
    definition unanticipated and its own text is the most useful thing there
    is to say about it.
    """
    detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:200]
    # The detail says what broke; this says what to do about it. Setup shells
    # out to `claude`, and that subprocess inherits the environment the setup
    # server was started with -- so a server left running from before an
    # upgrade, or started when the credit was still live, is the ordinary
    # cause of a failure that a fresh `hc setup-ui` simply does not have.
    # Naming the member's own account matters too: out of credit is not out of
    # options, and nobody should conclude their install is broken over it.
    return {"ok": False, "error": (
        f"setup could not reach Claude -- {detail}. "
        "Close this page and run `hc setup-ui` again."
        + credit_note())}


def from_chat(events, engine=None, root=None) -> Dict[str, Any]:
    """Three things worth focusing on, read out of the conversation."""
    from . import providers as PROVIDERS
    import os
    usable = [e for e in (events or [])
              if isinstance(e, dict) and str(e.get("text") or "").strip()]
    if not usable:
        # A chat with nothing in it is the other cold start, and the page
        # has a conversation for that one.
        return {"ok": False, "error": "there is nothing in this chat yet"}
    try:
        engine = engine or PROVIDERS.make(
            os.environ.get("HC_CHAT_PROVIDER", "claude"), "synthesize",
            setup_model(root), timeout=SETUP_TIMEOUT_SECONDS)
        raw = engine.generate_json("\n".join(compose_chat(usable)) + "\n")
    except PROVIDERS.ProviderError as exc:
        return {"ok": False,
                "error": " ".join(str(exc).split())[:200] + credit_note()}
    except Exception as exc:                             # noqa: BLE001
        return unexpected(exc)
    focus = normalize_focus(raw)
    if not focus:
        return {"ok": False, "error": "nothing came back to choose from"}
    return {"ok": True, "focus": focus}


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


def _long(value, cap: int) -> str:
    """Prose, with its paragraphs left in. Blank runs are collapsed so a
    model that pads with newlines does not push the buttons off screen."""
    lines = [" ".join(line.split()) for line in str(value or "").splitlines()]
    out, blank = [], False
    for line in lines:
        if not line:
            blank = True
            continue
        if out and blank:
            out.append("")
        blank = False
        out.append(line)
    return "\n".join(out)[:cap]


def _question_id(seen) -> str:
    while True:
        made = "q" + secrets.token_hex(3)
        if made not in seen:
            return made


def _candidates(value) -> List[Dict[str, str]]:
    """Options as {label, why}, however they were written.

    A bare string is a label with no argument under it, which is what an
    ordinary choice is; a dict may say why it is worth choosing, which is
    what makes "which of these is best" a different question.
    """
    out = []
    for row in value if isinstance(value, list) else []:
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
        if len(out) >= MAX_OPTIONS:
            break
    return out


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
        kind = str(row.get("type") or "").strip().lower()
        # An option is a label and, where the model is proposing rather
        # than asking, the argument for it. Read from either key: a model
        # that writes its proposals under "candidates" has still asked a
        # question the reader can answer.
        options = _candidates(row.get("options") or row.get("candidates"))
        # A kind nobody can draw, and a choice with nothing to choose from,
        # are both answered the same way: give them a box to type in. A
        # question the reader cannot answer is worse than an open one.
        if kind not in KINDS or (kind in CHOICES and not options):
            kind = FREE
        qid = _one(row.get("id"), 40)
        if not qid or qid in seen:
            qid = _question_id(seen)
        seen.add(qid)
        out.append({"id": qid, "type": kind, "title": title,
                    "subtitle": _one(row.get("subtitle"), 80),
                    "options": options if kind in CHOICES else [],
                    "placeholder": _one(row.get("placeholder"), MAX_TITLE)})
        if len(out) >= MAX_QUESTIONS:
            break
    return {"eyebrow": _one(value.get("eyebrow"), 40) or "a few questions",
            "items": out}


def _normalize_plan(value) -> Dict[str, Any]:
    """The plan: what it thinks the work is, and what it is still unsure of.

    Prose and doubts, not a form. A table of labelled rows read as something
    the reader had to fill in rather than something they had to agree with,
    and the labels were the model's invention anyway. What is worth saying
    here is the description -- which they can argue with -- and the list of
    what it could not settle, which is the part that tells them whether it
    understood them or guessed.
    """
    value = value if isinstance(value, dict) else {}
    said = _long(value.get("description") or value.get("head"), MAX_PLAN)
    # A model still writing the old shape has its rows folded into the prose
    # rather than dropped: half a plan is worse than a clumsy one.
    if not said:
        rows = [r for r in value.get("lines") or [] if isinstance(r, dict)]
        said = " ".join(_one(r.get("v"), MAX_LINE_VALUE) for r in rows).strip()
    unsure = []
    for row in value.get("unsure") or []:
        text = _one(row.get("text") if isinstance(row, dict) else row,
                    MAX_LINE_VALUE)
        if text:
            unsure.append(text)
        if len(unsure) >= MAX_UNSURE:
            break
    return {"description": said, "unsure": unsure}


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


def _normalize_subgoals(value) -> List[Dict[str, Any]]:
    """The pieces of the chosen goal, each with the rows that belong to it.

    A piece with no rows under it is dropped: a subgoal is a place to put
    work, and one with none is a heading. The reader can add rows to it
    afterwards, but a heading the model invented and left empty is not
    something they asked for.
    """
    out = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        label = _one(row.get("label") or row.get("title"), MAX_LABEL)
        rows = _normalize_todos(row.get("todos"))
        if not label or not rows:
            continue
        out.append({"label": label, "todos": rows})
        if len(out) >= MAX_SUBGOALS:
            break
    return out


def _named(value, due) -> Dict[str, Any]:
    """Whatever came back, with the envelope put back around it.

    Told "this card is the plan and nothing else", the model sometimes
    takes that literally and returns the payload at the top level -- no
    `say`, no `card`. It has answered; only the wrapper is missing, and
    throwing the answer away for that is how a reader ends up watching a
    card that never comes.

    Read by shape, never by hope: the kind is taken from the keys that are
    actually there, so a plan-shaped reply is a plan even when a plan is
    not what was due -- and the stage check is what refuses it. Guessing
    *toward* the due card would let the discard be walked around.
    """
    if isinstance(value, list):
        # A bare list is only ever the two lists this asks for, and the
        # rows in them tell which: a goal carries a label.
        rows = [r for r in value if isinstance(r, dict)]
        if rows and any("label" in r or "title" in r for r in rows):
            return {"card": "goals", "goals": value}
        return {"card": "todos", "todos": value}
    if not isinstance(value, dict):
        return {}
    if value.get("card"):
        return value
    # The envelope is there and the card is not: exactly one payload key
    # says what it was.
    for name in ("questions", "plan", "goals", "todos", "subgoals"):
        if value.get(name):
            return dict(value, card="todos" if name == "subgoals" else name)
    # No envelope at all: the object IS the payload.
    if "items" in value:
        return {"card": "questions", "questions": value}
    if "description" in value or "unsure" in value:
        return {"card": "plan", "plan": value}
    return value


def normalize_card(value, due="") -> Dict[str, Any]:
    """Whatever came back, as the one shape the rail draws.

    A card the reader cannot act on is not a card: questions with nothing
    left in them, and a plan with neither a head nor a line, both come back
    as ``none`` so the reply stands as prose alone rather than opening an
    empty box under it.
    """
    value = _named(value, due)
    value = value if isinstance(value, dict) else {}
    card = str(value.get("card") or "").strip().lower()
    if card not in CARDS:
        card = "none"
    out: Dict[str, Any] = {"say": _one(value.get("say"), MAX_SAY),
                           "card": card, "questions": {"eyebrow": "",
                                                       "items": []},
                           "plan": {"description": "", "unsure": []},
                           "goals": [], "todos": [], "subgoals": []}
    if card == "questions":
        held = _normalize_questions(value.get("questions"))
        out["questions"] = held
        if not held["items"]:
            out["card"] = "none"
    elif card == "plan":
        held = _normalize_plan(value.get("plan"))
        out["plan"] = held
        if not held["description"]:
            out["card"] = "none"
    elif card == "goals":
        out["goals"] = _normalize_goals(value.get("goals"))
        if not out["goals"]:
            out["card"] = "none"
    elif card == "todos":
        out["subgoals"] = _normalize_subgoals(value.get("subgoals"))
        out["todos"] = _normalize_todos(value.get("todos"))
        if not out["subgoals"] and not out["todos"]:
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
        # "Which of these is best" is answered with the proposal's label --
        # the argument under it was for the reader, not for the next call.
        if isinstance(got, dict):
            got = got.get("label")
        if isinstance(got, list):
            got = " · ".join(_one(g, MAX_LABEL) for g in got if _one(g, 1))
        got = _one(got, MAX_LINE_VALUE)
        if not got:
            continue
        said.append("%s: %s" % (_one(row.get("title"), MAX_TITLE), got))
    return "\n".join(said) if said else SKIPPED


# --- the approved conversation, as a project ---------------------------------

def to_goals(offered, chosen, todos, subgoals=()) -> List[Dict[str, Any]]:
    """Every goal that was offered, and under the one picked, its pieces.

    The goals not chosen are kept and left alone -- they were proposed and
    approved together, and dropping the two the reader did not start on
    would make the approval mean less than it said. Only the chosen one is
    in progress; only the chosen one has anything under it.

    Where the work broke into pieces, the rows live on the piece they belong
    to rather than in one list on the parent: that is the shape the
    workspace's tree already holds, and a goal with a wall of twelve rows is
    a wall nobody reads.

    *chosen* is a label rather than an index because the reader may have
    typed their own, which is then a goal of its own alongside the offer.
    """
    chosen = _one(chosen, MAX_LABEL)
    pieces = _normalize_subgoals(subgoals)
    rows = _normalize_todos(todos)
    doc: Dict[str, Any] = {"goals": []}
    labels = []
    picked = None
    for row in _normalize_goals(offered):
        labels.append(row["label"])
        mine = row["label"] == chosen
        made = _goal(doc, row["label"], row["why"],
                     [] if (mine and pieces) else (rows if mine else []))
        if mine:
            picked = made
            if pieces:
                made["status"] = "in_progress"
        doc["goals"].append(made)
    if chosen and chosen not in labels:
        picked = _goal(doc, chosen, "", [] if pieces else rows)
        if pieces:
            picked["status"] = "in_progress"
        doc["goals"].append(picked)
    if picked is not None:
        for piece in pieces:
            kid = _goal(doc, piece["label"], "", piece["todos"],
                        parent=picked["id"])
            doc["goals"].append(kid)
    return doc["goals"]


def _goal(doc, title, why, rows, parent=None) -> Dict[str, Any]:
    # Ids are allocated against the tree as it grows: next_goal_id reads
    # what is already in it, so each goal must be in the document before
    # the next one asks for a name. A child takes its id from its parent's,
    # which is how the tree says who belongs to whom.
    gid = GM.child_goal_id(doc, parent) if parent else GM.next_goal_id(doc)
    goal = GM.new_goal(gid, title[:MAX_LABEL], parent, origin="user")
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
    said = plan["description"]
    # The objective is one line -- every chat in the project reads it -- so
    # it is the first sentence of what was agreed, not the whole of it.
    first = said.split("\n")[0].strip()
    return {"name": _one(name, 80), "objective": first[:400],
            "description": said}


# --- what setup leaves behind ------------------------------------------------
#
# The setup conversation happens on a web page, not in a Claude Code chat.
# There is therefore no chat to bind: the project is made from the name the
# reader typed while the goals were being written, and its goal tree lives in
# a workspace this vault minted. A chat joins it later, by opening it, the
# same way it would join any project it did not start.

def commit(root, name, plan, offered, chosen, todos,
           subgoals=(), bind="") -> Dict[str, Any]:
    """Make the project, write its tree, attach it to nothing.

    Refused before anything is created when there is nothing to save, when
    the name cannot become a folder, and when the name is already a
    project's -- two projects with one name are two answers to "which one
    did I mean", and the reader is told rather than handed the first.
    """
    from . import chat_state as CS
    from . import project_store as PS

    made = to_goals(offered, chosen, todos, subgoals)
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
    # The chat that asked for this joins it. Only where one asked: the page
    # opened after an install has no chat behind it, and binding whatever
    # workspace happened to serve the page would attach the project to a
    # conversation that had no part in it.
    bound = False
    if str(bind or "").strip():
        try:
            CS.bind_project(str(bind).strip(), cwd, root)
            bound = True
        except Exception:                                # noqa: BLE001
            # The project was made. Failing to bind it is worth reporting,
            # not worth throwing the work away over.
            bound = False
    # Reported in the spelling the store and the bindings use, so a caller
    # that asks "which chats are in this?" with what it was handed gets an
    # answer rather than a miss on a symlinked path.
    return {"ok": True, "cwd": PS._resolved(cwd), "name": held["name"],
            "tree_session": session_id, "goals": len(made), "bound": bound}


# --- asking ------------------------------------------------------------------

# The four steps, in the order they have to happen. Left to its own
# judgement the model sometimes wrote a plan from one sentence, or offered
# goals before anybody had agreed what the work was -- and a reader who is
# handed goals for a project nobody described cannot tell whether the tool
# understood them or guessed. So the order is not asked for, it is worked
# out from what has actually been drawn and then required.
#
# One round of questions before a plan is the floor: the model may keep
# asking (it is told when that reads as a form), but it may not write a plan
# for someone it has not asked anything. Two was a floor the model fought --
# it would have enough after one round, try to move on, be discarded, and
# the reader would sit in front of a card that never came.
ORDER = ("questions", "plan", "goals", "todos")
MIN_QUESTION_ROUNDS = 1


def stage_of(transcript, shown) -> str:
    """Which card is due, from the cards already drawn.

    Read from what was drawn rather than from what was said: a reader who
    writes four paragraphs has still not answered a question, and the whole
    point of the questions is that they change what gets proposed.
    """
    drawn = [c for c in (shown or []) if c in ORDER]
    rounds = drawn.count("questions")
    if rounds < MIN_QUESTION_ROUNDS:
        return "questions"
    for card in ORDER[1:]:
        if card not in drawn:
            return card
    return "none"


_DUE = {
    "questions": "ask your questions -- this is a questions card",
    "plan": "write the plan: this card is the plan and nothing else",
    "goals": "offer the goals, as a goals card",
    "todos": "break the chosen goal into rows, as a todos card",
}


def ask(transcript, engine=None, extra=(), root=None, shown=()) -> Dict[str, Any]:
    """One round: put the conversation to the model, take back one card.

    The reply is JSON the reader never sees -- what they see is the modal it
    names. A provider that cannot be reached, or that will not answer in the
    shape after its own retry, is reported as itself rather than dressed up
    as an empty card: a setup that silently says nothing is the blank screen
    this whole surface exists to replace.
    """
    from . import providers as PROVIDERS
    import os
    due = stage_of(transcript, shown)
    if due in _DUE:
        extra = list(extra) + [
            "", "# The card you are writing now", "",
            "Whatever else you say, on this reply you %s." % _DUE[due],
            "The reader is stepped through four cards in one order --",
            "questions, plan, goals, todos -- and a card out of turn is not",
            "drawn at all, so naming a different one costs them the round.",
        ]
    try:
        engine = engine or PROVIDERS.make(
            os.environ.get("HC_CHAT_PROVIDER", "claude"), "synthesize",
            setup_model(root), timeout=SETUP_TIMEOUT_SECONDS)
        raw = engine.generate_json("\n".join(compose(transcript, extra)) + "\n")
    except PROVIDERS.ProviderError as exc:
        return {"ok": False,
                "error": " ".join(str(exc).split())[:200] + credit_note()}
    except Exception as exc:                             # noqa: BLE001
        return unexpected(exc)
    card = normalize_card(raw, due)
    # A card out of turn is not drawn. What it said is kept -- it is talking
    # to the reader, and dropping that would leave a silent round -- but the
    # card itself is refused, because drawing it is what skips the step.
    #
    # A reply that was ONLY the wrong card leaves nothing at all, and a
    # silent round reads to the reader as a tool that broke. So it is asked
    # once more, told plainly what it just did.
    if card["card"] != "none" and card["card"] != due:
        kept = card["say"]
        if not kept:
            try:
                raw = engine.generate_json(
                    "\n".join(compose(transcript, list(extra) + [
                        "", "You just replied with a %s card when the card"
                        " due is %s. That reply was discarded. Write the %s"
                        " card." % (card["card"], due, due)])) + "\n")
            except Exception:                            # noqa: BLE001
                raw = {}
            card = normalize_card(raw, due)
        if card["card"] != due:
            card = dict(card, card="none",
                        questions={"eyebrow": "", "items": []},
                        plan={"description": "", "unsure": []},
                        goals=[], todos=[], subgoals=[])
            card["say"] = card["say"] or kept
    if not card["say"] and card["card"] == "none":
        return {"ok": False, "error": "the model answered with nothing"}
    return dict(card, ok=True, due=due)


# --- opening a terminal for them ---------------------------------------------
#
# The last step of every path is "run this in your terminal", which is the
# step that loses people: they have to find the terminal, put it beside the
# browser, and type something they half-remember. Where the machine will
# open one with the command already in it, that is two of the three gone --
# they press Return.
#
# Never the whole answer, though. A machine with no terminal this knows how
# to drive, a desktop session it cannot reach, a reader on a remote host --
# all of those still need the command written down, so the copy rows stay
# and this is only ever an offer on top of them.

# Seeds the command into whichever interactive shell opened, so it is one
# Up-arrow away. zsh takes `print -rs`, bash takes `history -s`, and the one
# that is not there fails harmlessly into the other.
_SEED = "print -rs -- %s 2>/dev/null || history -s %s 2>/dev/null"


def open_terminal(command, cwd=None, run=None) -> Dict[str, Any]:
    """Open a terminal with *command* waiting in its history, unrun.

    Waiting rather than run: the reader presses the keys. What is about to
    happen is visible before it happens, which is the difference between a
    tool that helps and a tool that does things to your machine.

    Put in the shell's history rather than typed into the window. Typing
    means System Events, and System Events types into whatever is frontmost
    -- which, on a machine where a browser and an editor are also open, is
    not reliably the window that just opened. Measured: the first character
    was swallowed while the shell was still starting, and once the keystroke
    landed in an unrelated window that already had text in it. A tool that
    fires this on its own cannot be a tool that sometimes types into your
    editor.

    So there is one path, and it is the same on every platform: open a
    terminal, put the command in that shell's history, and print what it is
    waiting for. Up, then Return. No permission is needed, nothing races,
    and the window always has the command in it.
    """
    import shlex
    import subprocess
    import sys
    run = run or subprocess.run
    said = " ".join(str(command or "").split())
    if not said or any(c in said for c in ";&|`$\n><"):
        # Only the commands this page offers, and they are plain words. A
        # shell metacharacter here is a request to run something else.
        return {"ok": False, "error": "that is not a command this can open"}
    here = str(Path(str(cwd)).expanduser()) if cwd else str(Path.home())
    quoted = shlex.quote(said)
    # The window says for itself what it is waiting for: a terminal that
    # opened with nothing visible in it is worse than not opening one.
    inner = "cd %s; clear; %s; printf '\\n  %s\\n\\n'" % (
        shlex.quote(here), _SEED % (quoted, quoted),
        "press Up then Return to run: " + said)
    if sys.platform == "darwin":
        script = ('tell application "Terminal"\n'
                  '  activate\n'
                  '  do script "%s"\n'
                  'end tell\n') % inner.replace("\\", "\\\\").replace('"', '\\"')
        try:
            done = run(("osascript", "-e", script), timeout=20,
                       capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "error": "no terminal this could open"}
        if getattr(done, "returncode", 1) != 0:
            return {"ok": False,
                    "error": "macOS did not allow a terminal to be opened"}
        return {"ok": True, "typed": said, "note": "up"}
    for name, args in (("x-terminal-emulator", ("-e",)),
                       ("gnome-terminal", ("--",)),
                       ("konsole", ("-e",)),
                       ("xterm", ("-e",))):
        found = shutil.which(name)
        if not found:
            continue
        try:
            subprocess.Popen(
                (found,) + args + ("bash", "-c", inner + "; exec bash"),
                start_new_session=True)
        except (OSError, subprocess.SubprocessError):
            continue
        return {"ok": True, "typed": said, "note": "up"}
    return {"ok": False, "error": "no terminal this could open"}
