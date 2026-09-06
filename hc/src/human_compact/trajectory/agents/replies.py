"""A card or an answer, as the messages the page draws.

The page has two kinds of message from Bart: text, and a proposal -- one
row the reader can add. Everything an agent says is rendered into those.
Moved here from ui.py so every agent renders the one way; ui keeps its
old names pointing at these.
"""
from __future__ import annotations

from typing import Any, Dict, List


def choice(title, options, note="") -> str:
    """A question or a choice, as one message the reader can answer by
    typing: the title, then each option on its own line with its
    argument, when it has one."""
    lines = [str(title or "").strip()]
    said = str(note or "").strip()
    if said:
        lines[0] = (lines[0] + " (" + said + ")") if lines[0] else said
    for option in options or []:
        if not isinstance(option, dict):
            continue
        label = str(option.get("label") or "").strip()
        why = str(option.get("why") or "").strip()
        if label:
            lines.append("- " + label + (" -- " + why if why else ""))
    return "\n".join(line for line in lines if line)


def from_card(card: Dict[str, Any]) -> List[Dict[str, str]]:
    """A brainstorm card as the messages the page draws, in order.

    Prose is a text message. Each row proposed is its own proposal, whether
    it came flat or under a piece: the page has one list, the subgoal's,
    and a proposal is one row for it. A question or a choice is said as
    text with its options under it, so the reader answers by typing; the
    page has no form to draw them in. Goals the model proposes are said,
    not offered: nothing on this page makes a goal.
    """
    replies: List[Dict[str, str]] = []
    say = str(card.get("say") or "").strip()
    if say:
        replies.append({"kind": "text", "text": say})
    kind = str(card.get("card") or "")
    if kind == "questions":
        for item in (card.get("questions") or {}).get("items") or []:
            if isinstance(item, dict):
                replies.append({"kind": "text", "text": choice(
                    item.get("title"), item.get("options"), item.get("subtitle"))})
    elif kind == "focus":
        focus = card.get("focus") or {}
        replies.append({"kind": "text", "text": choice(
            focus.get("title"), focus.get("options"))})
    elif kind == "goals":
        for goal in card.get("goals") or []:
            if isinstance(goal, dict):
                replies.append({"kind": "text", "text": choice(
                    goal.get("label"), [], goal.get("why"))})
    elif kind == "todos":
        rows = list(card.get("todos") or [])
        for piece in card.get("subgoals") or []:
            if isinstance(piece, dict):
                rows.extend(piece.get("todos") or [])
        for text in rows:
            said = str(text or "").strip()
            if said:
                replies.append({"kind": "proposal", "text": said})
    if not replies:
        replies.append({"kind": "text", "text": "Bart had nothing to add."})
    return replies


def from_chat(answer: Dict[str, Any]) -> List[Dict[str, str]]:
    """The Chat agent's answer: its prose, then any row it put forward."""
    replies: List[Dict[str, str]] = []
    say = str(answer.get("say") or "").strip()
    if say:
        replies.append({"kind": "text", "text": say})
    for text in answer.get("todos") or []:
        said = str(text or "").strip()
        if said:
            replies.append({"kind": "proposal", "text": said})
    return replies
