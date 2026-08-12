import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact import cli, global_vault  # noqa: E402
from human_compact.trajectory import (  # noqa: E402
    discover,
    extract,
    goals,
    graph_build,
    lens,
    secure_io,
    state,
    synthesize,
    worker,
)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class Provider:
    def identity(self):
        return "test:private"

    def generate_json(self, _prompt):
        return {
            "apparent_objectives": ["Protect derived context"],
            "projects_or_topics": ["human-compact"],
            "actions_taken": ["Added private writes"],
            "decisions": [],
            "blockers": [],
            "unresolved_questions": [],
            "artifacts_or_outputs": [],
            "evidence": [{"id": "session1#000", "excerpt": "protect it"}],
        }


class SynthesisProvider(Provider):
    def generate_json(self, _prompt):
        return {
            "objectives": [],
            "scope": {"label": "privacy", "evidence_ids": []},
            "current_objective": {"label": "Protect state", "evidence_ids": []},
            "context_lens": {},
        }


class TrajectoryPermissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.vault = self.home / ".claude-vault"
        self.env = mock.patch.dict(os.environ, {
            "HC_HOME": str(self.home),
            "CLAUDE_VAULT_DIR": str(self.vault),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.vault_patch = mock.patch.object(discover, "VAULT", self.vault)
        self.vault_patch.start()
        self.addCleanup(self.vault_patch.stop)

    def assert_private_tree(self, root):
        for path in [root, *root.rglob("*")]:
            if path.is_symlink():
                self.fail(f"unexpected symlink: {path}")
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(expected, mode(path), path)

    def test_no_transcript_trajectory_initialization_is_private(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.trajectory_main([
                "--provider", "mock", "--synth-provider", "mock",
                "--no-interact",
            ])

        trajectory = self.vault / "trajectory"
        self.assertEqual(0o700, mode(self.vault))
        self.assertEqual(0o700, mode(trajectory))
        self.assertEqual(0o600, mode(trajectory / "config.json"))
        self.assertFalse((self.vault / "sessions").exists())

    def test_real_trajectory_goal_and_worker_writes_are_private(self):
        trajectory = self.vault / "trajectory"
        session = {
            "session_id": "session1", "date": "2026-08-12", "cwd": "/tmp",
            "turns": [{"id": "session1#000", "role": "user", "text": "protect it"}],
            "user_turn_count": 1, "low_evidence": True,
        }
        discover.write_evidence_index([session], trajectory)
        extractions, failures = extract.extract_all(
            [session], Provider(), trajectory / "conversations", workers=1,
            log=lambda _message: None,
        )
        self.assertFalse(failures)
        analysis = synthesize.synthesize(
            extractions, SynthesisProvider(), 30, trajectory,
            {"extract": "test", "synthesize": "test"},
        )
        graph_build.build(extractions, analysis, trajectory)
        goals.save(trajectory, {
            "version": 1,
            "goals": [{
                "id": "g1", "title": "Protect state", "status": "active",
                "parent_goal_id": None, "evidence_ids": [], "todos": [],
                "important_item_ids": [],
            }],
        }, {"items": []})
        lens.save_correction(trajectory, "objective", "edit", "Keep it private")
        state.enqueue("session1")
        state.set_processing("session1")

        # Exercise the failure writer in the real worker drain loop.
        state.clear_processing()
        with mock.patch.object(worker, "_providers", return_value=(Provider(), SynthesisProvider())), \
                mock.patch.object(worker, "_session_by_id", return_value=session), \
                mock.patch.object(extract, "extract_all", side_effect=RuntimeError("private failure")), \
                mock.patch.object(worker, "synthesize_from_cache"), \
                mock.patch.object(worker, "_update_goals"):
            worker.drain(log=lambda _message: None)

        with mock.patch.object(global_vault.subprocess, "Popen"):
            global_vault._start_worker(self.vault, "session2", "now")

        self.assert_private_tree(self.vault)

    def test_serve_only_migrates_legacy_permissions_recursively(self):
        trajectory = self.vault / "trajectory"
        conversations = trajectory / "conversations"
        conversations.mkdir(parents=True)
        analysis = trajectory / "analysis.json"
        cache = conversations / "session.json"
        analysis.write_text("{}")
        cache.write_text("{}")
        os.chmod(self.vault, 0o755)
        os.chmod(trajectory, 0o755)
        os.chmod(conversations, 0o755)
        os.chmod(analysis, 0o644)
        os.chmod(cache, 0o644)

        with mock.patch("human_compact.trajectory.serve.run") as run:
            cli.trajectory_main(["--serve-only"])
        run.assert_called_once()
        self.assert_private_tree(self.vault)

    def test_symlinked_vault_or_trajectory_is_rejected_without_touching_target(self):
        for link_at_root in (True, False):
            with self.subTest(link_at_root=link_at_root):
                case = self.home / ("root-link" if link_at_root else "trajectory-link")
                case.mkdir()
                outside = case / "outside"
                outside.mkdir(mode=0o755)
                sentinel = outside / "sentinel"
                sentinel.write_text("unchanged")
                os.chmod(sentinel, 0o644)
                vault = case / "vault"
                if link_at_root:
                    vault.symlink_to(outside, target_is_directory=True)
                else:
                    vault.mkdir(mode=0o755)
                    (vault / "trajectory").symlink_to(outside, target_is_directory=True)

                with self.assertRaisesRegex(RuntimeError, "private state"):
                    secure_io.secure_existing_tree(vault / "trajectory", vault)
                self.assertEqual("unchanged", sentinel.read_text())
                self.assertEqual(0o755, mode(outside))
                self.assertEqual(0o644, mode(sentinel))


if __name__ == "__main__":
    unittest.main()
