"""Reproduce prompt and batching observations without running Claude or a model.

Run with the installed hc runtime's Python, or PYTHONPATH=hc/src python3.
All state is synthetic and lives in TemporaryDirectory; stdout is JSON.
"""
import hashlib
import ast
import copy
import inspect
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from human_compact.trajectory import build as B, chat_state as CS, project_store as PS
from human_compact.trajectory.agents import overseer as OVERSEER, policy as POLICY


def routing_cases(root):
    """Exercise the real guards with deliberately hypothetical model output."""
    examples = [
        ("bart", "Bart: ordinary wording", POLICY.BART_MESSAGE, "How does Bart work?", 0),
        ("options", "Bart: explicit options", POLICY.BART_MESSAGE, "What are my options?", 0),
        ("plan", "Bart: explicit planning", POLICY.BART_MESSAGE, "Break this down", 0),
        ("both", "Bart: options + planning", POLICY.BART_MESSAGE, "Brainstorm, then break this down", 0),
        ("smalltalk", "Bart: hello (before orchestrator override)", POLICY.BART_MESSAGE, "Hi Bart", 0),
        ("plan_requested", "Plan requested", POLICY.PLAN_REQUESTED, "", 0),
        ("human", "Chat needs a human preference", POLICY.CHAT_NEEDS_HUMAN, "", 0),
        ("discovery", "Chat needs project discovery", POLICY.CHAT_NEEDS_DISCOVERY, "", 0),
        ("build_requested", "Build requested", POLICY.BUILD_REQUESTED, "", 0),
        ("completed", "Build completed", POLICY.BUILD_COMPLETED, "", 0),
        ("failed", "Build failed", POLICY.BUILD_FAILED, "", 0),
        ("question", "Builder asked a question", POLICY.BUILD_QUESTION, "", 0),
        ("passed", "Verification passed", POLICY.VERIFY_PASSED, "", 0),
        ("repair1", "Verification failed: 0 repairs used", POLICY.VERIFY_FAILED, "", 0),
        ("repair2", "Verification failed: 1 repair used", POLICY.VERIFY_FAILED, "", 1),
        ("escalate", "Verification failed: 2 repairs used", POLICY.VERIFY_FAILED, "", 2),
        ("notes", "Notes edited (not routed)", POLICY.NOTES_EDITED, "", 0),
    ]

    class StubEngine:
        def __init__(self, action):
            self.action, self.calls = action, 0

        def generate_json(self, prompt):
            self.calls += 1
            if self.action == "exception":
                raise ValueError("synthetic routing outage")
            return {"action": self.action, "reason": "Hypothetical model explanation"}

    cases = []
    with patch("subprocess.Popen", side_effect=AssertionError("No live process in routing probe")):
        for key, label, kind, words, attempts in examples:
            event = {"type": kind, "subgoalId": "g002", "todoId": "t001",
                     "payload": {"text": words, "rows": ["t001"], "reason": "Synthetic check result"}}
            state = {"attempts": {"g002": attempts}}
            safe = OVERSEER.fallback(event, state)
            outcomes = {}
            for action in (*OVERSEER.ACTIONS, "invalid", "exception"):
                engine = StubEngine(action)
                result = OVERSEER.route(event, state, engine=engine, root=root)
                assert result["action"] in OVERSEER.ACTIONS
                no_model = kind not in POLICY.MEANINGFUL or kind in (
                    POLICY.BUILD_REQUESTED, POLICY.BUILD_COMPLETED, POLICY.VERIFY_FAILED)
                assert engine.calls == (0 if no_model else 1)
                if no_model or action in ("invalid", "exception"):
                    assert all(result[k] == safe[k] for k in ("action", "reason", "targetSubgoalId", "targetTodoId"))
                    assert [(u["kind"], u["text"]) for u in result["contextUpdates"]] == [
                        (u["kind"], u["text"]) for u in safe["contextUpdates"]]
                if kind == POLICY.BART_MESSAGE:
                    assert result["action"] in ("chat", "brainstorm", "replan")
                    if safe["action"] != "chat":
                        assert result["action"] == safe["action"]
                elif kind == POLICY.VERIFY_PASSED:
                    assert result["action"] not in ("build", "verify")
                else:
                    assert result["action"] == safe["action"]
                outcomes[action] = {"model_calls": engine.calls, "decision": result}
            cases.append({"id": key, "label": label, "event": event, "attempts": attempts,
                          "fallback": safe, "outcomes": outcomes})
    return cases


