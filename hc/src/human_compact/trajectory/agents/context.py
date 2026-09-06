"""Shared context updates: what an agent learned, kept for the next one.

There is no Memory agent. A fact found while building, a preference the
reader stated, a constraint of the project, a decision taken, a
dependency discovered, a row's status, a run's result, an artifact, a
verdict: each is one update of a named kind, appended to
``agent_context.json`` in the chat's session directory, and ``render``
turns the durable ones into the lines every agent's prompt carries under
"What is already known". Infrastructure, not a role.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import chat_state as CS
from ..secure_io import atomic_write_json

NEW_FACT = "new_fact"
USER_PREFERENCE = "user_preference"
PROJECT_CONSTRAINT = "project_constraint"
DECISION = "decision"
DISCOVERED_DEPENDENCY = "discovered_dependency"
TODO_STATUS = "todo_status"
RUN_RESULT = "run_result"
ARTIFACT = "artifact"
VERIFICATION_RESULT = "verification_result"

KINDS = (NEW_FACT, USER_PREFERENCE, PROJECT_CONSTRAINT, DECISION,
         DISCOVERED_DEPENDENCY, TODO_STATUS, RUN_RESULT, ARTIFACT,
         VERIFICATION_RESULT)

# The kinds worth telling the next agent about in prose. A row's status
# and a run's result are on the tree and the run record already; they are
# kept for the record, not repeated into every prompt.
SAID = (NEW_FACT, USER_PREFERENCE, PROJECT_CONSTRAINT, DECISION,
        DISCOVERED_DEPENDENCY, ARTIFACT, VERIFICATION_RESULT)

KEEP = 200
MAX_TEXT = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path(session_id: str, root: Optional[Path]) -> Path:
    return CS.paths(session_id, root).session_dir / "agent_context.json"


def load(session_id: str, root: Optional[Path]) -> List[Dict[str, Any]]:
    try:
        value = json.loads(path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = value.get("updates") if isinstance(value, dict) else None
    return [u for u in items if isinstance(u, dict)] if isinstance(items, list) else []


def update(kind: str, text: str, *, subgoal_id: str = "", todo_id: str = "",
           run_id: str = "", by: str = "system", **extra: Any) -> Dict[str, Any]:
    """One update, shaped and checked; not yet written."""
    if kind not in KINDS:
        raise ValueError("not a context update kind: %r" % (kind,))
    said = " ".join(str(text or "").split())[:MAX_TEXT]
    out = {"kind": kind, "text": said, "at": _now(), "by": str(by or "system"),
           "subgoalId": str(subgoal_id or ""), "todoId": str(todo_id or ""),
           "runId": str(run_id or "")}
    for key, value in extra.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


def apply(session_id: str, root: Optional[Path], item: Dict[str, Any]) -> Dict[str, Any]:
    """Append one update (as ``update`` shaped it) and keep the file bounded."""
    if not isinstance(item, dict) or item.get("kind") not in KINDS:
        raise ValueError("not a context update")
    items = load(session_id, root)
    items.append(item)
    spot = path(session_id, root)
    spot.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(spot, {"updates": items[-KEEP:]}, root=spot.parent)
    return item


def render(session_id: str, root: Optional[Path], subgoal_id: str = "",
           limit: int = 12) -> List[str]:
    """The lines a prompt carries: the durable updates, this piece's first."""
    items = [u for u in load(session_id, root) if u.get("kind") in SAID and u.get("text")]
    if subgoal_id:
        items.sort(key=lambda u: 0 if u.get("subgoalId") in ("", subgoal_id) else 1)
    items = items[-limit:] if not subgoal_id else items[:limit]
    if not items:
        return []
    lines = ["", "# What is already known", ""]
    for item in items:
        lines.append("- (%s) %s" % (item["kind"].replace("_", " "), item["text"]))
    return lines
