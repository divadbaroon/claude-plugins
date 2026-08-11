import curses
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.tui import ReviewUI  # noqa: E402


class TuiSetupTests(unittest.TestCase):
    def make_ui(self):
        return ReviewUI(Mock(), {"episodes": []}, {}, {"items": []})

    def test_monochrome_terminal_skips_color_pairs(self):
        ui = self.make_ui()
        with (
            patch("compact_focus.tui.curses.has_colors", return_value=False),
            patch("compact_focus.tui.curses.init_pair") as init_pair,
        ):
            ui.setup()
        init_pair.assert_not_called()
        self.assertEqual({}, ui.colors)

    def test_invalid_color_pair_degrades_to_monochrome(self):
        ui = self.make_ui()
        with (
            patch("compact_focus.tui.curses.has_colors", return_value=True),
            patch("compact_focus.tui.curses.start_color"),
            patch("compact_focus.tui.curses.COLOR_PAIRS", 8, create=True),
            patch("compact_focus.tui.curses.use_default_colors"),
            patch("compact_focus.tui.curses.init_pair", side_effect=ValueError("no pairs")),
        ):
            ui.setup()
        self.assertEqual(0, ui.colors["preserve"])


if __name__ == "__main__":
    unittest.main()
