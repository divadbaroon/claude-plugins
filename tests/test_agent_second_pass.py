"""Second-pass boundaries: semantic routing, durable plans and actual artifacts."""
import json
import os
import stat
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from test_agents import AgentCase, Recorder, PIECE, GOAL, ROWS, mark_rows, STUB
from human_compact.trajectory.agents import acceptance, artifacts, context as CTX
from human_compact.trajectory.agents import events as EV, overseer as OVERSEER, path as PATH
from human_compact.trajectory.agents import runtime as RT, verifier as VERIFIER, trace as TRACE
from human_compact.trajectory import chat_state as CS, goals as GM, build as BUILD, reader, preview
from human_compact import telemetry as T

MODEL = OVERSEER._model
DERIVE = acceptance.derive


class Engine:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []
    def generate_json(self, prompt):
        self.prompts.append(prompt)
        return self.answer


class SecondPassTests(AgentCase):
    def test_interactions_never_route(self):
        orch = self.orchestrator(overseer=Recorder())
        for kind in sorted(EV.INTERACTIONS):
            event = EV.interaction(self.session, self.root,
                {"type": kind, "subgoalId": PIECE, "payload": {"tab": "preview"}})
            self.assertIsNone(orch.handle(event))
        self.assertEqual(len(EV.INTERACTIONS), len(orch.events()))
        self.assertEqual(0, orch.overseer_calls)
        with self.assertRaises(ValueError):
            EV.interaction(self.session, self.root, {"type": "todo.build_requested"})

    def test_overseer_context_and_durable_explicit_preference(self):
        engine = Engine({"action": "chat", "reason": "Answer the preference", "contextUpdates": [
            {"kind": "user_preference", "key": "output_format", "text": "Use parquet"}]})
        from human_compact.trajectory import project_store
        project_store.save_project(self.root, self.project,
            {"objective": "Help readers inspect datasets", "description": "Import, save, then inspect"})
        reader.save({"level": "plain", "major": "Biology"}, self.root)
        orch = self.orchestrator(chat=Recorder({"ok": True, "say": "Parquet it is."}))
        orch.emit("tab.changed", EV.USER, {"to": "preview"}, subgoal_id=PIECE)
        with mock.patch.object(OVERSEER, "_model", side_effect=lambda e, s, *_: MODEL(e, s, engine, self.root)):
            out = orch.bart_message(self.held(), [{"role": "you", "text": "I prefer parquet"}])
        self.assertTrue(out["ok"])
        prompt = engine.prompts[0]
        for name in ("project", "currentGoal", "plan", "selectedSubgoal", "currentRun", "user",
                     "persistentContext", "recentResults", "recentBartTurns", "recentActivity", "triggerResult"):
            self.assertIn('"' + name + '"', prompt)
        self.assertIn("tab.changed", prompt)
        self.assertIn("Write the file", prompt)
        self.assertIn("I prefer parquet", prompt)
        self.assertIn("Help readers inspect datasets", prompt)
        self.assertIn("Import, save, then inspect", prompt)
        self.assertIn("Biology", prompt)
        self.assertIn("plain", prompt)
        self.assertEqual("Use parquet", CTX.load(self.session, self.root)[0]["text"])

    def test_model_semantics_cannot_override_explicit_intent(self):
        engine = Engine({"action": "build", "reason": "misclassified"})
        with mock.patch.object(OVERSEER, "_model", side_effect=lambda e, s, *_: MODEL(e, s, engine)):
            for text, expected in (("hi", "chat"), ("brainstorm options", "brainstorm"),
                                   ("break it down", "replan")):
                decision = OVERSEER.route(EV.new_event("bart.message", EV.USER, {"text": text}))
                self.assertEqual(expected, decision["action"])
        self.assertEqual(3, len(engine.prompts))

    def test_overseer_routes_semantic_planning_beyond_keyword_policy(self):
        text = "Could you lay out a roadmap from the prototype to a usable release?"
        self.assertEqual("chat", OVERSEER.fallback(EV.new_event("bart.message", EV.USER, {"text": text}))["action"])
        engine = Engine({"action": "replan", "reason": "The reader requested a roadmap"})
        with mock.patch.object(OVERSEER, "_model", side_effect=lambda e, s, *_: MODEL(e, s, engine)):
            routed = OVERSEER.route(EV.new_event("bart.message", EV.USER, {"text": text}))
        self.assertEqual("replan", routed["action"])

    def test_path_adds_and_revises_subgoals_atomically(self):
        expected, _ = CS.load_goals(self.session, self.root)
        changes = [{"op": "revise_subgoal", "subgoalId": PIECE, "title": "Save as parquet"},
                   {"op": "add_subgoal", "parentGoalId": GOAL, "title": "Inspect the exported dataset",
                    "todos": [{"text": "Show its first ten rows"}]}]
        engine = Engine({"say": "Keep import; add inspection.", "changes": changes})
        result = PATH.plan([], json.dumps(expected), engine=engine)
        self.assertTrue(result["ok"])
        PATH.apply(self.session, self.root, result["changes"], expected)
        actual, _ = CS.load_goals(self.session, self.root)
        self.assertEqual("Save as parquet", GM.by_id(actual, PIECE)["title"])
        self.assertEqual("Show its first ten rows", actual["goals"][-1]["todo_items"][0]["text"])
        with self.assertRaisesRegex(ValueError, "changed while planning"):
            PATH.apply(self.session, self.root, changes, expected)
        with self.assertRaises(ValueError):
            PATH.apply(self.session, self.root, [changes[0], {"op": "revise_subgoal", "subgoalId": "missing"}], actual)
        self.assertEqual(actual, CS.load_goals(self.session, self.root)[0])

    def test_verify_pass_can_keep_or_revise_path_with_current_context(self):
        for action in ("none", "replan"):
            with self.subTest(action):
                called = []
                def route(event, state, **kw):
                    if event["type"] == "verify.passed":
                        called.append(state)
                        return OVERSEER.decision(action, "A concrete next step is missing", subgoal_id=PIECE)
                    return OVERSEER.fallback(event, state)
                path = Recorder({"ok": True, "say": "Add inspection", "changes": [
                    {"op": "add_subgoal", "parentGoalId": GOAL, "title": "Inspect"}], "replies": []})
                orch = self.orchestrator(overseer=route, path=path,
                    verify=Recorder({"passed": True, "reason": "the artifact is correct", "evidence": {"control": "Export"}}))
                self.assertFalse(orch.build_finished(PIECE, "idle", ROWS))
                self.assertEqual(1, len(called))
                self.assertIn("verification_result", [u["kind"] for u in called[0]["persistentContext"]])
                self.assertEqual(1 if action == "replan" else 0, len(path.calls))

    def test_superseding_context_keeps_latest_without_losing_other_rows(self):
        for text in ("Use CSV", "Use parquet"):
            CTX.apply(self.session, self.root, CTX.update(CTX.USER_PREFERENCE, text,
                key="output_format", subgoal_id=PIECE))
        for row in ROWS:
            CTX.apply(self.session, self.root, CTX.update(CTX.TODO_STATUS, "done", todo_id=row))
        items = CTX.load(self.session, self.root)
        self.assertEqual(3, len(items))
        rendered = "\n".join(CTX.render(self.session, self.root, PIECE))
        self.assertIn("Use parquet", rendered)
        self.assertNotIn("Use CSV", rendered)
        for n in range(15):
            CTX.apply(self.session, self.root, CTX.update(CTX.NEW_FACT, "fact %d" % n, subgoal_id=PIECE))
        self.assertIn("fact 14", "\n".join(CTX.render(self.session, self.root, PIECE, limit=2)))

    def test_acceptance_is_saved_and_shared_by_build_and_verifier(self):
        contract = {"criterion": "The saved file contains dataset rows", "coverage": "complete", "checks": [
            {"kind": "file", "path": "dataset.txt", "contains": "row one"}]}
        with mock.patch.object(acceptance, "derive", return_value={r: contract for r in ROWS}):
            got = acceptance.ensure(self.session, self.root, PIECE, ROWS)
        goals, important = CS.load_goals(self.session, self.root)
        piece = GM.by_id(goals, PIECE)
        prompt = BUILD.compose_prompt(self.session, goals, important, {}, piece,
            BUILD.picked_with_children(piece["todo_items"], ROWS), root=self.root)
        self.assertIn("dataset.txt", prompt)
        mark_rows(self.session, self.root, PIECE, ROWS, "done")
        BUILD._save_run(self.session, self.root, {"goal_id": PIECE, "status": "idle", "exit_code": 0, "acceptance": got})
        rt = RT.LocalRuntime(str(self.project), self.root)
        (self.project / "dataset.txt").write_text("row one")
        verdict = VERIFIER.verify(self.session, self.root, PIECE, ROWS, rt)
        self.assertTrue(verdict["passed"], verdict)
        (self.project / "dataset.txt").write_text("wrong data")
        verdict = VERIFIER.verify(self.session, self.root, PIECE, ROWS, rt)
        self.assertFalse(verdict["passed"])
        self.assertEqual(got, verdict["evidence"]["acceptance"])

    def test_evidence_reaches_repair_and_empty_rows_fail(self):
        evidence = {"expected": "Export", "observed": "Welcome", "status": 200}
        orch = self.orchestrator(verify=Recorder({"passed": False, "reason": "wrong page", "evidence": evidence}))
        self.assertTrue(orch.build_finished(PIECE, "idle", ROWS))
        self.assertIn(json.dumps(evidence), self.runtime.reopens[0][2])
        self.assertFalse(VERIFIER.verify(self.session, self.root, PIECE, [], self.runtime)["passed"])

    def test_repair_targets_the_failed_row_instead_of_a_completed_sibling(self):
        evidence = {"rows": {ROWS[0]: "done", ROWS[1]: "failed"}}
        orch = self.orchestrator(verify=Recorder({"passed": False, "reason": "second row failed", "evidence": evidence}))
        self.assertTrue(orch.build_finished(PIECE, "idle", ROWS))
        self.assertEqual(ROWS[1], self.runtime.reopens[0][1])
        self.assertIn(json.dumps(evidence), self.runtime.reopens[0][2])

    def test_nested_preview_health_and_404_fail(self):
        mark_rows(self.session, self.root, PIECE, ROWS, "done")
        self.runtime.preview = {"status": "running", "run": {"healthy": False, "status": "running"}}
        self.assertFalse(VERIFIER.verify(self.session, self.root, PIECE, ROWS, self.runtime)["passed"])
        # Actual HTTP 404 is exercised by ArtifactBrowserTests below.

    def test_system_message_survives_stale_chat_save(self):
        CS.save_bart_chat(self.session, PIECE, [{"id": "m1", "who": "you", "kind": "text", "text": "hello"}], self.root)
        stale = CS.load_bart_chats(self.session, self.root)[PIECE]
        CS.append_bart_message(self.session, PIECE, "Verification needs your input", self.root)
        CS.save_bart_chat(self.session, PIECE, stale, self.root)
        self.assertEqual(2, len(CS.load_bart_chats(self.session, self.root)[PIECE]))

    def test_agent_spans_use_existing_debugger_telemetry(self):
        sink = T.MemorySink()
        tele = T.Telemetry(settings={"enabled": True, "mode": "content"}, sinks=[sink])
        orch = self.orchestrator(chat=Recorder({"ok": True, "say": "hello"}))
        with tele.operation("goal-page.bart", "workflow"):
            orch.bart_message(self.held(), [{"role": "you", "text": "hello"}])
        records = sink.operations.values()
        names = {r["name"]: r for r in records}
        self.assertTrue({"goal-page.bart", "bart.message", "overseer.route", "chat.reply"} <= set(names))
        self.assertEqual(names["bart.message"]["span_id"], names["overseer.route"]["parent_span_id"])
        self.assertEqual(1, len({r["trace_id"] for r in records}))

    def test_real_repair_run_reverifies_picked_row_on_same_trace(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        stub = bindir / "claude"
        # Resume input carries the id in JSON; the fixture emits a real DONE protocol line.
        stub.write_text(STUB.replace('ids = [w.strip', 'ids = [w.strip').replace(
            'def say(text):', 'if not ids:\n    ids = [json.loads(prompt)["id"]]\ndef say(text):'))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        mark_rows(self.session, self.root, PIECE, ROWS, "done")
        BUILD._save_run(self.session, self.root, {"goal_id": PIECE, "status": "idle", "claude_session_id": "old-run", "cwd": str(self.project)})
        heard = []
        def check(session, root, goal, rows, *args):
            heard.append(list(rows))
            return {"passed": True, "reason": "fixed", "evidence": {}}
        sink = T.MemorySink()
        tele = T.Telemetry(settings={"enabled": True}, sinks=[sink])
        from human_compact.trajectory.agents import orchestrator as ORCH
        with mock.patch.dict(os.environ, {"PATH": str(bindir)+os.pathsep+os.environ["PATH"],
                "HC_BUILD_MODE": "headless", "HC_BUILD_RESTART_CHECK": "0"}), \
                mock.patch.object(VERIFIER, "verify", check), tele.operation("todo.build", "workflow") as rootop:
            result = BUILD.reopen(self.session, self.root, PIECE, ROWS[0], "Fix the missing Export control")
            self.assertTrue(result["ok"], result)
            run = BUILD._run_for(self.session, self.root, PIECE)
            run.thread.join(10)
            self.assertFalse(run.thread.is_alive())
        self.assertEqual([[ROWS[0]]], heard)
        self.assertEqual([ROWS[0]], run.picked)
        self.assertEqual([ROWS[0]], BUILD.load_run(self.session, self.root, PIECE)["picked"])
        self.assertTrue(any(r["name"] == "overseer.route" for r in sink.operations.values()))
        self.assertEqual({rootop.trace_id}, {r["trace_id"] for r in sink.operations.values()})


class PlanAndContextTests(AgentCase):
    def test_path_can_revise_remove_reorder_and_replace(self):
        def apply(changes):
            expected, _ = CS.load_goals(self.session, self.root)
            PATH.apply(self.session, self.root, changes, expected)
            return CS.load_goals(self.session, self.root)[0]
        changed = apply([{"op": "revise_todos", "subgoalId": PIECE,
                          "todos": [{"id": ROWS[0], "text": "Write parquet"}]},
                         {"op": "remove_obsolete_todo", "subgoalId": PIECE, "todoId": ROWS[1]}])
        self.assertEqual(["Write parquet"], [r["text"] for r in GM.by_id(changed, PIECE)["todo_items"]])
        changed = apply([{"op": "add_subgoal", "parentGoalId": GOAL, "title": "Inspect"}])
        new_id = changed["goals"][-1]["id"]
        changed = apply([{"op": "reorder_subgoal", "subgoalId": new_id, "beforeId": PIECE},
                         {"op": "replace_subgoal", "subgoalId": new_id, "title": "Inspect parquet",
                          "todos": [{"text": "Show the schema"}]}])
        self.assertLess([g["id"] for g in changed["goals"]].index(new_id),
                        [g["id"] for g in changed["goals"]].index(PIECE))
        self.assertEqual("Show the schema", GM.by_id(changed, new_id)["todo_items"][0]["text"])
        mark_rows(self.session, self.root, PIECE, [ROWS[0]], "done")
        with self.assertRaisesRegex(ValueError, "completed"):
            apply([{"op": "replace_subgoal", "subgoalId": PIECE, "title": "Discard it"}])

    def test_overseer_input_has_a_hard_size_bound_and_keeps_selected_work(self):
        goals, important = CS.load_goals(self.session, self.root)
        for n in range(70):
            goal = GM.new_goal("large%d" % n, "long title " * 200, GOAL)
            goal["todo_items"] = [{"id": GM.todo_id(), "text": "long todo " * 500} for _ in range(35)]
            goals["goals"].append(goal)
        CS.save_goals(self.session, goals, important, self.root)
        event = EV.new_event("bart.message", EV.USER, {"text": "hello"}, subgoal_id=PIECE)
        state = CTX.assemble(self.session, self.root, event)
        self.assertLess(len(json.dumps(state)), 32000)
        self.assertEqual(PIECE, state["plan"][0]["id"])
        self.assertIn("Write the file", json.dumps(state))

    def test_acceptance_derivation_uses_the_model_and_rejects_empty_contract(self):
        engine = Engine({"criteria": {ROWS[0]: {"criterion": "The file contains data",
                         "checks": [{"kind": "file", "path": "data.csv", "contains": "name"}]}}})
        actual = DERIVE([{"id": ROWS[0], "text": "Save data"}], {"user": {"level": "plain"}}, self.root, engine)
        self.assertEqual("data.csv", actual[ROWS[0]]["checks"][0]["path"])
        self.assertIn("Save data", engine.prompts[0])
        with mock.patch.object(acceptance, "derive", return_value={}):
            with self.assertRaisesRegex(ValueError, "observable acceptance"):
                acceptance.ensure(self.session, self.root, PIECE, ROWS)
        self.assertNotIn("acceptance", GM.by_id(CS.load_goals(self.session, self.root)[0], PIECE)["todo_items"][0])

    def test_model_routes_verified_result_to_path(self):
        engine = Engine({"action": "replan", "reason": "Inspection is missing from the plan"})
        path = Recorder({"ok": True, "say": "Add inspection", "changes": [
            {"op": "add_subgoal", "parentGoalId": GOAL, "title": "Inspect results"}], "replies": []})
        orch = self.orchestrator(path=path, verify=Recorder({"passed": True, "reason": "saved", "evidence": {}}))
        with mock.patch.object(OVERSEER, "_model", side_effect=lambda e, s, *_: MODEL(e, s, engine)):
            orch.build_finished(PIECE, "idle", ROWS)
        self.assertEqual(1, len(engine.prompts))
        self.assertIn("Inspection is missing", path.calls[0][0][1])
        self.assertEqual("Inspect results", CS.load_goals(self.session, self.root)[0]["goals"][-1]["title"])

    def test_background_preference_question_is_delivered_even_without_say(self):
        def route(event, state, **kw):
            return (OVERSEER.decision("brainstorm", "Choose a display style", subgoal_id=PIECE)
                    if event["type"] == "verify.passed" else OVERSEER.fallback(event, state))
        orch = self.orchestrator(overseer=route,
            verify=Recorder({"passed": True, "reason": "saved", "evidence": {}}),
            brainstorm=Recorder({"ok": True, "say": "", "replies": [
                {"kind": "text", "text": "Would you like a table or chart?"}]}))
        orch.build_finished(PIECE, "idle", ROWS)
        self.assertIn("table or chart", CS.load_bart_chats(self.session, self.root)[PIECE][-1]["text"])


class LifecycleTests(AgentCase):
    def test_real_build_repair_and_verification_share_one_lifecycle(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        stub = bindir / "claude"
        script = STUB.replace("import json, sys", "import json, sys, time\ntime.sleep(0.08)")
        script = script.replace("def say(text):", 'if not ids:\n    ids = [json.loads(prompt)["id"]]\ndef say(text):')
        stub.write_text(script)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        checks = []
        def verify(session, root, goal, rows, *args):
            with TRACE.span("verifier.agent"):
                checks.append(list(rows))
                return {"passed": len(checks) > 1, "reason": "missing control" if len(checks) == 1 else "correct",
                        "evidence": {"observed": "wrong" if len(checks) == 1 else "Export"}}
        sink = T.MemorySink()
        tele = T.Telemetry(settings={"enabled": True}, sinks=[sink])
        from human_compact.trajectory.agents import orchestrator as ORCH
        with mock.patch.dict(os.environ, {"PATH": str(bindir)+os.pathsep+os.environ["PATH"],
                "HC_BUILD_MODE": "headless", "HC_BUILD_RESTART_CHECK": "0"}), \
                mock.patch.object(VERIFIER, "verify", verify), tele.with_run({"run_id": "chat:test"}):
            orch = ORCH.Orchestrator(self.session, self.root, cwd=str(self.project))
            out = orch.build_requested(PIECE, ROWS)
            self.assertTrue(out["ok"], out)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                rootops = [r for r in sink.operations.values() if r["name"] == "todo.build"]
                if rootops and rootops[0]["status"] == "completed":
                    break
                time.sleep(0.03)
            for run in list(BUILD._RUNS.values()):
                if run.thread:
                    run.thread.join(5)
        self.assertEqual([list(ROWS), list(ROWS)], checks)
        records = list(sink.operations.values())
        self.assertEqual(1, len({r["trace_id"] for r in records}))
        [lifecycle] = [r for r in records if r["name"] == "todo.build"]
        self.assertEqual("completed", lifecycle["status"])
        for op in records:
            if op["name"] in ("build.agent", "verifier.agent", "overseer.route"):
                self.assertEqual(lifecycle["span_id"], op["parent_span_id"], op)
        self.assertEqual(2, len([r for r in records if r["name"] == "verifier.agent"]))


class ArtifactBrowserTests(AgentCase):
    def test_healthy_wrong_page_fails_correct_control_and_behavior_pass(self):
        try:
            import playwright.sync_api
        except ImportError:
            self.skipTest("Playwright is unavailable")
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(404 if self.path == "/missing" else 200)
                self.end_headers()
                self.wfile.write((b"<h1>Welcome</h1>" if self.path != "/right" else
                    b'''<button onclick="document.querySelector('p').textContent='Saved'">Export</button><p></p>'''))
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d" % server.server_port
            control = {"kind": "control", "role": "button", "name": "Export"}
            wrong = artifacts.inspect_page(url + "/wrong", [control])
            self.assertFalse(wrong["passed"], wrong)
            self.assertEqual(200, wrong["status"])
            right = artifacts.inspect_page(url + "/right", [control,
                {"kind": "text", "text": "Saved", "steps": [{"action": "click", "role": "button", "name": "Export"}]}])
            self.assertTrue(right["passed"], right)
            self.assertFalse(artifacts.inspect_page(url + "/missing", []) ["passed"])
            proc = preview.Proc(str(self.project), {}, None)
            proc.url = url + "/missing"
            proc.probe(force=True)
            self.assertFalse(proc.healthy)
            mark_rows(self.session, self.root, PIECE, ROWS, "done")
            BUILD._save_run(self.session, self.root, {"goal_id": PIECE, "status": "idle", "exit_code": 0,
                "acceptance": {r: {"criterion": "Export control is visible", "coverage": "complete", "checks": [control]} for r in ROWS}})
            rt = RT.LocalRuntime(str(self.project), self.root)
            for suffix, expected in (("/wrong", False), ("/right", True)):
                with mock.patch.object(rt, "preview_state", return_value={"status": "running", "run": {
                        "healthy": True, "url": url + suffix, "status": "running"}}):
                    verdict = VERIFIER.verify(self.session, self.root, PIECE, ROWS, rt)
                self.assertEqual(expected, verdict["passed"], verdict)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)




from test_goal_page import BrowserCase, ChatCase, seed_design, server_for, post_json


class InteractionRouteTests(ChatCase):
    def test_endpoint_records_without_model_calls_and_rejects_routing_events(self):
        _, subgoals = seed_design(self.chat)
        with mock.patch.object(OVERSEER, "route", side_effect=AssertionError("must not route")), server_for(self.chat) as url:
            for kind in ("tab.changed", "subgoal.selected", "preview.interacted"):
                answer = post_json(url + "/api/goal-page/interaction",
                    {"type": kind, "subgoalId": subgoals[0], "payload": {"tab": "preview"}}, {"Origin": url})
                self.assertTrue(answer["ok"])
        self.assertEqual(3, len(EV.read("chat", self.root)))


class InteractionBrowserTests(BrowserCase):
    def test_open_page_receives_background_message_and_preserves_it_on_next_save(self):
        _, subgoals = seed_design(self.chat)
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            page.wait_for_function("window.engelbart && window.engelbart.store.get().status === 'ready'")
            page.evaluate("window.engelbart.actions.showTab('preview')")
            page.evaluate("window.engelbart.actions.showTab('terminal')")
            page.evaluate("(id) => window.engelbart.actions.selectSubgoal(id)", subgoals[1])
            CS.append_bart_message("chat", subgoals[1], "Verification needs your input", self.root)
            self.expect(page.get_by_text("Verification needs your input", exact=True)).to_be_visible(timeout=7000)
            # A subsequent user save reads the system message back in the same chat.
            page.evaluate("""async () => {
                const {store, services} = window.engelbart;
                const id = store.get().activeId;
                await services.saveChat({subgoalId:id, messages:store.get().slices[id].chat});
            }""")
            messages = CS.load_bart_chats("chat", self.root)[subgoals[1]]
            self.assertIn("Verification needs your input", [m["text"] for m in messages])
            types = {e["type"] for e in EV.read("chat", self.root)}
            self.assertTrue({"project.opened", "goal.opened", "tab.changed", "preview.opened",
                             "preview.closed", "subgoal.selected"} <= types, types)
            self.assertNotIn("overseer.routed", types)
            self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
