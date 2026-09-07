"""The orchestrator: events in, the right agent's work out.

One ``Orchestrator`` per chat, made where the turn begins -- the Bart
route, the build_todos op, the end of a build -- and thrown away after.
It records every event it is handed (``emit``), asks the Overseer only
when the trigger policy names the event a meaningful transition
(``handle``), carries out the decision (``_dispatch``), applies the
decision's context updates, and follows the chain to its end: a build
that finishes is verified, a verdict that fails is repaired or escalated,
each step a recorded event and a span.

The agents are callables handed in (``agents``), so a test stands them
in without a model; the defaults are the modules beside this one. The
runtime is the one ``runtime.make`` gives, so a build and every look at
the project go where this install says they go.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import brainstorm as BRAIN
from .. import build as BUILD
from .. import chat_state as CS
from .. import goals as GM
from ..secure_io import atomic_write_json
from . import brainstorm as BRAINSTORM_AGENT
from . import chat as CHAT
from . import presentation as PUBLIC
from . import context as CTX
from . import events as EV
from . import overseer as OVERSEER
from . import path as PATH
from . import policy as POLICY
from . import replies as REPLIES
from . import runtime as RT
from . import trace
from . import communication as COMM
from . import verifier as VERIFIER

# HC_AGENTS=0 turns the routing off: Bart goes straight to the brainstorm
# as before and a finished build is not verified. For a machine where the
# new path misbehaves, not a mode.
ENABLED_VAR = "HC_AGENTS"


def enabled() -> bool:
    return os.environ.get(ENABLED_VAR, "").strip() != "0"


def focus(goal_title: str, subgoal_title: str) -> List[str]:
    """What the brainstorm and the planner are told about where the
    conversation is (the wording the page's route tests read)."""
    return [
        "",
        "# Where this conversation is",
        "",
        "They are talking about one piece of the work: \"%s\", under the "
        "goal \"%s\". What they want from you is the next TODO row or two "
        "for that piece, or the one question that decides what those rows "
        "are." % (subgoal_title, goal_title),
        "",
        "Propose rows as `todos` -- flat rows, for that piece; not as "
        "`subgoals` and not as `goals`. Do not offer to write goals here, "
        "and do not send an `offer` card for rows either: when you have "
        "the rows, send them as a `todos` card. Nothing is written until "
        "they add a row themselves, so a proposed row is the offer.",
    ]


def chat_focus(goal_title: str, subgoal_title: str) -> List[str]:
    return ["", "# Where this conversation is", "",
            "They are talking about one piece of the work: \"%s\", under the "
            "goal \"%s\"." % (subgoal_title, goal_title)]


def default_agents() -> Dict[str, Callable[..., Dict[str, Any]]]:
    return {"chat": CHAT.ask, "brainstorm": BRAINSTORM_AGENT.reply,
            "path": PATH.plan, "verify": VERIFIER.verify}


class Orchestrator:
    def __init__(self, session_id: str, root: Optional[Path], *, cwd: str = "",
                 runtime: Optional[RT.Runtime] = None,
                 agents: Optional[Dict[str, Callable[..., Dict[str, Any]]]] = None,
                 project_id: str = "", tracer: Optional[trace.Tracer] = None,
                 checks: Sequence[VERIFIER.Check] = ()):
        self.session_id = session_id
        self.root = root
        self.cwd = str(cwd or "")
        self.runtime = runtime or RT.make(cwd=self.cwd, root=root)
        self.agents = dict(default_agents(), **(agents or {}))
        self.project_id = str(project_id or self.cwd)
        self.tracer = tracer or trace.Tracer(session_id, root)
        self.checks = list(checks)
        # Every decision this instance took, in order: the flow, for whoever
        # asked (the route answers with it; the tests read it).
        self.steps: List[Dict[str, Any]] = []
        self.overseer_calls = 0

    # --- the log ---------------------------------------------------------------

    def emit(self, type: str, source: str, payload: Optional[Dict[str, Any]] = None,
             **ids: str) -> Dict[str, Any]:
        event = EV.new_event(type, source, payload, project_id=self.project_id, **ids)
        return EV.record(self.session_id, self.root, event)

    def events(self, **kw) -> List[Dict[str, Any]]:
        return EV.read(self.session_id, self.root, **kw)

    # --- what the orchestrator knows beyond the event ---------------------------

    def _state_path(self) -> Path:
        return CS.paths(self.session_id, self.root).session_dir / "agent_state.json"

    def state(self) -> Dict[str, Any]:
        try:
            value = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        value.setdefault("attempts", {})
        return value

    def _save_state(self, value: Dict[str, Any]) -> None:
        spot = self._state_path()
        spot.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(spot, value, root=spot.parent)

    def _attempts(self, subgoal_id: str, set_to: Optional[int] = None,
                  add: int = 0) -> int:
        with CS.session_lock(self.session_id, self.root, wait_s=5):
            state = self.state()
            held = int(state["attempts"].get(subgoal_id, 0) or 0)
            if set_to is not None or add:
                held = set_to if set_to is not None else held + add
                state["attempts"][subgoal_id] = held
                self._save_state(state)
            return held

    # --- routing ---------------------------------------------------------------

    def handle(self, event: Dict[str, Any], carry: Optional[Dict[str, Any]] = None
               ) -> Optional[Dict[str, Any]]:
        """One event through the policy and, when it is meaningful, the
        Overseer and the agent it names. Returns the agent's result, or None
        when nothing was asked or the decision was ``none``."""
        if not POLICY.is_meaningful(event):
            return None
        if event["type"] == POLICY.VERIFY_PASSED:
            for item in OVERSEER.fallback(event, self.state())["contextUpdates"]:
                CTX.apply(self.session_id, self.root, item)
        with trace.span("overseer.route", event=event["type"]):
            self.overseer_calls += 1
            state = dict(self.state(), **CTX.assemble(self.session_id, self.root, event, carry))
            decision = self.agents.get("overseer", OVERSEER.route)(event, state, root=self.root)
        if event["type"] == POLICY.BART_MESSAGE and PUBLIC.smalltalk((event.get("payload") or {}).get("text")):
            decision = dict(decision, action=OVERSEER.CHAT, targetSubgoalId=event.get("subgoalId", ""))
        self.emit(POLICY.OVERSEER_ROUTED, EV.SYSTEM,
                  {"event": event["type"], "action": decision["action"],
                   "reason": decision["reason"]},
                  subgoal_id=decision.get("targetSubgoalId") or event.get("subgoalId") or "",
                  todo_id=decision.get("targetTodoId") or "")
        for item in decision.get("contextUpdates") or []:
            try:
                CTX.apply(self.session_id, self.root, item)
                self.emit(POLICY.CONTEXT_UPDATED, EV.SYSTEM,
                          {"kind": item.get("kind"), "text": item.get("text")},
                          subgoal_id=item.get("subgoalId") or "",
                          todo_id=item.get("todoId") or "")
            except ValueError:
                continue
        step = {"event": event["type"], "action": decision["action"],
                "reason": decision["reason"]}
        self.steps.append(step)
        target = decision.get("targetSubgoalId")
        if target and target != event.get("subgoalId") and event["type"] not in (
                POLICY.BUILD_REQUESTED, POLICY.BUILD_COMPLETED, POLICY.VERIFY_FAILED):
            from .. import goals as GM
            goals, _ = CS.load_goals(self.session_id, self.root)
            if GM.by_id(goals, target):
                event = dict(event, subgoalId=target)
        result = self._dispatch(decision, event, dict(carry or {}))
        step["result"] = _brief(result)
        return result

    def _dispatch(self, decision, event, carry) -> Optional[Dict[str, Any]]:
        action = decision["action"]
        if event["type"] == POLICY.BUILD_QUESTION:
            return self._build_question(event)
        if event["type"] == POLICY.BUILD_FAILED:
            goal = event.get("subgoalId", "")
            topic = COMM.subject(self.session_id, self.root, goal, event.get("payload", {}).get("rows", []))
            COMM.publish(self.session_id, self.root, goal, "failed", COMM.summary("failed", topic))
        if action == OVERSEER.NONE:
            return None
        if action == OVERSEER.CHAT:
            return self._chat(decision, event, carry)
        if action == OVERSEER.BRAINSTORM:
            return self._brainstorm(decision, event, carry)
        if action == OVERSEER.REPLAN:
            return self._plan(decision, event, carry)
        if action == OVERSEER.BUILD:
            return self._build(decision, event, carry)
        if action == OVERSEER.VERIFY:
            return self._verify(decision, event, carry)
        return None

    # --- Bart --------------------------------------------------------------------

    def bart_message(self, held: Dict[str, Any], transcript) -> Dict[str, Any]:
        """The page's message to Bart: recorded, routed, answered.

        ``held`` is what the route read under the lock: root, cwd, digest,
        the goal's and the piece's titles, the piece's id.
        """
        text = POLICY.last_user_text(transcript)
        subgoal_id = str(held.get("subgoal_id") or "")
        with self.tracer.span("bart.message", subgoal=subgoal_id):
            trace.use(self.tracer)
            try:
                context = BRAIN.project_context(held["root"], held["cwd"], held.get("digest", ""))
                carry = {"transcript": list(transcript or []), "context": context,
                         "goal": str(held.get("goal") or ""),
                         "subgoal": str(held.get("subgoal") or "")}
                event = self.emit(POLICY.BART_MESSAGE, EV.USER, {"text": text},
                                  subgoal_id=subgoal_id)
                result = None if PUBLIC.smalltalk(text) else self._answer_pending(event, carry)
                if result is None:
                    result = self.handle(event, carry)
            finally:
                trace.use(None)
        if not result:
            result = {"ok": False, "error": "Bart had nothing to say to that"}
        result.setdefault("card", "none")
        result["flow"] = list(self.steps)
        return result

    def _answer_pending(self, event, carry):
        goal = event.get("subgoalId", "")
        pending = COMM.pending(self.session_id, self.root, goal)
        if not pending:
            return None
        payload = pending.get("payload") or {}
        if payload.get("resume") != "build":
            # The normal conversation consumes the answer with its question in
            # context. Only a successful response without a new dependency clears it.
            result = self.handle(event, dict(carry, pending_question=payload.get("question", "")))
            latest = COMM.pending(self.session_id, self.root, goal)
            if (result and result.get("ok") and result.get("resolution") in ("resume", "cancel")
                    and latest and latest["id"] == pending["id"]):
                self.emit("human.answered", EV.USER, {"questionId": pending["id"]}, subgoal_id=goal)
            return result
        answer = self.agents["chat"](carry["transcript"], carry.get("context", ""),
            focus=["A build is paused on this question: " + str(payload.get("question") or ""),
                   "Determine whether the reader's last message resolves it. If it does not, "
                   "return needs.kind=human_preference with the one remaining question. "
                   "Do not treat a question about progress as an answer. Additionally return resolution: "
                   "resume only for an answer authorizing continuation, cancel for an explicit request to stop, "
                   "or wait otherwise. Never ask the reader to paste secrets into the conversation."],
            known=self._known(goal), root=self.root)
        if not answer or not answer.get("ok"):
            return {"ok": False, "error": "I couldn’t process that answer. Please try again."}
        if (answer.get("needs") or {}).get("kind") or answer.get("resolution") not in ("resume", "cancel"):
            return {"ok": True, "replies": [{"kind": "text", "text": payload.get("question", "")}], "route": "chat"}
        try:
            if answer.get("resolution") == "cancel":
                result = self.runtime.cancel(self.session_id, self.root, goal, payload.get("rows") or [pending.get("todoId", "")])
            else:
                self.emit(POLICY.BUILD_STARTED, EV.SYSTEM, {"rows": payload.get("rows") or [pending.get("todoId")]}, subgoal_id=goal)
                result = self.runtime.answer(self.session_id, self.root, goal, pending.get("todoId", ""),
                                             POLICY.last_user_text(carry["transcript"]))
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        if not result or not result.get("ok"):
            self.emit(POLICY.CHAT_NEEDS_HUMAN, EV.AGENT, payload, subgoal_id=goal, todo_id=pending.get("todoId", ""))
            BUILD.note_activity(self.session_id, self.root, goal, "error", (result or {}).get("error") or "resume failed")
            return {"ok": False, "error": "I couldn’t resume the work. The details are in Terminal."}
        self._learn(answer, "chat", goal)
        self.emit("human.answered", EV.USER, {"questionId": pending["id"]}, subgoal_id=goal)
        if answer.get("resolution") == "cancel":
            self.emit("build.cancelled", EV.SYSTEM, {"rows": payload.get("rows") or [pending.get("todoId")]}, subgoal_id=goal)
        return {"ok": True, "replies": [], "route": "build"}

    def _build_question(self, event):
        from .. import goals as GM
        goal = event.get("subgoalId", "")
        goals, _ = CS.load_goals(self.session_id, self.root)
        rows = (GM.by_id(goals, goal) or {}).get("todo_items") or []
        row = next((r for r in rows if r.get("status") == "asking" and r.get("question")), None)
        if not row:
            return None
        question = row["question"]
        answer = self.agents["chat"]([{"role": "user", "text": question}],
            json.dumps(CTX.assemble(self.session_id, self.root, event)),
            focus=["Classify this paused build question before involving the reader. "
                   "Use needs.kind=human_preference ONLY for a preference, direction, subjective tradeoff, "
                   "unavailable secret/credential or physical/manual action. Ask one clear question. "
                   "Repo facts, columns, file locations and implementation details are environment; "
                   "return needs.kind=environment. Do not ask the reader to investigate."],
            known=self._known(goal), root=self.root)
        needs = (answer or {}).get("needs") or {}
        if (answer or {}).get("ok") and needs.get("kind") == CHAT.HUMAN:
            question = str(needs.get("question") or question)
            self.emit(POLICY.CHAT_NEEDS_HUMAN, EV.AGENT,
                {"question": question, "rows": [row["id"]], "resume": "build"},
                subgoal_id=goal, todo_id=row["id"])
            COMM.publish(self.session_id, self.root, goal, "question", question, problem=question)
            return {"ok": True, "route": "chat", "replies": [{"kind": "text", "text": question}]}
        # Resolve environment questions locally and return them to the same
        # build. Bound repeated identical discovery without mislabeling it human.
        previous = self.events(types=[POLICY.DISCOVERED, POLICY.BUILD_REQUESTED], subgoal_id=goal)
        previous = previous[next((i + 1 for i in range(len(previous)-1, -1, -1)
                                  if previous[i]["type"] == POLICY.BUILD_REQUESTED), 0):]
        if any(e.get("payload", {}).get("buildQuestion") == question for e in previous):
            return self._fail_question(goal, row["id"], "local discovery did not resolve the build question")
        try:
            discovered = self.runtime.discover(question)
        except Exception as exc:
            return self._fail_question(goal, row["id"], str(exc))
        self.emit(POLICY.DISCOVERED, EV.SYSTEM, {"buildQuestion": question}, subgoal_id=goal)
        self.emit(POLICY.BUILD_STARTED, EV.SYSTEM, {"rows": (event.get("payload") or {}).get("rows") or [row["id"]]}, subgoal_id=goal)
        try:
            result = self.runtime.answer(self.session_id, self.root, goal, row["id"],
                "Resolve this from the project, without asking the reader. Local inspection:\n" + discovered)
        except Exception as exc:
            return self._fail_question(goal, row["id"], str(exc))
        if not result or not result.get("ok"):
            return self._fail_question(goal, row["id"], (result or {}).get("error") or "could not resume discovery")
        return dict(result, route="build")

    def _fail_question(self, goal, row_id, error):
        from .. import goals as GM
        with CS.session_lock(self.session_id, self.root, wait_s=5):
            goals, important = CS.load_goals(self.session_id, self.root)
            for row in (GM.by_id(goals, goal) or {}).get("todo_items") or []:
                if row.get("id") == row_id:
                    row.update(status="failed", question="")
            CS.save_goals(self.session_id, goals, important, self.root)
        BUILD.note_activity(self.session_id, self.root, goal, "error", error)
        self.emit(POLICY.BUILD_FAILED, EV.SYSTEM, {"rows": [row_id], "error": error}, subgoal_id=goal)
        topic = COMM.subject(self.session_id, self.root, goal, [row_id])
        COMM.publish(self.session_id, self.root, goal, "failed", COMM.summary("failed", topic))
        return {"ok": False, "route": "none"}

    def _learn(self, answer, by, subgoal_id):
        for item in CTX.normalize_updates(answer.get("contextUpdates"), by, subgoal_id):
            CTX.apply(self.session_id, self.root, item)

    def _known(self, subgoal_id: str) -> List[str]:
        return CTX.render(self.session_id, self.root, subgoal_id) + [
            "Recent activity: " + json.dumps(EV.summary(self.session_id, self.root))]

    def _chat(self, decision, event, carry) -> Dict[str, Any]:
        subgoal_id = str(event.get("subgoalId") or decision.get("targetSubgoalId") or "")
        if not carry.get("transcript"):
            # No conversation in hand: this is the Overseer telling the reader
            # something on its own account (a repair that would not take).
            return self._escalate(decision, event)
        discovered = str(carry.get("discovered") or "")
        if event.get("type") == POLICY.CHAT_NEEDS_DISCOVERY and not discovered:
            question = str((event.get("payload") or {}).get("question") or "")
            with trace.span("discover", question=question[:120]):
                discovered = self.runtime.discover(question)
            self.emit(POLICY.DISCOVERED, EV.SYSTEM,
                      {"question": question, "chars": len(discovered)}, subgoal_id=subgoal_id)
            CTX.apply(self.session_id, self.root, CTX.update(
                CTX.DISCOVERED_DEPENDENCY,
                "asked of the directory: %s -- %s" % (question, discovered[:450]),
                subgoal_id=subgoal_id))
            carry["discovered"] = discovered
        with trace.span("chat.reply"):
            answer = self.agents["chat"](
                carry["transcript"], carry.get("context", ""),
                focus=chat_focus(carry.get("goal", ""), carry.get("subgoal", "")) + (
                    ["Pending human question: " + carry["pending_question"],
                     "Return resolution=resume only when the last message resolves that question, "
                     "cancel if explicitly abandoned, or wait otherwise. A progress question does not resolve it."]
                    if carry.get("pending_question") else []),
                known=self._known(subgoal_id), discovered=discovered, root=self.root)
        if not isinstance(answer, dict) or not answer.get("ok"):
            error = (answer or {}).get("error") if isinstance(answer, dict) else ""
            return {"ok": False, "error": str(error or "Bart could not answer"), "route": "chat"}
        self._learn(answer, "chat", subgoal_id)
        last = POLICY.last_user_text(carry["transcript"])
        answer = dict(answer, say=PUBLIC.text(answer.get("say"), CHAT.MAX_SAY, PUBLIC.debug_requested(last)))
        if PUBLIC.smalltalk(last):
            answer.update(todos=[], needs={})
        goals, _ = CS.load_goals(self.session_id, self.root)
        piece = GM.by_id(goals, subgoal_id) or {}
        existing = {PUBLIC.equivalent(r.get("text")) for r in piece.get("todo_items", [])}
        unique = []
        for proposed in answer.get("todos") or []:
            key = PUBLIC.equivalent(proposed)
            if key and key not in existing:
                unique.append(PUBLIC.text(proposed, 400)); existing.add(key)
        answer["todos"] = unique
        needs = answer.get("needs") or {}
        replies = REPLIES.from_chat(answer)
        if needs.get("kind") == CHAT.HUMAN and not carry.get("asked_human"):
            carry["asked_human"] = True
            asked = self.emit(POLICY.CHAT_NEEDS_HUMAN, EV.AGENT,
                              {"question": needs.get("question"), "resume": "conversation", "rows": carry.get("rows") or []}, subgoal_id=subgoal_id)
            more = self.handle(asked, dict(carry, question=needs.get("question")))
            if more and more.get("ok"):
                return dict(more, replies=replies + list(more.get("replies") or []))
            return dict(more or {"ok": False, "error": "Bart could not ask"}, route="brainstorm")
        if needs.get("kind") == CHAT.ENVIRONMENT and not discovered:
            asked = self.emit(POLICY.CHAT_NEEDS_DISCOVERY, EV.AGENT,
                              {"question": needs.get("question")}, subgoal_id=subgoal_id)
            more = self.handle(asked, carry)
            if more:
                return more
        if not replies:
            replies = [{"kind": "text", "text": "Bart had nothing to add."}]
        self.emit(POLICY.CHAT_REPLIED, EV.AGENT,
                  {"say": answer.get("say"), "todos": len(answer.get("todos") or [])},
                  subgoal_id=subgoal_id)
        return {"ok": True, "say": str(answer.get("say") or ""), "card": "none",
                "replies": replies, "route": "chat", "resolution": answer.get("resolution", "")}

    def _brainstorm(self, decision, event, carry) -> Dict[str, Any]:
        subgoal_id = str(event.get("subgoalId") or "")
        with trace.span("brainstorm.reply"):
            answer = self.agents["brainstorm"](
                carry.get("transcript") or [], carry.get("context", ""),
                focus=focus(carry.get("goal", ""), carry.get("subgoal", "")),
                known=self._known(subgoal_id), question=str(carry.get("question") or (decision.get("reason") if carry.get("background") else "") or ""),
                root=self.root)
        if isinstance(answer, dict) and answer.get("ok"):
            self._learn(answer, "brainstorm", subgoal_id)
            if carry.get("background") and answer.get("card") in ("questions", "focus"):
                questions = [r.get("text") for r in answer.get("replies") or [] if r.get("kind") == "text"]
                if questions:
                    self.emit(POLICY.CHAT_NEEDS_HUMAN, EV.AGENT,
                              {"question": questions[-1], "rows": carry.get("rows") or [],
                               "resume": "conversation"}, subgoal_id=subgoal_id)

            self.emit(POLICY.BRAINSTORM_REPLIED, EV.AGENT,
                      {"card": answer.get("card")}, subgoal_id=subgoal_id)
        return dict(answer or {"ok": False, "error": "Bart could not answer"}, route="brainstorm")

    def _plan(self, decision, event, carry) -> Dict[str, Any]:
        subgoal_id = str(event.get("subgoalId") or "")
        expected, _ = CS.load_goals(self.session_id, self.root)
        project = CTX.assemble(self.session_id, self.root, event, carry)
        project["routingReason"] = str(decision.get("reason") or "")[:600]
        with trace.span("path.plan"):
            answer = self.agents["path"](
                carry.get("transcript") or [], json.dumps(project, default=str),
                focus=chat_focus(carry.get("goal", ""), carry.get("subgoal", "")),
                known=self._known(subgoal_id), root=self.root)
        if isinstance(answer, dict) and answer.get("ok"):
            try:
                applied = PATH.apply(self.session_id, self.root, answer.get("changes") or [], expected)
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "route": "replan"}
            if applied:
                CTX.apply(self.session_id, self.root, CTX.update(CTX.DECISION,
                    answer.get("say") or "Updated the path", key="current_path", by="path"))
            self.emit(POLICY.PATH_PLANNED, EV.AGENT,
                      {"rows": sum(1 for r in answer.get("replies") or [] if r.get("kind") == "proposal")},
                      subgoal_id=subgoal_id)
        return dict(answer or {"ok": False, "error": "Bart could not plan this"}, route="replan")

    def _escalate(self, decision, event) -> Dict[str, Any]:
        """The repair loop is spent: one plain message into the piece's
        conversation, and a line in the Terminal, with no model asked."""
        subgoal_id = str(event.get("subgoalId") or "")
        reason = str((event.get("payload") or {}).get("reason") or decision.get("reason") or "")
        topic = COMM.subject(self.session_id, self.root, subgoal_id,
                             (event.get("payload") or {}).get("rows") or [])
        said = COMM.summary("escalated", topic)
        COMM.publish(self.session_id, self.root, subgoal_id, "escalated", said)
        BUILD.note_activity(self.session_id, self.root, subgoal_id, "verify",
                            "verification failed %d times; asking you" % (OVERSEER.REPAIR_LIMIT + 1))
        rows = (event.get("payload") or {}).get("rows") or []
        self.emit(POLICY.CHAT_NEEDS_HUMAN, EV.AGENT,
                  {"question": said, "rows": rows, "resume": "build"}, subgoal_id=subgoal_id,
                  todo_id=rows[0] if rows else "")
        self.emit(POLICY.VERIFY_ESCALATED, EV.AGENT, {"reason": reason, "said": said},
                  subgoal_id=subgoal_id)
        self._attempts(subgoal_id, set_to=0)
        return {"ok": True, "say": said, "card": "none",
                "replies": [{"kind": "text", "text": said}], "route": "chat"}

    # --- Build and the loop after it ----------------------------------------------

    def build_requested(self, goal_id: str, row_ids: Sequence[str],
                        quick: Optional[bool] = None) -> Dict[str, Any]:
        """The page's Build: recorded, routed, started."""
        ids = [str(r) for r in row_ids if isinstance(r, str)]
        if quick is None:
            goals, _ = CS.load_goals(self.session_id, self.root)
            goal = GM.by_id(goals, goal_id) or {}
            selected = [r for r in goal.get("todo_items", []) if r.get("id") in ids]
            quick = len(selected) == len(ids) and BUILD.prefer_quick(BUILD.picked_with_children(goal.get("todo_items", []), ids))
        from ... import telemetry
        lifecycle = telemetry.start_operation("todo.build", "workflow", attributes={"goal": goal_id})
        with telemetry.activate(lifecycle):
            trace.phase("build.click")
            token = trace.build_context.set(telemetry.context())
            try:
                with self.tracer.span("todo.build", goal=goal_id, rows=len(ids)):
                    trace.use(self.tracer)
                    try:
                        event = self.emit(POLICY.BUILD_REQUESTED, EV.USER,
                                          {"rows": ids, "quick": bool(quick)}, subgoal_id=goal_id,
                                          todo_id=ids[0] if len(ids) == 1 else "")
                        result = self.handle(event, {"rows": ids, "quick": bool(quick)})
                    finally:
                        trace.use(None)
            finally:
                trace.build_context.reset(token)
        if not isinstance(result, dict) or not result.get("ok"):
            self.emit("build.cancelled", EV.SYSTEM, {"rows": ids, "error": (result or {}).get("error") or "build did not start"}, subgoal_id=goal_id)
            lifecycle.fail(RuntimeError((result or {}).get("error") or "build did not start"))
        elif result.get("queued") or result.get("joined"):
            lifecycle.complete({"queued": True})
        return result or {"ok": False, "error": "the build was not started"}

    def preview_failed(self, goal_id, reason, lines):
        """Repair a previously built artifact through the same bounded loop."""
        record = BUILD.load_run(self.session_id, self.root, goal_id) or {}
        goals, _ = CS.load_goals(self.session_id, self.root)
        goal = GM.by_id(goals, goal_id) or {}
        ids = [r["id"] for r in goal.get("todo_items", [])
               if r.get("id") in (record.get("acceptance") or {}) and r.get("status") == "done"]
        if not ids:
            return {"ok": False, "error": "There is no completed build with saved acceptance to repair."}
        from ... import telemetry
        lifecycle = telemetry.start_operation("todo.build", "workflow", attributes={"goal": goal_id, "origin": "preview"})
        evidence = {"preview_startup": {"passed": False, "reason": str(reason)[:300],
                                       "lines": [str(line)[:500] for line in lines[-20:]]}}
        with telemetry.activate(lifecycle):
            token = trace.build_context.set(telemetry.context())
            trace.use(self.tracer)
            try:
                event = self.emit(POLICY.VERIFY_FAILED, EV.SYSTEM,
                    {"rows": ids, "reason": reason, "evidence": evidence}, subgoal_id=goal_id)
                result = self.handle(event, {"rows": ids, "evidence": evidence})
            finally:
                trace.use(None)
                trace.build_context.reset(token)
        if not isinstance(result, dict) or not result.get("ok"):
            lifecycle.complete()
        return result or {"ok": False, "error": reason}

    def _build(self, decision, event, carry) -> Dict[str, Any]:
        goal_id = str(event.get("subgoalId") or decision.get("targetSubgoalId") or "")
        if event.get("type") == POLICY.BUILD_REQUESTED:
            ids = [str(r) for r in (carry.get("rows") or [])]
            from . import acceptance
            try:
                criteria = acceptance.ensure(self.session_id, self.root, goal_id, ids,
                    CTX.assemble(self.session_id, self.root, event, carry))
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200]}
            trace.phase("acceptance.ready")
            self._attempts(goal_id, set_to=0)
            with trace.span("build.agent", goal=goal_id, rows=len(ids), quick=bool(carry.get("quick"))):
                result = self.runtime.build(self.session_id, self.root, goal_id, ids,
                                            quick=bool(carry.get("quick")))
            if isinstance(result, dict) and result.get("ok"):
                self.emit(POLICY.BUILD_STARTED, EV.SYSTEM,
                          {"rows": list(result.get("rows") or ids), "quick": bool(carry.get("quick"))},
                          subgoal_id=goal_id, run_id=str(result.get("claude_session_id") or ""))
            return result if isinstance(result, dict) else {"ok": False, "error": "no answer from the build"}
        # A repair: the verdict named what is wrong; the row goes back out
        # with that as the note, so the build fixes THAT and not the row
        # again from nothing.
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        rows = [str(r) for r in (payload.get("rows") or carry.get("rows") or [])]
        evidence = carry.get("evidence", payload.get("evidence")) or {}
        statuses = (evidence.get("rows") or {}) if isinstance(evidence, dict) else {}
        failed_rows = [rid for rid in rows if statuses.get(rid) == "failed"]
        row_id = str(decision.get("targetTodoId") or
                     (failed_rows[0] if failed_rows else rows[0] if rows else ""))
        reason = str(payload.get("reason") or decision.get("reason") or "the check failed")
        attempt = self._attempts(goal_id, add=1)
        note = ("Verification failed: %s. Fix that; the rows in this build were: %s"
                % (reason, ", ".join(rows) or row_id))
        note += "\nVerification evidence: " + json.dumps(carry.get("evidence", payload.get("evidence")) or {}, ensure_ascii=False)
        BUILD.note_activity(self.session_id, self.root, goal_id, "verify",
                            "Fixing: " + PUBLIC.text(reason, 160))
        trace.phase("repair.started")
        self.emit(POLICY.REPAIR_REQUESTED, EV.AGENT, {"attempt": attempt, "reason": reason,
                                                        "rows": rows}, subgoal_id=goal_id, todo_id=row_id)
        with trace.span("build.agent", goal=goal_id, row=row_id, repair=attempt):
            result = self.runtime.repair(self.session_id, self.root, goal_id, row_id, note, rows)
        if isinstance(result, dict) and result.get("ok"):
            self.emit(POLICY.BUILD_STARTED, EV.SYSTEM, {"rows": rows, "repair": attempt},
                      subgoal_id=goal_id, todo_id=row_id)
            topic = COMM.subject(self.session_id, self.root, goal_id, rows, evidence)
            COMM.publish(self.session_id, self.root, goal_id, "repair",
                         COMM.summary("repair", topic, reason), problem=reason)
        return dict(result or {"ok": False}, route="build", repair=attempt)

    def build_finished(self, goal_id: str, ended: str, row_ids: Sequence[str],
                       run_id: str = "", error: str = "") -> bool:
        """What a build's end sets off. ``ended`` is the run's word for it:
        idle (done), failed, waiting (on a question), cancelled. True when a
        repair went out, so the caller leaves the goal to it."""
        ids = [str(r) for r in row_ids]
        kind = {"idle": POLICY.BUILD_COMPLETED, "failed": POLICY.BUILD_FAILED,
                "waiting": POLICY.BUILD_QUESTION}.get(str(ended), "build.cancelled")
        with self.tracer.span("build.finished", goal=goal_id, ended=str(ended)):
            trace.use(self.tracer)
            try:
                event = self.emit(kind, EV.SYSTEM, {"rows": ids, "error": str(error or "")},
                                  subgoal_id=goal_id, run_id=str(run_id or ""))
                result = self.handle(event, {"rows": ids, "run_id": str(run_id or "")})
            finally:
                trace.use(None)
        return bool(isinstance(result, dict) and result.get("route") == "build"
                    and result.get("ok"))

    def _verify(self, decision, event, carry) -> Optional[Dict[str, Any]]:
        goal_id = str(event.get("subgoalId") or "")
        rows = [str(r) for r in (carry.get("rows") or (event.get("payload") or {}).get("rows") or [])]
        run_id = str(carry.get("run_id") or event.get("runId") or "")
        trace.phase("verifier.started")
        self.emit(POLICY.VERIFY_STARTED, EV.SYSTEM, {"rows": rows}, subgoal_id=goal_id, run_id=run_id)
        BUILD.note_activity(self.session_id, self.root, goal_id, "verify", "Checking the result")
        verdict = self.agents["verify"](self.session_id, self.root, goal_id, rows,
                                        self.runtime, self.checks)
        COMM.evidence_to_terminal(self.session_id, self.root, goal_id, (verdict or {}).get("evidence") or {})
        passed = bool(isinstance(verdict, dict) and verdict.get("passed"))
        reason = str((verdict or {}).get("reason") or "")
        BUILD.note_activity(self.session_id, self.root, goal_id, "verify",
                            ("verified: " if passed else "verification failed: ") + reason)
        outcome = self.emit(POLICY.VERIFY_PASSED if passed else POLICY.VERIFY_FAILED, EV.SYSTEM,
                            {"rows": rows, "reason": reason,
                             "evidence": (verdict or {}).get("evidence")},
                            subgoal_id=goal_id, run_id=run_id)
        CTX.apply(self.session_id, self.root, CTX.update(CTX.RUN_RESULT,
            "build ended; verification " + ("passed" if passed else "failed") + ": " + reason,
            subgoal_id=goal_id, run_id=run_id, key="current_run"))
        artifact = (verdict or {}).get("evidence", {}).get("artifact")
        if artifact:
            CTX.apply(self.session_id, self.root, CTX.update(CTX.ARTIFACT,
                json.dumps(artifact, ensure_ascii=False)[:600], subgoal_id=goal_id, key="current_artifact"))
        if passed:
            trace.phase("build.done")
            self._attempts(goal_id, set_to=0)
        followed = self.handle(outcome, {"rows": rows, "run_id": run_id,
            "evidence": (verdict or {}).get("evidence"),
            **({"transcript": [{"role": "user", "text": "Explain the verified result and the next step: " + reason}],
                "context": json.dumps(CTX.assemble(self.session_id, self.root, outcome)),
                "background": True} if passed else {})})
        if passed:
            topic = COMM.subject(self.session_id, self.root, goal_id, rows, (verdict or {}).get("evidence"))
            said = ""
            if isinstance(followed, dict) and followed.get("ok"):
                texts = [COMM.plain(r.get("text")) for r in followed.get("replies", []) if r.get("kind") == "text"]
                said = (texts[-1] if texts and followed.get("route") == "brainstorm"
                        else COMM.plain(followed.get("say")))
                if not said:
                    said = " ".join(COMM.plain(r.get("text")) for r in followed.get("replies", [])
                                    if r.get("kind") == "text").strip()
            COMM.publish(self.session_id, self.root, goal_id, "done", said or COMM.summary("done", topic))
        return followed if followed is not None else {"ok": True, "route": "verify",
                                                     "passed": passed, "reason": reason}


