"""The standalone install's node-free Claude credential helper."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact import credential_helper as HELPER  # noqa: E402


class CredentialHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.credentials = self.root / "auth.json"
        self.settings = self.root / "settings.json"
        self.helper = str(self.root / "bin" / "engelbart-key")
        self.base_url = "https://proxy.example.com"
        self.credentials.write_text(json.dumps({
            "apiBase": "https://account.example.com",
            "token": "device-token",
            "claude": {"baseUrl": self.base_url},
        }))
        self.settings.write_text(json.dumps({
            "apiKeyHelper": self.helper,
            "theme": "dark",
            "env": {
                "ANTHROPIC_BASE_URL": self.base_url,
                "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": HELPER.HELPER_TTL_MS,
                "EDITOR": "vim",
            },
        }))

    @patch.object(HELPER, "_request")
    def test_a_healthy_account_prints_only_the_fresh_key(self, request):
        request.return_value = (200, {"apiKey": "sk-fresh", "status": "active"}, "")
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = HELPER.fetch_key(self.credentials, self.settings,
                                    self.helper, self.base_url)

        self.assertEqual(0, code)
        self.assertEqual("sk-fresh", output.getvalue())
        self.assertEqual("", errors.getvalue())
        self.assertEqual(self.helper,
                         json.loads(self.settings.read_text())["apiKeyHelper"])

    @patch.object(HELPER, "_request")
    def test_spent_credit_unwires_only_engelbart_settings(self, request):
        request.return_value = (402, {"error": "credit used up"}, "")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = HELPER.fetch_key(self.credentials, self.settings,
                                    self.helper, self.base_url)

        self.assertEqual(1, code)
        after = json.loads(self.settings.read_text())
        self.assertNotIn("apiKeyHelper", after)
        self.assertEqual({"EDITOR": "vim"}, after["env"])
        self.assertEqual("dark", after["theme"])

    def test_disconnect_works_after_the_credentials_file_is_gone(self):
        self.credentials.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            code = HELPER.disconnect(self.settings, self.helper, self.base_url)

        self.assertEqual(0, code)
        after = json.loads(self.settings.read_text())
        self.assertNotIn("apiKeyHelper", after)
        self.assertEqual({"EDITOR": "vim"}, after["env"])

    @patch.object(HELPER, "_request")
    def test_an_outage_leaves_the_machine_wired(self, request):
        request.return_value = (0, None, "connection refused")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = HELPER.fetch_key(self.credentials, self.settings,
                                    self.helper, self.base_url)

        self.assertEqual(1, code)
        self.assertEqual(self.helper,
                         json.loads(self.settings.read_text())["apiKeyHelper"])


if __name__ == "__main__":
    unittest.main()
