import contextlib
import importlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"


class HcPluginInstallTests(unittest.TestCase):
    def _cli(self, home):
        if str(HC_SRC) not in sys.path:
            sys.path.insert(0, str(HC_SRC))
        patcher = mock.patch.dict(os.environ, {"HC_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        import human_compact.cli as cli
        return importlib.reload(cli)

    def _install(self, cli):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.install_plugin()

    def _snapshot(self, root):
        return {
            path.relative_to(root).as_posix(): (
                "dir" if path.is_dir() else path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in sorted(root.rglob("*"))
        }

    def _leftovers(self, cli):
        return [p for p in cli.SKILLS_DIR.parent.iterdir()
                if ".hc-stage-" in p.name or ".hc-backup-" in p.name]

    def test_unmanaged_directory_is_refused_before_any_asset_is_changed(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cli = self._cli(home)
            cli.SKILLS_DIR.mkdir(parents=True)
            sentinel = cli.SKILLS_DIR / "mine.txt"
            sentinel.write_text("unrelated plugin")

            with self.assertRaisesRegex(RuntimeError, "unmanaged Claude skill directory"):
                self._install(cli)

            self.assertEqual("unrelated plugin", sentinel.read_text())
            self.assertFalse(cli.HC_UI_SKILL_DIR.exists())
            self.assertEqual([], self._leftovers(cli))

    def test_exact_unmarked_legacy_assets_are_migrated_to_owned_installs(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cli = self._cli(home)
            cli.SKILLS_DIR.parent.mkdir(parents=True)
            shutil.copytree(cli.asset_root() / "plugin", cli.SKILLS_DIR)
            shutil.copytree(cli.asset_root() / "hc-ui-skill",
                            cli.HC_UI_SKILL_DIR)

            self._install(cli)

            for destination, asset in ((cli.SKILLS_DIR, "vault"),
                                       (cli.HC_UI_SKILL_DIR, "hc-ui")):
                marker = json.loads(
                    (destination / cli.MANAGED_MARKER).read_text())
                self.assertEqual("human-compact", marker["owner"])
                self.assertEqual(asset, marker["asset"])
            self.assertEqual([], self._leftovers(cli))

    def test_managed_reinstall_removes_stale_files_instead_of_merging(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            self._install(cli)
            stale_file = cli.SKILLS_DIR / "scripts" / "removed-old-hook.sh"
            stale_file.write_text("stale")
            stale_dir = cli.HC_UI_SKILL_DIR / "removed-assets"
            stale_dir.mkdir()
            (stale_dir / "old.txt").write_text("stale")

            self._install(cli)

            self.assertFalse(stale_file.exists())
            self.assertFalse(stale_dir.exists())
            self.assertEqual([], self._leftovers(cli))

    def test_second_asset_promotion_failure_rolls_back_both_assets(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            self._install(cli)
            # Marker ownership permits managed state to contain stale files;
            # rollback must restore them byte-for-byte if the upgrade fails.
            (cli.SKILLS_DIR / "owned-state.txt").write_text("keep on rollback")
            (cli.HC_UI_SKILL_DIR / "owned-state.txt").write_text("keep too")
            before_vault = self._snapshot(cli.SKILLS_DIR)
            before_skill = self._snapshot(cli.HC_UI_SKILL_DIR)
            real_replace = os.replace

            def fail_skill_promotion(source, destination):
                if ".hc-ui.hc-stage-" in Path(source).name:
                    raise OSError("simulated rename failure")
                return real_replace(source, destination)

            with mock.patch("human_compact.cli.os.replace",
                            side_effect=fail_skill_promotion):
                with self.assertRaisesRegex(RuntimeError,
                                            "previous install restored"):
                    self._install(cli)

            self.assertEqual(before_vault, self._snapshot(cli.SKILLS_DIR))
            self.assertEqual(before_skill, self._snapshot(cli.HC_UI_SKILL_DIR))
            self.assertEqual([], self._leftovers(cli))

    def test_installed_assets_are_owner_only_with_scripts_executable(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            self._install(cli)

            self.assertEqual(0o700, stat.S_IMODE(cli.SKILLS_DIR.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(
                cli.HC_UI_SKILL_DIR.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(
                (cli.SKILLS_DIR / "scripts" / "chat-hook.sh").stat().st_mode))
            for path in (
                cli.SKILLS_DIR / cli.MANAGED_MARKER,
                cli.SKILLS_DIR / "README.md",
                cli.HC_UI_SKILL_DIR / cli.MANAGED_MARKER,
                cli.HC_UI_SKILL_DIR / "SKILL.md",
            ):
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode), path)


if __name__ == "__main__":
    unittest.main()
