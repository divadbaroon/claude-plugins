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
    def test_api_keys_are_kept_only_for_an_explicit_issued_key_process(self):
        base = {"ANTHROPIC_API_KEY": "sk-personal",
                "ANTHROPIC_AUTH_TOKEN": "sk-issued"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HC_USE_API_KEY", None)
            ordinary = providers.subscription_env(base)
        self.assertNotIn("ANTHROPIC_API_KEY", ordinary)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", ordinary)

        with patch.dict(os.environ, {"HC_USE_API_KEY": "1"}, clear=False):
            issued = providers.subscription_env(base)
        self.assertEqual("sk-issued", issued["ANTHROPIC_AUTH_TOKEN"])

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
    def test_a_reader_supplied_link_allows_only_web_tools(self, run):
        run.return_value = Mock(returncode=0, stdout=json.dumps({"card": "none"}),
                                stderr="")

        result = providers.ClaudeCLI("sonnet").generate_json_with_web(
            "Read https://example.com/brief")

        self.assertEqual({"card": "none"}, result)
        command = run.call_args.args[0]
        self.assertEqual(providers.WEB_TOOLS,
                         command[command.index("--tools") + 1])
        self.assertEqual(providers.WEB_TOOLS,
                         command[command.index("--allowed-tools") + 1])
        self.assertEqual("low", command[command.index("--effort") + 1])

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
    def test_a_plain_answer_is_asked_for_without_tools(self, run):
        # One question about text the prompt already carries. A subprocess
        # that goes reading the project for it spends the deadline instead.
        run.return_value = Mock(returncode=0, stdout="  it says hello  ",
                                stderr="")

        result = providers.ClaudeCLI("sonnet").generate_plain("what does it say?")

        self.assertEqual("  it says hello  ", result)
        command = run.call_args.args[0]
        self.assertEqual("", command[command.index("--tools") + 1])
        # Not an extraction call: the reader's own effort still applies.
        self.assertNotIn("--effort", command)

    def test_a_provider_with_no_plain_answer_gives_its_ordinary_one(self):
        class Only(providers.Base):
            def generate(self, prompt):
                return "said once"

        self.assertEqual("said once", Only("m").generate_plain("ask"))

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_an_answer_from_files_may_open_the_ones_it_is_pointed_at(self, run):
        # The opposite call: a scenario written from screenshots is written
        # from files the prompt names, so the subprocess is given Read and
        # the directories those files are in -- and nothing else.
        run.return_value = Mock(returncode=0, stdout="two people, one tree",
                                stderr="")

        said = providers.ClaudeCLI("sonnet").generate_reading(
            "describe /shots/a.png", read_dirs=["/shots"])

        self.assertEqual("two people, one tree", said)
        command = run.call_args.args[0]
        self.assertEqual("Read", command[command.index("--tools") + 1])
        # Available is not the same as permitted, and nobody is sitting in
        # front of this one to permit it.
        self.assertEqual("Read", command[command.index("--allowed-tools") + 1])
        self.assertEqual("/shots", command[command.index("--add-dir") + 1])

    def test_a_provider_that_cannot_open_a_file_still_answers(self):
        class Only(providers.Base):
            def generate(self, prompt):
                return "said once"

        self.assertEqual("said once",
                         Only("m").generate_reading("ask", ["/shots"]))

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_an_answer_that_is_in_the_code_may_go_and_look_for_it(self, run):
        # A question about how the project behaves is answered out of the
        # project, so the subprocess is started in it and given the tools
        # that find things and read them -- and nothing that writes or runs.
        run.return_value = Mock(returncode=0, stdout="GIVEN a build is running",
                                stderr="")

        said = providers.ClaudeCLI("sonnet").generate_searching(
            "what happens to the second build?", str(ROOT))

        self.assertEqual("GIVEN a build is running", said)
        command = run.call_args.args[0]
        self.assertEqual("Read,Grep,Glob", command[command.index("--tools") + 1])
        # Available is not the same as permitted, and nobody is sitting in
        # front of this one to permit it.
        self.assertEqual("Read,Grep,Glob",
                         command[command.index("--allowed-tools") + 1])
        self.assertEqual(str(ROOT), command[command.index("--add-dir") + 1])
        # Started in the project too: a grep rooted wherever this server
        # happened to be launched is a grep of the wrong repository.
        self.assertEqual(str(ROOT), run.call_args.kwargs["cwd"])
        # Finding the answer, opening what was found and then writing it are
        # three rounds where a quoted-prompt question is one.
        self.assertEqual(providers.CLAUDE_SEARCH_TIMEOUT_SECONDS,
                         run.call_args.kwargs["timeout"])

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_a_search_that_times_out_reports_its_own_deadline(self, run):
        run.side_effect = subprocess.TimeoutExpired(
            ["claude"], providers.CLAUDE_SEARCH_TIMEOUT_SECONDS)

        with self.assertRaisesRegex(
            providers.ProviderError,
            "timed out after %ds" % providers.CLAUDE_SEARCH_TIMEOUT_SECONDS
        ):
            providers.ClaudeCLI("sonnet").generate_searching("ask", str(ROOT))

    @patch("human_compact.trajectory.providers.subprocess.run")
    def test_a_directory_that_is_not_there_is_not_a_missing_cli(self, run):
        # Two things can be missing once a call names a directory to run in,
        # and "install the CLI" is the wrong thing to say about the other.
        run.side_effect = FileNotFoundError()

        with self.assertRaisesRegex(providers.ProviderError,
                                    "not a directory to look in"):
            providers.ClaudeCLI("sonnet").generate_searching(
                "ask", str(ROOT / "no-such-project"))

    def test_a_provider_with_nothing_to_search_with_still_answers(self):
        class Only(providers.Base):
            def generate(self, prompt):
                return "said once"

        self.assertEqual("said once", Only("m").generate_searching("ask", "/p"))

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
