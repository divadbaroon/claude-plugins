"""The Docs page: the questions a reader arrives with, answered in the box.

A static page beside the shelf -- nothing on it is fetched or saved. The
answer people come for is the credit one, so it is the page's only stepped
answer, and the ways to reach a person close the page: running out of
tokens is fixed by a human topping the credit up, not by a setting.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from test_goal_ui_bridge import BridgeTestCase, NODE  # noqa: E402
from test_project_saved import Saved  # noqa: E402


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class DocsPageTests(Saved, BridgeTestCase):

    def test_the_docs_tab_swaps_the_page_under_the_tabs(self):
        got = self.open(
            "click(tab('docs'));"
            "return JSON.stringify([P.overviewPage(),"
            " box.querySelector('.hc-overview-main').getAttribute('data-hc-on'),"
            " box.querySelector('.hc-docs').getAttribute('data-hc-on'),"
            " tab('docs').getAttribute('class'),"
            " P.overviewShown()]);")
        self.assertEqual(["docs", None, "",
                          "hc-overview-tab hc-overview-tab-on", True], got)

    def test_going_back_to_the_overview_still_works(self):
        got = self.open(
            "click(tab('docs')); click(tab('overview'));"
            "return JSON.stringify([P.overviewPage(),"
            " box.querySelector('.hc-docs').getAttribute('data-hc-on')]);")
        self.assertEqual(["overview", None], got)

    def test_the_token_answer_is_there_and_has_the_steps(self):
        got = self.open(
            "click(tab('docs'));"
            "return JSON.stringify([texts(box, 'hc-docs-q').length,"
            " texts(box, 'hc-docs-q').slice(-1)[0],"
            " texts(box, 'hc-docs-step').length]);")
        self.assertEqual(5, got[0])
        self.assertEqual("What if I run out of tokens?", got[1])
        self.assertEqual(3, got[2])

    def test_the_ways_to_reach_a_person_close_the_page(self):
        got = self.open(
            "click(tab('docs'));"
            "var page = box.querySelector('.hc-docs');"
            "var last = page.children[page.children.length - 1];"
            "return JSON.stringify(["
            " texts(box, 'hc-docs-reach-where'),"
            " texts(box, 'hc-docs-reach-at'),"
            " String(last.className)]);")
        self.assertEqual(["discord", "phone", "email", "email"], got[0])
        self.assertEqual(["discord.com/invite/ACEK85XnR", "571-492-2873",
                          "david@mathetic.com", "hudson@mathetic.com"],
                         got[1])
        self.assertEqual("hc-docs-reach", got[2])


if __name__ == "__main__":
    unittest.main()
