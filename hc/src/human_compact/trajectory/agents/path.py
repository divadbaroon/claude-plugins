"""The Path agent: the plan for one piece, as rows.

The same planning primitive the setup and the brainstorm use, told to
answer with a ``todos`` card and nothing else: the reader asked for the
piece to be broken down, so a question back would be the wrong card.
Rows come back as proposals; nothing is written until the reader adds
one, as everywhere on the page.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .. import brainstorm as BRAIN
from . import replies as REPLIES
from . import trace

DUE = [
    "", "# The card you are writing now", "",
    "The reader asked for this piece to be planned. On this reply send a",
    "`todos` card: the ordered rows that get the piece done, each one an",
    "imperative line a build could take up, three to eight of them. Say",
    "one sentence first if it helps. Do not send questions, focus, goals",
    "or an offer.",
]


def plan(transcript, context: str = "", focus=(), known=(), root=None,
         engine=None) -> Dict[str, Any]:
    extra: List[str] = list(focus or []) + list(known or []) + DUE
    with trace.span("model.call", agent="path"):
        card = BRAIN.ask(transcript, context, engine=engine, root=root, extra=extra)
    if not isinstance(card, dict) or not card.get("ok"):
        error = (card or {}).get("error") if isinstance(card, dict) else ""
        return {"ok": False, "error": str(error or "Bart could not plan this")}
    return {"ok": True, "say": str(card.get("say") or ""),
            "card": str(card.get("card") or "none"),
            "replies": REPLIES.from_card(card)}
