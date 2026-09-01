"""The project's record as relational rows, for Supabase (Postgres).

``project_store.build`` answers "what should a reader see?" -- one nested
document, denormalized, with the derived halves spelled out so a pane or a
teammate's agent can read it without joining anything. That is the wrong
shape for a database, and this module is the other answer: the same facts,
flattened into the rows a table wants, with everything derivable left out.

What changes on the way across:

* IDENTITY. A goal's id is unique only within its chat, and a project is
  identified on disk by the digest of its directory -- so the same
  repository cloned to two machines is two projects. Here a project carries
  a UUID minted once and kept in its file, and every row under it takes a
  deterministic UUIDv5 in that project's namespace. The same workspace
  serializes to the same ids on every machine and every run, which is what
  makes an upsert idempotent without asking the server what it already has.
* DERIVED COLUMNS GO. ``todos_md``, ``attachments``, ``title_path``,
  ``depth``, ``child_keys`` and ``sibling_keys`` are all recomputable from
  the rows themselves. Stored, they are a second answer that goes stale;
  ``sibling_keys`` is quadratic besides. Hierarchy travels as ``parent_id``
  alone.
* OWNERSHIP ARRIVES. Every row carries ``user_id``. Row-level security is
  the whole of Postgres's answer to "whose row is this?", and a table
  without that column cannot have a policy written against it.
* TIMESTAMPS ARE COERCED. The stores pass ``updated_at`` through from
  whatever wrote it; ``timestamptz`` will not. Anything unparseable becomes
  null rather than failing the insert.
* DELETION BECOMES EXPRESSIBLE. A snapshot is the complete set of rows for
  one project, so a loader can upsert what is here and delete what is not --
  which is the only way a goal removed locally ever leaves the remote.

The payload::

    {"schema_version", "generated_at", "project_id", "user_id",
     "projects": [...], "chats": [...], "goals": [...], "todos": [...],
     "goal_sources": [...], "project_sources": [...],
     "related_prompts": [...]}

Every list is complete for this project: what is absent was deleted.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import goals as GM
from . import project_store as PS

SYNC_SCHEMA_VERSION = 1

# The namespace every project UUID that has to be *derived* hangs from. A
# project normally mints a random id and keeps it; this is the fallback for
# a caller that wants rows without writing to disk (a dry run, a read-only
# export), so that even then the ids are stable rather than fresh each time.
NAMESPACE = uuid.UUID("6f1b5a2e-3c47-5d18-9a0b-7e2c4d8f1a35")


def _uuid5(namespace: uuid.UUID, *parts: str) -> str:
    """A deterministic id for a row, from its parents and its local name."""
    return str(uuid.uuid5(namespace, "\x1f".join(parts)))


def _ts(value: Any) -> Optional[str]:
    """An ISO-8601 UTC instant, or None -- never a string Postgres refuses.

    The stores hold whatever wrote them: an ISO string, a float of seconds,
    or nothing at all. A ``timestamptz`` column takes the first and the
    second only after coercion, and would reject the rest mid-insert.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc
                                          ).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, limit: Optional[int] = None) -> str:
    """A column's worth of text: never None, optionally bounded."""
    out = value if isinstance(value, str) else ("" if value is None
                                                else str(value))
    return out[:limit] if limit else out


def project_uuid(root: Optional[Path], cwd, mint: bool = True) -> str:
    """The project's stable id, minted once and kept in its own file.

    Kept with the authored half rather than derived from the directory: a
    path is where a project sits today, not what it is. Move the checkout,
    clone it onto another machine, and the rows still line up.

    With *mint* false the id is derived instead of written -- for callers
    that want to look without leaving a mark.
    """
    existing = PS.load_project(root, cwd).get("id")
    if isinstance(existing, str):
        try:
            return str(uuid.UUID(existing))
        except (ValueError, AttributeError, TypeError):
            pass
    derived = _uuid5(NAMESPACE, PS._resolved(cwd))
    if not mint:
        return derived
    minted = str(uuid.uuid4())
    try:
        # Bookkeeping, not authorship: minting an id must not bring a project
        # the reader deleted back onto their screen.
        PS.save_project(root, cwd, {"id": minted}, revive=False)
    except OSError:
        return derived
    return minted


def goal_uuid(project_id: str, session_id: str, local_id: str) -> str:
    """The remote id of one goal, from the three names that make it.

    Derived, never looked up: the snapshot mints goal ids this way, so any
    caller holding the project's id, the chat and the goal's local id can
    name the same row without a round trip. The Archive's permanent delete
    is the one caller that needs it outside a snapshot.
    """
    return _uuid5(uuid.UUID(project_id), "goal", session_id, local_id)


