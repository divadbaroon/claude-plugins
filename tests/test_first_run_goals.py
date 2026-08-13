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
