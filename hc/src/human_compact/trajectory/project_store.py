"""One JSON file per project: everything its chats know about it.

A project is the directory a chat was started in -- what its manifest
recorded as ``cwd`` -- so every chat started in that directory is a chat of
the same project. This module keeps one file per directory, holding both
halves of what is known about it:

* the AUTHORED half -- the objective and description in the reader's words,
  and the sources they saved to the project. Written by the workspace, never
  inferred, and never overwritten by a regeneration.
* the DERIVED half -- every goal of every chat in the directory, each with
  where it sits in its tree (parent, children, siblings, depth, the titles
  above it), its notes, its TODO rows with the status of each, its prompt,
  and the prompts marked as related to it. Regenerated from the chat stores.

The shape, in one glance::

    {"schema_version", "generated_at",
     "project": {"cwd", "name", "objective", "description", "sources"},
     "chats": [{"session_id", "created_at", "updated_at",
                "prompt_count", "goal_count"}],
     "goals": [{"key": "<session>:<goal id>", "id", "session_id",
                "title", "status", "priority", "origin", "description",
                "updated_at",
                "location": {"parent_id", "parent_key", "child_ids",
                             "child_keys", "sibling_ids", "sibling_keys",
                             "depth", "title_path"},
                "notes", "todos", "todos_md", "attachments",
                "prompt", "related_prompts", "sources",
                "evidence_ids", "important"}]}

A goal's id is unique only within its chat, so goals are keyed by both --
two chats in one directory each have a ``g1``.

The derived half is a snapshot, not a source of truth: it is rebuilt from
``goals.json``/``todos.json``/``prompts.json`` after every goal save, so
deleting the file loses nothing but the authored lines -- which is why those
are read out and written back around each regeneration rather than rebuilt.

Reads of the other chats' stores are taken without their locks on purpose. A
save already holds its own session's lock, and reaching for a second one is
how two chats saving at the same moment deadlock; every writer here replaces
files atomically, so a lock-free read sees one whole version or another,
never half of one.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import chat_state as CS
from . import goals as GM
from .secure_io import atomic_write_json

SCHEMA_VERSION = 1
PROJECT_OBJECTIVE_LIMIT = 2000
PROJECT_DESCRIPTION_LIMIT = 8000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolved(cwd) -> str:
    """A directory as one comparable string, links and ``~`` resolved."""
    try:
        return str(Path(str(cwd)).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(cwd)


def project_path(root: Optional[Path], cwd) -> Path:
    """Where the project's file lives: beside the chat sessions, keyed by the
    digest of the resolved directory -- never by the path itself, which is
    not a safe file name."""
    digest = hashlib.sha256(_resolved(cwd).encode("utf-8")).hexdigest()[:16]
    return CS._state_base(root) / "projects" / f"{digest}.json"


def read_file(root: Optional[Path], cwd) -> Dict[str, Any]:
    """The file as it stands, migrating the flat shape it was first written
    in (``{"cwd": ..., "objective": ...}``) into its ``project`` section.

    The flat keys are moved rather than copied: the file is meant to be
    opened and read, and a top-level ``objective`` left beside the one in
    ``project`` is a second answer to the same question that no writer
    afterwards keeps up to date.
    """
    if not cwd:
        return {}
    try:
        value = json.loads(project_path(root, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    if not isinstance(value.get("project"), dict):
        flat = ("cwd", "objective")
        section = {k: value[k] for k in flat if k in value}
        value = {k: v for k, v in value.items() if k not in flat}
        value["project"] = section
    return value


def load_project(root: Optional[Path], cwd) -> Dict[str, Any]:
    """The authored half only: what the reader wrote about the project."""
    section = read_file(root, cwd).get("project")
    section = section if isinstance(section, dict) else {}
    out: Dict[str, Any] = {}
    objective = section.get("objective")
    if isinstance(objective, str):
        out["objective"] = objective[:PROJECT_OBJECTIVE_LIMIT]
    description = section.get("description")
    if isinstance(description, str):
        out["description"] = description[:PROJECT_DESCRIPTION_LIMIT]
    sources = GM.normalize_sources(section.get("sources"))
    if sources:
        out["sources"] = sources
    identity = section.get("id")
    if isinstance(identity, str) and identity:
        out["id"] = identity[:64]
    return out


def save_project(root: Optional[Path], cwd, authored: Dict[str, Any]) -> Path:
    """Write the authored half, leaving the derived goals where they are."""
    record = read_file(root, cwd)
    section = record.get("project")
    section = dict(section) if isinstance(section, dict) else {}
    section.update(authored)
    return _write(root, cwd, dict(record, project=_project_section(
        cwd, section)))


def _write(root: Optional[Path], cwd, record: Dict[str, Any]) -> Path:
    path = project_path(root, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    record["schema_version"] = SCHEMA_VERSION
    record["generated_at"] = _now()
    atomic_write_json(path, record, root=path.parent)
    return path


def _project_section(cwd, authored: Dict[str, Any]) -> Dict[str, Any]:
    """The project's own metadata: where it is, what it is called, what it is
    for. ``description`` falls back to the objective -- the reader writes one
    line about a project in the workspace, and a consumer asking for the
    project's description should get it rather than an empty string."""
    objective = str(authored.get("objective") or "")[:PROJECT_OBJECTIVE_LIMIT]
    description = str(authored.get("description") or "")[
        :PROJECT_DESCRIPTION_LIMIT]
    section = {"cwd": str(cwd), "name": Path(str(cwd)).name,
               "objective": objective,
               "description": description or objective,
               "sources": GM.normalize_sources(authored.get("sources"))}
    # The project's own identity, when one has been minted: a directory is
    # where a project sits today, not what it is, so anything keyed on the
    # path alone calls the same repository on two machines two projects.
    identity = authored.get("id")
    if isinstance(identity, str) and identity:
        section["id"] = identity[:64]
    return section


