"""Editing a goal from the shared workspace, in both places it lives.

A goal edited here has two homes: the row in Postgres that both people
read, and the goal in its author's own ``goals.json``. Writing one and not
the other is how the two drift, so this writes both, in an order chosen for
what happens when the second one fails.

Postgres first. It is the copy the collaborator reads, it is the one that
can refuse the write -- because someone moved the row while the reader was
typing -- and a refusal there should leave the vault untouched. Only once
it has accepted does the same edit go into the vault. If the vault write
then fails, the caller is told plainly: the shared copy moved and the local
one did not, which is a thing to fix rather than a thing to hide.

The vault half is done through ``chat_state.save_goals``, which holds the
same cross-process lock every other writer holds. Reaching around it would
be the one way to corrupt a file that two processes are already sharing
carefully.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from . import chat_state as CS
from . import goals as GM
from . import supabase_client as SB

# What a reader may change from the shared tree. Deliberately short: these
# are the fields the tree shows. Anything else -- parentage, ids, the TODO
# rail -- has its own machinery and would need its own care.
EDITABLE = ("title", "notes", "description", "prompt", "status",
            "priority")


def _apply_locally(session_id: str, local_id: str, fields: Dict[str, Any],
                   root: Optional[Path]) -> Dict[str, Any]:
    """Put the same edit into the goal's own chat, under its lock."""
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        rows = goals.get("goals") or []
        for row in rows:
            if row.get("id") != local_id:
                continue
            if "title" in fields:
                row["title"] = str(fields["title"])[:120]
            if "notes" in fields:
                row["notes"] = str(fields["notes"])
            if "description" in fields:
                row["description"] = str(fields["description"])
            if "prompt" in fields:
                row["prompt_md"] = str(fields["prompt"])
            if GM.norm_status(fields.get("status")):
                row["status"] = GM.norm_status(fields["status"])
            if fields.get("priority") in ("urgent", "high", "normal"):
                row["priority"] = fields["priority"]
            GM.sanitize(goals)
            if not CS.save_goals(session_id, goals, important, root):
                return {"ok": False, "error": "the chat's goals changed "
                                              "during the save"}
            return {"ok": True}
    return {"ok": False, "error": "that goal is not in this chat any more"}


def update_goal(goal_id: str, expect: Optional[str], fields: Dict[str, Any],
                root: Optional[Path] = None,
                project_id: Optional[str] = None) -> Dict[str, Any]:
    """One edit, into Postgres and then into the vault it came from."""
    wanted = {k: v for k, v in (fields or {}).items() if k in EDITABLE}
    if not wanted:
        return {"ok": False, "error": "nothing to change"}

    config = SB.load_config(root)
    session = SB.current_session(root)
    out = SB._rpc("hc_update_goal",
                  {"p_goal_id": str(goal_id), "p_expect": expect,
                   "p_fields": wanted},
                  config, session["access_token"])
    if not isinstance(out, dict) or not out.get("ok"):
        # A conflict is handed back whole: the caller shows what the row
        # says now, so the reader can choose rather than guess.
        return dict(out or {"ok": False, "error": "the edit was refused"})

    # The cached view is now behind; the next poll should see the edit
    # rather than the copy it was made against.
    # The row does not name its project, and the caller does -- a shared
    # workspace serves exactly one.
    if project_id:
        try:
            SB.forget_shared(project_id)
        except Exception:  # noqa: BLE001 - a stale cache is not a failed edit
            pass

    local = {"ok": True}
    session_id, local_id = out.get("session_id"), out.get("local_id")
    if session_id and local_id:
        try:
            local = _apply_locally(session_id, local_id, wanted, root)
        except (OSError, ValueError, RuntimeError) as exc:
            local = {"ok": False, "error": str(exc)[:200]}
    else:
        local = {"ok": False, "error": "the shared row names no chat"}

    if not local.get("ok"):
        # Said, not swallowed: the two copies now disagree, and the reader
        # is the only one who can decide what to do about it.
        return {"ok": True, "updated_at": out.get("updated_at"),
                "local_ok": False,
                "warning": "saved to the shared project, but this chat's own "
                           "copy did not take it: " + str(local.get("error"))}
    return {"ok": True, "updated_at": out.get("updated_at"), "local_ok": True}


