"""The Verifier: whether the Build's "done" holds.

Deterministic checks first, then semantic artifact inspection. A build that
ends says its rows are done; this reads what is on disk afterwards -- the rows' statuses on the
tree, the run record, and the preview's health when a preview is up --
and gives one verdict with the evidence under it. Extra ``checks`` may be
handed in (a command to run, a page to fetch); each is a callable taking
the runtime and answering ``(passed, reason)``.

A fail goes back to the Overseer, which repairs (reopens the row with the
reason as the note, so the build fixes THAT) up to its limit and then
escalates to the reader.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import build as BUILD
from .. import chat_state as CS
from .. import goals as GM
from . import trace

Check = Callable[[Any], Tuple[bool, str]]


def verify(session_id: str, root: Optional[Path], goal_id: str,
           row_ids: Sequence[str], runtime=None,
           checks: Sequence[Check] = ()) -> Dict[str, Any]:
    with trace.span("verifier.agent", goal=goal_id, rows=len(list(row_ids))) as span:
        record = BUILD.load_run(session_id, root, goal_id) or {}
        token = (record.get("preview_readiness") or {}).get("token")
        def ready():
            BUILD.set_preview_readiness(session_id, root, goal_id, token, "ready")
            trace.phase("preview.ready_to_show")
        try:
            verdict = _verify(session_id, root, goal_id, list(row_ids), runtime, checks, ready)
        except Exception:
            BUILD.set_preview_readiness(session_id, root, goal_id, token, "held")
            raise
        BUILD.set_preview_readiness(session_id, root, goal_id, token,
                                    "ready" if verdict["passed"] and
                                    (verdict.get("evidence", {}).get("artifact", {}).get("page") or {}).get("passed")
                                    else "held")
        span["attrs"]["passed"] = verdict["passed"]
        return verdict


def _verify(session_id, root, goal_id, row_ids, runtime, checks, on_ready=None) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {}
    if not row_ids:
        return _fail("the run contains no rows to verify", evidence)
    goals, _important = CS.load_goals(session_id, root)
    goal = GM.by_id(goals, goal_id)
    if not goal:
        return _fail("the goal is gone from the tree", evidence)
    rows = {str(r.get("id")): r for r in goal.get("todo_items") or []}
    statuses = {rid: str((rows.get(rid) or {}).get("status") or "") for rid in row_ids}
    evidence["rows"] = statuses
    missing = [rid for rid in row_ids if rid not in rows]
    if missing:
        return _fail("rows were removed while the build ran: %s" % ", ".join(missing), evidence)
    failed = [rid for rid, status in statuses.items() if status == "failed"]
    if failed:
        return _fail("the build left %d row%s failed: %s" % (
            len(failed), "" if len(failed) == 1 else "s",
            "; ".join(_text(rows[r]) for r in failed)), evidence)
    asking = [rid for rid, status in statuses.items() if status == "asking"]
    if asking:
        return _fail("the build is waiting on an answer for: %s"
                     % "; ".join(_text(rows[r]) for r in asking), evidence)
    not_done = [rid for rid, status in statuses.items() if status != "done"]
    if not_done:
        return _fail("%d row%s not marked done: %s" % (
            len(not_done), " is" if len(not_done) == 1 else "s are",
            "; ".join(_text(rows[r]) for r in not_done)), evidence)
    record = BUILD.load_run(session_id, root, goal_id) or {}
    evidence["run"] = {k: record.get(k) for k in ("status", "exit_code", "error")}
    if record.get("error"):
        return _fail("the run recorded an error: %s" % str(record["error"])[:200], evidence)
    if record.get("exit_code"):
        return _fail("the build process exited %s" % record["exit_code"], evidence)
    saved = record.get("acceptance") or {}
    criteria = {rid: saved.get(rid) or rows[rid].get("acceptance") for rid in row_ids}
    if runtime is not None:
        state = _preview(runtime, session_id)
        from .acceptance import normalize
        web = any(c.get("kind") in ("control", "control_value", "text", "layout")
                  for a in criteria.values() for c in (normalize(a) or {}).get("checks", []))
        if web and not state.get("url") and callable(getattr(runtime, "ensure_preview", None)):
            try:
                state = runtime.ensure_preview(session_id)
            except Exception as exc:
                evidence["preview_startup"] = {"ok": False, "error": str(exc)[:300]}
                return _fail("The app could not be started for its check", evidence)
            evidence["preview_startup"] = state.get("startup", {})
        evidence["preview"] = {k: state.get(k) for k in ("status", "url", "healthy", "exit_code")}
        if state.get("status") in ("failed", "exited") :
            return _fail("the project's run %s after the build (exit %s)" % (
                state.get("status"), state.get("exit_code")), evidence)
        if state.get("status") == "running" and state.get("healthy") is False:
            return _fail("the project is running but its page does not answer", evidence)
    evidence["acceptance"] = criteria
    if any(criteria.get(rid) for rid in row_ids):
        try:
            inspect_ready = getattr(runtime, "verify_artifact_ready", None)
            if callable(inspect_ready) and on_ready:
                artifact = inspect_ready(criteria, evidence.get("preview") or {}, on_ready)
            else:
                artifact = runtime.verify_artifact(criteria, evidence.get("preview") or {})
        except Exception as exc:
            artifact = {"passed": False, "reason": "artifact inspection unavailable: " + str(exc)[:160]}
        evidence["artifact"] = artifact
        if not artifact.get("passed"):
            return _fail(artifact.get("reason") or "the artifact does not meet acceptance", evidence)
    for check in checks or ():
        try:
            passed, reason = check(runtime)
        except Exception as exc:  # noqa: BLE001 -- a broken check is a failed check
            passed, reason = False, "a check raised: %s" % str(exc)[:160]
        evidence.setdefault("checks", []).append({"passed": bool(passed), "reason": reason})
        if not passed:
            return _fail(reason or "a check failed", evidence)
    return {"passed": True, "reason": "every row is done, the run ended clean"
            + (", the preview answers" if evidence.get("preview", {}).get("status") == "running" else ""),
            "evidence": evidence}


def _preview(runtime, session_id: str) -> Dict[str, Any]:
    try:
        state = runtime.preview_state(session_id)
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "error": str(exc)[:200]}
    if not isinstance(state, dict):
        return {}
    return dict(state, **(state.get("run") or {}))


def _text(row: Dict[str, Any]) -> str:
    return " ".join(str(row.get("text") or "").split())[:80]


def _fail(reason: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {"passed": False, "reason": reason, "evidence": evidence}
