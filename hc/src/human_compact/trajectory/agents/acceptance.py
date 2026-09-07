"""One observable contract, persisted before Build and reused by Verifier."""
import json
import os

from .. import chat_state as CS, goals as GM, providers, setup_chat
from ... import telemetry


def normalize(value):
    if isinstance(value, str):
        value = {"criterion": value}
    if not isinstance(value, dict) or not str(value.get("criterion") or "").strip():
        return None
    checks = value.get("checks") or []
    if not isinstance(checks, list) or len(checks) > 12:
        return None
    clean = []
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in ("control", "text", "file"):
            return None
        c = {k: str(check[k])[:500] for k in ("kind", "role", "name", "text", "path", "contains") if k in check}
        if c["kind"] == "control" and not (c.get("role") and c.get("name")):
            return None
        if c["kind"] == "text" and not c.get("text"):
            return None
        if c["kind"] == "file" and not c.get("path"):
            return None
        # Only bounded browser actions; no generated code or shell commands.
        steps = check.get("steps") or []
        if not isinstance(steps, list) or len(steps) > 8:
            return None
        c["steps"] = []
        for step in steps:
            if not isinstance(step, dict) or step.get("action") not in ("click", "fill"):
                return None
            if not step.get("role") or not step.get("name"):
                return None
            c["steps"].append({k: str(step.get(k) or "")[:300]
                               for k in ("action", "role", "name", "value")})
        clean.append(c)
    return {"criterion": str(value["criterion"]).strip()[:800], "checks": clean}


def derive(rows, context, root=None, engine=None):
    engine = engine or providers.make(os.environ.get("HC_CHAT_PROVIDER", "claude"),
        "synthesize", setup_chat.setup_model(root), timeout=setup_chat.SETUP_TIMEOUT_SECONDS)
    prompt = '''Derive the minimal observable acceptance criterion for each TODO.
Return JSON {"criteria": {"todo-id": {"criterion":"observable outcome", "checks":[]}}}.
Checks must actually establish the requested result, not just process/page health.
For web UI use checks {kind:"control",role:"button",name:"Export"} or
{kind:"text",text:"expected rendered content"}. To test behavior a check may have
steps:[{action:"click"|"fill",role:"button"|"textbox",name:"accessible name",value:"..."}]
executed before asserting that check. File checks: {kind:"file",path:"relative",contains:"expected text"}.
Do not invent selectors, expected labels or paths unrelated to the task. Keep the
contract minimal; use a prose criterion when checks cannot express it. The user
must not be asked for environment facts. Context and TODOs follow:\n'''
    with telemetry.purpose("acceptance"):
        raw = engine.generate_json(prompt + json.dumps({"rows": rows, "context": context}, default=str))
    return raw.get("criteria", {}) if isinstance(raw, dict) else {}


def ensure(session_id, root, goal_id, ids, context=None, engine=None):
    goals, _ = CS.load_goals(session_id, root)
    goal = GM.by_id(goals, goal_id) or {}
    rows = [r for r in goal.get("todo_items", []) if r.get("id") in ids]
    if not rows or len(rows) != len(set(ids)):
        raise ValueError("those TODOs are not on this goal")
    missing = [r for r in rows if not normalize(r.get("acceptance"))]
    proposed = derive(missing, context or {}, root, engine) if missing else {}
    with CS.session_lock(session_id, root, wait_s=5):
        current, important = CS.load_goals(session_id, root)
        piece = GM.by_id(current, goal_id) or {}
        actual = {r["id"]: r for r in piece.get("todo_items", [])}
        for before in rows:
            row = actual.get(before["id"])
            if row is None or row.get("text") != before.get("text"):
                raise ValueError("the TODO changed while deriving acceptance")
            if not normalize(row.get("acceptance")):
                criterion = normalize(proposed.get(row["id"]))
                if not criterion:
                    raise ValueError("could not derive an observable acceptance criterion")
                row["acceptance"] = criterion
        if missing and not CS.save_goals(session_id, current, important, root):
            raise ValueError("goal state changed while saving acceptance")
        return {r["id"]: actual[r["id"]]["acceptance"] for r in rows}
