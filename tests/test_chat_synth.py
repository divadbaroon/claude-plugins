import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import chat_synth as S  # noqa: E402


SID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def generate_json(self, prompt):
        self.prompts.append(prompt)
        return self.responses.pop(0)


class RacingProvider(Provider):
    def __init__(self, responses, callback):
        super().__init__(responses)
        self.callback = callback

    def generate_json(self, prompt):
        result = super().generate_json(prompt)
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        return result


class ChatSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "state"
        self.cwd = Path(self.temp.name) / "repo"
        self.cwd.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, event, **extra):
        payload = {"session_id": SID, "hook_event_name": event,
                   "cwd": str(self.cwd), **extra}
        return CS.ingest_hook(payload, root=self.root)

    def test_initial_inference_consumes_plan_and_project_context(self):
        (self.cwd / "AGENTS.md").write_text("PROJECT-CONTEXT-SENTINEL")
        self.hook("UserPromptSubmit", prompt="Ship the chat goal UI")
        self.hook("PostToolBatch", tool_calls=[{
            "tool_name": "update_plan",
            "tool_use_id": "plan1",
            "tool_input": {"plan": [{"step": "Debug rebuild", "status": "completed"}]},
            "tool_response": {"success": True},
        }])
        provider = Provider([{"goals": [{
            "id": "g1", "title": "Ship chat goals", "status": "in_progress",
            "parent_goal_id": None, "evidence_ids": ["tool:plan1"],
            "todos": [{"text": "Connect the UI", "done": False,
                       "evidence_ids": ["tool:plan1"]}],
        }]}])

        result = S.refresh(SID, root=self.root, provider=provider)

        self.assertEqual("updated", result["status"])
        goals, _ = CS.load_goals(SID, self.root)
        self.assertEqual("Ship chat goals", goals["goals"][0]["title"])
        self.assertIn("PROJECT-CONTEXT-SENTINEL", provider.prompts[0])
        self.assertIn("Debug rebuild", provider.prompts[0])
        self.assertNotIn("private scratch", provider.prompts[0])
        state = CS.get_analyzer_state(SID, self.root)
        self.assertEqual("idle", state["status"])
        self.assertEqual(CS.load_manifest(SID, self.root)["last_ordinal"],
                         state["last_analyzed_ordinal"])

    def test_incremental_update_preserves_prompt_links_and_manual_fields(self):
        prompt = self.hook("UserPromptSubmit", prompt="Implement prompt linking")
        pid = CS.load_prompts(SID, self.root)[0]["id"]
        goals = {"version": 1, "goals": [{
            "id": "g1", "title": "Manual title", "status": "active",
            "parent_goal_id": None, "evidence_ids": [], "todos": [],
            "prompt_ids": [pid], "important_item_ids": [],
            "notes": "human note", "priority": "urgent", "origin": "user",
        }]}
        CS.save_goals(SID, goals, {"items": []}, root=self.root)
        CS.set_analyzer_state(SID, last_analyzed_ordinal=prompt.last_ordinal,
                              status="idle", root=self.root)
        self.hook("Stop", last_assistant_message="Prompt linking is implemented and tested.")
        provider = Provider([{"operations": [
            {"op": "attach_evidence", "goal_id": "g1",
             "evidence_ids": [CS.load_events(SID, self.root)[-1]["id"]]},
            {"op": "set_status", "goal_id": "g1", "status": "completed"},
            {"op": "rename_goal", "goal_id": "g1", "title": "model title"},
        ]}])

        result = S.refresh(SID, root=self.root, provider=provider)

        self.assertEqual("updated", result["status"])
        goal = CS.load_goals(SID, self.root)[0]["goals"][0]
        self.assertEqual("Manual title", goal["title"])
        self.assertEqual([pid], goal["prompt_ids"])
        self.assertEqual("human note", goal["notes"])
        self.assertEqual("urgent", goal["priority"])
        self.assertEqual("completed", goal["status"])

    def test_incremental_subgoal_stays_under_existing_goal(self):
        first = self.hook("UserPromptSubmit", prompt="Build the chat UI")
        CS.save_goals(SID, {"version": 1, "goals": [{
            "id": "g1", "title": "Build the chat UI", "status": "active",
            "parent_goal_id": None, "evidence_ids": [], "todos": [],
            "prompt_ids": [], "important_item_ids": [],
        }]}, {"items": []}, root=self.root)
        CS.set_analyzer_state(SID, last_analyzed_ordinal=first.last_ordinal,
                              status="idle", root=self.root)
        self.hook("UserPromptSubmit", prompt="Add prompt search")
        evidence = CS.load_events(SID, self.root)[-1]["id"]
        provider = Provider([{"operations": [{
            "op": "new_goal", "parent_goal_id": "g1",
            "title": "Add prompt search", "evidence_ids": [evidence],
            "todos": [], "distinct_because": "",
        }]}])
        S.refresh(SID, root=self.root, provider=provider)
        child = next(g for g in CS.load_goals(SID, self.root)[0]["goals"]
                     if g["id"] != "g1")
        self.assertEqual("g1", child["parent_goal_id"])

    def test_browser_race_retries_without_losing_manual_edit(self):
        self.hook("UserPromptSubmit", prompt="Build a goal")
        CS.save_goals(SID, {"version": 1, "goals": [{
            "id": "g1", "title": "Existing", "status": "active",
            "parent_goal_id": None, "evidence_ids": [], "todos": [],
            "prompt_ids": [], "important_item_ids": [], "notes": "",
            "priority": "normal", "origin": "inferred",
        }]}, {"items": []}, root=self.root)

        def browser_edit():
            goals, important = CS.load_goals(SID, self.root)
            goals["goals"][0]["notes"] = "edited while model ran"
            CS.save_goals(SID, goals, important, root=self.root)

        evidence = CS.load_events(SID, self.root)[0]["id"]
        response = {"operations": [{"op": "attach_evidence", "goal_id": "g1",
                                    "evidence_ids": [evidence]}]}
        provider = RacingProvider([response, response], browser_edit)

        result = S.refresh(SID, root=self.root, provider=provider)

        self.assertEqual("updated", result["status"])
        self.assertEqual(2, len(provider.prompts))
        goal = CS.load_goals(SID, self.root)[0]["goals"][0]
        self.assertEqual("edited while model ran", goal["notes"])
        self.assertEqual([evidence], goal["evidence_ids"])

    def test_goal_snapshot_holds_session_lock_through_revision_read(self):
        self.hook("UserPromptSubmit", prompt="Build a goal")
        CS.save_goals(SID, {"version": 1, "goals": [{
            "id": "g1", "title": "Existing", "status": "active",
            "parent_goal_id": None, "evidence_ids": [], "todos": [],
            "prompt_ids": [], "important_item_ids": [], "notes": "",
            "priority": "normal", "origin": "inferred",
        }]}, {"items": []}, root=self.root)
        evidence = CS.load_events(SID, self.root)[0]["id"]
        response = {"operations": [{
            "op": "attach_evidence", "goal_id": "g1",
            "evidence_ids": [evidence],
        }]}

        revision_entered = threading.Event()
        release_revision = threading.Event()
        edit_attempted = threading.Event()
        edit_done = threading.Event()
        original_revision = CS.goal_revision
        first_revision = [True]

        def blocked_revision(session_id, root=None):
            if first_revision[0]:
                first_revision[0] = False
                revision_entered.set()
                if not release_revision.wait(2):
                    raise TimeoutError("test did not release revision read")
            return original_revision(session_id, root)

        def browser_edit():
            edit_attempted.set()
            goals, important = CS.load_goals(SID, self.root)
            goals["goals"][0]["notes"] = "atomic browser edit"
            CS.save_goals(SID, goals, important, root=self.root)
            edit_done.set()

        provider = RacingProvider(
            [response, response], lambda: edit_done.wait(2)
        )
        results = []
        analyzer = threading.Thread(
            target=lambda: results.append(
                S.refresh(SID, root=self.root, provider=provider)
            )
        )
        with mock.patch.object(CS, "goal_revision", side_effect=blocked_revision):
            analyzer.start()
            self.assertTrue(revision_entered.wait(2))
            editor = threading.Thread(target=browser_edit)
            editor.start()
            self.assertTrue(edit_attempted.wait(2))
            self.assertFalse(edit_done.wait(0.1))
            release_revision.set()
            editor.join(2)
            analyzer.join(2)

        self.assertFalse(editor.is_alive())
        self.assertFalse(analyzer.is_alive())
        self.assertEqual("updated", results[0]["status"])
        self.assertEqual(2, len(provider.prompts))
        goal = CS.load_goals(SID, self.root)[0]["goals"][0]
        self.assertEqual("atomic browser edit", goal["notes"])
        self.assertEqual([evidence], goal["evidence_ids"])

    def test_failure_is_persisted_and_cursor_does_not_advance(self):
        self.hook("UserPromptSubmit", prompt="A goal")
        provider = Provider([{"wrong": []}])
        result = S.refresh(SID, root=self.root, provider=provider)
        self.assertEqual("error", result["status"])
        state = CS.get_analyzer_state(SID, self.root)
        self.assertEqual("error", state["status"])
        self.assertEqual(0, state["last_analyzed_ordinal"])
        self.assertFalse(
            (CS.paths(SID, self.root).session_dir / "analyzer.json").exists()
        )

    def test_empty_refresh_resets_pending_state(self):
        CS.ingest_hook({"session_id": SID, "hook_event_name": "SessionStart",
                        "cwd": str(self.cwd)}, root=self.root)
        CS.request_analysis(SID, root=self.root)
        result = S.refresh(SID, root=self.root, provider=Provider([]))
        self.assertEqual("empty", result["status"])
        self.assertEqual("idle", CS.get_analyzer_state(SID, self.root)["status"])

    def test_project_context_skips_symlinked_repo_files(self):
        secret = Path(self.temp.name) / "secret.txt"
        secret.write_text("DO-NOT-EXFILTRATE")
        (self.cwd / "README.md").symlink_to(secret)
        context = S.project_context(str(self.cwd), [])
        self.assertNotIn("DO-NOT-EXFILTRATE", context)

    def test_project_context_reads_only_in_repo_referenced_files(self):
        source = self.cwd / "src" / "feature.py"
        source.parent.mkdir()
        source.write_text("FEATURE-FILE-SENTINEL = True")
        outside = Path(self.temp.name) / "secret.py"
        outside.write_text("OUTSIDE-SECRET")
        (self.cwd / "linked.py").symlink_to(outside)
        events = [{"text": json.dumps({
            "path": "src/feature.py", "other": "linked.py"
        })}]
        context = S.project_context(str(self.cwd), events)
        self.assertIn("FEATURE-FILE-SENTINEL", context)
        self.assertNotIn("OUTSIDE-SECRET", context)

    def test_bounded_batches_do_not_advance_past_unsent_events(self):
        for index in range(6):
            self.hook("UserPromptSubmit", prompt=f"prompt-{index}-" + "x" * 80)
        provider = Provider([
            {"goals": [{"id": "g1", "title": "Batched goal",
                        "status": "active", "parent_goal_id": None,
                        "evidence_ids": [], "todos": []}]},
            {"operations": []},
            {"operations": []},
        ])
        with mock.patch.object(S, "MAX_EVENT_CHARS", 300):
            result = S.refresh(SID, root=self.root, provider=provider)
        state = CS.get_analyzer_state(SID, self.root)
        self.assertLess(state["last_analyzed_ordinal"],
                        CS.load_manifest(SID, self.root)["last_ordinal"])
        self.assertEqual("pending", state["status"])
        self.assertEqual("updated", result["status"])
        self.assertTrue(result["needs_handoff"])
        self.assertFalse(
            (CS.paths(SID, self.root).session_dir / "analyzer.json").exists()
        )

    def test_turn_landing_after_empty_read_stays_pending_for_handoff(self):
        self.hook("UserPromptSubmit", prompt="first turn")
        provider = Provider([{"goals": [{
            "id": "g1", "title": "First goal", "status": "active",
            "parent_goal_id": None, "evidence_ids": [], "todos": [],
        }]}])
        original_new_events = CS.new_events_since
        injected = [False]

        def racing_new_events(session_id, cursor, root=None):
            events = original_new_events(session_id, cursor, root)
            if cursor == 1 and not events and not injected[0]:
                injected[0] = True
                self.hook("UserPromptSubmit", prompt="turn during worker exit")
            return events

        with mock.patch.object(CS, "new_events_since", side_effect=racing_new_events):
            result = S.refresh(SID, root=self.root, provider=provider)

        state = CS.get_analyzer_state(SID, self.root)
        self.assertTrue(injected[0])
        self.assertEqual("pending", state["status"])
        self.assertEqual(1, state["last_analyzed_ordinal"])
        self.assertEqual(2, state["requested_ordinal"])
        self.assertTrue(result["needs_handoff"])
        self.assertFalse(
            (CS.paths(SID, self.root).session_dir / "analyzer.json").exists()
        )

    def test_concurrent_direct_refresh_coalesces_into_token_owner(self):
        self.hook("UserPromptSubmit", prompt="one analyzer only")
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider(Provider):
            def generate_json(inner_self, prompt):
                inner_self.prompts.append(prompt)
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release provider")
                return inner_self.responses.pop(0)

        first_provider = BlockingProvider([{"goals": [{
            "id": "g1", "title": "Single owner", "status": "active",
            "parent_goal_id": None, "evidence_ids": [], "todos": [],
        }]}])
        second_provider = Provider([{"goals": []}])
        first_results = []
        with mock.patch.dict(os.environ, {"HC_CHAT_WORKER_TOKEN": ""}):
            worker = threading.Thread(
                target=lambda: first_results.append(
                    S.refresh(SID, root=self.root, provider=first_provider)
                )
            )
            worker.start()
            self.assertTrue(entered.wait(2))
            second = S.refresh(SID, root=self.root, provider=second_provider)
            release.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual("coalesced", second["status"])
        self.assertEqual([], second_provider.prompts)
        self.assertEqual("updated", first_results[0]["status"])
        self.assertFalse(
            (CS.paths(SID, self.root).session_dir / "analyzer.json").exists()
        )

    def test_detached_worker_executes_real_cli_route(self):
        self.hook("UserPromptSubmit", prompt="Infer this goal")
        mock_dir = Path(self.temp.name) / "mock"
        mock_dir.mkdir()
        (mock_dir / "goal_synth.json").write_text(json.dumps({"goals": [{
            "id": "g1", "title": "Infer this goal", "status": "active",
            "parent_goal_id": None,
            "evidence_ids": [CS.load_events(SID, self.root)[0]["id"]],
            "todos": [],
        }]}))
        with mock.patch.dict(os.environ, {
            "HC_CHAT_PROVIDER": "mock",
            "HC_MOCK_DIR": str(mock_dir),
            "PYTHONPATH": str(HC_SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }):
            spawned = S.spawn_refresh(SID, root=self.root)
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            status = CS.get_analyzer_state(SID, self.root)
            if status["status"] in ("idle", "error"):
                break
            time.sleep(0.05)
        try:
            self.assertEqual("idle", status["status"], status)
            self.assertEqual(
                "Infer this goal", CS.load_goals(SID, self.root)[0]["goals"][0]["title"]
            )
            self.assertFalse(
                (CS.paths(SID, self.root).session_dir / "analyzer.json").exists()
            )
        finally:
            pid = spawned.get("pid")
            if pid:
                process = next((p for p in S._DETACHED_PROCESSES if p.pid == pid), None)
                if process and process.poll() is None:
                    os.kill(pid, signal.SIGTERM)
                if process:
                    process.wait(timeout=5)

    def test_live_but_unrelated_stale_pid_does_not_block_worker(self):
        self.hook("UserPromptSubmit", prompt="Needs analysis")
        p = CS.paths(SID, self.root)
        (p.session_dir / "analyzer.json").write_text(json.dumps({
            "pid": os.getpid(), "session_id": SID,
        }))
        CS.set_analyzer_state(SID, status="running", root=self.root)
        fake_process = mock.Mock(pid=987654)
        with (mock.patch.object(S, "_worker_process_matches", return_value=False),
              mock.patch.object(S.subprocess, "Popen", return_value=fake_process) as popen):
            result = S.spawn_refresh(SID, root=self.root)
        self.assertEqual("spawned", result["status"])
        self.assertEqual("pending", CS.get_analyzer_state(SID, self.root)["status"])
        popen.assert_called_once()

    def test_pending_live_worker_is_coalesced_before_it_marks_running(self):
        self.hook("UserPromptSubmit", prompt="Needs analysis")
        p = CS.paths(SID, self.root)
        (p.session_dir / "analyzer.json").write_text(json.dumps({
            "pid": os.getpid(), "session_id": SID,
        }))
        CS.set_analyzer_state(SID, status="pending", root=self.root)
        with (mock.patch.object(S, "_worker_process_matches", return_value=True),
              mock.patch.object(S.subprocess, "Popen") as popen):
            result = S.spawn_refresh(SID, root=self.root)
        self.assertEqual("coalesced", result["status"])
        popen.assert_not_called()

    def test_idle_live_worker_is_coalesced_during_atomic_exit_window(self):
        self.hook("UserPromptSubmit", prompt="Needs analysis")
        p = CS.paths(SID, self.root)
        (p.session_dir / "analyzer.json").write_text(json.dumps({
            "pid": os.getpid(), "session_id": SID,
        }))
        CS.set_analyzer_state(SID, status="idle", root=self.root)
        with (mock.patch.object(S, "_worker_process_matches", return_value=True),
              mock.patch.object(S.subprocess, "Popen") as popen):
            result = S.spawn_refresh(SID, root=self.root)
        self.assertEqual("coalesced", result["status"])
        self.assertEqual("pending", CS.get_analyzer_state(SID, self.root)["status"])
        popen.assert_not_called()

    def test_old_worker_token_cannot_clear_successor_record(self):
        p = CS.paths(SID, self.root)
        p.session_dir.mkdir(parents=True)
        worker = p.session_dir / "analyzer.json"
        S._write_worker_record(worker, {
            "pid": os.getpid(), "session_id": SID, "token": "successor",
            "mode": "direct", "started_at": time.time(),
        })

        with mock.patch.dict(os.environ, {"HC_CHAT_WORKER_TOKEN": "departed"}):
            S.clear_worker_record(SID, root=self.root)
        self.assertTrue(worker.exists())

        S.clear_worker_record(SID, root=self.root, owner_token="successor")
        self.assertFalse(worker.exists())


if __name__ == "__main__":
    unittest.main()
