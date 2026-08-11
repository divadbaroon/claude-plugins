import curses
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.review import new_review  # noqa: E402
from compact_focus.tui import ReviewUI  # noqa: E402


class FakeScreen:
    def __init__(self, keys):
        self.keys = list(keys)

    def keypad(self, _enabled):
        return None

    def getmaxyx(self):
        return 32, 120

    def erase(self):
        return None

    def addnstr(self, *_args):
        return None

    def refresh(self):
        return None

    def getch(self):
        if not self.keys:
            raise AssertionError("review requested an unexpected key")
        return self.keys.pop(0)

    def move(self, *_args):
        return None


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

    def test_cluster_first_document_keeps_nested_units_and_explicit_submit(self):
        trace = {
            "context": {"window_tokens": 1000},
            "episodes": [
                {
                    "sources": [
                        {
                            "id": "s-old",
                            "kind": "tool_result",
                            "class": "other",
                            "text": "old output",
                            "tokens_estimate": 10,
                        },
                        {
                            "id": "s-live",
                            "kind": "user_prompt",
                            "class": "other",
                            "text": "active constraint",
                            "tokens_estimate": 10,
                        },
                    ]
                }
            ],
        }
        proposal = {
            "items": [
                {
                    "id": "old",
                    "title": "Older cluster",
                    "summary": "old",
                    "type": "mechanical",
                    "status": "resolved",
                    "retention": "demote",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": ["s-old"],
                    "rationale": "finished",
                    "next_step": "",
                    "rival_interpretations": [],
                },
                {
                    "id": "live",
                    "title": "Active cluster",
                    "summary": "live",
                    "type": "constraint",
                    "status": "active",
                    "retention": "preserve",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": ["s-live"],
                    "rationale": "ongoing",
                    "next_step": "test",
                    "rival_interpretations": [],
                },
            ],
            "class_rules": [],
        }
        review = new_review(proposal)
        ui = ReviewUI(Mock(), trace, proposal, review)
        targets, lines = ui.build_lines(120)
        rendered = "\n".join(value for _target, value, _style in lines)
        cluster_targets = [target for target in targets if target.kind == "cluster"]

        self.assertEqual([0, 1], [target.item for target in cluster_targets])
        self.assertLess(rendered.index("Older cluster"), rendered.index("Active cluster"))
        self.assertIn("TOOL RESULT DELETE", rendered)
        self.assertEqual("submit", targets[-1].kind)
        self.assertIn("SUBMIT REFINED CLUSTERS FOR COMPACTION", rendered)
        self.assertIn("Nothing reaches compaction until the submit row", rendered)

    def test_enter_on_cluster_only_expands_and_does_not_approve(self):
        proposal = {
            "items": [
                {
                    "id": "cluster",
                    "title": "Active cluster",
                    "summary": "Keep it.",
                    "type": "constraint",
                    "status": "active",
                    "retention": "preserve",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": [],
                    "rationale": "ongoing",
                    "next_step": "",
                    "rival_interpretations": [],
                    "origin": "human",
                }
            ],
            "class_rules": [],
        }
        review = new_review(proposal)
        screen = FakeScreen([10, ord("q")])
        with patch("compact_focus.tui.curses.has_colors", return_value=False):
            approved = ReviewUI(screen, {"episodes": []}, proposal, review).run()
        self.assertFalse(approved)
        self.assertEqual("", review["approved_summary"])

    def test_submit_opens_draft_and_second_enter_confirms(self):
        proposal = {
            "items": [
                {
                    "id": "cluster",
                    "title": "Active cluster",
                    "summary": "Keep it.",
                    "type": "constraint",
                    "status": "active",
                    "retention": "preserve",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": [],
                    "rationale": "ongoing",
                    "next_step": "",
                    "rival_interpretations": [],
                    "origin": "human",
                }
            ],
            "class_rules": [],
        }
        review = new_review(proposal)
        screen = FakeScreen([curses.KEY_DOWN, 10, 10])
        with patch("compact_focus.tui.curses.has_colors", return_value=False):
            approved = ReviewUI(screen, {"episodes": []}, proposal, review).run()
        self.assertTrue(approved)
        self.assertIn("Active cluster", review["approved_summary"])
        self.assertTrue(review["draft_review"]["approved"])


if __name__ == "__main__":
    unittest.main()
