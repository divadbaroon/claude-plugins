import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "onboarding_sim", ROOT / "scripts" / "onboarding_sim.py"
)
SIM = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SIM)


class OnboardingSimulatorTests(unittest.TestCase):
    def test_seed_is_unbound_and_every_fixture_stays_inside_the_sandbox(self):
        with tempfile.TemporaryDirectory() as held:
            state_root = Path(held).resolve()
            session_dir = SIM.seed(state_root)

            self.assertEqual(state_root / SIM.SESSION_ID, session_dir)
            self.assertFalse(
                SIM.CS.project_bound(SIM.SESSION_ID, root=state_root)
            )
            projects = SIM.PS.list_projects(state_root)
            self.assertEqual(
                {name for name, _objective in SIM.EXAMPLE_PROJECTS},
                {project["name"] for project in projects},
            )
            for project in projects:
                self.assertTrue(
                    Path(project["cwd"]).is_relative_to(state_root),
                    project["cwd"],
                )

    def test_seed_exposes_the_real_project_onboarding_state(self):
        with tempfile.TemporaryDirectory() as held:
            state_root = Path(held).resolve()
            session_dir = SIM.seed(state_root)

            state = SIM.UI._payload(session_dir, True)

            self.assertEqual("chat", state["scope"])
            self.assertFalse(state["project_bound"])
            self.assertEqual([], state["goals"])


if __name__ == "__main__":
    unittest.main()
