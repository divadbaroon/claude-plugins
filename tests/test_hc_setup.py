import contextlib
import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"


class HcSetupTests(unittest.TestCase):
    def _modules(self, home):
        if str(HC_SRC) not in sys.path:
            sys.path.insert(0, str(HC_SRC))
        env = {
            "HC_HOME": str(home),
            "CLAUDE_PROJECTS_DIR": str(home / ".claude" / "projects"),
            "CLAUDE_VAULT_DIR": str(home / ".claude-vault"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        import human_compact.cli as cli
        import human_compact.global_vault as global_vault
        return importlib.reload(cli), importlib.reload(global_vault)

    def _transcript(self, home, sid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"):
        project = home / ".claude" / "projects" / "project"
        project.mkdir(parents=True)
        source = project / f"{sid}.jsonl"
        source.write_text(json.dumps({
            "timestamp": "2026-08-12T08:30:00Z",
            "cwd": "/tmp/project",
            "type": "user",
            "message": {"content": "ship it"},
        }) + "\n")
        return source

    def test_setup_without_global_overrides_legacy_environment_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cli, gv = self._modules(home)
            gv.enable_always_on()
            with mock.patch.object(cli, "install_main") as install, \
                    mock.patch.object(cli, "_validate_claude_cli"), \
                    mock.patch.object(gv, "backfill") as backfill:
                cli.setup_main(["--global-vault", "no", "--goals", "no"])
            install.assert_called_once_with([])
            backfill.assert_not_called()
            self.assertEqual("disabled", gv.enable_file().read_text().strip())
            with mock.patch.dict(os.environ, {"CLAUDE_VAULT": "1"}):
                self.assertFalse(gv.is_enabled())

    def test_legacy_environment_remains_fallback_before_explicit_setup(self):
        with tempfile.TemporaryDirectory() as td:
            _, gv = self._modules(Path(td))
            self.assertFalse(gv.enable_file().exists())
            with mock.patch.dict(os.environ, {"CLAUDE_VAULT": "1"}):
                self.assertTrue(gv.is_enabled())

    def test_setup_rejects_goals_without_global_vault(self):
        with tempfile.TemporaryDirectory() as td:
            cli, _ = self._modules(Path(td))
            with self.assertRaises(SystemExit) as raised, \
                    mock.patch.object(cli, "install_main") as install:
                cli.setup_main(["--global-vault", "no", "--goals", "yes"])
            self.assertEqual(2, raised.exception.code)
            install.assert_not_called()

    def test_setup_goals_uses_explicit_claude_providers_and_noninteractive_flags(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cli, gv = self._modules(home)
            goals_file = home / ".claude-vault" / "trajectory" / "goals.json"

            def write_goals(_args):
                goals_file.parent.mkdir(parents=True)
                goals_file.write_text('{"version":1,"goals":[]}')

            with mock.patch.object(cli, "install_main"), \
                    mock.patch.object(cli, "_validate_claude_cli"), \
                    mock.patch.object(gv, "backfill",
                                      return_value={"imported": 2, "skipped": 1}), \
                    mock.patch.object(cli, "trajectory_main") as trajectory, \
                    mock.patch.object(cli, "goals_main", side_effect=write_goals) as goals:
                cli.setup_main(["--global-vault", "yes", "--goals", "yes"])

            trajectory.assert_called_once_with([
                "--provider", "claude", "--synth-provider", "claude",
                "--refresh", "--no-interact", "--strict",
            ])
            goals.assert_called_once_with(["--rebuild", "--no-interact"])
            self.assertEqual("enabled", gv.enable_file().read_text().strip())
            with mock.patch.dict(os.environ, {"CLAUDE_VAULT": "0"}):
                self.assertTrue(gv.is_enabled())

    def test_missing_claude_fails_after_base_integration_is_installed(self):
        with tempfile.TemporaryDirectory() as td:
            cli, _ = self._modules(Path(td))
            with mock.patch.object(cli, "install_main") as install, \
                    mock.patch("human_compact.cli.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "Claude Code is required"):
                    cli.setup_main(["--global-vault", "no", "--goals", "no"])
            install.assert_called_once_with([])

    def test_python_backfill_is_idempotent_private_and_needs_no_jq(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, gv = self._modules(home)
            source = self._transcript(home)
            first = gv.backfill()
            second = gv.backfill()
            self.assertEqual({"imported": 1, "skipped": 0}, first)
            self.assertEqual({"imported": 0, "skipped": 1}, second)
            base = (home / ".claude-vault" / "sessions" / "2026-08-12" /
                    source.stem)
            self.assertEqual(source.read_text(), (base / "conversation.jsonl").read_text())
            self.assertEqual("backfill", json.loads(
                (base / "metadata.json").read_text())["start_source"])
            self.assertEqual(0o700, stat.S_IMODE(base.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(
                (base / "conversation.jsonl").stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(
                (base / "metadata.json").stat().st_mode))

    def test_failed_backfill_never_makes_partial_session_look_complete(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, gv = self._modules(home)
            source = self._transcript(home)
            with mock.patch.object(gv, "_atomic_json",
                                   side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    gv.backfill()
            expected = (home / ".claude-vault" / "sessions" /
                        "2026-08-12" / source.stem)
            self.assertFalse(expected.exists())
            self.assertIsNone(gv._existing_session(home / ".claude-vault",
                                                   source.stem))

    def test_backfill_and_live_hook_reject_symlinked_transcripts(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, gv = self._modules(home)
            project = home / ".claude" / "projects" / "project"
            project.mkdir(parents=True)
            outside = home / "secret.jsonl"
            outside.write_text('{"timestamp":"2026-08-12T08:30:00Z"}\n')
            sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
            link = project / f"{sid}.jsonl"
            link.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlinked or out-of-scope"):
                gv.backfill()

            gv.enable_always_on()
            gv.handle_hook({
                "hook_event_name": "SessionStart", "session_id": sid,
                "transcript_path": str(link),
            }, output=io.StringIO())
            sessions = list((home / ".claude-vault" / "sessions").glob(f"*/{sid}"))
            self.assertEqual(1, len(sessions))
            self.assertFalse((sessions[0] / "conversation.jsonl").exists())

    def test_hook_rejects_path_session_and_sanitizes_compaction_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            _, gv = self._modules(home)
            source = self._transcript(home)
            gv.enable_always_on()
            gv.handle_hook({
                "hook_event_name": "SessionStart", "session_id": "../escape",
                "transcript_path": str(source),
            }, output=io.StringIO())
            self.assertFalse((home / ".claude-vault" / "escape").exists())

            gv.handle_hook({
                "hook_event_name": "PreCompact", "session_id": source.stem,
                "transcript_path": str(source), "trigger": "../../escape/name",
            }, output=io.StringIO())
            snapshots = list((home / ".claude-vault" / "sessions").glob(
                f"*/{source.stem}/snapshots/*.jsonl"))
            self.assertEqual(1, len(snapshots))
            self.assertNotIn("..", snapshots[0].name)
            self.assertNotIn("/", snapshots[0].name)


if __name__ == "__main__":
    unittest.main()
