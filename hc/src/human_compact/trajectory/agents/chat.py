"""The Chat agent: Bart answering a message.

Most of what a reader says to Bart is a message -- a fact, a thought, a
question, a "thanks" -- and wants an answer, not a card. This is that
answer: one model call with a small prompt, prose back, at most a row or
two proposed when the message plainly asks for one.

It also says when it cannot answer. An uncertainty is one of two kinds,
and the difference decides who resolves it: ``human_preference`` is the
reader's to settle (which of two good ways they want), and the Overseer
sends it to the Brainstorm agent to be asked properly; ``environment`` is
a fact about the project (which framework, where the tests are, what
port) and is discovered from the directory, never asked.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .. import setup_chat as SC
from . import trace
from . import presentation as PUBLIC
from . import context as CTX

MAX_SAY = 1200
MAX_TODOS = 3
HUMAN, ENVIRONMENT = "human_preference", "environment"
NEEDS = (HUMAN, ENVIRONMENT)

PROMPT = [
    "You are Bart, the assistant on a project's goal page. The reader is",
    "talking to you about one piece of their work. Answer the last thing",
    "they said, in prose, in the voice of a colleague who has read the",
    "project: two to five sentences, plain, no headings, no lists.",
    "",
    "Propose a TODO row only when the message asks for the next step or",
    "states work to be done; then give it as one short imperative line per",
    "row, at most %d rows. Do not propose goals or subgoals. Do not offer" % MAX_TODOS,
    "cards or menus of options here; if the reader wants options they will",
    "ask, and a different agent answers that.",
    "",
    "If you cannot answer without knowing something, say which of two",
    "things it is. If it is the reader's own preference or intent -- which",
    "of two reasonable ways they want, what they mean by a word only they",
    "can define, an unavailable credential, or a physical/manual action -- set needs.kind to \"human_preference\" and put the one",
    "question in needs.question. If it is a fact about the project that",
    "the directory would show -- which framework, where a file is, how it",
    "runs -- set needs.kind to \"environment\" and name the fact in",
    "needs.question; do NOT ask the reader for it. Otherwise leave needs",
    "empty.",
    "",
    "Reply with JSON only:",
    '{"say": "your answer", "todos": ["row", ...],',
    ' "needs": {"kind": "" | "human_preference" | "environment", "question": ""}}',
]


def compose(transcript, context: str = "", focus=(), known=(),
            discovered: str = "") -> List[str]:
    lines = list(PROMPT) + [CTX.UPDATE_INSTRUCTIONS]
    if context:
        lines += ["", "# The project", "", str(context).rstrip()]
    lines += list(focus or [])
    lines += list(known or [])
    if discovered:
        lines += ["", "# What the project directory shows", "",
                  "You asked about the environment; this is what is there.",
                  "Answer from it and do not ask the reader for it.", "",
                  str(discovered).rstrip()]
    lines += ["", "# The conversation", ""]
    for turn in transcript or []:
        if not isinstance(turn, dict):
            continue
        who = "Reader" if str(turn.get("role") or "") in ("you", "user") else "Bart"
        text = " ".join(str(turn.get("text") or "").split())
        if text:
            lines.append("%s: %s" % (who, text))
    return lines


def normalize(raw: Any) -> Dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    say = PUBLIC.text(value.get("say"), MAX_SAY, allow_ids=True)
    todos = [" ".join(str(t).split())[:SC.MAX_LABEL]
             for t in (value.get("todos") or []) if str(t or "").strip()][:MAX_TODOS]
    needs = value.get("needs") if isinstance(value.get("needs"), dict) else {}
    kind = str(needs.get("kind") or "").strip().lower()
    question = PUBLIC.text(needs.get("question"), 400)
    if kind not in NEEDS or not question:
        kind, question = "", ""
    out = {"say": say, "todos": todos, "needs": {"kind": kind, "question": question}}
    if value.get("resolution") in ("resume", "wait", "cancel"):
        out["resolution"] = value["resolution"]
    updates = CTX.normalize_updates(value.get("contextUpdates"), by="chat")
    if updates:
        out["contextUpdates"] = updates
    return out


def ask(transcript, context: str = "", focus=(), known=(), discovered: str = "",
        engine=None, root=None) -> Dict[str, Any]:
    """One answer. ``ok`` false carries the reason it could not be had."""
    from .. import providers as PROVIDERS
    try:
        engine = engine or PROVIDERS.make(
            os.environ.get("HC_CHAT_PROVIDER", "claude"), "synthesize",
            SC.workspace_model(root), timeout=SC.SETUP_TIMEOUT_SECONDS)
        with trace.span("model.call", agent="chat", model=getattr(engine, "model", "")):
            raw = engine.generate_json(
                "\n".join(compose(transcript, context, focus, known, discovered)) + "\n")
    except PROVIDERS.ProviderError as exc:
        return {"ok": False,
                "error": " ".join(str(exc).split())[:200] + SC.credit_note(root)}
    except Exception as exc:                             # noqa: BLE001
        return SC.unexpected(exc)
    answer = normalize(raw)
    last = next((t.get("text", "") for t in reversed(transcript or []) if t.get("role") in ("you", "user")), "")
    answer["say"] = PUBLIC.text(answer["say"], MAX_SAY, PUBLIC.debug_requested(last))
    if PUBLIC.smalltalk(last):
        answer["todos"] = []
        answer["needs"] = {"kind":"", "question":""}
    if not answer["say"] and not answer["todos"] and not answer["needs"]["kind"]:
        return {"ok": False, "error": "the model answered with nothing"}
    return dict(answer, ok=True)
