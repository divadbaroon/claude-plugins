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
    with CS.session_lock(session_id, root, wait_s=5):
        item = update(item["kind"], item.get("text", ""),
                      **{k: v for k, v in item.items() if k not in ("kind", "text")})
        items = load(session_id, root)
        def identity(u):
            key = u.get("key") or (u.get("todoId") if u.get("kind") == TODO_STATUS else
                                   "current" if u.get("kind") in (RUN_RESULT, VERIFICATION_RESULT) else "")
            return (u.get("kind"), u.get("subgoalId"), key) if key else None
        subject = identity(item)
        items = [u for u in items if not (subject and identity(u) == subject)
                 and not (u.get("kind") == item["kind"] and u.get("text") == item["text"]
                          and u.get("subgoalId") == item.get("subgoalId")
                          and u.get("todoId") == item.get("todoId"))]
        items.append(item)
        spot = path(session_id, root)
        spot.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(spot, {"updates": items[-KEEP:]}, root=spot.parent)
        return item


def render(session_id: str, root: Optional[Path], subgoal_id: str = "",
           limit: int = 12) -> List[str]:
    """The lines a prompt carries: the durable updates, this piece's first."""
    items = [u for u in reversed(load(session_id, root)) if u.get("kind") in SAID and u.get("text")]
    if subgoal_id:
        items.sort(key=lambda u: 0 if u.get("subgoalId") in ("", subgoal_id) else 1)
    items = items[:limit]
    if not items:
        return []
    lines = ["", "# What is already known", ""]
    for item in items:
        lines.append("- (%s) %s" % (item["kind"].replace("_", " "), item["text"]))
    return lines


def assemble(session_id, root, event, carry=None):
    """Bound each section independently; selected work survives large projects."""
    from .. import goals as GM, build, reader, project_store
    from . import events
    carry = carry or {}
    goals, important = CS.load_goals(session_id, root)
    selected = str(event.get("subgoalId") or "")
    piece = GM.by_id(goals, selected) or {}
    parent = GM.by_id(goals, piece.get("parent_goal_id")) or piece
    plan = sorted(goals.get("goals") or [], key=lambda g: g.get("id") != selected)
    cwd = CS.bound_project(session_id, root) or CS.load_manifest(session_id, root).get("cwd")
    authored = project_store.load_project(root, cwd) if cwd else {}
    objective = authored.get("objective") or "\n".join(str(g.get("title") or "")
        for g in plan if not g.get("parent_goal_id"))
    compact = [{k: g.get(k) for k in ("id", "parent_goal_id", "title", "status")}
               | {"todos": [{k: r.get(k) for k in ("id", "text", "status", "acceptance")}
                            for r in (g.get("todo_items") or [])[:30]]} for g in plan[:40]]
    chats = CS.load_bart_chats(session_id, root)
    sections = events.bounded({
        "project": {"objective": str(objective)[:4000],
                    "direction": authored.get("description") or important,
                    "context": str(carry.get("context") or "")[:4000]},
        "currentGoal": {k: parent.get(k) for k in ("id", "title", "status", "notes")}, "plan": compact, "selectedSubgoal": selected,
        "currentRun": build.load_run(session_id, root, selected) or {},
        "user": reader.load(root), "persistentContext": list(reversed(load(session_id, root)[-40:])),
        "recentResults": events.read(session_id, root, limit=8,
            types=["chat.replied", "brainstorm.replied", "path.planned", "verify.passed", "verify.failed"]),
        "recentBartTurns": (carry.get("transcript") or chats.get(selected) or [])[-12:],
        "recentActivity": events.summary(session_id, root), "triggerResult": event.get("payload")},
        chars=1000, items=40)

    budgets = {"project": 3000, "currentGoal": 1200, "plan": 8000, "selectedSubgoal": 100,
               "currentRun": 1800, "user": 1600, "persistentContext": 4000,
               "recentResults": 2000, "recentBartTurns": 2400, "recentActivity": 2400,
               "triggerResult": 3000}
    return {key: fit(value, budgets[key]) for key, value in sections.items()}


def fit(value, budget):
    """Bound a structured section without cutting JSON into invalid fragments."""
    if len(json.dumps(value, ensure_ascii=False, default=str)) <= budget:
        return value
    if isinstance(value, str):
        return value[:max(0, budget // 2)]
    if isinstance(value, list):
        out = []
        for item in value:
            item = fit(item, min(1800, budget // 2))
            if len(json.dumps(out + [item], ensure_ascii=False, default=str)) > budget:
                break
            out.append(item)
        return out
    if isinstance(value, dict):
        share = max(20, (budget - len(value)*24) // max(1, len(value)))
        return {k: fit(v, share) for k, v in value.items()}
    return value


UPDATE_INSTRUCTIONS = """Also return contextUpdates: [{kind,text,key}]. Record only facts
actually learned, explicit user preferences, project constraints, decisions,
discovered dependencies or artifacts. Use a stable subject key so later facts
supersede stale ones. Never treat a suggestion as an accepted user preference."""


def normalize_updates(value, by="agent", subgoal_id=""):
    out = []
    for item in (value if isinstance(value, list) else [])[:12]:
        if not isinstance(item, dict) or item.get("kind") not in KINDS or not item.get("text"):
            continue
        scope = str(item.get("subgoalId") or ("" if item["kind"] in
                    (USER_PREFERENCE, PROJECT_CONSTRAINT) else subgoal_id))
        out.append(update(item["kind"], item["text"], by=by, subgoal_id=scope,
            todo_id=str(item.get("todoId") or ""), key=str(item.get("key") or "")[:120]))
    return out
