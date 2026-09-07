"""One observable contract, persisted before Build and reused by Verifier."""
import json
import os
import threading

from .. import chat_state as CS, goals as GM, providers, setup_chat
from ... import telemetry


WEB_KINDS = ("control", "control_value", "text", "layout")
CHECK_KINDS = WEB_KINDS + ("file", "file_exists", "file_nonempty")


def normalize(value):
    if isinstance(value, str): value = {"criterion": value}
    if not isinstance(value, dict) or not str(value.get("criterion") or "").strip(): return None
    checks = value.get("checks") or []
    if not isinstance(checks, list) or len(checks) > 12: return None
    clean = []
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in CHECK_KINDS: return None
        fields = ("kind", "role", "name", "text", "path", "contains", "value", "from_file", "match", "relation")
        if any(len(str(check[k])) > (4000 if k == "value" else 500) for k in fields if k in check): return None
        c = {k: str(check[k]) for k in fields if k in check}
        kind = c["kind"]
        if kind in ("control", "control_value") and not (c.get("role") and c.get("name")): return None
        if kind == "control":
            c["visible"] = check.get("visible", True)
            if not isinstance(c["visible"], bool): return None
        if kind == "control_value":
            if c.get("match") not in ("equals", "contains", "empty", "nonempty"): return None
            if c["match"] in ("equals", "contains"):
                if bool(c.get("from_file")) == ("value" in c): return None
            elif c.get("from_file"): return None
            if c["match"] == "contains" and "value" in c and not c["value"].strip(): return None
        if kind == "text" and not c.get("text"): return None
        if kind in ("file", "file_exists", "file_nonempty") and not c.get("path"): return None
        if kind == "file" and not c.get("contains", "").strip(): return None
        if kind == "layout":
            controls = check.get("controls")
            if c.get("relation") != "side_by_side" or not isinstance(controls, list) or len(controls) != 2: return None
            if any(not isinstance(x, dict) or not x.get("role") or not x.get("name") for x in controls): return None
            c["controls"] = [{k:str(x[k])[:200] for k in ("role", "name")} for x in controls]
        steps = check.get("steps") or []
        if not isinstance(steps, list) or len(steps) > 8: return None
        c["steps"] = []
        for step in steps:
            if not isinstance(step, dict) or step.get("action") not in ("click", "fill"): return None
            if not step.get("role") or not step.get("name"): return None
            c["steps"].append({k:str(step.get(k) or "")[:500] for k in ("action", "role", "name", "value")})
        clean.append(c)
    out = {"criterion":str(value["criterion"]).strip()[:800], "checks":clean}
    if value.get("coverage") in ("complete", "partial"): out["coverage"] = value["coverage"]
    unverified=value.get("unverified") or []
    if not isinstance(unverified,list): return None
    if unverified: out["unverified"] = [str(x)[:300] for x in unverified[:8]]
    return out


def is_current(value):
    contract = normalize(value)
    return bool(contract and contract.get("coverage") in ("complete", "partial"))


def checks_cover(criterion):
    """Explicit coverage plus guardrails for common omitted UI properties.

    Legacy prose/partial contracts require grounded semantic verification.
    """
    import re
    checks=criterion["checks"]
    if not checks or criterion.get("coverage") != "complete" or criterion.get("unverified"): return False
    prose=criterion["criterion"].lower()
    values=[c for c in checks if c["kind"]=="control_value"]
    if re.search(r"(?:textbox|text box|textarea)",prose):
        names={c.get("name") for c in checks if c["kind"] in ("control","control_value") and c.get("role")=="textbox"}
        multiple=bool(re.search(r"\b(two|both)\b",prose))
        if multiple and len(names)<2: return False
        if "empty" in prose and "nonempty" not in prose and "non-empty" not in prose:
            empty={c["name"] for c in values if c["match"]=="empty" or (c["match"]=="equals" and c.get("value")=="")}
            if len(empty)<(2 if multiple else 1): return False
        if re.search(r"populat|loads? .*into|fills?",prose) and len({c["name"] for c in values})<(2 if multiple else 1): return False
    if re.search(r"side[ -]by[ -]side",prose) and not any(c["kind"]=="layout" for c in checks): return False
    return True


