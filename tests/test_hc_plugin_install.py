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
# The exact v0.15.0 /hc-ui skill. Unmarked installs of it are recognized by
# digest, so the bytes are pinned here rather than read from the renamed asset.
LEGACY_HC_UI_SKILL_MD = (
    "---\n"
    "name: hc-ui\n"
    "description: Open the goal workspace for this Claude Code conversation.\n"
    "disable-model-invocation: true\n"
    "---\n"
    "\n"
    "The hc-ui expansion hook has opened the local goal workspace for Claude\n"
    "session `${CLAUDE_SESSION_ID}` and supplied its exact URL as hook context.\n"
    "\n"
    "Report that URL and say the workspace belongs to this chat, then stop. Do not\n"
    "run a second server or modify the goal state yourself.\n"
)


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
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.install_plugin()
        return out.getvalue()

    def _write_legacy_hc_ui(self, cli, skill_md, marker_asset=None):
        legacy = cli.LEGACY_HC_UI_SKILL_DIR
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text(skill_md)
        if marker_asset is not None:
            (legacy / cli.MANAGED_MARKER).write_text(json.dumps({
                "owner": "human-compact", "asset": marker_asset, "format": 1,
            }, sort_keys=True) + "\n")
        return legacy

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
            self.assertFalse(cli.GOALS_UI_SKILL_DIR.exists())
            self.assertEqual([], self._leftovers(cli))

    def test_exact_unmarked_legacy_assets_are_migrated_to_owned_installs(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cli = self._cli(home)
            cli.SKILLS_DIR.parent.mkdir(parents=True)
            shutil.copytree(cli.asset_root() / "plugin", cli.SKILLS_DIR)
            shutil.copytree(cli.asset_root() / "goals-ui-skill",
                            cli.GOALS_UI_SKILL_DIR)

            self._install(cli)

            for destination, asset in ((cli.SKILLS_DIR, "vault"),
                                       (cli.GOALS_UI_SKILL_DIR, "goals-ui")):
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
            stale_dir = cli.GOALS_UI_SKILL_DIR / "removed-assets"
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
            (cli.GOALS_UI_SKILL_DIR / "owned-state.txt").write_text("keep too")
            before_vault = self._snapshot(cli.SKILLS_DIR)
            before_skill = self._snapshot(cli.GOALS_UI_SKILL_DIR)
            real_replace = os.replace

            def fail_skill_promotion(source, destination):
                if ".goals-ui.hc-stage-" in Path(source).name:
                    raise OSError("simulated rename failure")
                return real_replace(source, destination)

            with mock.patch("human_compact.cli.os.replace",
                            side_effect=fail_skill_promotion):
                with self.assertRaisesRegex(RuntimeError,
                                            "previous install restored"):
                    self._install(cli)

            self.assertEqual(before_vault, self._snapshot(cli.SKILLS_DIR))
            self.assertEqual(before_skill, self._snapshot(cli.GOALS_UI_SKILL_DIR))
            self.assertEqual([], self._leftovers(cli))

    def test_install_removes_owned_legacy_hc_ui_skill(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            legacy = self._write_legacy_hc_ui(
                cli, LEGACY_HC_UI_SKILL_MD, marker_asset="hc-ui")

            self._install(cli)

            self.assertFalse(legacy.exists())
            self.assertIn(
                "name: goals-ui",
                (cli.GOALS_UI_SKILL_DIR / "SKILL.md").read_text())
            self.assertEqual([], self._leftovers(cli))

    def test_install_removes_exact_unmarked_legacy_hc_ui_skill(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            legacy = self._write_legacy_hc_ui(cli, LEGACY_HC_UI_SKILL_MD)

            self._install(cli)

            self.assertFalse(legacy.exists())
            self.assertTrue((cli.GOALS_UI_SKILL_DIR / "SKILL.md").is_file())
            self.assertEqual([], self._leftovers(cli))

    def test_install_leaves_unmanaged_legacy_hc_ui_dir_alone(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            legacy = self._write_legacy_hc_ui(
                cli, "---\nname: something-else\n---\n")
            before = self._snapshot(legacy)

            output = self._install(cli)

            self.assertTrue(legacy.is_dir())
            self.assertEqual(before, self._snapshot(legacy))
            self.assertIn(f"left unmanaged {legacy} in place", output)
            self.assertTrue((cli.GOALS_UI_SKILL_DIR / "SKILL.md").is_file())

    # Every event the chat layer must stay registered on for /goals-ui to work.
    CHAT_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolBatch",
                        "Stop", "TaskCreated", "TaskCompleted", "PostCompact",
                        "SessionEnd")

    def test_installed_default_hooks_register_the_whole_goals_ui_surface(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": ""}):
                self._install(cli)

            installed = json.loads(
                (cli.SKILLS_DIR / "hooks" / "hooks.json").read_text())
            source = json.loads(
                (cli.asset_root() / "plugin" / "hooks" / "hooks.json").read_text())
            # Flags the launch depends on (async, timeout, matcher) are the
            # checked-in ones, not whatever survived the swap.
            self.assertEqual(source, installed)

            expansion = installed["hooks"]["UserPromptExpansion"]
            self.assertEqual(1, len(expansion))
            self.assertEqual("goals-ui", expansion[0]["matcher"])
            self.assertEqual(1, len(expansion[0]["hooks"]))
            self.assertIn("chat-hook.sh", expansion[0]["hooks"][0]["command"])
            self.assertEqual(45, expansion[0]["hooks"][0]["timeout"])

            for event in self.CHAT_HOOK_EVENTS:
                commands = [entry["command"]
                            for group in installed["hooks"][event]
                            for entry in group["hooks"]]
                self.assertTrue(
                    any("chat-hook.sh" in c for c in commands), event)
            every_command = [entry["command"]
                             for groups in installed["hooks"].values()
                             for group in groups for entry in group["hooks"]]
            self.assertEqual([], [c for c in every_command
                                  if "vault-hook.sh" in c])

    def test_install_says_which_hook_set_it_wired(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": ""}):
                default = self._install(cli)
            with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": "1"}):
                experimental = self._install(cli)

        self.assertIn("hooks: chat-scoped only (set HC_EXPERIMENTAL=1 at "
                      "install to wire global Vault hooks)", default)
        self.assertNotIn("global Vault (HC_EXPERIMENTAL=1)", default)
        self.assertIn("hooks: chat-scoped + global Vault (HC_EXPERIMENTAL=1)",
                      experimental)
        self.assertNotIn("chat-scoped only", experimental)

    def test_default_install_leaves_the_global_vault_hooks_unwired(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": ""}):
                self._install(cli)

            hooks = (cli.SKILLS_DIR / "hooks" / "hooks.json").read_text()
            self.assertNotIn("vault-hook.sh", hooks)
            self.assertIn("chat-hook.sh", hooks)
            # The implementation still ships; only its wiring is withheld.
            self.assertIn("vault-hook.sh", (cli.SKILLS_DIR / "hooks" /
                                            "hooks.experimental.json").read_text())
            for script in ("vault-hook.sh", "vault-backfill.sh"):
                self.assertTrue((cli.SKILLS_DIR / "scripts" / script).is_file())
            self.assertEqual([], self._leftovers(cli))

    def test_experimental_install_wires_the_global_vault_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": "1"}):
                self._install(cli)

            hooks = cli.SKILLS_DIR / "hooks" / "hooks.json"
            experimental = cli.SKILLS_DIR / "hooks" / "hooks.experimental.json"
            self.assertIn("vault-hook.sh", hooks.read_text())
            self.assertEqual(experimental.read_text(), hooks.read_text())
            # The swapped tree must still be one this installer owns, with the
            # same modes the packaged files get.
            self.assertTrue(cli._owned_asset(cli.SKILLS_DIR, "vault"))
            self.assertEqual(0o600, stat.S_IMODE(hooks.stat().st_mode))
            self.assertEqual([], self._leftovers(cli))

    def test_reinstall_without_the_flag_unwires_the_global_vault_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": "1"}):
                self._install(cli)
            with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": ""}):
                self._install(cli)

            self.assertNotIn("vault-hook.sh",
                             (cli.SKILLS_DIR / "hooks" / "hooks.json").read_text())
            self.assertEqual([], self._leftovers(cli))

    def test_installed_assets_are_owner_only_with_scripts_executable(self):
        with tempfile.TemporaryDirectory() as td:
            cli = self._cli(Path(td))
            self._install(cli)

            self.assertEqual(0o700, stat.S_IMODE(cli.SKILLS_DIR.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(
                cli.GOALS_UI_SKILL_DIR.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(
                (cli.SKILLS_DIR / "scripts" / "chat-hook.sh").stat().st_mode))
            for path in (
                cli.SKILLS_DIR / cli.MANAGED_MARKER,
                cli.SKILLS_DIR / "README.md",
                cli.GOALS_UI_SKILL_DIR / cli.MANAGED_MARKER,
                cli.GOALS_UI_SKILL_DIR / "SKILL.md",
            ):
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode), path)


class InstallBannerTests(unittest.TestCase):
    """The first line of an install must not name a product that does not exist.

    The npm package is ``human-vault`` and the runtime is ``hc``. Both READMEs
    say so; the banner used to greet every install with ``human-compact``,
    which is only ever a path (``~/.human-compact/``) and a distribution name.
    """

    def _cli(self, home):
        if str(HC_SRC) not in sys.path:
            sys.path.insert(0, str(HC_SRC))
        patcher = mock.patch.dict(os.environ, {"HC_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        import human_compact.cli as cli
        return importlib.reload(cli)

    def test_the_install_greets_with_the_runtime_and_the_command(self):
        with tempfile.TemporaryDirectory() as home:
            cli = self._cli(Path(home))
            out = io.StringIO()
            with mock.patch.object(cli, "install_plugin"), \
                    contextlib.redirect_stdout(out):
                cli.install_main([])
            text = out.getvalue()
            first = next(line for line in text.splitlines() if line.strip())
            self.assertEqual("hc \u00b7 /goals-ui", first)
            self.assertNotIn("human-compact \u00b7", text)


if __name__ == "__main__":
    unittest.main()
