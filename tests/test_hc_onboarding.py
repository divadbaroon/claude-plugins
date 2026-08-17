import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"


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
            self.assertIn('${CLAUDE_SESSION_ID}', skill.read_text())
            self.assertNotIn('!`', skill.read_text())
            self.assertTrue(hooks.is_file())
            self.assertFalse((home / ".claude-vault" / "bin" / "claude").exists())
            self.assertFalse((home / ".zshrc").exists())

    def test_chat_hooks_are_always_on_and_global_hook_remains_opt_in(self):
        hooks = json.loads((
            HC_SRC / "human_compact" / "assets" / "plugin" / "hooks" /
            "hooks.json").read_text())["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "PostToolBatch", "Stop"):
            commands = [h["command"] for group in hooks[event]
                        for h in group["hooks"]]
            self.assertTrue(any("chat-hook.sh" in c for c in commands), event)
        vault_script = (HC_SRC / "human_compact" / "assets" / "plugin" /
                        "scripts" / "vault-hook.sh").read_text()
        chat_script = (HC_SRC / "human_compact" / "assets" / "plugin" /
                       "scripts" / "chat-hook.sh").read_text()
        self.assertIn('CLAUDE_VAULT:-', vault_script)
        self.assertNotIn('CLAUDE_VAULT:-', chat_script)

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


if __name__ == "__main__":
    unittest.main()
