"""The virtual key stays private while Claude Code receives a stable helper."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact import engelbart_auth as EA  # noqa: E402
from human_compact.trajectory import supabase_client as SB  # noqa: E402


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault = self.base / "vault"
        self.settings = self.base / ".claude" / "settings.json"
        self.environment = mock.patch.dict(os.environ, {
            "CLAUDE_VAULT_DIR": str(self.vault),
        }, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ("HC_SUPABASE_URL", "HC_SUPABASE_ANON_KEY"):
            os.environ.pop(name, None)

    @staticmethod
    def credentials():
        return {
            "apiKey": "sk-member-virtual-key",
            "baseUrl": "https://proxy.example.com",
            "budgetUsd": 25.0,
            "spendUsd": 0.0,
            "models": ["claude-sonnet-4-6", "claude-haiku-4-5"],
            "rpmLimit": 30,
            "tpmLimit": None,
        }

    def test_an_existing_different_supabase_project_is_never_overwritten(self):
        SB.save_config("https://mine.supabase.co", "my-public-key")
        with self.assertRaises(EA.EngelbartAuthError) as caught:
            EA.ensure_mathetic_config({
                "url": "https://mathetic.supabase.co",
                "anon_key": "mathetic-public-key",
            })
        self.assertIn("another Supabase project", str(caught.exception))
        self.assertEqual("https://mine.supabase.co", SB.load_config()["url"])

    def test_an_unrecognized_vault_config_is_not_claimed(self):
        SB.config_path().parent.mkdir(parents=True)
        SB.config_path().write_text('{"mine": true}\n')
        with self.assertRaises(EA.EngelbartAuthError):
            EA.ensure_mathetic_config({
                "url": "https://mathetic.supabase.co",
                "anon_key": "mathetic-public-key",
            })
        self.assertEqual({"mine": True}, json.loads(SB.config_path().read_text()))

    def test_claude_settings_use_a_helper_and_restore_prior_values(self):
        self.settings.parent.mkdir(parents=True)
        original = {
            "apiKeyHelper": "/my/old-helper",
            "availableModels": ["mine"],
            "model": "mine",
            "env": {
                "ANTHROPIC_API_KEY": "sk-personal",
                "UNRELATED": "kept",
            },
        }
        self.settings.write_text(json.dumps(original))
        state = EA.configure_claude(
            self.credentials(),
            settings_file=self.settings,
            executable="/path with spaces/bart",
        )
        configured = json.loads(self.settings.read_text())
        self.assertEqual("'/path with spaces/bart' token", configured["apiKeyHelper"])
        self.assertNotIn("ANTHROPIC_API_KEY", configured["env"])
        self.assertNotIn("sk-member-virtual-key", self.settings.read_text())
        self.assertEqual("kept", configured["env"]["UNRELATED"])
        self.assertTrue(configured["enforceAvailableModels"])
        self.assertEqual("claude-sonnet-4-6", configured["model"])
        self.assertEqual(0, self.settings.stat().st_mode & 0o077)

        EA.restore_claude(state, settings_file=self.settings)
        self.assertEqual(original, json.loads(self.settings.read_text()))

    def test_authentication_writes_one_owner_only_virtual_key_record(self):
        with mock.patch.object(EA, "fetch_public_config", return_value={
                "url": "https://mathetic.supabase.co",
                "anon_key": "a-public-key-that-is-long-enough",
                "credits_enabled": True,
             }), mock.patch.object(EA, "_current_or_browser_session", return_value={
                "access_token": "member-jwt",
                "user_id": "member-uuid",
                "email": "member@example.com",
             }), mock.patch.object(EA, "fetch_credentials",
                                   return_value=self.credentials()):
            record = EA.authenticate(
                settings_file=self.settings,
                executable="/managed/bin/bart",
            )
        stored = json.loads(EA.credentials_path().read_text())
        self.assertEqual("sk-member-virtual-key", stored["apiKey"])
        self.assertEqual("member-uuid", stored["userId"])
        self.assertEqual(0, EA.credentials_path().stat().st_mode & 0o077)
        self.assertEqual(record["apiKey"], EA.token())
        configured = json.loads(self.settings.read_text())
        self.assertEqual("/managed/bin/bart token", configured["apiKeyHelper"])
        self.assertNotIn("sk-member-virtual-key", self.settings.read_text())

    def test_logout_removes_tokens_and_only_our_current_settings(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{}")
        state = EA.configure_claude(
            self.credentials(), settings_file=self.settings,
            executable="/managed/bin/bart")
        record = {**self.credentials(), **state}
        EA.credentials_path().parent.mkdir(parents=True, exist_ok=True)
        from human_compact.trajectory.secure_io import atomic_write_json
        atomic_write_json(EA.credentials_path(), record,
                          root=EA.credentials_path().parent)
        settings = json.loads(self.settings.read_text())
        settings["model"] = "user-changed-this"
        self.settings.write_text(json.dumps(settings))
        EA.logout(settings_file=self.settings)
        restored = json.loads(self.settings.read_text())
        self.assertEqual("user-changed-this", restored["model"])
        self.assertNotIn("apiKeyHelper", restored)
        self.assertFalse(EA.credentials_path().exists())


if __name__ == "__main__":
    unittest.main()
