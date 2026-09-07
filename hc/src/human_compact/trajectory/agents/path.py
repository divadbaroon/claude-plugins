"""Project-level Path: the smallest useful change to the current plan."""
from __future__ import annotations

import copy
import json
import os

from .. import providers, setup_chat, chat_state as CS, goals as GM
from ... import telemetry

OPERATIONS = frozenset({"keep_plan", "add_subgoal", "revise_subgoal", "reorder_subgoal",
    "replace_subgoal", "add_todos", "revise_todos", "remove_obsolete_todo"})
PROMPT = """You are the internal Path agent. Given where this project is now, what is
the smallest sensible path forward? Keep useful work; minimize prerequisites for
the human. Inspectable environment facts are not questions for the user.
Return JSON {"say":"brief explanation", "changes":[...]}. Each change has
op, subgoalId, parentGoalId, title, beforeId, todos, todoId as needed.
Supported ops: keep_plan, add_subgoal, revise_subgoal (title), reorder_subgoal
(beforeId), replace_subgoal (title and todos), add_todos, revise_todos (by id),
remove_obsolete_todo (todoId). Todos are objects {id,text,acceptance}, with
acceptance {criterion, checks:[]}; omit ids for new rows. Only use existing IDs
from the supplied plan. Do not change running or completed work. Do not rewrite
an entire plan when one changed step suffices. Empty changes means keep_plan.
"""


def plan(transcript, context="", focus=(), known=(), root=None, engine=None):
    try:
        engine = engine or providers.make(os.environ.get("HC_CHAT_PROVIDER", "claude"),
            "synthesize", setup_chat.setup_model(root), timeout=setup_chat.SETUP_TIMEOUT_SECONDS)
        with telemetry.purpose("path"):
            raw = engine.generate_json(PROMPT + "\n" + str(context) + "\n" +
                "\n".join(list(focus) + list(known)) + "\n" + json.dumps(transcript))
        if not isinstance(raw, dict) or not isinstance(raw.get("changes", []), list):
            raise ValueError("Path returned an invalid plan")
        changes = raw.get("changes", [])
        if len(changes) > 12 or any(not isinstance(c, dict) or c.get("op") not in OPERATIONS for c in changes):
            raise ValueError("Path returned an unsupported plan change")
        said = str(raw.get("say") or "The current path still fits.")[:1200]
        return {"ok": True, "say": said, "changes": changes, "card": "none",
                "replies": [{"kind": "text", "text": said}]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def apply(session_id, root, changes, expected):
    """Validate the full patch against the snapshot before one atomic write."""
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        if goals != expected:
            raise ValueError("The plan changed while planning; please try again")
        work = copy.deepcopy(goals)
        applied = []
        for change in changes:
            op = change.get("op")
            if op not in OPERATIONS:
                raise ValueError("unsupported plan operation")
            if op == "keep_plan":
                continue
            target = GM.by_id(work, str(change.get("subgoalId") or ""))
            if op == "add_subgoal":
                parent = GM.by_id(work, str(change.get("parentGoalId") or ""))
                if not parent or not str(change.get("title") or "").strip():
                    raise ValueError("a new subgoal needs an existing parent and title")
                target = GM.new_goal(GM.child_goal_id(work, parent["id"]),
                                     str(change["title"])[:400], parent["id"], origin="agent")
                work["goals"].append(target)
            elif not target:
                raise ValueError("plan target no longer exists")
            rows = target.setdefault("todo_items", [])
            busy = {"building", "queued", "asking"}
            if op in ("replace_subgoal", "revise_subgoal"):
                if any(r.get("status") in busy for r in rows):
                    raise ValueError("cannot change work during a build")
                title = str(change.get("title") or "").strip()[:400]
                if not title:
                    raise ValueError("a subgoal needs a title")
                target["title"] = title
            if op == "replace_subgoal":
                if any(r.get("status") == "done" for r in rows):
                    raise ValueError("cannot replace completed work")
                target["todo_items"] = rows = []
            if op == "reorder_subgoal":
                before = GM.by_id(work, str(change.get("beforeId") or ""))
                if before is target or (before and before.get("parent_goal_id") != target.get("parent_goal_id")):
                    raise ValueError("reorder must stay among siblings")
                work["goals"].remove(target)
                work["goals"].insert(work["goals"].index(before) if before else len(work["goals"]), target)
            if op in ("add_todos", "revise_todos", "add_subgoal", "replace_subgoal"):
                additions = change.get("todos") or []
                if not isinstance(additions, list) or len(additions) > 20:
                    raise ValueError("invalid todo changes")
                for item in additions:
                    if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                        raise ValueError("a todo needs text")
                    if op == "revise_todos":
                        row = next((r for r in rows if r["id"] == item.get("id")), None)
                        if not row or row.get("status") in busy | {"done"}:
                            raise ValueError("cannot revise running, completed or missing todo")
                        row.pop("acceptance", None)
                    else:
                        row = {"id": GM.todo_id(), "depth": 0, "status": ""}
                        rows.append(row)
                    row["text"] = str(item["text"])[:1200]
                    if isinstance(item.get("acceptance"), dict):
                        row["acceptance"] = item["acceptance"]
            if op == "remove_obsolete_todo":
                row = next((r for r in rows if r["id"] == change.get("todoId")), None)
                if not row or row.get("status") in busy | {"done"}:
                    raise ValueError("cannot remove running, completed or missing todo")
                rows.remove(row)
            target["todos_md"] = GM.render_todos(target.get("todo_items") or [])
            target["updated_at"] = GM._now()
            applied.append(dict(change, subgoalId=target["id"]))
        GM.sanitize(work)
        if applied and not CS.save_goals(session_id, work, important, root):
            raise ValueError("plan changed during save")
        return applied