def project_sessions(root: Optional[Path], cwd) -> List[str]:
    """Every chat started in this directory, oldest state first.

    The manifest is read as written rather than through ``load_manifest``,
    which hands back a blank default when a seeded or copied workspace's
    manifest disagrees with its directory name -- the directory a chat was
    started in is still the directory it was started in.
    """
    base = CS._state_base(root)
    target = _resolved(cwd)
    out = []
    try:
        entries = sorted(base.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            manifest = CS.paths(entry.name, root).manifest
        except ValueError:
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        here = value.get("cwd") if isinstance(value, dict) else None
        if isinstance(here, str) and here and _resolved(here) == target:
            out.append(entry.name)
    return out


def _location(goal: Dict[str, Any], goals: List[Dict[str, Any]],
              session_id: str) -> Dict[str, Any]:
    """Where a goal sits in its chat's tree: the parent it hangs from, the
    goals under it, the ones beside it, and the titles above it."""
    gid = goal.get("id")
    parent_id = goal.get("parent_goal_id")
    by_id = {g.get("id"): g for g in goals}

    def key(other):
        return f"{session_id}:{other}" if other else None

    path, seen, walk = [], set(), goal
    while isinstance(walk, dict) and walk.get("id") not in seen:
        seen.add(walk.get("id"))
        path.append(str(walk.get("title") or ""))
        walk = by_id.get(walk.get("parent_goal_id"))
    path.reverse()
    children = [g.get("id") for g in goals if g.get("parent_goal_id") == gid]
    siblings = [g.get("id") for g in goals
                if g.get("parent_goal_id") == parent_id and g.get("id") != gid]
    return {"parent_id": parent_id, "parent_key": key(parent_id),
            "child_ids": children, "child_keys": [key(c) for c in children],
            "sibling_ids": siblings, "sibling_keys": [key(s) for s in siblings],
            "depth": len(path), "title_path": path}


def _related_prompts(goal: Dict[str, Any],
                     prompts: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The prompts marked as belonging with this goal, whole.

    ``auto`` says which of them inference linked from the evidence it had
    already cited, so a reader can tell the reader's own marks apart from the
    machine's. A prompt the user detached is not here: a detach is a
    decision, and the store keeps it out of ``prompt_ids`` for good.
    """
    automatic = set(goal.get("auto_prompt_ids") or [])
    out = []
    for pid in goal.get("prompt_ids") or []:
        prompt = prompts.get(pid)
        if not isinstance(prompt, dict):
            continue
        out.append({"id": pid, "text": str(prompt.get("text") or ""),
                    "created_at": prompt.get("created_at"),
                    "session_id": prompt.get("session_id"),
                    "auto": pid in automatic})
    return out


def _goal_record(goal: Dict[str, Any], goals: List[Dict[str, Any]],
                 session_id: str, prompts: Dict[str, Dict[str, Any]],
                 items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    todos = GM.normalize_todo_items(goal.get("todo_items"))
    return {
        "key": f"{session_id}:{goal.get('id')}",
        "id": goal.get("id"),
        "session_id": session_id,
        "title": str(goal.get("title") or ""),
        "status": str(goal.get("status") or "active"),
        "priority": str(goal.get("priority") or "normal"),
        "origin": str(goal.get("origin") or ""),
        "description": str(goal.get("description") or ""),
        "updated_at": goal.get("updated_at"),
        "location": _location(goal, goals, session_id),
        # The goal's whole markdown document, as written.
        "notes": str(goal.get("notes") or ""),
        # The rail's rows are the list; the markdown beside them is derived
        # from the same rows, for a consumer that wants it as text.
        "todos": todos,
        "todos_md": GM.render_todos(todos),
        "attachments": GM.todo_attachments(todos),
        "prompt": str(goal.get("prompt_md") or ""),
        "related_prompts": _related_prompts(goal, prompts),
        "sources": GM.normalize_sources(goal.get("sources")),
        "evidence_ids": [e for e in goal.get("evidence_ids") or []
                         if isinstance(e, str)],
        "important": [items[iid] for iid in goal.get("important_item_ids") or []
                      if iid in items],
    }


def build(root: Optional[Path], cwd) -> Dict[str, Any]:
    """The whole record for one project, authored half and derived half."""
    authored = load_project(root, cwd)
    chats, goal_rows = [], []
    for session_id in project_sessions(root, cwd):
        try:
            goals, important = CS.load_goals(session_id, root)
            prompts = {p.get("id"): p for p in CS.load_prompts(session_id, root)
                       if isinstance(p, dict)}
        except (OSError, ValueError):
            continue
        items = {i.get("id"): i for i in important.get("items", [])
                 if isinstance(i, dict)}
        rows = [g for g in goals.get("goals", []) if isinstance(g, dict)]
        manifest = CS.load_manifest(session_id, root)
        chats.append({"session_id": session_id,
                      "updated_at": manifest.get("updated_at"),
                      "created_at": manifest.get("created_at"),
                      "prompt_count": len(prompts),
                      "goal_count": len(rows)})
        goal_rows.extend(_goal_record(g, rows, session_id, prompts, items)
                         for g in rows)
    return {"schema_version": SCHEMA_VERSION, "generated_at": _now(),
            "project": _project_section(cwd, authored),
            "chats": chats, "goals": goal_rows}


def write(root: Optional[Path], cwd) -> Optional[Path]:
    """Regenerate the project's file. ``None`` when there is no directory to
    write one for -- a chat whose manifest never recorded a cwd belongs to no
    project, and inventing a digest for "" would collect all of them."""
    if not cwd:
        return None
    return _write(root, cwd, build(root, cwd))


def refresh_for_session(session_id: str,
                        root: Optional[Path] = None) -> Optional[Path]:
    """Rewrite the project file of whichever directory this chat works in.

    Called after a goal save. Never raises into its caller: the file is a
    snapshot of state that is already durable elsewhere, so failing to
    refresh it must not fail the save that state just came from.
    """
    try:
        manifest = CS.paths(session_id, root).manifest
        value = json.loads(manifest.read_text(encoding="utf-8"))
        cwd = value.get("cwd") if isinstance(value, dict) else None
        if not isinstance(cwd, str) or not cwd:
            return None
        return write(root, cwd)
    except Exception:                                    # noqa: BLE001
        return None
