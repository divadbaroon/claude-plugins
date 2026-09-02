"""A project approved on the web arrives with who it is for."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact import cli  # noqa: E402
from human_compact.trajectory import reader as READER  # noqa: E402


class SetupImportReaderTests(unittest.TestCase):
    def test_the_payloads_reader_is_remembered_before_the_workspace_opens(self):
        payload = {"name": "zebra-tuner", "plan": {"description": "d"}, "goals": [{"label": "G", "why": ""}],
                   "chosen": "G", "todos": ["a", "b"],
                   "reader": {"name": "Maya", "year": "Second year", "major": "CogSci", "level": "expert",
                              "knowledge": [{"area": "Transformers", "level": 25}]}}
        remembered = {}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(cli, "chat_ui_main", side_effect=lambda argv: print("http://127.0.0.1:1/")), \
             mock.patch("human_compact.trajectory.setup_chat.commit",
                        return_value={"ok": True, "tree_session": "s", "cwd": tmp, "name": "zebra-tuner"}), \
             mock.patch.object(READER, "remember", side_effect=lambda value, root=None: remembered.update(value) or {"ok": True}):
            with mock.patch("sys.stdin", new=__import__("io").StringIO(json.dumps(payload))):
                cli.setup_import_main(["--stdin", "--no-open"])
        self.assertEqual("Maya", remembered["name"])
        self.assertEqual("expert", remembered["level"])
        self.assertEqual(25, remembered["knowledge"][0]["level"])


if __name__ == "__main__":
    unittest.main()