def snapshot(root: Optional[Path], cwd, user_id: str,
             mint: bool = True) -> Dict[str, Any]:
    """Every row of one project, ready for ``hc_sync_project``.

    *user_id* is the ``auth.users`` id the rows belong to. It is stamped on
    every row rather than only on the project: a policy is written per
    table, and a join back to the project to find the owner is a policy that
    reads another table -- slower, and easy to get subtly wrong.
    """
    owner = _text(user_id)
    if not owner:
        raise ValueError("user_id is required: every row is owned")
    record = PS.build(root, cwd)
    pid = project_uuid(root, cwd, mint=mint)
    namespace = uuid.UUID(pid)
    section = record.get("project") or {}

    projects = [{
        "id": pid,
        "user_id": owner,
        "cwd": _text(section.get("cwd")),
        "name": _text(section.get("name"), 300),
        "objective": _text(section.get("objective"),
                           PS.PROJECT_OBJECTIVE_LIMIT),
        "description": _text(section.get("description"),
                             PS.PROJECT_DESCRIPTION_LIMIT),
        "generated_at": _ts(record.get("generated_at")),
    }]

    project_sources = [
        {"id": _uuid5(namespace, "project_source", str(src.get("id"))),
         "user_id": owner, "project_id": pid,
         "local_id": _text(src.get("id"), 40),
         "type": _text(src.get("type"), 40),
         "label": _text(src.get("label"), 300),
         "position": index}
        for index, src in enumerate(section.get("sources") or [])]

    chats, goals, todos, goal_sources, related = [], [], [], [], []

    for chat in record.get("chats") or []:
        session_id = _text(chat.get("session_id"))
        if not session_id:
            continue
        chats.append({
            "id": _uuid5(namespace, "chat", session_id),
            "user_id": owner, "project_id": pid,
            "session_id": session_id,
            "created_at": _ts(chat.get("created_at")),
            "updated_at": _ts(chat.get("updated_at")),
            "prompt_count": int(chat.get("prompt_count") or 0),
            "goal_count": int(chat.get("goal_count") or 0),
        })

    def gid_of(session_id: str, local_id: str) -> str:
        return goal_uuid(pid, session_id, local_id)

    for goal in record.get("goals") or []:
        session_id = _text(goal.get("session_id"))
        local_id = _text(goal.get("id"))
        if not session_id or not local_id:
            continue
        gid = gid_of(session_id, local_id)
        location = goal.get("location") or {}
        parent_local = location.get("parent_id")
        goals.append({
            "id": gid,
            "user_id": owner, "project_id": pid,
            "session_id": session_id,
            "local_id": local_id,
            # Hierarchy is one edge. Children, siblings, depth and the
            # titles above are all walks of these edges, computed by
            # whoever needs them rather than stored n times over.
            "parent_id": (gid_of(session_id, _text(parent_local))
                          if isinstance(parent_local, str) and parent_local
                          else None),
            "title": _text(goal.get("title"), 2000),
            "status": _text(goal.get("status")) or "active",
            "priority": _text(goal.get("priority")) or "normal",
            "origin": _text(goal.get("origin"), 200),
            "relevance": (goal.get("relevance")
                          if goal.get("relevance") in ("core", "supporting",
                                                       "unrelated")
                          else "core"),
            "relevance_why": _text(goal.get("relevance_why"), 200),
            "relevance_for": _text(goal.get("relevance_for"), 2000),
            "description": _text(goal.get("description")),
            "notes": _text(goal.get("notes")),
            "prompt": _text(goal.get("prompt")),
            "evidence_ids": [e for e in goal.get("evidence_ids") or []
                             if isinstance(e, str)],
            # Opaque records the workspace round-trips but never queries by
            # field: jsonb keeps them whole without inventing columns.
            "important": goal.get("important") or [],
            "updated_at": _ts(goal.get("updated_at")),
        })

        for position, row in enumerate(goal.get("todos") or []):
            row_id = _text(row.get("id"))
            if not row_id:
                continue
            status = _text(row.get("status"))
            todos.append({
                "id": _uuid5(namespace, "todo", session_id, local_id, row_id),
                "user_id": owner, "project_id": pid, "goal_id": gid,
                "local_id": row_id,
                # The rail is an ordered list and its order is meaning, not
                # presentation: a parent row is the one above its children.
                "position": position,
                "depth": int(row.get("depth") or 0),
                "text": _text(row.get("text")),
                "status": status if status in GM.TODO_STATUSES else "",
                "question": _text(row.get("question")),
            })

        for position, src in enumerate(goal.get("sources") or []):
            goal_sources.append({
                "id": _uuid5(namespace, "goal_source", session_id, local_id,
                             str(src.get("id"))),
                "user_id": owner, "project_id": pid, "goal_id": gid,
                "local_id": _text(src.get("id"), 40),
                "type": _text(src.get("type"), 40),
                "label": _text(src.get("label"), 300),
                "position": position,
            })

        for position, prompt in enumerate(goal.get("related_prompts") or []):
            prompt_id = _text(prompt.get("id"))
            if not prompt_id:
                continue
            related.append({
                "id": _uuid5(namespace, "related_prompt", session_id,
                             local_id, prompt_id),
                "user_id": owner, "project_id": pid, "goal_id": gid,
                "prompt_id": prompt_id,
                "text": _text(prompt.get("text")),
                "session_id": _text(prompt.get("session_id")),
                "auto": bool(prompt.get("auto")),
                "created_at": _ts(prompt.get("created_at")),
                "position": position,
            })

    return {
        "schema_version": SYNC_SCHEMA_VERSION,
        "generated_at": _ts(record.get("generated_at")),
        "project_id": pid,
        "user_id": owner,
        "projects": projects,
        "project_sources": project_sources,
        "chats": chats,
        "goals": goals,
        "todos": todos,
        "goal_sources": goal_sources,
        "related_prompts": related,
    }


# The tables a snapshot fills, in the order a loader must write them: a
# child's foreign key is only satisfiable once its parent is in.
TABLES = ("projects", "project_sources", "chats", "goals", "todos",
          "goal_sources", "related_prompts")


def counts(payload: Dict[str, Any]) -> Dict[str, int]:
    """How many rows of each table a snapshot holds -- for a caller that
    wants to say what it is about to send before sending it."""
    return {name: len(payload.get(name) or []) for name in TABLES}