def _brief(result) -> Any:
    if not isinstance(result, dict):
        return result
    return {k: v for k, v in result.items()
            if k in ("ok", "route", "card", "passed", "reason", "repair", "error", "started", "rows")}


# --- entry points for the rest of hc -----------------------------------------------

def for_chat(session_id: str, root: Optional[Path], cwd: str = "",
             project_id: str = "", **kw) -> Orchestrator:
    cwd = cwd or _cwd(session_id, root)
    return Orchestrator(session_id, root, cwd=cwd, project_id=project_id or cwd, **kw)


def _cwd(session_id: str, root: Optional[Path]) -> str:
    try:
        return str(BUILD._cwd_for(session_id, root) or "")
    except Exception:  # noqa: BLE001
        return ""


def note_op(session_id: str, root: Optional[Path], op: Dict[str, Any],
            result: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """A goal page operation that went through: one minor event, never
    routed. build_todos does not come here; it goes through
    ``build_requested`` and is routed."""
    if not enabled():
        return None
    kind = POLICY.op_event(str(op.get("op") or ""))
    if not kind or kind == POLICY.BUILD_REQUESTED:
        return None
    payload = {k: op.get(k) for k in ("text", "done", "title", "id", "todo_id") if k in op}
    payload["ok"] = bool((result or {}).get("ok", True))
    try:
        orch = for_chat(session_id, root)
        return orch.emit(kind, EV.USER, payload,
                         subgoal_id=str(op.get("goal_id") or op.get("parent_goal_id") or ""),
                         todo_id=str(op.get("id") or op.get("todo_id") or ""))
    except (OSError, ValueError):
        return None


def build_finished(session_id: str, root: Optional[Path], goal_id: str, ended: str,
                   row_ids: Sequence[str], run_id: str = "", error: str = "") -> bool:
    """Called by build.Run._finish. Never raises into the build."""
    if not enabled():
        return False
    try:
        return for_chat(session_id, root).build_finished(goal_id, ended, row_ids, run_id, error)
    except Exception:  # noqa: BLE001 -- the build's own record is already written
        return False
