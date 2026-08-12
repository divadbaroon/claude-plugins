import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import goal_synth, providers  # noqa: E402


class ClaudeCLIProviderTests(unittest.TestCase):
    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_json_calls_are_bounded_low_effort_and_tool_free(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({"goals": []}),
            stderr="",
        )

        result = providers.ClaudeCLI("sonnet").generate_json("large prompt")

        self.assertEqual({"goals": []}, result)
        command = run.call_args.args[0]
        child_env = run.call_args.kwargs["env"]
        self.assertEqual("low", command[command.index("--effort") + 1])
        self.assertEqual("", command[command.index("--tools") + 1])
        self.assertNotIn("--json-schema", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual("1", child_env["HC_CHAT_INFERENCE"])
        self.assertNotIn("CLAUDE_VAULT", child_env)
        self.assertEqual("large prompt", run.call_args.kwargs["input"])
        self.assertEqual(
            providers.CLAUDE_TIMEOUT_SECONDS,
            run.call_args.kwargs["timeout"],
        )

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_free_form_calls_do_not_force_low_effort_or_json_schema(self, run):
        run.return_value = Mock(returncode=0, stdout="summary", stderr="")

        result = providers.ClaudeCLI("sonnet").generate("summarize")

        self.assertEqual("summary", result)
        command = run.call_args.args[0]
        self.assertNotIn("--effort", command)
        self.assertNotIn("--json-schema", command)
        self.assertIn("--no-session-persistence", command)

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_timeout_reports_the_enforced_deadline(self, run):
        run.side_effect = subprocess.TimeoutExpired(["claude"], 180)

        with self.assertRaisesRegex(
            providers.ProviderError, "timed out after 180s"
        ):
            providers.ClaudeCLI("sonnet").generate_json("prompt")

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_mixed_draft_and_prose_uses_the_last_complete_object(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout=(
                '{"goals":[{"todos":[]}][0]}\n'
                "Let me redo this properly.\n"
                '{"goals":[{"id":"g1"}]}'
            ),
            stderr="",
        )

        result = providers.ClaudeCLI("sonnet").generate_json("prompt")

        self.assertEqual({"goals": [{"id": "g1"}]}, result)
        run.assert_called_once()

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_invalid_json_fails_without_a_second_model_call(self, run):
        run.return_value = Mock(returncode=0, stdout="not-json", stderr="")

        with self.assertRaisesRegex(
            providers.ProviderError, "parseable JSON"
        ):
            providers.ClaudeCLI("sonnet").generate_json("prompt")

        run.assert_called_once()


class GoalRebuildValidationTests(unittest.TestCase):
    def test_missing_goals_array_is_rejected_before_state_can_be_saved(self):
        provider = Mock()
        provider.generate_json.return_value = {}

        with self.assertRaisesRegex(ValueError, "missing the goals array"):
            goal_synth.rebuild(provider, [])

    def test_goals_array_is_returned_in_versioned_state(self):
        provider = Mock()
        provider.generate_json.return_value = {
            "goals": [{"id": "g1", "title": "Ship it"}]
        }

        result = goal_synth.rebuild(provider, [])

        self.assertEqual(
            {"version": 1, "goals": [{"id": "g1", "title": "Ship it"}]},
            result,
        )


if __name__ == "__main__":
    unittest.main()
