import json
import os
import signal
import sys
import tempfile
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

    def test_failure_is_persisted_and_cursor_does_not_advance(self):
        self.hook("UserPromptSubmit", prompt="A goal")
        provider = Provider([{"wrong": []}])
        result = S.refresh(SID, root=self.root, provider=provider)
        self.assertEqual("error", result["status"])
        state = CS.get_analyzer_state(SID, self.root)
        self.assertEqual("error", state["status"])
        self.assertEqual(0, state["last_analyzed_ordinal"])

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


if __name__ == "__main__":
    unittest.main()
