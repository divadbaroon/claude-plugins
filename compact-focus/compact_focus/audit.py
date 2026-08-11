from __future__ import annotations

import re
from typing import Any, Dict, List, Set


WORD_RE = re.compile(r"[A-Za-z0-9_.-]{3,}")
STOPWORDS = {
    "about",
    "active",
    "after",
    "also",
    "before",
    "constraint",
    "context",
    "decision",
    "from",
    "have",
    "into",
    "must",
    "next",
    "only",
    "preserve",
    "resolved",
    "should",
    "summarize",
    "that",
    "their",
    "then",
    "this",
    "through",
    "user",
    "with",
}


def _terms(value: Any) -> Set[str]:
    return {
        term.lower()
        for term in WORD_RE.findall(str(value or ""))
        if term.lower() not in STOPWORDS
    }


def audit_summary(review: Dict[str, Any], summary: str) -> Dict[str, Any]:
    """Conservative lexical check; never presented as semantic verification."""
    summary_terms = _terms(summary)
    items: List[Dict[str, Any]] = []
    possible = 0
    for item in review.get("items", []):
        if item.get("retention") == "demote":
            continue
        anchors = _terms(
            str(item.get("title") or "")
            + " "
            + str(item.get("summary") or "")
            + " "
            + str(item.get("next_step") or "")
        )
        matched = sorted(anchors & summary_terms)
        coverage = len(matched) / max(1, len(anchors))
        normalized_title = " ".join(str(item.get("title") or "").lower().split())
        title_present = bool(normalized_title and normalized_title in summary.lower())
        possible_omission = bool(anchors) and not title_present and (
            not matched or coverage < 0.08
        )
        if possible_omission:
            possible += 1
        items.append(
            {
                "item_id": item.get("id"),
                "title": item.get("title"),
                "retention": item.get("retention"),
                "anchor_count": len(anchors),
                "matched_anchors": matched[:20],
                "lexical_coverage": round(coverage, 3),
                "title_present": title_present,
                "possible_omission": possible_omission,
            }
        )
    return {
        "method": "conservative lexical anchors; not semantic verification",
        "summary_chars": len(summary),
        "checked_items": len(items),
        "possible_omissions": possible,
        "items": items,
    }
