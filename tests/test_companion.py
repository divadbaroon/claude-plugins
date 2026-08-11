import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.companion import (  # noqa: E402
    CompanionError,
    open_review_terminal,
    run_companion_review,
    terminal_launch_command,
    wait_for_review,
)
from compact_focus.cli import main  # noqa: E402
from compact_focus.review import new_review  # noqa: E402
from compact_focus.state import StatePaths, atomic_write_json  # noqa: E402


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = StatePaths.explicit("session", str(self.root))
        self.paths = StatePaths(
            self.root / "state",
            self.paths.session_id,
            self.paths.project_id,
            self.paths.cwd,
        )
        self.paths.ensure()
        self.cycle_id = "c-test"
        self.paths.cycle(self.cycle_id).mkdir(parents=True)

    def test_companion_review_uses_a_separate_executable_script(self):
        captured = {}

        def launch(script):
            captured["script"] = script.read_text(encoding="utf-8")
            captured["mode"] = script.stat().st_mode & 0o777
            atomic_write_json(
                self.paths.cycle(self.cycle_id) / "review-result.json",
                {"approved": True, "cycle_id": self.cycle_id, "error": ""},
            )
            return "test-terminal", None

        with patch("compact_focus.companion.open_review_terminal", side_effect=launch):
            approved, launcher = run_companion_review(self.paths, self.cycle_id)

        self.assertTrue(approved)
        self.assertEqual("test-terminal", launcher)
        self.assertEqual(0o700, captured["mode"])
        self.assertIn("compact-focus", captured["script"])
        self.assertIn("--result-file", captured["script"])
        self.assertIn("stty -ixon", captured["script"])
        self.assertIn('stty "$terminal_state"', captured["script"])
        self.assertNotIn("SIGSTOP", captured["script"])
        self.assertFalse((self.paths.cycle(self.cycle_id) / "open-review.command").exists())

    def test_custom_launcher_replaces_script_placeholder(self):
        script = self.root / "review.command"
        with patch.dict(
            os.environ,
            {"COMPACT_FOCUS_TERMINAL_LAUNCHER": "terminal --new {script}"},
        ):
            command, launcher = terminal_launch_command(script)
        self.assertEqual("custom", launcher)
        self.assertEqual(["terminal", "--new", str(script)], list(command))

    def test_long_lived_terminal_launcher_returns_without_waiting_for_review(self):
        process = unittest.mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("xterm", 0.5)
        with (
            patch(
                "compact_focus.companion.terminal_launch_command",
                return_value=(("xterm", "-e", "/tmp/review"), "xterm"),
            ),
            patch("compact_focus.companion.subprocess.Popen", return_value=process),
        ):
            launcher, active = open_review_terminal(self.root / "review.command")
        self.assertEqual("xterm", launcher)
        self.assertIs(process, active)

    def test_invalid_custom_launcher_is_reported(self):
        with (
            patch.dict(
                os.environ,
                {"COMPACT_FOCUS_TERMINAL_LAUNCHER": "terminal 'unterminated"},
            ),
            self.assertRaisesRegex(CompanionError, "TERMINAL_LAUNCHER is invalid"),
        ):
            terminal_launch_command(self.root / "review.command")

    def test_review_error_blocks_compaction(self):
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                {
                    "approved": False,
                    "cycle_id": self.cycle_id,
                    "error": "review window closed before a decision",
                }
            ),
            encoding="utf-8",
        )
        value = wait_for_review(result, self.cycle_id, timeout=0.2)
        self.assertFalse(value["approved"])
        self.assertIn("closed", value["error"])

    def test_wrong_cycle_result_is_rejected(self):
        result = self.root / "result.json"
        atomic_write_json(result, {"approved": True, "cycle_id": "c-other"})
        with self.assertRaisesRegex(CompanionError, "expected c-test"):
            wait_for_review(result, self.cycle_id, timeout=0.2)

    def test_invalid_timeout_is_reported(self):
        with (
            patch.dict(
                os.environ,
                {"COMPACT_FOCUS_REVIEW_TIMEOUT_SECONDS": "forever"},
            ),
            self.assertRaisesRegex(CompanionError, "REVIEW_TIMEOUT_SECONDS is invalid"),
        ):
            wait_for_review(self.root / "missing.json", self.cycle_id)

    def test_review_cli_publishes_the_companion_decision(self):
        cycle = self.paths.cycle(self.cycle_id)
        trace = {"source_hash": "source", "episodes": []}
        proposal = {"source_hash": "source", "items": [], "class_rules": []}
        atomic_write_json(cycle / "trace.json", trace)
        atomic_write_json(cycle / "proposal.initial.json", proposal)
        atomic_write_json(cycle / "review.draft.json", new_review(proposal))
        result = cycle / "review-result.json"
        prior = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, prior)
        with (
            patch("compact_focus.cli.run_review", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = main(
                [
                    "--state-root",
                    str(self.paths.base),
                    "review",
                    "--session",
                    self.paths.session_id,
                    "--cycle",
                    self.cycle_id,
                    "--result-file",
                    str(result),
                ]
            )
        self.assertEqual(0, code)
        self.assertEqual(
            {"approved": True, "cycle_id": self.cycle_id, "error": ""},
            json.loads(result.read_text(encoding="utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
