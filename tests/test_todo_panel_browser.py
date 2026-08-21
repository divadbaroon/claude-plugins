"""The TODO panel in a browser: rows, picks, build, questions, generate.

Playwright against an in-process chat server, with a stub `claude` on PATH
(see test_build_runs) so a Build round-trips without a model.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import build as BUILD  # noqa: E402
from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_chat_ui_server import browser_executable  # noqa: E402

STUB = r'''#!/usr/bin/env python3
import json, sys, os
args = sys.argv[1:]
if "--output-format" not in args:
    # providers.ClaudeCLI: the prompt arrives on stdin, plain text goes out
    sys.stdin.read()
    print("Implement the router carefully; run the tests after each change.")
    sys.exit(0)
prompt = args[args.index("-p") + 1]
resume = "--resume" in args
def say(text):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}), flush=True)
if not resume:
    ids = [w.strip("[]") for w in prompt.split() if w.startswith("[t") and w.endswith("]")]
    say('{"id": "%s", "question": "Which router file: src/a.ts or src/b.ts?"}' % ids[0])
    for other in ids[1:]:
        say('{"id": "%s", "state": "DONE"}' % other)
else:
    msg = json.loads(prompt)
    say('{"id": "%s", "state": "DONE"}' % msg["id"])
print(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
'''


@contextmanager
def server_for(path):
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
    ui._configure_server(server, Path(path), True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TodoPanelBrowserTests(unittest.TestCase):
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
        self.session = "chat-panel"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        g = GM.new_goal("g1", "Ship the router", origin="user")
        goals = {"version": 1, "goals": [g]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        self.trajdir = p.session_dir
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "claude"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.old_env = dict(os.environ)
        os.environ["PATH"] = str(self.bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["HC_BUILD_MODE"] = "headless"
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
        BUILD._RUNS.clear()

    def rows(self):
        goals, _ = chat_state.load_goals(self.session, self.root)
        return [(r["text"], r["depth"], r["status"])
                for r in GM.by_id(goals, "g1")["todo_items"]]

    def open(self, playwright):
        browser = playwright.chromium.launch(executable_path=self.chrome)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        return browser, page

    def test_rows_are_typed_selected_across_and_copied_as_markdown(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("ship it")
                page.keyboard.press("Enter")
                page.keyboard.press("Tab")
                page.keyboard.type("and test")
                page.keyboard.press("Enter")
                page.keyboard.press("Shift+Tab")
                page.keyboard.type("docs")
                page.wait_for_timeout(1500)
                self.assertEqual([("ship it", 0, ""), ("and test", 1, ""),
                                  ("docs", 0, "")], self.rows(),
                                 "the list reaches the server as rows")
                # Cmd+A picks every row for the build -- a selection for
                # building, not a text selection -- and Cmd+A again
                # releases them.
                page.keyboard.press("Meta+a")
                expect(page.locator(".hc-todo-build")).to_have_text("Build 3")
                expect(page.locator(".hc-rail-select")).to_have_text("Deselect all")
                page.keyboard.press("Meta+a")
                expect(page.locator(".hc-todo-build")).to_have_text("Build")
                # A selection dragged across rows is one selection: what is
                # typed over it lands in the row the selection began in.
                page.locator(".hc-todo-line").nth(2).click()
                page.keyboard.press("End")
                page.keyboard.press("Shift+ArrowUp")
                page.keyboard.press("Shift+ArrowUp")
                page.keyboard.type("Z")
                page.wait_for_timeout(1500)
                texts = [r[0] for r in self.rows()]
                self.assertEqual(1, len(texts), texts)
                self.assertTrue(texts[0].endswith("Z"), texts)
                # Copy TODOs at the lower left copies the whole list, each
                # row named with its state (no notes here, so no CONTEXT).
                page.locator(".hc-todo-copy").click()
                expect(page.get_by_text("copied ✓", exact=True)).to_be_visible()
                self.assertEqual("- [active] " + texts[0] + "\n",
                                 page.evaluate("() => navigator.clipboard.readText()"))
                # No goal document was involved
                goals, _ = chat_state.load_goals(self.session, self.root)
                self.assertEqual("", GM.by_id(goals, "g1")["notes"])
            finally:
                browser.close()

    def test_a_pasted_list_lands_as_one_row_per_bullet(self):
        from playwright.sync_api import sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                # A list whose newlines were lost on the way through the
                # clipboard, plus a properly indented line: one row each.
                page.evaluate(
                    "() => navigator.clipboard.writeText("
                    "'- Create projects- Global vault\\n    - a child row')")
                page.keyboard.press("Meta+v")
                page.wait_for_timeout(1500)
                self.assertEqual([("Create projects", 0, ""),
                                  ("Global vault", 0, ""),
                                  ("a child row", 1, "")], self.rows())
            finally:
                browser.close()

    def test_a_pasted_screenshot_lands_as_a_marker_and_a_file_the_copy_names(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("fix the header")
                # Cmd+V with an image on the clipboard. Playwright cannot put
                # an image on the real clipboard, so the paste event the
                # browser would fire is dispatched with the same payload.
                page.evaluate(
                    "() => { const bytes = new Uint8Array([137,80,78,71,13,10,26,10,0,0,0,0]);"
                    " const dt = new DataTransfer();"
                    " dt.items.add(new File([bytes], 'Screenshot.png', {type: 'image/png'}));"
                    " document.activeElement.dispatchEvent(new ClipboardEvent('paste',"
                    "   {clipboardData: dt, bubbles: true, cancelable: true})); }")
                page.wait_for_timeout(2000)
                rows = self.rows()
                self.assertEqual([("fix the header [attachment #1]", 0, "")], rows)
                goals, _ = chat_state.load_goals(self.session, self.root)
                shots = GM.by_id(goals, "g1")["todo_items"][0]["attachments"]
                self.assertEqual(1, shots[0]["n"])
                self.assertEqual("Screenshot.png", shots[0]["name"])
                path = Path(shots[0]["path"])
                self.assertTrue(path.is_file(), path)
                self.assertEqual(self.trajdir.resolve() / "attachments", path.parent)
                self.assertEqual(b"\x89PNG", path.read_bytes()[:4])
                # The copied body resolves the marker to the file.
                page.locator(".hc-todo-copy").click()
                expect(page.get_by_text("copied ✓", exact=True)).to_be_visible()
                self.assertEqual(
                    "- [active] fix the header [attachment #1]\n"
                    "\nAttachments (files the rows cite; open them for the rows"
                    " that name them):\n[attachment #1]: " + str(path) + "\n",
                    page.evaluate("() => navigator.clipboard.readText()"))
            finally:
                browser.close()

    def test_picked_rows_build_ask_and_finish_on_the_answer(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.keyboard.press("Enter")
                page.keyboard.type("Update the docs")
                page.wait_for_timeout(1200)
                build = page.locator(".hc-todo-build")
                expect(build).to_have_text("Build")
                # nothing picked, nothing built
                build.click()
                page.wait_for_timeout(300)
                self.assertEqual(["", ""], [r[2] for r in self.rows()])
                # pick the first with the gutter, the second with Cmd+/
                page.locator(".hc-todo-dash").first.click()
                expect(build).to_have_text("Build 1")
                page.locator(".hc-todo-line").nth(1).click()
                page.keyboard.press("Meta+/")
                expect(build).to_have_text("Build 2")
                expect(page.locator(".hc-rail-select")).to_have_text("Deselect all")
                build.click()
                # building at once, then asking on the first and done on the
                # second, as the stub session says
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
                expect(page.locator(".hc-todo-status").nth(1)).to_have_text("done")
                thread = page.locator(".hc-todo-ask")
                expect(thread).to_be_visible()
                expect(thread).to_contain_text("Which router file: src/a.ts or src/b.ts?")
                page.locator(".hc-todo-answer").click()
                page.keyboard.type("src/a.ts")
                page.keyboard.press("Enter")
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("done", timeout=10_000)
                expect(page.locator(".hc-todo-ask")).to_have_count(0)
                self.assertEqual(["done", "done"], [r[2] for r in self.rows()])
                # done rows cannot be picked again
                page.locator(".hc-todo-dash").first.click()
                expect(build).to_have_text("Build")
            finally:
                browser.close()

    def test_a_row_out_with_the_builder_comes_back_on_escape_or_its_corner(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.keyboard.press("Enter")
                page.keyboard.type("Update the docs")
                page.wait_for_timeout(1200)
                build = page.locator(".hc-todo-build")
                page.keyboard.press("Meta+a")
                expect(build).to_have_text("Build 2")
                build.click()
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
                expect(page.locator(".hc-todo-status").nth(1)).to_have_text("done")
                # The corner control: on the row that is out, not on the done one.
                expect(page.locator(".hc-todo-cancel")).to_have_count(1)
                # Escape in the answer box withdraws the question: the row is
                # back in the active band, unbuilt, and the done row is left.
                page.locator(".hc-todo-answer").click()
                page.keyboard.press("Escape")
                expect(page.locator(".hc-todo-ask")).to_have_count(0)
                expect(page.locator(".hc-todo-cancel")).to_have_count(0)
                page.wait_for_timeout(600)
                self.assertEqual([("Add the route", 0, ""), ("Update the docs", 0, "done")],
                                 self.rows())
                # Out again, and back by the corner this time.
                page.locator(".hc-todo-dash").first.click()
                expect(build).to_have_text("Build 1")
                build.click()
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
                page.locator(".hc-todo-cancel").click()
                expect(page.locator(".hc-todo-cancel")).to_have_count(0)
                page.wait_for_timeout(600)
                self.assertEqual("", self.rows()[0][2])
                # And it can be picked once more: the row is live again.
                page.locator(".hc-todo-dash").first.click()
                expect(build).to_have_text("Build 1")
            finally:
                browser.close()

    def test_generate_writes_the_prompt_into_the_prompt_tab(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-rail-tabs", timeout=15000)
                page.locator(".hc-rail-tabs").get_by_text("Prompt", exact=True).click()
                field = page.locator("textarea.hc-rail-code")
                expect(field).to_be_visible()
                self.assertEqual("", field.input_value())
                page.locator(".hc-rail-generate").click()
                expect(field).to_have_value(
                    "Implement the router carefully; run the tests after each change.",
                    timeout=15_000)
                page.wait_for_timeout(1200)
                goals, _ = chat_state.load_goals(self.session, self.root)
                self.assertIn("Implement the router carefully",
                              GM.by_id(goals, "g1")["prompt_md"])
                # the reader's own edit still wins and is kept
                field.click()
                page.keyboard.press("End")
                page.keyboard.type(" Keep diffs small.")
                page.wait_for_timeout(1500)
                goals, _ = chat_state.load_goals(self.session, self.root)
                self.assertTrue(GM.by_id(goals, "g1")["prompt_md"].endswith(
                    "Keep diffs small."))
            finally:
                browser.close()


class SessionBuildBrowserTests(TodoPanelBrowserTests):
    """Default mode: the build waits for the connected session's next turn."""

    def setUp(self):
        super().setUp()
        os.environ["HC_BUILD_MODE"] = "session"
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text("")

    def say(self, text):
        with self.transcript.open("a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": text}]}}) + "\n")

    def test_rows_are_queued_then_building_then_asking_and_the_session_can_close(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.wait_for_timeout(1200)
                page.locator(".hc-todo-dash").first.click()
                page.locator(".hc-todo-build").click()
                # Queued, and the rail says what that means.
                expect(page.locator(".hc-todo-status").first).to_have_text("queued", timeout=10_000)
                expect(page.locator(".hc-todo-error")).to_contain_text(
                    "queued — Claude picks it up when its turn ends or on your next message")
                self.assertEqual(1, len(BUILD.pending(self.session, self.root)))
                # The Stop hook takes it: building.
                text = BUILD.deliver(self.session, self.root, "Stop")
                self.assertIn("Add the route", text)
                expect(page.locator(".hc-todo-status").first).to_have_text("building", timeout=10_000)
                expect(page.locator(".hc-todo-error")).to_be_hidden()
                # Claude asks, in the transcript; the rail shows it.
                goals, _ = chat_state.load_goals(self.session, self.root)
                row_id = GM.by_id(goals, "g1")["todo_items"][0]["id"]
                self.say('{"id": "%s", "question": "Which router file?"}' % row_id)
                BUILD.scan_transcript(self.session, self.root, str(self.transcript))
                expect(page.locator(".hc-todo-status").first).to_have_text("needs you", timeout=10_000)
                page.locator(".hc-todo-answer").click()
                page.keyboard.type("src/a.ts")
                page.keyboard.press("Enter")
                expect(page.locator(".hc-todo-status").first).to_have_text("queued", timeout=10_000)
                self.assertEqual("answer", BUILD.pending(self.session, self.root)[0]["kind"])
                # The session ends: the rail offers to reopen it.
                BUILD.note_hook(self.session, self.root, "SessionEnd")
                expect(page.locator(".hc-todo-reopen")).to_be_visible(timeout=10_000)
                expect(page.locator(".hc-todo-error")).to_contain_text("session closed")
                BUILD.note_hook(self.session, self.root, "SessionStart")
                expect(page.locator(".hc-todo-reopen")).to_have_count(0, timeout=10_000)
            finally:
                browser.close()

    # the inherited headless tests run again here in session mode only where
    # they do not build; skip the build one.
    def test_picked_rows_build_ask_and_finish_on_the_answer(self):
        self.skipTest("headless-only")


if __name__ == "__main__":
    unittest.main()