def queue_probe(base, source_goals, cwd):
    """Real join/queue/prompt functions; fake the live process, never launch it."""
    root = base / "queue-state"
    root.mkdir()
    sid, goal_id = "queue-fixture", "g002"
    p = CS.paths(sid, root)
    p.session_dir.mkdir()
    p.manifest.write_text(json.dumps({"session_id": sid, "cwd": str(cwd)}))
    goals = copy.deepcopy(source_goals)
    goal = next(g for g in goals["goals"] if g["id"] == goal_id)
    goal["notes"] = "SAVED NOTE: Preserve the scenario history."
    for row in goal["todo_items"]:
        row["id"] = "t" + row["id"][1:].zfill(4)
        row["status"] = "building" if row["id"] in ("t0001", "t0005") else ""
    assert CS.save_goals(sid, goals, {"items": []}, root)

    class LiveStub:
        phase = "rows"
        quick = False
        claude_session = "existing-full-conversation"

        def __init__(self):
            self.picked = ["t0001", "t0005"]
            self.acceptance, self.verification_rows = {}, []
            self.picked_chars, self.message = 0, ""

        def alive(self):
            return True

        def redirect(self, message):
            self.message = message
            return True

        def record(self, **kwargs):
            pass

    with patch("subprocess.Popen", side_effect=AssertionError("No process in queue probe")):
        live = LiveStub()
        with patch.dict(os.environ, {"HC_BUILD_MODE": "headless"}), patch.object(B, "_run_for", return_value=live):
            joined = B.start(sid, root, goal_id, ["t0006", "t0007"], quick=True)
        assert joined.get("joined") and joined["claude_session_id"] == live.claude_session, joined
        assert "Rename the Run button" in live.message and "Add a Clear button" in live.message
        assert "SAVED NOTE" not in live.message
        assert live.picked == ["t0001", "t0005", "t0006", "t0007"]
        assert live.quick is False
        assert not B.pending(sid, root)

        with patch.dict(os.environ, {"HC_BUILD_MODE": "session"}), patch.object(B, "_run_for", return_value=None):
            queued = B.start(sid, root, goal_id, ["t0006", "t0007"], quick=False)
        assert queued["queued"] and len(B.pending(sid, root)) == 1
        before, important = CS.load_goals(sid, root)
        piece = next(g for g in before["goals"] if g["id"] == goal_id)
        assert all(r["status"] == "queued" for r in piece["todo_items"] if r["id"] in ("t0006", "t0007"))
        piece["notes"] += "\nAFTER QUEUE: Require a confirmation before clearing."
        assert CS.save_goals(sid, before, important, root)
        delivered = B.deliver(sid, root, "Stop")
        assert "SAVED NOTE" in delivered and "AFTER QUEUE" not in delivered
        assert not B.pending(sid, root)
        after, _ = CS.load_goals(sid, root)
        piece = next(g for g in after["goals"] if g["id"] == goal_id)
        assert all(r["status"] == "building" for r in piece["todo_items"] if r["id"] in ("t0006", "t0007"))
        assert B.deliver(sid, root, "Stop") == ""
    return {"scope": "Real backend functions in temporary state; live process redirect stubbed; no hook or Claude process run",
            "joined": joined, "joined_message": live.message,
            "session_delivered_message": delivered.replace(str(cwd), "/example/specification-study"),
            "checks": {"join_preserves_conversation": True, "join_keeps_existing_lane": True,
                       "join_includes_new_rows": True, "join_omits_saved_notes": True,
                       "join_does_not_enqueue": True, "session_queue_drains": True,
                       "session_rows_queued_then_building": True, "queued_prompt_is_snapshot": True,
                       "second_delivery_empty": True}}


