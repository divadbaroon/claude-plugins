"""What a first-time user actually gets.

The panels in the Goals UI were empty because descriptions and project
directories were only ever produced by commands run by hand. Wiring them into
the build paths is not worth much unless the very first analysis — the one
that runs from the UI's onboarding button, through the worker, over an empty
vault — comes out described. That is what this exercises: a real
worker.drain() with nothing about goal-building mocked.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import discover, state, worker  # noqa: E402


class ExtractProvider:
    def identity(self):
        return "test:extract"

    def generate_json(self, _prompt):
        return {
            "apparent_objectives": ["Ship the goal UI"],
            "projects_or_topics": ["vault"],
            "actions_taken": ["Wired the panels"],
            "decisions": ["Hooks, not the SDK"],
            "blockers": ["The pty swallowed the prompt"],
            "unresolved_questions": ["Which port should it bind?"],
            "artifacts_or_outputs": ["bridge.js"],
            "evidence": [{"id": "session1#000", "excerpt": "ship the goal ui"}],
        }


class SynthProvider:
    """Answers whichever of the three synthesis prompts it is handed."""

    def __init__(self):
        self.prompts = []

    def identity(self):
        return "test:synth"

    def generate_json(self, prompt):
        self.prompts.append(prompt)
        if "descriptions" in prompt and "GOALS NEEDING DESCRIPTIONS" in prompt:
            return {"descriptions": {"g1": "Finishing it means the panels "
                                           "carry real context."}}
        if "FULL GOAL TREE" in prompt:
            return {"goals": [{
                "id": "g1", "title": "Ship the goal UI", "description": "",
                "parent_goal_id": None, "status": "active",
                "evidence_ids": ["session1#000"], "todos": [],
                "important_item_ids": [],
            }]}
        return {"objectives": [],
                "scope": {"label": "vault", "evidence_ids": []},
                "current_objective": {"label": "Ship it", "evidence_ids": []},
                "context_lens": {}}


class FirstRunTests(unittest.TestCase):
    """An empty vault, one conversation, one drain."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.vault = home / ".claude-vault"
        self.project = home / "project"
        self.project.mkdir()
        self.env = mock.patch.dict(
            os.environ, {"HOME": str(home), "HC_VAULT_DIR": str(self.vault)})
        self.env.start()
        self.addCleanup(self.env.stop)
        for module in (discover, state):
            for name in ("VAULT", "_VAULT"):
                if hasattr(module, name):
                    setattr(module, name, self.vault)
        self.session = {
            "session_id": "session1", "date": "2026-08-13",
            "cwd": str(self.project),
            "turns": [{"id": "session1#000", "role": "user",
                       "text": "ship the goal ui"}],
            "user_turn_count": 1, "low_evidence": False,
        }
        self.synth = SynthProvider()

    def drain(self):
        state.enqueue("session1")
        with mock.patch.object(worker, "_providers",
                               return_value=(ExtractProvider(), self.synth)), \
             mock.patch.object(worker, "_session_by_id",
                               return_value=self.session), \
             mock.patch.object(discover, "discover",
                               return_value=[self.session]):
            worker.drain(log=lambda _message: None)
        return json.loads((state.trajdir() / "goals.json").read_text())

    def test_the_first_tree_arrives_described(self):
        goals = self.drain()
        self.assertEqual(1, len(goals["goals"]))
        self.assertEqual("Finishing it means the panels carry real context.",
                         goals["goals"][0]["description"])

    def test_the_first_tree_arrives_with_its_project_directory(self):
        goals = self.drain()
        self.assertEqual([{"id": "s1", "type": "local",
                           "label": str(self.project)}],
                         goals["goals"][0]["sources"])

    def test_the_evidence_index_exists_before_goals_are_described(self):
        # describe() reads the evidence index; if the index were written after
        # the tree was built, every first run would silently skip descriptions.
        self.drain()
        self.assertTrue((state.trajdir() / "evidence_index.json").is_file())
        asked = [p for p in self.synth.prompts
                 if "GOALS NEEDING DESCRIPTIONS" in p]
        self.assertEqual(1, len(asked))
        self.assertIn("ship the goal ui", asked[0])


