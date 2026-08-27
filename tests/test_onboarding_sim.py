import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "onboarding_sim", ROOT / "scripts" / "onboarding_sim.py"
)
SIM = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = SIM
SPEC.loader.exec_module(SIM)


class OnboardingSimulatorTests(unittest.TestCase):
    def test_the_simulator_imports_this_checkout_not_an_installed_runtime(self):
        loaded = Path(SIM.CS.__file__).resolve()
        self.assertIn(SIM.HC_SRC.resolve(), loaded.parents)

    def test_every_scenario_stays_inside_the_sandbox(self):
        with tempfile.TemporaryDirectory() as held:
            state_root = Path(held).resolve()
            for name in SIM.SCENARIOS:
                scenario_root = state_root / name
                session_dir = SIM.seed(scenario_root, name)

                self.assertEqual(scenario_root / SIM.SESSION_ID, session_dir)
                manifest = SIM.CS.load_manifest(
                    SIM.SESSION_ID, root=scenario_root
                )
                self.assertTrue(
                    Path(manifest["cwd"]).is_relative_to(scenario_root),
                    manifest["cwd"],
                )
                for project in SIM.PS.list_projects(scenario_root):
                    self.assertTrue(
                        Path(project["cwd"]).is_relative_to(scenario_root),
                        project["cwd"],
                    )

    def test_scenarios_reach_the_distinct_production_states(self):
        expected = {
            "first-use": (False, 0, 0),
            "returning": (False, 0, 2),
            "in-project": (False, 0, 2),
            "legacy-goals": (True, 1, 2),
            "already-onboarded": (True, 0, 2),
        }
        for name, (bound, goals, projects) in expected.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as held:
                state_root = Path(held).resolve()
                session_dir = SIM.seed(state_root, name)

                state = SIM.UI._payload(session_dir, True)

                self.assertEqual("chat", state["scope"])
                self.assertEqual(bound, state["project_bound"])
                self.assertEqual(goals, len(state["goals"]))
                self.assertEqual(projects, len(SIM.PS.list_projects(state_root)))

    def test_list_is_a_terminal_scenario_index(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = SIM.main(["list"])

        self.assertEqual(0, code)
        for name, scenario in SIM.SCENARIOS.items():
            self.assertIn(name, output.getvalue())
            self.assertIn(scenario.description, output.getvalue())


if __name__ == "__main__":
    unittest.main()