def main():
    with tempfile.TemporaryDirectory(prefix="engelbart-explanation-") as tmp:
        base = Path(tmp)
        root, cwd = base / "state", base / "project"
        root.mkdir(); cwd.mkdir()
        sid = "explanation-fixture"
        p = CS.paths(sid, root)
        p.session_dir.mkdir()
        p.manifest.write_text(json.dumps({"session_id": sid, "cwd": str(cwd)}))
        PS.save_project(root, cwd, {"name": "Specification study", "objective": "Help novices discover requirements."})
        items = [
            {"id": "t001", "text": "Add a scenario editor", "depth": 0},
            {"id": "t002", "text": "Add an input field", "depth": 1},
            {"id": "t003", "text": "Add an expected-output field", "depth": 1},
            {"id": "t004", "text": "Add a Run button", "depth": 1},
            {"id": "t005", "text": "Display scenario history", "depth": 0},
        ]
        later = [
            {"id": "t006", "text": "Rename the Run button to Test scenario", "depth": 0},
            {"id": "t007", "text": "Add a Clear button", "depth": 0},
        ]
        goal = {"id": "g002", "parent_goal_id": "g001", "title": "Scenario editor", "status": "active",
                "notes": "Students must state an expected outcome before running a scenario.", "todo_items": items,
                "todos_md": "\n".join("  " * r["depth"] + "- [ ] " + r["text"] for r in items)}
        goals = {"goals": [{"id": "g001", "title": "Help novices discover requirements", "status": "active"}, goal]}
        important = {"items": []}
        picked = B.picked_with_children(items, [r["id"] for r in items])
        # Explicitly neutralize only environment overrides and process creation.
        with patch.dict(os.environ, {"HC_BUILD_LANE": "auto"}), patch("subprocess.Popen", side_effect=AssertionError("Probe must never launch a process")):
            full = B.compose_prompt(sid, goals, important, [], goal, picked, root=root)
            p.goal_context.write_text(CS._goal_context_text(sid, goals, important, []))
            estimate = B.fallback_estimate(sid, root, 5)
            goal["notes"] += "\nNEW NOTE: Keep the history after clearing the editor."
            goal["todo_items"] = [dict(r, status="done") for r in items] + later
            goal["todos_md"] = "\n".join(
                "  " * r["depth"] + ("- [x] " if r.get("status") == "done" else "- [ ] ") + r["text"]
                for r in goal["todo_items"])
            newer_full = B.compose_prompt(sid, goals, important, [], goal, B.picked_with_children(later, [r["id"] for r in later]), root=root)
            quick = B.compose_prompt(sid, goals, important, [], goal, B.picked_with_children(later, [r["id"] for r in later]), root=root, quick=True)
            probes = {
                "initial_rows": len(picked),
                "initial_protocol_ids": [r["id"] for r in picked if r["_picked"]],
                "initial_quick": B.prefer_quick(picked),
                "later_quick": B.prefer_quick(later),
                "full_includes_notes": "Students must state" in full,
                "next_full_includes_new_note": "NEW NOTE" in newer_full,
                "next_quick_includes_new_note": "NEW NOTE" in quick,
                "cold_fallback": estimate,
            }
            assert probes["initial_rows"] == 5
            assert probes["initial_protocol_ids"] == ["t001", "t005"]
            assert not probes["initial_quick"] and probes["later_quick"]
            assert probes["full_includes_notes"] and probes["next_full_includes_new_note"]
            assert not probes["next_quick_includes_new_note"]
            assert estimate["minutes"] == 20 and estimate["tokens"] >= 150000
        package = Path(B.__file__).resolve().parents[1]
        files = ["trajectory/build.py", "trajectory/ui.py", "trajectory/chat_state.py",
                 "trajectory/web/goal/components/todos.js", "trajectory/web/goal/actions.js", "trajectory/web/goal/services.js", "trajectory/web/goal/store.js",
                 "trajectory/agents/orchestrator.py", "trajectory/agents/overseer.py", "trajectory/agents/context.py",
                 "trajectory/agents/chat.py", "trajectory/agents/path.py", "trajectory/agents/acceptance.py",
                 "trajectory/agents/policy.py", "trajectory/agents/brainstorm.py",
                 "trajectory/agents/replies.py", "trajectory/agents/presentation.py",
                 "trajectory/agents/verifier.py", "trajectory/agents/artifacts.py",
                 "trajectory/agents/runtime.py", "trajectory/agents/communication.py",
                 "trajectory/preview.py", "trajectory/brainstorm.py", "trajectory/setup_chat.py", "trajectory/providers.py"]
        lane_patterns = {}
        for node in ast.walk(ast.parse(inspect.getsource(B.prefer_quick))):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("risky", "bounded"):
                        lane_patterns[target.id] = ast.literal_eval(node.value.args[0])
        lane_examples = [
            ("original", [r["text"] for r in items]),
            ("later", [r["text"] for r in later]),
            ("vague", ["fix it"]), ("risky", ["Add a database button"]),
            ("wording_full", ["Rename the navigation item", "Add a Clear button"]),
            ("wording_fast", ["Rename the navigation label", "Add a Clear button"]),
            ("empty", []), ("blank", [""]), ("four", ["Add a button"] * 4),
            ("one_300", ["x" * 300]), ("one_301", ["x" * 301]),
            ("two_500", ["button " + "x" * 493] * 2),
            ("two_501", ["button " + "x" * 494] * 2),
            ("unicode_boundary", ["αdatabaseα"]), ("unicode_length", ["😀" * 300]),
        ]
        with patch.dict(os.environ, {"HC_BUILD_LANE": "auto"}):
            lane_cases = [{"id": key, "rows": rows, "quick": B.prefer_quick([{"text": t} for t in rows])}
                          for key, rows in lane_examples]
        out = {"date": "2026-09-13", "scope": "Installed Engelbart 0.20.4; synthetic fixture; no model calls",
               "items": items, "later": later, "probes": probes,
               "lane_patterns": lane_patterns, "lane_cases": lane_cases,
               "routing_cases": routing_cases(root),
               "queue_probe": queue_probe(base, goals, cwd),
               "full_prompt": full.replace(str(cwd), "/example/specification-study"),
               "later_full_prompt": newer_full.replace(str(cwd), "/example/specification-study"),
               "later_quick_prompt": quick.replace(str(cwd), "/example/specification-study"),
               "sources": {name: {"sha256": hashlib.sha256((package / name).read_bytes()).hexdigest(),
                                  "text": (package / name).read_text()} for name in files if (package / name).exists()}}
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
