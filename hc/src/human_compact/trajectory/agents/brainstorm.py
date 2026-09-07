"""The Brainstorm agent: the existing brainstorm, asked on purpose.

``trajectory.brainstorm.ask`` is what every Bart message used to be. It
stays exactly what it was -- the card grammar, the project digest, the
focus on one piece -- and is now reached only from here: when the reader
asked for options in so many words, or when the Chat agent found the
message turns on a preference only the reader can settle. In that second
case the question the Chat agent could not answer is handed along, so the
card asks it rather than something else.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .. import brainstorm as BRAIN
from . import replies as REPLIES
from . import trace
from . import context as CTX


def reply(transcript, context: str = "", focus=(), known=(), question: str = "",
          root=None, engine=None) -> Dict[str, Any]:
    extra: List[str] = list(focus or []) + list(known or []) + [CTX.UPDATE_INSTRUCTIONS]
    if question:
        extra += ["", "# The question to put to them", "",
                  "The reader's choice decides the next rows and only they can",
                  "make it. Ask this, as a `questions` or `focus` card with the",
                  "reasonable options and a word on each:", "", question]
    with trace.span("model.call", agent="brainstorm"):
        card = BRAIN.ask(transcript, context, engine=engine, root=root, extra=extra)
    if not isinstance(card, dict) or not card.get("ok"):
        error = (card or {}).get("error") if isinstance(card, dict) else ""
        return {"ok": False, "error": str(error or "Bart could not answer")}
    return {"ok": True, "say": str(card.get("say") or ""),
            "card": str(card.get("card") or "none"),
            "replies": REPLIES.from_card(card), "contextUpdates": card.get("contextUpdates") or []}
