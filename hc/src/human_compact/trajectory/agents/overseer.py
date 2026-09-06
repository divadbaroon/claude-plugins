"""The Overseer: given a meaningful transition, which agent acts next.

One function, ``route``, from an event and what is known to a decision::

    {"action": none | chat | brainstorm | replan | build | verify,
     "reason": "...", "targetSubgoalId": "...", "targetTodoId": "...",
     "contextUpdates": [...]}

It is rules, not a model call, and that is deliberate: the transitions
are few and their handling is policy the reader should be able to read.
A model-backed Overseer would slot in behind ``route`` for the cases the
rules leave to ``chat`` by default, and would still be asked only on the
events the trigger policy names.

The rules:

- A message to Bart goes to Chat -- unless its words ask for options
  (Brainstorm) or for a plan (Path).
- Chat finding the message turns on the reader's preference: Brainstorm,
  with that question. Chat finding it turns on a fact of the project:
  Chat again, after the runtime has looked (the orchestrator does the
  looking; no question reaches the reader).
- Rows sent to build: Build.
- A build that finished: Verifier.
- A pass: nothing more to do; the verdict is written to context.
- A fail: Build again, on the failed rows with the reason, up to
  REPAIR_LIMIT repairs; then Chat, to tell the reader plainly.
- A build that failed on its own, or stopped to ask: nothing; the page
  already shows the row's state, and the reader's answer resumes it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import context as CTX
from . import policy as POLICY

NONE, CHAT, BRAINSTORM, REPLAN, BUILD, VERIFY = (
    "none", "chat", "brainstorm", "replan", "build", "verify")
ACTIONS = (NONE, CHAT, BRAINSTORM, REPLAN, BUILD, VERIFY)

# Repairs after the first build: with 2, a row is built at most three times
# before the reader is told.
REPAIR_LIMIT = 2


def decision(action: str, reason: str, *, subgoal_id: str = "", todo_id: str = "",
             context_updates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError("not an Overseer action: %r" % (action,))
    return {"action": action, "reason": str(reason or ""),
            "targetSubgoalId": str(subgoal_id or ""),
            "targetTodoId": str(todo_id or ""),
            "contextUpdates": list(context_updates or [])}


def route(event: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The decision for one meaningful event. ``state`` carries what the
    orchestrator knows that the event does not: ``attempts`` per subgoal."""
    state = state or {}
    kind = str(event.get("type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    subgoal = str(event.get("subgoalId") or "")
    todo = str(event.get("todoId") or "")

    if kind == POLICY.BART_MESSAGE:
        action, reason = POLICY.bart_intent(str(payload.get("text") or ""))
        return decision(action, reason, subgoal_id=subgoal)

    if kind == POLICY.PLAN_REQUESTED:
        return decision(REPLAN, "a plan was asked for", subgoal_id=subgoal)

    if kind == POLICY.CHAT_NEEDS_HUMAN:
        return decision(BRAINSTORM, "the answer turns on the reader's preference: "
                        + str(payload.get("question") or ""), subgoal_id=subgoal)

    if kind == POLICY.CHAT_NEEDS_DISCOVERY:
        return decision(CHAT, "the answer is in the project directory; discover "
                        "it, do not ask the reader: " + str(payload.get("question") or ""),
                        subgoal_id=subgoal)

    if kind == POLICY.BUILD_REQUESTED:
        return decision(BUILD, "rows were handed to the build", subgoal_id=subgoal,
                        todo_id=todo)

    if kind == POLICY.BUILD_COMPLETED:
        return decision(VERIFY, "the build says it is done; check it",
                        subgoal_id=subgoal, todo_id=todo)

    if kind == POLICY.BUILD_FAILED:
        return decision(NONE, "the build failed on its own account; the rows say so "
                        "on the page", subgoal_id=subgoal,
                        context_updates=[CTX.update(
                            CTX.RUN_RESULT, "build failed: " + str(payload.get("error") or ""),
                            subgoal_id=subgoal, run_id=str(event.get("runId") or ""))])

    if kind == POLICY.BUILD_QUESTION:
        return decision(NONE, "the build stopped to ask; the reader's answer resumes it",
                        subgoal_id=subgoal, todo_id=todo)

    if kind == POLICY.VERIFY_PASSED:
        rows = payload.get("rows") or []
        return decision(NONE, "verified: " + str(payload.get("reason") or ""),
                        subgoal_id=subgoal, context_updates=[
                            CTX.update(CTX.VERIFICATION_RESULT,
                                       "passed: " + str(payload.get("reason") or ""),
                                       subgoal_id=subgoal, run_id=str(event.get("runId") or "")),
                            *[CTX.update(CTX.TODO_STATUS, "done", subgoal_id=subgoal,
                                         todo_id=str(r)) for r in rows]])

    if kind == POLICY.VERIFY_FAILED:
        attempts = int((state.get("attempts") or {}).get(subgoal, 0) or 0)
        reason = str(payload.get("reason") or "the check failed")
        note = CTX.update(CTX.VERIFICATION_RESULT, "failed: " + reason,
                          subgoal_id=subgoal, run_id=str(event.get("runId") or ""))
        if attempts < REPAIR_LIMIT:
            return decision(BUILD, "repair %d of %d: %s" % (attempts + 1, REPAIR_LIMIT, reason),
                            subgoal_id=subgoal, todo_id=todo, context_updates=[note])
        return decision(CHAT, "repaired %d times and still failing; tell the reader: %s"
                        % (REPAIR_LIMIT, reason), subgoal_id=subgoal, todo_id=todo,
                        context_updates=[note])

    return decision(NONE, "not a transition the Overseer acts on: " + kind,
                    subgoal_id=subgoal)
