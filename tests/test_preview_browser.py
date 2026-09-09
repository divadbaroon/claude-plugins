"""The middle pane, through the browser that draws it.

What is asserted here is what a reader sees: an unrecognized project waits
for an artifact without a model call, one that needs an install says which command and
why, and a program that prints has its output in the middle while it runs.
The pairing of surface and status is tested in test_preview.py; this is about
the cards being on screen and the buttons doing what they say.
"""

import json
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_chat_ui_server import browser_executable, server_for  # noqa: E402

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import preview as PV  # noqa: E402


class PreviewBrowserTests(unittest.TestCase):
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
        self.root = Path(self.tmp.name) / "vault"
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir(parents=True)
        self.addCleanup(self.quiet)
        model = mock.patch.object(PV, "_ask_model", side_effect=AssertionError(
            "A declared run command must not call a model"))
        self.model = model.start()
        self.addCleanup(model.stop)

    def quiet(self):
        proc = PV.running(self.project)
        if proc:
            proc.stop()
        PV.forget(self.project)

    def write(self, name, text):
        spot = self.project / name
        spot.write_text(text if isinstance(text, str) else json.dumps(text))

    def workspace(self, todo="Make the middle a live preview"):
        paths = CS.paths("chat-preview", self.root)
        paths.session_dir.mkdir(parents=True)
        goal = GM.new_goal("g1", "Live preview in the middle", origin="user")
        goal["todo_items"] = [{"id": "t1", "text": todo, "depth": 0,
                               "status": "", "question": ""}]
        goals = {"version": 1, "goals": [goal]}
        GM.sanitize(goals)
        GM.save(paths.session_dir, goals, {"items": []})
        held, _ = GM.load(paths.session_dir)
        self.row_id = held["goals"][0]["todo_items"][0]["id"]
        paths.prompts.write_text(json.dumps({"prompts": []}))
        paths.manifest.write_text(json.dumps({
            "cwd": str(self.project),
            "project_bound_at": "2026-01-01T00:00:00+00:00"}))
        return paths.session_dir

    def open(self, playwright, url):
        browser = playwright.chromium.launch(
            executable_path=self.chrome, headless=True,
            args=["--disable-background-networking"])
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url + "/legacy", wait_until="domcontentloaded")
        page.wait_for_selector(".hc-preview-mount", timeout=20_000)
        return browser, page

    def status(self, page):
        return page.evaluate(
            "() => (window.__hcPromptUI.previewState() || {}).status")

    def wait_for(self, page, status, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.status(page) == status:
                return True
            page.wait_for_timeout(300)
        return False

    def test_an_unrecognized_project_waits_for_an_artifact_without_a_model_call(self):
        from playwright.sync_api import expect, sync_playwright
        # main.py is recognized automatically. This entrypoint is unknown to
        # the detector; the current empty pane offers no model-fallback button.
        self.write("custom_task.py", "print('hello')\n")
        with server_for(self.workspace()) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                empty = page.locator(".hc-preview-mount .hc-pv-empty")
                expect(empty).to_contain_text("NO ARTIFACT YET")
                expect(empty).to_contain_text("Builds and previews will appear here.")
                expect(page.get_by_text("Find how to run it", exact=True)).to_have_count(0)
                self.model.assert_not_called()
                self.assertIsNone(PV.running(self.project))
            finally:
                browser.close()

    def test_nothing_is_run_by_opening_the_page(self):
        from playwright.sync_api import sync_playwright
        self.write("main.py", "open('ran.txt', 'w').write('x')\n")
        with server_for(self.workspace()) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                self.assertTrue(self.wait_for(page, "ready"))
                page.wait_for_timeout(2_500)
                # A pane that ran the project because a page was opened is a
                # pane that runs arbitrary repository code on a page load.
                self.assertFalse((self.project / "ran.txt").exists())
                self.assertIsNone(PV.running(self.project))
            finally:
                browser.close()

    def test_a_missing_install_is_one_card_with_the_command_and_the_why(self):
        from playwright.sync_api import expect, sync_playwright
        self.write("package.json", {"scripts": {"dev": "vite"}})
        with server_for(self.workspace()) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                self.assertTrue(self.wait_for(page, "needs_user_action"))
                card = page.locator(".hc-pv-card")
                expect(card).to_contain_text("dependencies are not installed")
                expect(page.locator(".hc-pv-cmd-text")).to_have_text(
                    "npm install")
                expect(card).to_contain_text("Why: package.json lists")
                # Offered as something to press, since running it is the
                # whole of the step.
                expect(page.get_by_text("Run this", exact=True)).to_be_visible()
                expect(page.get_by_text("I did this", exact=True)).to_be_visible()
            finally:
                browser.close()

    def test_a_program_that_prints_prints_into_the_middle_while_it_runs(self):
        from playwright.sync_api import expect, sync_playwright
        self.write("main.py", "import time\n"
                              "for i in range(40):\n"
                              "    print('step', i)\n"
                              "    time.sleep(0.2)\n")
        with server_for(self.workspace()) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                self.assertTrue(self.wait_for(page, "ready"))
                page.get_by_text("Run", exact=True).first.click()
                expect(page.locator(".hc-pv-term-body")).to_contain_text(
                    "step 0", timeout=20_000)
                # Still running, and saying so where the address would be.
                expect(page.locator(".hc-pv-bar")).to_contain_text(
                    "python main.py")
                expect(page.get_by_text("Stop", exact=True)).to_be_visible()
                page.get_by_text("Stop", exact=True).click()
                self.assertTrue(self.wait_for(page, "finished"))
                expect(page.locator(".hc-pv-card")).to_contain_text("It ended")
            finally:
                browser.close()

    def test_the_pane_says_what_the_picked_row_is_for(self):
        from playwright.sync_api import expect, sync_playwright
        self.write("main.py", "print('hello')\n")
        session = self.workspace(todo="Allow users to rename goals")
        PV.configure(self.root, self.project)
        # As if the reader had already asked what to look for: the model call
        # behind that button is tested where the other model calls are.
        PV.save_intent(self.root, self.project, self.row_id,
                       "Allow users to rename goals",
                       {"entrypoint": "/goals",
                        "scenario": ["Open a goal", "Rename it"],
                        "expected": "The new name survives a reload"})
        with server_for(session) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                # Nothing is picked, so the pane says nothing about a row.
                expect(page.locator(".hc-pv-intent")).to_have_count(0)
                page.locator(".hc-todo-dash").first.click()
                head = page.locator(".hc-pv-intent")
                expect(head).to_be_visible(timeout=10_000)
                expect(head).to_contain_text("Allow users to rename goals")
                expect(head).to_contain_text("Where: /goals")
                expect(head).to_contain_text("1. Open a goal")
                expect(head).to_contain_text(
                    "Expect: The new name survives a reload")
            finally:
                browser.close()


    # --- the one control that promises something visual ------------------

    def test_a_project_with_no_page_says_so_instead_of_offering_one(self):
        from playwright.sync_api import expect, sync_playwright
        self.write("main.py", "print('hello')\n")
        with server_for(self.workspace()) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                self.assertTrue(self.wait_for(page, "ready"))
                # Run is still offered -- the project does run, it just does
                # not serve -- and the promise of a page is not made.
                expect(page.get_by_text("Run", exact=True)).to_be_visible()
                expect(page.get_by_text("Show UI", exact=True)).to_have_count(0)
                expect(page.locator(".hc-pv-card")).to_contain_text(
                    "UI not ready for preview — nothing in this project"
                    " serves a page yet")
            finally:
                browser.close()

    def test_show_ui_brings_the_page_up_and_embeds_it(self):
        from playwright.sync_api import expect, sync_playwright
        self.write("index.html", "<h1>the app</h1>")
        self.write("main.py",
                   "import http.server, socketserver\n"
                   "srv = socketserver.TCPServer(('127.0.0.1', 0),"
                   " http.server.SimpleHTTPRequestHandler)\n"
                   "print('http://127.0.0.1:%s/' % srv.server_address[1])\n"
                   "srv.serve_forever()\n")
        # This case covers the explicit button after a reader has disabled
        # autostart. Default automatic startup is covered by the next test.
        PV.configure(self.root, self.project)
        config = PV.read_config(self.root, self.project)
        config["autostart"] = False
        PV.write_config(self.root, self.project, config)
        with server_for(self.workspace()) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                self.assertTrue(self.wait_for(page, "ready"))
                page.get_by_text("Show UI", exact=True).click()
                self.assertTrue(self.wait_for(page, "running"))
                # The page itself, in a frame that lives outside the
                # artifact's subtree so a re-render cannot reload it.
                frame = page.locator(".hc-pv-frame")
                expect(frame).to_be_visible(timeout=15_000)
                expect(frame).to_have_attribute("src", PV.running(self.project).url)
                expect(page.frame_locator(".hc-pv-frame").get_by_role(
                    "heading", name="the app", exact=True)).to_be_visible()
                self.assertGreater(
                    page.evaluate("() => document.querySelector"
                                  "('.hc-pv-frame').getBoundingClientRect()"
                                  ".height"), 300)
                # Running web previews omit the former URL/Stop bar. The
                # fixture stops its process in quiet(); the CLI case above
                # still exercises its actual visible Stop control.
                expect(page.locator(".hc-preview-mount .hc-pv-bar")).to_have_count(0)
            finally:
                browser.close()

    def test_the_embedded_page_shows_only_on_the_goals_view(self):
        """Off the Goals view, the frame stands down.

        The frame is fixed over the pane's slot and stacked above the
        overview and brainstorm panels, and the slot keeps its rectangle
        while those panels are up -- so switching views has to take the
        frame off the paint itself, and give it back with the click that
        returns to Goals, including the docked brainstorm in that column.
        """
        from playwright.sync_api import expect, sync_playwright
        self.write("index.html", "<h1>the app</h1>")
        self.write("main.py",
                   "import http.server, socketserver\n"
                   "srv = socketserver.TCPServer(('127.0.0.1', 0),"
                   " http.server.SimpleHTTPRequestHandler)\n"
                   "print('http://127.0.0.1:%s/' % srv.server_address[1])\n"
                   "srv.serve_forever()\n")
        with server_for(self.workspace()) as url, sync_playwright() as pw:
            browser, page = self.open(pw, url)
            try:
                # A repository-declared web server starts automatically.
                self.assertTrue(self.wait_for(page, "running"))
                frame = page.locator(".hc-pv-frame")
                expect(frame).to_be_visible()
                src = frame.get_attribute("src")
                for other in ("overview", "brainstorm", "docs"):
                    page.locator(f'[data-hc-viewtab="{other}"]').click()
                    expect(frame).to_be_hidden()
                    goals = page.locator('[data-hc-viewtab="goals"]')
                    goals.click()
                    expect(goals).to_have_attribute("data-hc-on", "")
                    expect(page.locator("html")).to_have_attribute("data-hc-bs-dock", "")
                    expect(frame).to_be_visible()
                    expect(frame).to_have_attribute("src", src)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
