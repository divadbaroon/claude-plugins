"""The search bar under GOALS, in a browser.

Playwright against an in-process chat server: the box is drawn under the
heading, typing replaces the tree with ranked hits, and picking a hit opens
its branch, selects it, and puts the tree back.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402

from test_chat_ui_server import browser_executable  # noqa: E402
from test_todo_panel_browser import server_for  # noqa: E402


class GoalSearchBrowserTests(unittest.TestCase):
    def setUp(self):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright is not installed")
        self.chrome = browser_executable()
        if not self.chrome:
            self.skipTest("Chrome/Chromium is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-search"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        g1 = GM.new_goal("g1", "Ship the router", origin="user")
        g1["notes"] = "the banner goes in the corner"
        g1a = GM.new_goal("g1a", "Notifications for builds", origin="user")
        g1a["parent_goal_id"] = "g1"
        g1a["todo_items"] = [{"id": "t1", "text": "count the unread alerts",
                              "depth": 0, "status": ""}]
        g2 = GM.new_goal("g2", "Search the rail", origin="user")
        goals = {"version": 1, "goals": [g1, g1a, g2]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        self.trajdir = p.session_dir

    def open(self, playwright):
        browser = playwright.chromium.launch(executable_path=self.chrome)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        return browser, page

    def test_typing_finds_a_goal_by_a_todo_row_and_picking_it_selects_it(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-rail-left .hc-search-input", timeout=15000)
                # The box is the heading's next sibling, with the rows under it.
                self.assertTrue(page.evaluate(
                    "() => { const h = document.querySelector('.hc-rail-left .hc-rail-head');"
                    " return !!h && h.nextElementSibling.className === 'hc-search'; }"))
                expect(page.locator(".hc-rowtitle")).to_have_count(3)
                # Fold the parent first, so picking the child has a branch to open.
                page.locator(".hc-row").first.locator("span").first.click()
                expect(page.locator(".hc-rowtitle")).to_have_count(2)
                box = page.locator(".hc-search-input")
                box.click()
                page.keyboard.type("unraed")          # a slip: unread
                expect(page.locator(".hc-search-hit")).to_have_count(1)
                expect(page.locator(".hc-search-hit-title")).to_have_text(
                    "Notifications for builds")
                expect(page.locator(".hc-search-hit-trail")).to_have_text("Ship the router")
                expect(page.locator(".hc-search-hit-where")).to_contain_text(
                    "count the unread alerts")
                self.assertTrue(page.evaluate(
                    "() => document.querySelector('.hc-rail-left')"
                    ".hasAttribute('data-hc-searching')"))
                # While searching the tree is out of sight.
                expect(page.locator(".hc-rowtitle").first).to_be_hidden()
                page.screenshot(path="/tmp/hc-search-hits.png")
                page.keyboard.press("Enter")
                # The box is cleared, the tree is back with the branch open,
                # and the child is the selection.
                expect(box).to_have_value("")
                expect(page.locator(".hc-rowtitle")).to_have_count(3)
                page.wait_for_timeout(300)
                saved = page.evaluate(
                    "() => JSON.parse(localStorage.getItem('hc-vault-ui-v1')).selId")
                self.assertEqual("g1a", saved)
                self.assertFalse(page.evaluate(
                    "() => document.querySelector('.hc-rail-left')"
                    ".hasAttribute('data-hc-searching')"))
                # Escape on a query puts the tree back without choosing.
                box.click()
                page.keyboard.type("rail")
                expect(page.locator(".hc-search-hit")).to_have_count(1)
                page.keyboard.press("Escape")
                expect(box).to_have_value("")
                expect(page.locator(".hc-rowtitle")).to_have_count(3)
                self.assertEqual("g1a", page.evaluate(
                    "() => JSON.parse(localStorage.getItem('hc-vault-ui-v1')).selId"))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
