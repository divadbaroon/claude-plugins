"""Saying whether the chat is still being read.

The server has always reported this and the page never drew it, so a
workspace catching up on a long chat looked exactly like one where
inference was broken: an empty tree, and no way to tell which. These tests
are about the four states a reader needs told apart.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_goal_ui_bridge import NODE, BridgeTestCase  # noqa: E402
from test_project_ui import PRELUDE  # noqa: E402


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class AnalyzerLineTests(BridgeTestCase):
    def line(self, analyzer):
        return json.loads(self.run_js(
            PRELUDE + "var P = window.__hcPromptUI;"
            + "JSON.stringify(P.analyzerLine(%s));"
            % json.dumps({"analyzer": analyzer})))

    def test_caught_up_still_says_so(self):
        # Silence is what made a working workspace and a broken one look
        # identical: an empty tree and an empty corner tells a reader
        # nothing about which they are looking at.
        said = self.line({"status": "idle", "last_analyzed_ordinal": 900,
                          "requested_ordinal": 900})
        self.assertIn("up to date", said)
        self.assertIn("900", said)

    def test_a_chat_with_nothing_read_says_that_instead(self):
        self.assertIn("nothing read", self.line({
            "status": "idle", "last_analyzed_ordinal": 0,
            "requested_ordinal": 0}))

    def test_reading_says_how_far(self):
        said = self.line({"status": "running", "last_analyzed_ordinal": 1579,
                          "requested_ordinal": 1807})
        self.assertIn("reading this chat", said)
        self.assertIn("1,579", said)
        self.assertIn("1,807", said)

    def test_pending_is_not_reading(self):
        # "pending" means queued with nobody holding the lease -- which is
        # exactly what a dead hook leaves behind. Calling it "reading" would
        # claim progress that is not happening, which is the ambiguity this
        # line exists to remove.
        said = self.line({"status": "pending", "last_analyzed_ordinal": 0,
                          "requested_ordinal": 12})
        self.assertIn("waiting to read", said)
        self.assertNotIn("reading this chat", said)

    def test_behind_with_nobody_working_says_it_is_waiting(self):
        # This is what a dead hook leaves behind, and it used to look
        # identical to nothing at all.
        said = self.line({"status": "idle", "last_analyzed_ordinal": 100,
                          "requested_ordinal": 103})
        self.assertIn("waiting to read", said)
        self.assertIn("3 new messages", said)

    def test_one_message_is_not_pluralised(self):
        self.assertIn("1 new message",
                      self.line({"status": "idle",
                                 "last_analyzed_ordinal": 10,
                                 "requested_ordinal": 11}))

    def test_an_error_is_said_plainly(self):
        said = self.line({"status": "error", "last_analyzed_ordinal": 3,
                          "requested_ordinal": 90,
                          "error": "provider timed out"})
        self.assertIn("could not read this chat", said)
        self.assertIn("provider timed out", said)

    def test_no_analyzer_at_all_says_nothing(self):
        self.assertEqual("", self.line(None))


if __name__ == "__main__":
    unittest.main()
