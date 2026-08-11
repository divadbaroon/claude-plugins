import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.terminal import find_terminal  # noqa: E402


class TerminalTests(unittest.TestCase):
    def test_finds_claude_and_ancestor_terminal(self):
        rows = {
            30: (20, "?", "/bin/sh /plugin/bin/compact-focus hook precompact"),
            20: (10, "ttys009", "/usr/local/bin/claude --resume abc"),
            10: (1, "ttys009", "-zsh"),
        }
        with (
            patch("compact_focus.terminal.os.isatty", return_value=False),
            patch("compact_focus.terminal.os.getppid", return_value=30),
            patch("compact_focus.terminal._process_row", side_effect=lambda pid: rows.get(pid)),
        ):
            target = find_terminal()
        self.assertEqual("/dev/ttys009", target.path)
        self.assertEqual(20, target.claude_pid)


if __name__ == "__main__":
    unittest.main()