class AnalysisEntryPointTests(unittest.TestCase):
    """What the UI's analysis button runs must end with a goal tree.

    `hc refresh` extracts and rebuilds the lens and stops there. Spawning it
    for the button left a vault full of analyzed conversations and no goals,
    which on screen is indistinguishable from a failed analysis.
    """

    def test_the_ui_spawns_the_command_that_builds_goals(self):
        source = (ROOT / "hc" / "src" / "human_compact"
                  / "trajectory" / "ui.py").read_text()
        self.assertIn('"human_compact.cli", "analyze"', source)
        self.assertNotIn('"human_compact.cli", "refresh"', source)

    def test_analyze_runs_the_extraction_then_the_rebuild(self):
        from human_compact import cli
        order = []
        with mock.patch.object(cli, "refresh_main",
                               side_effect=lambda a: order.append(("refresh", a))), \
             mock.patch.object(cli, "goals_main",
                               side_effect=lambda a: order.append(("goals", a))), \
             mock.patch("human_compact.trajectory.state.set_processing"), \
             mock.patch("human_compact.trajectory.state.clear_processing"):
            cli.analyze_main([])
        self.assertEqual(["refresh", "goals"], [step for step, _ in order])
        self.assertIn("--rebuild", order[1][1])
        self.assertIn("--no-interact", order[1][1])

    def test_the_goal_build_reports_itself_so_the_banner_stays_up(self):
        from human_compact import cli
        phases = []
        with mock.patch.object(cli, "refresh_main"), \
             mock.patch.object(cli, "goals_main"), \
             mock.patch("human_compact.trajectory.state.set_processing",
                        side_effect=lambda sid, phase="extracting": phases.append(phase)), \
             mock.patch("human_compact.trajectory.state.clear_processing") as done:
            cli.analyze_main([])
        self.assertEqual(["synthesizing"], phases)
        self.assertEqual(1, done.call_count)

    def test_a_failed_rebuild_still_clears_the_running_marker(self):
        from human_compact import cli
        with mock.patch.object(cli, "refresh_main"), \
             mock.patch.object(cli, "goals_main", side_effect=RuntimeError("no provider")), \
             mock.patch("human_compact.trajectory.state.set_processing"), \
             mock.patch("human_compact.trajectory.state.clear_processing") as done:
            with self.assertRaises(RuntimeError):
                cli.analyze_main([])
        self.assertEqual(1, done.call_count)


class MachineSessionTests(unittest.TestCase):
    """hc's own calls to the Claude CLI must not come back as the user's work."""

    def _session(self, text):
        from human_compact.trajectory import discover as D
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sid" / "conv.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "user", "cwd": "/p",
                                    "message": {"content": text}}) + "\n"
                        + json.dumps({"type": "assistant",
                                      "message": {"content": "ok"}}) + "\n")
        return D.load_session(path, "2026-08-13")

    def test_the_extraction_prompt_is_not_a_conversation(self):
        self.assertIsNone(self._session(
            "You are analyzing one conversation between a user and Claude "
            "Code. Each turn below has a stable id."))

    def test_the_goal_tree_prompt_is_not_a_conversation(self):
        self.assertIsNone(self._session(
            "You will construct the FULL GOAL TREE for a user from structured "
            "extractions of their recent conversations."))

    def test_a_launched_goal_session_is_not_a_conversation(self):
        self.assertIsNone(self._session("Work on my Vault goal g1a1: ship it"))

    def test_a_real_conversation_still_loads(self):
        got = self._session("why is the overlay not showing up?")
        self.assertIsNotNone(got)
        self.assertEqual(1, got["user_turn_count"])

    def test_the_filtered_prefixes_match_the_prompts_actually_sent(self):
        # If a prompt is reworded, this fails rather than silently re-admitting
        # the analyzer's own sessions into the user's history.
        from human_compact.trajectory import discover as D
        extract_src = (ROOT / "hc" / "src" / "human_compact" / "trajectory"
                       / "extract.py").read_text()
        synth_src = (ROOT / "hc" / "src" / "human_compact" / "trajectory"
                     / "goal_synth.py").read_text()
        for prefix in ("You are analyzing one conversation between a user and "
                       "Claude Code",):
            self.assertIn(prefix, extract_src)
            self.assertIn(prefix, D.MACHINE_SESSION_PREFIXES)
        for prefix in ("You will construct the FULL GOAL TREE",
                       "Write the missing one-sentence description"):
            self.assertIn(prefix, synth_src)
            self.assertIn(prefix, D.MACHINE_SESSION_PREFIXES)