# The artifact posts the whole tree after any edit -- that is how it saves.
# A shared tree is two people's goals at once, so posting it wholesale would
# be one browser submitting the other's rows, which row security refuses and
# should. Instead the tree is compared with what was served, and only the
# goals that actually changed, and belong to this reader, are written -- one
# concurrency-checked update each.

def _flatten(nested, out=None):
    """The posted tree as {id: node}, children and all."""
    out = {} if out is None else out
    for node in nested if isinstance(nested, list) else []:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("id"), str):
            out[node["id"]] = node
        _flatten(node.get("children"), out)
    return out


def _posted_fields(node: Dict[str, Any]) -> Dict[str, Any]:
    """One posted node in the words the database uses.

    The artifact has its own names -- desc, prompt_md, prio -- and says
    done/inprog rather than a status. This is that translation, and it has
    to match what the goal tree itself does or an edit means one thing on
    the way out and another on the way back.
    """
    done = bool(node.get("done"))
    status = ("completed" if done else
              "in_progress" if node.get("status") == "inprog" else "active")
    prio = node.get("prio")
    return {
        "title": str(node.get("title") or "").strip()[:120],
        "notes": str(node.get("notes") or ""),
        "description": str(node.get("desc") or ""),
        "prompt": str(node.get("prompt_md") or ""),
        "status": status,
        "priority": prio if prio in ("urgent", "high", "normal") else "normal",
    }


def _served_fields(goal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": str(goal.get("title") or ""),
        "notes": str(goal.get("notes") or ""),
        "description": str(goal.get("description") or ""),
        "prompt": str(goal.get("prompt_md") or ""),
        "status": str(goal.get("status") or "active"),
        "priority": str(goal.get("priority") or "normal"),
    }


def _rows_of(node: Dict[str, Any]):
    """The TODO rail as the rail itself stores it."""
    out = []
    for row in node.get("todo_items") or []:
        if not isinstance(row, dict):
            continue
        out.append({"local_id": str(row.get("id") or ""),
                    "text": str(row.get("text") or ""),
                    "depth": int(row.get("depth") or 0),
                    "status": str(row.get("status") or ""),
                    "question": str(row.get("question") or "")})
    return out


def _parents_of(nested, parent=None, out=None):
    """Who each posted node hangs from, so a move can be noticed."""
    out = {} if out is None else out
    for node in nested if isinstance(nested, list) else []:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("id"), str):
            out[node["id"]] = parent
            _parents_of(node.get("children"), node["id"], out)
        else:
            _parents_of(node.get("children"), parent, out)
    return out


def _rpc(name, body, root):
    config = SB.load_config(root)
    session = SB.current_session(root)
    return SB._rpc(name, body, config, session["access_token"])