def derive(rows, context, root=None, engine=None):
    engine = engine or providers.make(os.environ.get("HC_CHAT_PROVIDER", "claude"),
        "synthesize", setup_chat.setup_model(root), timeout=setup_chat.SETUP_TIMEOUT_SECONDS)
    prompt = '''Derive the minimal observable acceptance criterion for each TODO.
Return JSON {"criteria": {"todo-id": {"criterion":"observable outcome", "checks":[]}}}.
Checks must establish EVERY requested property, not just labels or page health.
Set coverage:"complete" only if the declarative checks establish the entire criterion.
Otherwise set coverage:"partial", unverified:["specific properties needing semantic evidence"].
For textboxes/textarea use control_value with role:"textbox", name:accessible label,
match:"equals"|"contains"|"empty"|"nonempty", value:expected value when applicable.
When a textbox should load a project file, use from_file:"instruction.txt" instead
of value. The verifier reads that safe local file and compares the control value;
do not invent file contents before they exist. Literal values must be <=4000 characters.
Text assertions inspect visible page text, NOT input/textarea values. Keep the requested
textbox UI; never replace it with pre/text just to satisfy an unsuitable assertion.
For two-panel Load Example behavior assert BOTH separately labeled textbox values,
with the Load Example click step on EACH check. A label/button alone proves no result.
Control checks may specify visible:true|false.
For side-by-side layout use kind:"layout", relation:"side_by_side", controls:[{role,name},{role,name}].
File existence: kind:"file_exists",path. Nonempty file: kind:"file_nonempty",path.
File substring: kind:"file",path,contains:meaningful nonempty fragment. Empty contains is invalid.
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


_derivations = {}
_derivations_guard = threading.Lock()


def ensure(session_id, root, goal_id, ids, context=None, engine=None):
    # A Build click can join in-flight precomputation instead of paying for
    # a second identical derivation. Re-read persisted criteria after waiting.
    key = (session_id, str(root), goal_id)
    with _derivations_guard:
        lock = _derivations.setdefault(key, threading.RLock())
    with lock:
        return _ensure(session_id, root, goal_id, ids, context, engine)


def _ensure(session_id, root, goal_id, ids, context=None, engine=None):
    goals, _ = CS.load_goals(session_id, root)
    goal = GM.by_id(goals, goal_id) or {}
    rows = [r for r in goal.get("todo_items", []) if r.get("id") in ids]
    if not rows or len(rows) != len(set(ids)):
        raise ValueError("those TODOs are not on this goal")
    missing = [r for r in rows if not is_current(r.get("acceptance"))]
    proposed = derive(missing, context or {}, root, engine) if missing else {}
    with CS.session_lock(session_id, root, wait_s=5):
        current, important = CS.load_goals(session_id, root)
        piece = GM.by_id(current, goal_id) or {}
        actual = {r["id"]: r for r in piece.get("todo_items", [])}
        for before in rows:
            row = actual.get(before["id"])
            if row is None or row.get("text") != before.get("text"):
                raise ValueError("the TODO changed while deriving acceptance")
            if not is_current(row.get("acceptance")):
                criterion = normalize(proposed.get(row["id"]))
                if not criterion:
                    raise ValueError("could not derive an observable acceptance criterion")
                criterion.setdefault("coverage", "partial")
                row["acceptance"] = criterion
        if missing and not CS.save_goals(session_id, current, important, root):
            raise ValueError("goal state changed while saving acceptance")
        return {r["id"]: actual[r["id"]]["acceptance"] for r in rows}


# At most one background derivation per goal; edits during a call are re-read.
# Failed preparation is retried by ensure at Build, never in an endless loop.
_pending = set()
_pending_lock = threading.Lock()


def prepare(session_id, root, goal_id):
    key = (session_id, str(root), goal_id)
    with _pending_lock:
        if key in _pending:
            return
        _pending.add(key)
    def work():
        try:
            from . import context
            goals, _ = CS.load_goals(session_id, root)
            goal = GM.by_id(goals, goal_id) or {}
            ids = [r["id"] for r in goal.get("todo_items", []) if not is_current(r.get("acceptance"))]
            if ids:
                ensure(session_id, root, goal_id, ids, context.assemble(session_id, root, {"subgoalId": goal_id}))
        except Exception:
            # Best effort only: Build retains the synchronous correctness gate.
            pass
        finally:
            with _pending_lock:
                _pending.discard(key)
    threading.Thread(target=work, name="acceptance-preparation", daemon=True).start()
