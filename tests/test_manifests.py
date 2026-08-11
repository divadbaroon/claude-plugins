import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def test_host_manifests_and_marketplaces_are_valid_json(self):
        paths = (
            ROOT / ".claude-plugin" / "marketplace.json",
            ROOT / ".agents" / "plugins" / "marketplace.json",
            ROOT / "compact-focus" / ".claude-plugin" / "plugin.json",
            ROOT / "compact-focus" / ".codex-plugin" / "plugin.json",
            ROOT / "compact-focus" / "hooks" / "hooks.json",
        )
        for path in paths:
            with self.subTest(path=path):
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(value, dict)

    def test_codex_marketplace_points_to_cache_busted_plugin(self):
        marketplace = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        plugin = json.loads(
            (ROOT / "compact-focus" / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        entry = marketplace["plugins"][0]
        self.assertEqual("compact-focus", entry["name"])
        self.assertEqual("./compact-focus", entry["source"]["path"])
        self.assertRegex(plugin["version"], r"^0\.20\.6\+codex\.\d{14}$")


if __name__ == "__main__":
    unittest.main()