def apply_tree(project_id: str, nested, root: Optional[Path] = None
               ) -> Dict[str, Any]:
    """Write whatever the reader actually changed, and nothing else."""
    served = SB.shared_payload(project_id, root)
    by_id = {g.get("id"): g for g in served.get("goals") or []}
    posted = _flatten(nested)

    parents = _parents_of(nested)
    changed, refused, conflicts, made = [], [], [], []

    # Goals the tree has that the server does not. The artifact mints an id
    # locally when the reader adds a row, so a node nobody served is a new
    # goal -- not a stray. Written first, so a child added under it in the
    # same save has a parent to point at.
    for gid, node in posted.items():
        if gid in by_id or not str(node.get("title") or "").strip():
            continue
        parent = parents.get(gid)
        out = _rpc("hc_create_goal",
                   {"p_project_id": project_id,
                    "p_title": str(node.get("title"))[:120],
                    "p_parent_id": parent if parent in by_id else None}, root)
        if out.get("ok"):
            made.append({"posted_id": gid, "id": out.get("id")})
        else:
            refused.append({"id": gid, "title": str(node.get("title"))[:60],
                            "error": out.get("error")})
    if made:
        SB.forget_shared(project_id)
        served = SB.shared_payload(project_id, root)
        by_id = {g.get("id"): g for g in served.get("goals") or []}

    for gid, node in posted.items():
        goal = by_id.get(gid)
        if not goal:
            continue                     # just created, or not a goal
        want = _posted_fields(node)
        have = _served_fields(goal)
        delta = {k: v for k, v in want.items() if have.get(k) != v}
        if not delta:
            continue
        if goal.get("shared_readonly"):
            # Someone else's row. Say so rather than dropping it quietly --
            # the reader typed something and deserves to know it did not
            # land.
            refused.append({"id": gid, "title": have["title"],
                            "author": goal.get("shared_author") or "someone"})
            continue
        out = update_goal(gid, goal.get("updated_at"), delta, root,
                          project_id=project_id)
        if out.get("conflict"):
            conflicts.append(dict(out, id=gid, title=have["title"]))
        elif out.get("ok"):
            changed.append(gid)
        else:
            refused.append({"id": gid, "title": have["title"],
                            "error": out.get("error")})

    # Where each goal now hangs, and what its rail holds. Both are the
    # reader's own rows only, and both are checked against the copy that
    # was served -- a move onto a branch someone else has since changed is
    # the same overwrite as a field edit would be.
    for gid, node in posted.items():
        goal = by_id.get(gid)
        if not goal or goal.get("shared_readonly"):
            continue
        wanted_parent = parents.get(gid)
        if wanted_parent is not None and wanted_parent not in by_id:
            wanted_parent = None
        if goal.get("parent_goal_id") != wanted_parent:
            out = _rpc("hc_move_goal", {"p_goal_id": gid,
                                        "p_expect": goal.get("updated_at"),
                                        "p_parent_id": wanted_parent}, root)
            if out.get("conflict"):
                conflicts.append(dict(out, id=gid,
                                      title=_served_fields(goal)["title"]))
            elif out.get("ok"):
                changed.append(gid)
            else:
                refused.append({"id": gid,
                                "title": _served_fields(goal)["title"],
                                "error": out.get("error")})
            continue      # the row moved; its rail waits for the next save

        rows = _rows_of(node)
        held = [{"local_id": str(r.get("id") or ""),
                 "text": str(r.get("text") or ""),
                 "depth": int(r.get("depth") or 0),
                 "status": str(r.get("status") or ""),
                 "question": str(r.get("question") or "")}
                for r in goal.get("todo_items") or []]
        if rows != held:
            out = _rpc("hc_replace_todos",
                       {"p_goal_id": gid, "p_expect": goal.get("updated_at"),
                        "p_rows": rows}, root)
            if out.get("conflict"):
                conflicts.append(dict(out, id=gid,
                                      title=_served_fields(goal)["title"]))
            elif out.get("ok"):
                changed.append(gid)
            else:
                refused.append({"id": gid,
                                "title": _served_fields(goal)["title"],
                                "error": out.get("error")})

    # Deleting. The artifact deletes by leaving the goal out of the tree it
    # posts, and the local store answers that by marking it archived and
    # keeping it -- a tombstone, restored only from the Archive view. The same
    # answer here, rather than a second meaning for delete in the shared
    # window.
    #
    # Guarded on the tree being non-empty: a save that posted nothing is a
    # page in a bad state, not an instruction to bury the project.
    removed = []
    if posted:
        for gid, goal in by_id.items():
            if gid in posted or goal.get("shared_readonly"):
                continue
            if GM.norm_status(goal.get("status")) == GM.ARCHIVED:
                continue
            out = update_goal(gid, goal.get("updated_at"),
                              {"status": GM.ARCHIVED}, root,
                              project_id=project_id)
            if out.get("conflict"):
                conflicts.append(dict(out, id=gid,
                                      title=_served_fields(goal)["title"]))
            elif out.get("ok"):
                removed.append(gid)
            else:
                refused.append({"id": gid,
                                "title": _served_fields(goal)["title"],
                                "error": out.get("error")})

    if changed or made or removed:
        SB.forget_shared(project_id)
    fresh = SB.shared_payload(project_id, root)
    return {"ok": not conflicts and not refused,
            "changed": changed, "created": made, "removed": removed,
            "refused": refused, "conflicts": conflicts,
            "conflict": bool(conflicts), "state": fresh}


def local_goal(session_id: str, local_id: str,
               root: Optional[Path] = None) -> Dict[str, Any]:
    """The vault's own copy of one goal -- for a test, or for a reader
    checking the two halves agree."""
    goals, _ = CS.load_goals(session_id, root)
    for row in goals.get("goals") or []:
        if row.get("id") == local_id:
            return row
    return {}
