import contextlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
PLUGIN_HOOKS = HC_SRC / "human_compact" / "assets" / "plugin" / "hooks"
# The only events the global layer is allowed to add itself to.
VAULT_HOOK_EVENTS = {"SessionStart", "PreCompact", "PostCompact", "SessionEnd"}


CHAT_HOOK = '"${CLAUDE_PLUGIN_ROOT}/scripts/chat-hook.sh"'


def script_of(entry):
    """The hook script a command runs, with the quoting and any args off."""
    command = entry["command"].split(" -", 1)[0].strip()
    return command.strip('"').rsplit(" ", 1)[-1].strip('"')


def chat_only(hooks):
    """The same hook map with every vault-hook.sh entry removed."""
    kept = {}
    for event, groups in hooks.items():
        remaining = []
        for group in groups:
            entries = [entry for entry in group["hooks"]
                       if not script_of(entry).endswith("vault-hook.sh")]
            if entries:
                remaining.append({**group, "hooks": entries})
        if remaining:
            kept[event] = remaining
    return kept


def vault_hook_events(hooks):
    return {event for event, groups in hooks.items() for group in groups
            for entry in group["hooks"]
            if script_of(entry).endswith("vault-hook.sh")}


class HcOnboardingTests(unittest.TestCase):
    def _cli(self, home):
        if str(HC_SRC) not in sys.path:
            sys.path.insert(0, str(HC_SRC))
        with mock.patch.dict(os.environ, {"HC_HOME": str(home)}):
            import human_compact.cli as cli
            return importlib.reload(cli)

    def test_install_adds_bare_goals_ui_without_enabling_global_vault(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / ".claude").mkdir()
            cli = self._cli(home)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.install_main([])

            skill = home / ".claude" / "skills" / "goals-ui" / "SKILL.md"
            hooks = home / ".claude" / "skills" / "vault" / "hooks" / "hooks.json"
            self.assertTrue(skill.is_file())
            body = skill.read_text()
            self.assertNotIn('!`', body)
            self.assertIn('disable-model-invocation: true', body)
            # When the hook runs, /goals-ui is silent and this body never
            # reaches Claude. The one case where it does reach Claude is the
            # one that used to be silent for the wrong reason: a session that
            # loaded its hooks before the plugin was installed. So the body
            # is an instruction for exactly that case, and it says what to do.
            self.assertIn("the `/goals-ui` hook did not run", body)
            self.assertIn("restart\nClaude Code (or run `/reload-plugins`)", body)
            self.assertIn("Do nothing\nelse", body)
            self.assertIn("session start", body)
            self.assertIn("`/goals-ui disable` turns that off", body)
            self.assertNotIn('${CLAUDE_SESSION_ID}', body)
            self.assertTrue(hooks.is_file())
            self.assertFalse((home / ".claude-vault" / "bin" / "claude").exists())
            self.assertFalse((home / ".zshrc").exists())

    def test_one_name_binds_the_matcher_the_installed_skill_and_frontmatter(self):
        # `/goals-ui` only reaches the hook when all three agree. Renaming any
        # one of them alone degrades the command into an ordinary prompt
        # instead of failing loudly, so bind them here.
        hooks = json.loads((PLUGIN_HOOKS / "hooks.json").read_text())["hooks"]
        matchers = {group.get("matcher")
                    for group in hooks["UserPromptExpansion"]}
        source = (HC_SRC / "human_compact" / "assets" / "goals-ui-skill" /
                  "SKILL.md").read_text()
        frontmatter = re.search(r"^name:[ \t]*(\S+)[ \t]*$", source, re.M)
        if str(HC_SRC) not in sys.path:
            sys.path.insert(0, str(HC_SRC))
        import human_compact.cli as cli

        self.assertEqual({"goals-ui"}, matchers)
        self.assertIsNotNone(frontmatter)
        self.assertEqual("goals-ui", frontmatter.group(1))
        self.assertEqual("goals-ui", cli.GOALS_UI_SKILL_DIR.name)

    def test_chat_hooks_are_always_on_and_global_hook_remains_opt_in(self):
        default = (PLUGIN_HOOKS / "hooks.json").read_text()
        experimental = (PLUGIN_HOOKS / "hooks.experimental.json").read_text()
        hooks = json.loads(default)["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "PostToolBatch", "Stop"):
            commands = [h["command"] for group in hooks[event]
                        for h in group["hooks"]]
            self.assertTrue(any("chat-hook.sh" in c for c in commands), event)
        # The global layer is a separate, experimental hook set: shipped, but
        # never wired up by a default install.
        self.assertNotIn("vault-hook.sh", default)
        self.assertIn("vault-hook.sh", experimental)
        self.assertIn("chat-hook.sh", experimental)
        vault_script = (HC_SRC / "human_compact" / "assets" / "plugin" /
                        "scripts" / "vault-hook.sh").read_text()
        chat_script = (HC_SRC / "human_compact" / "assets" / "plugin" /
                       "scripts" / "chat-hook.sh").read_text()
        self.assertIn('CLAUDE_VAULT:-', vault_script)
        self.assertNotIn('CLAUDE_VAULT:-', chat_script)

    def test_the_experimental_hooks_are_the_default_set_plus_vault_entries(self):
        # One file is the other plus vault-hook.sh. Nothing else may drift.
        default = json.loads((PLUGIN_HOOKS / "hooks.json").read_text())
        experimental = json.loads(
            (PLUGIN_HOOKS / "hooks.experimental.json").read_text())

        self.assertEqual(default["hooks"], chat_only(experimental["hooks"]))
        self.assertEqual(VAULT_HOOK_EVENTS,
                         vault_hook_events(experimental["hooks"]))
        self.assertEqual(set(), vault_hook_events(default["hooks"]))
        # The global layer backgrounds `hc worker`, which the release gate
        # refuses unless the flag is set; the vault entries carry it, and
        # nothing else in either file does.
        for event, groups in experimental["hooks"].items():
            for entry in (e for group in groups for e in group["hooks"]):
                with self.subTest(event=event, command=entry["command"]):
                    self.assertEqual(
                        script_of(entry).endswith("vault-hook.sh"),
                        entry["command"].startswith("HC_EXPERIMENTAL=1 "))
        self.assertNotIn("HC_EXPERIMENTAL", json.dumps(default))

    def test_goal_context_reaches_subagents_and_tool_batches(self):
        # A subagent starts with an empty context and a tool batch can create
        # tasks, so both need the goals the main conversation already has.
        for name in ("hooks.json", "hooks.experimental.json"):
            hooks = json.loads((PLUGIN_HOOKS / name).read_text())["hooks"]
            with self.subTest(name=name):
                subagent = [entry for group in hooks["SubagentStart"]
                            for entry in group["hooks"]]
                self.assertEqual(1, len(subagent))
                # A subagent injection reads cached state and speaks; it needs
                # neither the ingest nor the agent-run observation.
                self.assertEqual(f"{CHAT_HOOK} --inject-only",
                                 subagent[0]["command"])
                self.assertFalse(subagent[0].get("async"))
                self.assertEqual(5, subagent[0]["timeout"])

                batch = [entry for group in hooks["PostToolBatch"]
                         for entry in group["hooks"]]
                # One entry ingests off the critical path; the other speaks on
                # it. Collapsing them would either block Claude or say nothing.
                self.assertEqual(
                    [(True, CHAT_HOOK),
                     (False, f"{CHAT_HOOK} --inject-only")],
                    [(bool(entry.get("async")), entry["command"])
                     for entry in batch])
                self.assertEqual(5, batch[1]["timeout"])

    def test_a_finished_subagent_reaches_the_workspace(self):
        # The workspace's one job the terminal cannot do is say "it is done".
        # Stop covers the conversation; without SubagentStop, a dispatched
        # agent returning is invisible to it.
        for name in ("hooks.json", "hooks.experimental.json"):
            hooks = json.loads((PLUGIN_HOOKS / name).read_text())["hooks"]
            with self.subTest(name=name):
                entries = [entry for group in hooks["SubagentStop"]
                           for entry in group["hooks"]]
                self.assertEqual(1, len(entries))
                self.assertEqual(CHAT_HOOK, entries[0]["command"])
                # Same shape as Stop: nothing is injected here, so it has no
                # business sitting on the model's critical path.
                self.assertTrue(entries[0]["async"])
                self.assertEqual(30, entries[0]["timeout"])
                self.assertEqual(
                    [entry for group in hooks["Stop"]
                     for entry in group["hooks"]],
                    entries)

    def test_hook_commands_quote_the_plugin_path(self):
        # ${CLAUDE_PLUGIN_ROOT} is substituted into a shell command line. An
        # unquoted install path containing a space would reach chat-hook.sh as
        # two arguments and run nothing.
        for name in ("hooks.json", "hooks.experimental.json"):
            hooks = json.loads((PLUGIN_HOOKS / name).read_text())["hooks"]
            for event, groups in hooks.items():
                for entry in (e for group in groups for e in group["hooks"]):
                    if not script_of(entry).endswith("chat-hook.sh"):
                        continue
                    with self.subTest(name=name, event=event):
                        self.assertTrue(entry["command"].startswith(CHAT_HOOK),
                                        entry["command"])

    def test_the_chat_hook_forwards_its_own_arguments(self):
        script = (HC_SRC / "human_compact" / "assets" / "plugin" / "scripts" /
                  "chat-hook.sh").read_text()
        self.assertIn('"$HC_CMD" chat-hook "$@"', script)

    def test_ui_expansion_reports_missing_cli_instead_of_claiming_success(self):
        script = (
            HC_SRC / "human_compact" / "assets" / "plugin" / "scripts" /
            "chat-hook.sh"
        )
        payload = json.dumps({
            "session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "hook_event_name": "UserPromptExpansion",
        })
        # The hook resolves hc from HC_EXECUTABLE, then ~/.human-compact/bin,
        # then PATH. A developer with a real install would otherwise satisfy the
        # second of those and open an actual server from a unit test.
        with tempfile.TemporaryDirectory() as empty_home:
            environment = {**os.environ, "PATH": "/usr/bin:/bin",
                           "HOME": empty_home}
            environment.pop("HC_EXECUTABLE", None)
            result = subprocess.run(
                ["/bin/bash", str(script)],
                input=payload,
                text=True,
                capture_output=True,
                env=environment,
                check=True,
            )
        response = json.loads(result.stdout)
        self.assertEqual("block", response["decision"])
        self.assertIn("npx human-vault", response["reason"])


class HcCommandGateTests(unittest.TestCase):
    """Only the launch surface is reachable without HC_EXPERIMENTAL=1."""

    LAUNCH_COMMANDS = {"install", "setup", "chat-ui", "chat-serve", "chat-hook",
                       "chat-refresh", "global-hook"}

    def _cli(self):
        if str(HC_SRC) not in sys.path:
            sys.path.insert(0, str(HC_SRC))
        import human_compact.cli as cli
        return cli

    def _run(self, argv, experimental=""):
        """Run hc_main and return (exit code, stdout, stderr)."""
        cli = self._cli()
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.dict(os.environ, {"HC_EXPERIMENTAL": experimental}), \
                mock.patch.object(sys, "argv", ["hc"] + argv), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                cli.hc_main()
            except SystemExit as exit_status:
                code = exit_status.code
        return code, out.getvalue(), err.getvalue()

    def _listed(self, help_text):
        return {line.split()[0] for line in help_text.splitlines()
                if line.startswith("  ") and line.strip()}

    def test_help_lists_the_launch_surface_and_points_at_the_flag(self):
        code, out, err = self._run([])
        self.assertEqual(0, code)
        self.assertEqual("", err)
        self.assertEqual(self.LAUNCH_COMMANDS, self._listed(out))
        self.assertIn(
            "Experimental commands are available with HC_EXPERIMENTAL=1.", out)

    def test_help_documents_experimental_commands_only_when_enabled(self):
        cli = self._cli()
        _, out, _ = self._run(["--help"], experimental="1")
        self.assertEqual(self.LAUNCH_COMMANDS | set(cli.EXPERIMENTAL_COMMANDS),
                         self._listed(out))

    def test_experimental_commands_refuse_to_run_without_the_flag(self):
        cli = self._cli()
        with contextlib.ExitStack() as stack:
            # Every gated implementation is stubbed, so a regression that lets
            # one through fails here instead of touching the real Vault.
            entrypoints = {
                command: stack.enter_context(
                    mock.patch.object(cli, f"{command}_main"))
                for command in cli.EXPERIMENTAL_COMMANDS
            }
            for command in cli.EXPERIMENTAL_COMMANDS:
                with self.subTest(command=command):
                    code, out, err = self._run([command])
                    self.assertEqual(2, code)
                    self.assertEqual("", out)
                    self.assertEqual(
                        f"hc {command} is experimental in this release; "
                        "set HC_EXPERIMENTAL=1 to enable it\n", err)
            for command, entrypoint in entrypoints.items():
                self.assertFalse(entrypoint.called, command)

    def test_an_enabled_experimental_command_reaches_its_implementation(self):
        cli = self._cli()
        with mock.patch.object(cli, "ui_main") as ui:
            code, _, err = self._run(["ui", "--port", "9999"], experimental="1")
        ui.assert_called_once_with(["--port", "9999"])
        self.assertEqual(0, code)
        self.assertEqual("", err)

    def test_launch_commands_and_unknown_commands_are_unchanged(self):
        cli = self._cli()
        with mock.patch.object(cli, "install_main") as install:
            code, _, _ = self._run(["install"])
        install.assert_called_once_with([])
        self.assertEqual(0, code)

        code, out, _ = self._run(["surprise"])
        self.assertEqual(2, code)
        self.assertIn("unknown command: surprise", out)


if __name__ == "__main__":
    unittest.main()
