"""A shared project, in the shape the workspace already knows how to draw.

The browser reads one thing: ``/api/state``. Everything the goal tree does
-- folding, the TODO rail, notes, search, the project chip -- is drawn from
the ``goals`` list in that payload, which is the same shape ``goals.json``
holds on disk. So a shared workspace does not need a second renderer. It
needs this: the rows Postgres has for one project, turned back into that
list, with a contributor's name on the ones that are not yours.

Two people's goals meet here, and their local ids collide -- both trees
have a ``g1``. The row's UUID is used as the goal id instead, and
``parent_id`` already points at UUIDs, so the tree survives the trip. The
local id is kept alongside for anyone reading the two side by side.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Fields the browser reads that a shared goal cannot answer for. Present so
# the page sees one shape in every scope, empty because there is nothing
# truthful to put in them.
_ABSENT: Dict[str, Any] = {
    "todos": [], "todos_md": "", "opening": "",
    "detached_prompt_ids": [], "important_item_ids": [],
}


def _goal(row: Dict[str, Any], todos: List[Dict[str, Any]],
          sources: List[Dict[str, Any]], prompts: List[Dict[str, Any]],
          mine: bool, who: str) -> Dict[str, Any]:
    goal = dict(_ABSENT)
    goal.update({
        "id": row.get("id"),
        "title": str(row.get("title") or ""),
        "status": str(row.get("status") or "active"),
        "parent_goal_id": row.get("parent_id"),
        "description": str(row.get("description") or ""),
        "priority": str(row.get("priority") or "normal"),
        "notes": str(row.get("notes") or ""),
        "prompt_md": str(row.get("prompt") or ""),
        "origin": str(row.get("origin") or ""),
        "relevance": str(row.get("relevance") or "core"),
        "relevance_why": str(row.get("relevance_why") or ""),
        "relevance_for": str(row.get("relevance_for") or ""),
        # Present for shape, and always empty: it is an absolute path on
        # the machine of whoever made the goal, and a build here would run
        # in a directory that does not exist on this one.
        "project_cwd": "",
        "updated_at": row.get("updated_at"),
        "evidence_ids": list(row.get("evidence_ids") or []),
        "sources": [{"id": s.get("local_id") or s.get("id"),
                     "type": s.get("type") or "doc",
                     "label": s.get("label") or ""} for s in sources],
        "todo_items": [{"id": t.get("local_id") or t.get("id"),
                        "text": str(t.get("text") or ""),
                        "depth": int(t.get("depth") or 0),
                        "status": str(t.get("status") or ""),
                        "question": str(t.get("question") or "")}
                       for t in todos],
        "prompt_ids": [p.get("prompt_id") for p in prompts],
        "auto_prompt_ids": [p.get("prompt_id") for p in prompts
                            if p.get("auto")],
        # What the local tree has no word for: whose goal this is, and
        # whether this reader may touch it. The page shows the first and
        # obeys the second.
        "shared_local_id": row.get("local_id"),
        "shared_session_id": row.get("session_id"),
        "shared_mine": mine,
        "shared_author": who,
        "shared_readonly": not mine,
    })
    return goal


def _author(user_id: Optional[str], names: Dict[str, str]) -> str:
    """What to call whoever wrote this goal.

    The name they chose. Failing that the local part of an email, which is
    a poor name but better than none. Failing that a short form of the id --
    never nothing, because an unattributed goal in a shared tree is the
    confusing case.
    """
    if not user_id:
        return "someone"
    named = str(names.get(user_id) or "").strip()
    if named:
        return named.split("@", 1)[0] if "@" in named else named
    return "contributor " + str(user_id)[:8]


def build(rows: Dict[str, Any], me: Optional[str],
          names: Optional[Dict[str, str]] = None,
          can_write: bool = False) -> Dict[str, Any]:
    """The ``/api/state`` payload for a shared project.

    *rows* is what the database gave back; *me* is the reader's user id, so
    the tree can say which goals are theirs; *names* maps user ids to the
    emails the owner invited them by, when the reader is allowed to know.
    """
    names = names or {}
    project = (rows.get("projects") or [{}])[0] if rows.get("projects") else {}
    todos_by_goal: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows.get("todos") or []:
        todos_by_goal.setdefault(row.get("goal_id"), []).append(row)
    for bucket in todos_by_goal.values():
        bucket.sort(key=lambda r: int(r.get("position") or 0))

    sources_by_goal: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows.get("goal_sources") or []:
        sources_by_goal.setdefault(row.get("goal_id"), []).append(row)
    for bucket in sources_by_goal.values():
        bucket.sort(key=lambda r: int(r.get("position") or 0))

    prompts_by_goal: Dict[str, List[Dict[str, Any]]] = {}
    prompts: List[Dict[str, Any]] = []
    seen_prompts = set()
    for row in rows.get("related_prompts") or []:
        prompts_by_goal.setdefault(row.get("goal_id"), []).append(row)
        pid = row.get("prompt_id")
        if pid and pid not in seen_prompts:
            seen_prompts.add(pid)
            prompts.append({"id": pid, "role": "user",
                            "text": str(row.get("text") or ""),
                            "session_id": row.get("session_id") or "",
                            "created_at": row.get("created_at")})

    goals = []
    contributors: Dict[str, int] = {}
    for row in rows.get("goals") or []:
        owner = row.get("user_id")
        mine = bool(me) and owner == me
        if not mine:
            contributors[owner] = contributors.get(owner, 0) + 1
        goals.append(_goal(
            row, todos_by_goal.get(row.get("id")) or [],
            sources_by_goal.get(row.get("id")) or [],
            prompts_by_goal.get(row.get("id")) or [],
            mine, _author(owner, names)))
    # Newest first, as the local tree is.
    goals.sort(key=lambda g: str(g.get("updated_at") or ""), reverse=True)

    return {
        "goals": goals,
        "items": [],
        "prompts": prompts,
        "generated_at": project.get("generated_at") or "",
        "sessions": None,
        "analyzer": None,
        "notices": [],
        # The workspace the reader knows -- the rail, the project chip, the
        # search, the full-bleed layout -- is all gated on scope being
        # "chat"; anything else falls through to the old vault page. A
        # shared project is not a chat, but it is that workspace, so it
        # says so and marks itself shared alongside.
        "session_id": project.get("id"),
        "injection": {"cached": False, "last_delta_chars": None,
                      "last_at": None, "active": False, "reads": []},
        "agent_runs": {},
        "agent_claim": None,
        "scope": "chat",
        "project": {
            "cwd": project.get("cwd") or "",
            "name": project.get("name") or "shared project",
            "branch": "", "remote": "",
            "objective": project.get("objective") or "",
        },
        "provider": None,
        "revision": str(project.get("generated_at") or "") + ":" + str(len(goals)),
        # What only a shared scope has to say.
        "shared": {
            "project_id": project.get("id"),
            # Whether this reader may change anything at all. Per goal it
            # is narrower still -- a contributor writes their own rows and
            # nobody else's -- which each goal carries as shared_readonly.
            "readonly": not can_write,
            "can_write": bool(can_write),
            "me": me,
            "contributors": [
                {"user_id": uid, "name": _author(uid, names), "goals": n}
                for uid, n in sorted(contributors.items(),
                                     key=lambda kv: -kv[1])],
            "mine": sum(1 for g in goals if g.get("shared_mine")),
        },
    }
