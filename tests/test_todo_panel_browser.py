"""The TODO panel in a browser: rows, picks, build, questions, generate.

Playwright against an in-process chat server, with a stub `claude` on PATH
(see test_build_runs) so a Build round-trips without a model.
"""

import json
import os
import re
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


def tokens_in(text):
    """The number a "~1.5k tok" label is claiming, as tokens."""
    hit = re.search(r"~?\s*([\d.]+)(k?)\s*tok", text or "")
    if not hit:
        return 0.0
    return float(hit.group(1)) * (1000.0 if hit.group(2) else 1.0)


STUB = r'''#!/usr/bin/env python3
import json, sys, os, time
args = sys.argv[1:]
if "--output-format" not in args:
    # providers.ClaudeCLI: the prompt arrives on stdin, plain text goes out
    sys.stdin.read()
    print("Implement the router carefully; run the tests after each change.")
    sys.exit(0)
prompt = args[args.index("-p") + 1]
resume = "--resume" in args
if os.environ.get("STUB_LOG"):
    with open(os.environ["STUB_LOG"], "a") as fh:
        fh.write(json.dumps({"prompt": prompt, "resume": resume}) + "\n")
def say(text):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}), flush=True)
def end():
    print(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
    sys.exit(0)
if resume and '{"restart": true' in prompt:
    # The restart check that follows a finished build (RestartCheckBrowserTests).
    if os.environ.get("STUB_CHECK_HOLD"):
        time.sleep(float(os.environ["STUB_CHECK_HOLD"]))
    if os.environ.get("STUB_RESTART") == "yes":
        say(json.dumps({"restart": True,
                        "why": "the session-cache change lives in a long-running process",
                        "prompt": "Restart the goals-ui dev process so the new session-cache"
                                  " code loads: kill the running `goals-ui serve`, then"
                                  " re-run `goals-ui serve --dev`."}))
    else:
        say('{"restart": false}')
    end()
if not resume:
    ids = [w.strip("[]") for w in prompt.split() if w.startswith("[t") and w.endswith("]")]
    if os.environ.get("STUB_HOLD"):
        # Mid-work for a while: long enough to be told something.
        say('{"estimate": {"tokens": 12000, "minutes": 3}}')
        time.sleep(float(os.environ["STUB_HOLD"]))
        for i in ids:
            say('{"id": "%s", "state": "DONE"}' % i)
        end()
    say('{"id": "%s", "question": "Which router file: src/a.ts or src/b.ts?"}' % ids[0])
    for other in ids[1:]:
        say('{"id": "%s", "state": "DONE"}' % other)
else:
    if prompt.startswith("[Engelbart] The user added context"):
        ids = [w.strip("[]") for w in prompt.split()
               if w.startswith("[t") and w.endswith("]")]
        if os.environ.get("STUB_HOLD"):
            time.sleep(float(os.environ["STUB_HOLD"]))
        for i in ids:
            say('{"id": "%s", "state": "DONE"}' % i)
        end()
    try:
        msg = json.loads(prompt)
    except ValueError:
        # Not an answer -- a note, a deleted row: carry on to the end.
        say("Moving on.")
        end()
    say('{"id": "%s", "state": "DONE"}' % msg["id"])
end()
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
        # A chat that has said which project it is for: otherwise the page
        # opens on onboarding, whose shade sits over the rail and takes
        # every click these tests make.
        chat_state.bind_project(self.session, str(self.root), root=self.root)
        self.trajdir = p.session_dir
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "claude"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.old_env = dict(os.environ)
        os.environ["PATH"] = str(self.bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["HC_BUILD_MODE"] = "headless"
        # The restart check that follows a finished build is a second
        # process on the same session; the tests that are about it turn it
        # on themselves (see RestartCheckBrowserTests).
        os.environ["HC_BUILD_RESTART_CHECK"] = "0"
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
        BUILD._RUNS.clear()

    def rows(self, blank=False):
        # The rows on the server. A Build leaves a fresh empty row at the
        # foot of the active band for the next TODO; `blank` keeps it.
        goals, _ = chat_state.load_goals(self.session, self.root)
        return [(r["text"], r["depth"], r["status"])
                for r in GM.by_id(goals, "g1")["todo_items"]
                if blank or r["text"].strip()]

    def tile(self, page, text):
        # The row reading `text`, whole tile: its gutter, line, badge, x.
        return page.locator(".hc-todo").filter(has_text=text)

    def go(self, page):
        # Build starts the build: nothing stands between the click and it.
        from playwright.sync_api import expect
        expect(page.locator(".hc-ask")).to_have_count(0)

    def open(self, playwright):
        browser = playwright.chromium.launch(executable_path=self.chrome)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            permissions=["clipboard-read", "clipboard-write"])
        # These tests specify the full TODO rail. Novice is the product
        # default now, so select the projection under test before first paint.
        context.add_init_script(
            "localStorage.setItem('hc-interface-mode-v1','advanced')")
        page = context.new_page()
        return browser, page

    def test_first_visit_teaches_the_novice_workspace_and_can_return_from_brainstorm(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=self.chrome)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded")
                expect(page.locator(".hc-nv-intro")).to_be_visible(timeout=15000)
                expect(page.locator(".hc-nv-intro-step")).to_have_text(
                    "How Engelbart works · 1 of 3")
                page.get_by_role("button", name="Next").click()
                page.get_by_role("button", name="Next").click()
                page.get_by_role("button", name="Open workspace").click()
                expect(page.locator(".hc-nv-side-head")).to_have_text("Goals")
                expect(page.locator(".hc-nv-activity-head")).to_have_text("Activity")
                expect(page.locator(".hc-rail-right")).not_to_be_visible()

                # The novice button enters the existing brainstorm route and
                # closing that route restores the same two-column projection.
                page.get_by_role("button", name="Brainstorm").click()
                expect(page.locator(".hc-brainstorm")).to_be_visible(timeout=10000)
                page.keyboard.press("Escape")
                expect(page.locator(".hc-novice")).to_be_visible()

                # Settings makes the projection explicit and Advanced reveals
                # the full existing workspace without changing the goal data.
                page.get_by_role("button", name="Settings").click()
                expect(page.locator("[data-hc-tab=interface]")).to_be_visible()
                page.locator("[data-hc-interface-mode=advanced]").click()
                expect(page.locator(".hc-novice")).to_have_count(0)
                expect(page.locator(".hc-rail-right")).to_be_visible()
            finally:
                browser.close()

    def test_novice_build_note_and_followup_message_reach_the_active_todo(self):
        from playwright.sync_api import expect, sync_playwright
        os.environ["STUB_HOLD"] = "2"
        self.addCleanup(lambda: os.environ.pop("STUB_HOLD", None))
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=self.chrome)
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_init_script(
                "localStorage.setItem('hc-novice-instructions-v1','seen')")
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded")
                add = page.locator("[data-hc-nv-add]")
                expect(add).to_be_visible(timeout=15000)
                add.fill("Build the first visible screen")
                add.press("Enter")
                expect(page.locator(".hc-nv-todo-text")).to_have_text(
                    "Build the first visible screen")
                page.get_by_role("button", name="Build", exact=True).click()
                expect(page.get_by_text("Anything Bart should know first?")) \
                    .to_be_visible()
                page.locator("[data-hc-nv-build-note]").fill(
                    "Keep the first screen visual and small.")
                page.get_by_role("button", name="Send to Bart").click()
                expect(page.get_by_text(
                    "Build note: Keep the first screen visual and small.")) \
                    .to_be_visible(timeout=10000)
                message = page.locator("[data-hc-nv-note]")
                expect(message).to_be_visible(timeout=10000)
                message.fill("Also preserve the keyboard path.")
                message.press("Enter")
                expect(page.get_by_text(
                    "Your note: Also preserve the keyboard path.")) \
                    .to_be_visible(timeout=10000)
            finally:
                browser.close()

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
                page.keyboard.press("Meta+a")
                # Released: the button is back at the whole list, which is
                # what it offers when nothing is picked.
                expect(page.locator(".hc-todo-build")).to_have_text("Build all")
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
                # Copy all, at the top right of the list, copies the whole
                # list, each row named with its state (no notes here, so no
                # CONTEXT).
                page.locator(".hc-todo-copy").click()
                expect(page.get_by_text("copied ✓", exact=True)).to_be_visible()
                self.assertEqual("- [active] " + texts[0] + "\n",
                                 page.evaluate("() => navigator.clipboard.readText()"))
                # No goal document was involved
                goals, _ = chat_state.load_goals(self.session, self.root)
                self.assertEqual("", GM.by_id(goals, "g1")["notes"])
            finally:
                browser.close()

    def test_rows_survive_a_switch_of_the_tree_filter(self):
        # The tree's filter chips are the artifact's: switching one makes it
        # save its own copy of the goals, which knows nothing of the rows the
        # rail wrote. The rows must not go with it -- on screen or on disk.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("ship it")
                page.wait_for_timeout(1200)
                self.assertEqual([("ship it", 0, "")], self.rows())
                chips = page.locator(".hc-chip")
                chips.filter(has_text="Done").first.click()
                page.wait_for_timeout(1500)
                chips.filter(has_text="All").first.click()
                page.wait_for_timeout(1500)
                expect(page.locator(".hc-todo-line").first).to_have_text("ship it")
                self.assertEqual([("ship it", 0, "")], self.rows())
                # And a row still in the rail's save window when the chip is
                # clicked -- typed, not yet written -- is written, not lost.
                page.locator(".hc-todo-line").first.click()
                page.keyboard.press("End")
                page.keyboard.press("Enter")
                page.keyboard.type("and docs")
                chips.filter(has_text="Active").first.click()
                page.wait_for_timeout(1500)
                self.assertEqual([("ship it", 0, ""), ("and docs", 0, "")], self.rows())
                expect(page.locator(".hc-todo-line").nth(1)).to_have_text("and docs")
            finally:
                browser.close()

    def test_rows_typed_and_reloaded_on_are_still_there_on_the_way_back(self):
        # The rail writes 600ms after the last keystroke, and a reload inside
        # that window used to take the words with it: nothing flushed on the
        # way out, and the next load rebuilds the store from the server, so a
        # row the server never heard of came back blank. The reader typed a
        # TODO, refreshed, and read an empty row.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.keyboard.press("Enter")
                # No pause anywhere: both rows are still in the save window,
                # and the caret is still in the list, when the page goes.
                page.keyboard.type("and the docs")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                # The beacon and the reload's own read of /api/state race, so
                # the words may land a poll after the page does.
                expect(page.locator(".hc-todo-line").first
                       ).to_have_text("Add the route", timeout=10_000)
                expect(page.locator(".hc-todo-line").nth(1)
                       ).to_have_text("and the docs")
                self.assertEqual([("Add the route", 0, ""),
                                  ("and the docs", 0, "")], self.rows())
            finally:
                browser.close()

    def test_the_dash_holds_its_line_on_a_row_with_nothing_typed_in_it_yet(self):
        # A row with no words has no line box of its own, so the row's
        # baseline alignment falls back to the empty box's bottom edge: the
        # dash drops most of a line and the caret sits above it, off to one
        # side of the gutter it belongs beside. The line's zero-width strut
        # is what holds them level, and nothing else in the suite would
        # notice if it went: the rows would still read and save correctly.
        from playwright.sync_api import sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                # The caret is where the first character will be drawn, so a
                # throwaway glyph in the empty line stands in for it: measure
                # where it lands, then take it back out.
                geometry = """() => {
                  const row = document.querySelector('.hc-todo-row');
                  const dash = row.querySelector('.hc-todo-dash');
                  const line = row.querySelector('.hc-todo-line');
                  if (line.textContent !== '') throw new Error('row not empty');
                  const probe = document.createElement('span');
                  probe.textContent = 'X';
                  line.appendChild(probe);
                  const at = probe.getBoundingClientRect();
                  probe.remove();
                  const d = dash.getBoundingClientRect();
                  const l = line.getBoundingClientRect();
                  return {drop: +(d.top - l.top).toFixed(2),
                          caretLeft: +(at.left - l.left).toFixed(2),
                          height: +row.getBoundingClientRect().height.toFixed(2)};
                }"""
                seen = page.evaluate(geometry)
                self.assertEqual(0, seen["drop"],
                                 "the dash sits on the empty line's own first"
                                 " line, not half a line under it: " + str(seen))
                self.assertEqual(0, seen["caretLeft"],
                                 "the caret opens at the line's left edge, so"
                                 " the strut takes no width: " + str(seen))
                # One line tall: 22.8px of line box inside 2px of padding
                # either side. A strut that took a line of its own would
                # make the row twice this.
                self.assertLess(seen["height"], 28, str(seen))
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

    def test_build_starts_the_build_without_asking_first(self):
        # What a build will spend is printed in each row's corner, where it
        # is read before the button is pressed. The button itself is the
        # decision: it starts the build, with no dialog in between.
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
                expect(page.locator(".hc-ask")).to_have_count(0)
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
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
                # Nothing picked is the whole list, and the button says so.
                expect(build).to_have_text("Build all")
                # pick the first with the gutter, the second with Cmd+/
                page.locator(".hc-todo-dash").first.click()
                expect(build).to_have_text("Build 1")
                page.locator(".hc-todo-line").nth(1).click()
                page.keyboard.press("Meta+/")
                expect(build).to_have_text("Build 2")
                build.click()
                self.go(page)
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
                self.tile(page, "Add the route").locator(".hc-todo-dash").click()
                expect(build).to_have_text("Build")
            finally:
                browser.close()

    def test_one_cmd_enter_builds_what_is_unsent_and_the_page_stays(self):
        # Nothing picked: Cmd+Enter builds what has not been sent -- once,
        # and from wherever the caret is. The page is not reloaded to learn
        # the rows' new state or the goal's: the tree's own chips update in
        # place. A fresh empty row is waiting for the next TODO, with the
        # caret in it. And the answer box wraps rather than scrolling.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.evaluate("() => { window.__hcStay = 'same page'; }")
                expect(page.locator(".hc-chip").filter(has_text="In progress")
                       ).to_have_text("In progress 0")
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.wait_for_timeout(1200)
                page.keyboard.press("Meta+Enter")
                self.go(page)
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
                # No reload: the marker set before the build is still there,
                # and the artifact's tree learned the goal is in progress.
                self.assertEqual("same page", page.evaluate("() => window.__hcStay"))
                expect(page.locator(".hc-chip").filter(has_text="In progress")
                       ).to_have_text("In progress 1", timeout=10_000)
                # The caret is on a fresh empty row: typing is the next TODO.
                page.keyboard.type("Update the docs")
                page.wait_for_timeout(1200)
                self.assertEqual([("Update the docs", 0, ""), ("Add the route", 0, "asking")],
                                 self.rows())
                # The question thread wraps: a long answer grows the box
                # downward instead of running off to the right.
                answer = page.locator(".hc-todo-answer")
                self.assertEqual("TEXTAREA", answer.evaluate("el => el.tagName"))
                one_line = answer.evaluate("el => el.getBoundingClientRect().height")
                answer.click()
                page.keyboard.type("src/a.ts " * 20)
                page.wait_for_timeout(200)
                grown = answer.evaluate("el => el.getBoundingClientRect().height")
                self.assertGreater(grown, one_line * 1.8, (one_line, grown))
                self.assertEqual(
                    answer.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), True)
                page.keyboard.press("Enter")
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("done", timeout=10_000)
                # Two rows unsent, one picked from the gutter (which leaves
                # the caret in no row at all): Cmd+Enter still builds, once.
                self.tile(page, "Update the docs").locator(".hc-todo-line").click()
                page.keyboard.press("End")
                page.keyboard.press("Enter")
                page.keyboard.type("Write the tests")
                page.keyboard.press("Enter")
                page.keyboard.type("Ship it")
                page.wait_for_timeout(1200)
                self.tile(page, "Ship it").locator(".hc-todo-dash").click()
                expect(page.locator(".hc-todo-build")).to_have_text("Build 1")
                page.keyboard.press("Meta+Enter")
                self.go(page)
                expect(self.tile(page, "Ship it").locator(".hc-todo-status")
                       ).to_have_text("needs you", timeout=10_000)
                self.assertEqual("same page", page.evaluate("() => window.__hcStay"))
                # Several unsent rows is not a choice the reader has to make
                # first: with nothing picked, the button offers the whole
                # list and Cmd+Enter takes it -- all three, since "Update
                # the docs" was never sent either.
                page.keyboard.type("Also this")
                page.wait_for_timeout(1200)
                page.keyboard.press("Meta+a")   # picks them
                page.keyboard.press("Meta+a")   # and releases them again
                expect(page.locator(".hc-todo-build")).to_have_text("Build all")
                page.keyboard.press("Meta+Enter")
                self.go(page)
                expect(self.tile(page, "Also this").locator(".hc-todo-status")
                       ).to_have_text("done", timeout=10_000)
                self.assertEqual(
                    [], [r for r in self.rows()
                         if r[0] in ("Write the tests", "Also this",
                                     "Update the docs") and not r[2]],
                    "every unsent row went with the build")
            finally:
                browser.close()

    def test_a_build_pressed_mid_save_still_carries_the_row_it_names(self):
        # The rail writes 600ms after the last keystroke; a build pressed
        # inside that window used to leave with the row's text still on this
        # side. The server marked the blank row it was holding as building,
        # then refused the import carrying the text -- the build had moved
        # the revision under it -- and the page took that blank back on the
        # next merge. What the reader saw, then and after every refresh, was
        # a row that said "building" with nothing written on it.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                # No pause: the save is still in its window when this goes.
                page.keyboard.press("Meta+Enter")
                self.go(page)
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
                # The row the build is about says what it says, on the
                # server and on screen.
                self.assertEqual([("Add the route", 0, "asking")], self.rows())
                expect(self.tile(page, "Add the route")).to_have_count(1)
                # And still does when the page is loaded again.
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                expect(self.tile(page, "Add the route")).to_have_count(1)
                page.wait_for_timeout(1200)
                self.assertEqual([("Add the route", 0, "asking")], self.rows())
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
                self.go(page)
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
                self.tile(page, "Add the route").locator(".hc-todo-dash").click()
                expect(build).to_have_text("Build 1")
                build.click()
                self.go(page)
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
                page.locator(".hc-todo-cancel").click()
                expect(page.locator(".hc-todo-cancel")).to_have_count(0)
                page.wait_for_timeout(600)
                self.assertEqual("", self.rows()[0][2])
                # And it can be picked once more: the row is live again.
                self.tile(page, "Add the route").locator(".hc-todo-dash").click()
                expect(build).to_have_text("Build 1")
            finally:
                browser.close()

    def test_a_done_row_is_clicked_open_and_sent_back_out_with_the_note(self):
        # "No, you did a bad job." Clicking a finished row -- not hovering it
        # -- opens the same thread shape Claude's own questions use; the note
        # sends the row back out on its next run, and the run that ended is
        # kept under the row so the argument is legible afterwards.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Drop the 2k char cap")
                page.wait_for_timeout(1200)
                page.keyboard.press("Meta+Enter")
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("needs you", timeout=10_000)
                page.locator(".hc-todo-answer").click()
                page.keyboard.type("src/a.ts")
                page.keyboard.press("Enter")
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("done", timeout=10_000)
                row = self.tile(page, "Drop the 2k char cap")
                struck = "el => getComputedStyle(el).textDecorationLine"
                self.assertIn("line-through",
                              row.locator(".hc-todo-line").evaluate(struck))
                self.assertTrue(self.settled())
                # Nothing on hover: the row itself is the control.
                expect(page.locator("[data-hc-todo-reopen]")).to_have_count(0)
                row.locator(".hc-todo-line").click()
                expect(page.locator(".hc-todo-question")
                       ).to_have_text("What went wrong?")
                expect(row.locator(".hc-todo-status")).to_have_text("reopened")
                self.assertNotIn("line-through",
                                 row.locator(".hc-todo-line").evaluate(struck))
                note = page.locator("[data-hc-todo-reopen]")
                self.assertEqual("TEXTAREA", note.evaluate("el => el.tagName"))
                # Escape is second thoughts: the row is done again, untouched.
                page.keyboard.press("Escape")
                expect(page.locator("[data-hc-todo-reopen]")).to_have_count(0)
                self.assertEqual("done", self.rows()[0][2])
                # Reopened for real: the note goes back into the same session.
                row.locator(".hc-todo-line").click()
                page.locator("[data-hc-todo-reopen]").click()
                page.keyboard.type("truncation still happens in the subagent path")
                page.keyboard.press("Enter")
                expect(row.locator(".hc-todo-run").first
                       ).to_have_text("run 1 · done · reopened", timeout=10_000)
                expect(row.locator(".hc-todo-run-note").first).to_contain_text(
                    "truncation still happens in the subagent path")
                # And it finishes again with its history still under it.
                expect(row.locator(".hc-todo-status")
                       ).to_have_text("done · run 2", timeout=10_000)
                page.wait_for_timeout(600)
                goals, _ = chat_state.load_goals(self.session, self.root)
                held = next(r for r in GM.by_id(goals, "g1")["todo_items"]
                            if r["text"] == "Drop the 2k char cap")
                self.assertEqual(
                    [{"state": "done",
                      "note": "truncation still happens in the subagent path"}],
                    held.get("history"))
            finally:
                browser.close()

    def settled(self):
        # The build process this page started, ended: a row cannot be
        # reopened into a session that is still writing.
        run = BUILD._run_for(self.session, self.root, "g1")
        for _ in range(200):
            if run is None or not run.alive():
                return True
            time.sleep(0.05)
        return False

    def test_the_dash_sits_where_the_caret_does_before_a_character_is_typed(self):
        # An empty row's text box has no line box of its own, so baseline
        # alignment falls back to its bottom edge and the gutter dash drops
        # ~8px -- then jumps back up on the first keystroke. The dash must
        # sit at the same height empty as it does with text in the row.
        from playwright.sync_api import sync_playwright
        measure = """() => {
          const row = document.querySelector('.hc-todo-row');
          const dash = row.querySelector('.hc-todo-dash').getBoundingClientRect();
          const line = row.querySelector('.hc-todo-line').getBoundingClientRect();
          return {drop: dash.top - line.top, height: row.getBoundingClientRect().height};
        }"""
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                empty = page.evaluate(measure)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("ship it")
                typed = page.evaluate(measure)
                self.assertAlmostEqual(typed["drop"], empty["drop"], delta=0.6,
                                       msg="the dash moves when the row is typed into")
                self.assertAlmostEqual(typed["height"], empty["height"], delta=0.6,
                                       msg="the empty row is taller than a typed one")
            finally:
                browser.close()

    def test_the_prompt_tab_prints_the_context_the_build_opens_on(self):
        # The tab is the prompt and nothing else: the project, the goal tree
        # and this goal's rows, printed whole rather than assembled out of
        # sight behind a box to type in.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("ship it")
                page.wait_for_timeout(1500)
                page.locator(".hc-rail-tabs").get_by_text("Prompt", exact=True).click()
                body = page.locator(".hc-rail-ctx-body")
                expect(body).to_contain_text("# Current goals for this Claude chat",
                                             timeout=15000)
                expect(body).to_contain_text("Ship the router")
                expect(body).to_contain_text("ship it")
                expect(body).to_contain_text("# How to work")
                # It folds away by its own header, and comes back the same way
                head = page.locator(".hc-rail-ctx-head")
                head.click()
                expect(body).to_be_hidden()
                head.click()
                expect(body).to_be_visible()
            finally:
                browser.close()

    def test_an_unbuilt_row_carries_no_price_but_the_prompt_tab_still_counts(self):
        # A row's corner used to guess at what its build would cost, before
        # the build; the number beside a row still being written was the
        # wrong place for a guess, and the guess is now the build's own,
        # on the watch panel once it has been asked for. The Prompt tab
        # still counts the string a build opens on, since that is a
        # measurement of a string that exists.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.wait_for_timeout(1500)
                corner = page.locator(".hc-todo-cost").first
                page.locator(".hc-rail-tabs").get_by_text("Prompt", exact=True).click()
                note = page.locator(".hc-rail-ctx-note")
                expect(note).to_contain_text("tok", timeout=15000)
                self.assertGreater(tokens_in(note.inner_text()), 0)
                expect(corner).to_have_text("")
                expect(corner).to_be_hidden()
            finally:
                browser.close()

    def test_the_prompt_tab_is_a_prompt_to_read_not_a_box_to_type_in(self):
        # The tab used to be a textarea for the reader's own paragraph, with
        # the assembled prompt printed above it and a Generate that filled the
        # box. Two halves of one string, and the half that mattered was the
        # one nobody could edit. Only the prompt is left -- and Copy takes it.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-rail-tabs", timeout=15000)
                page.locator(".hc-rail-tabs").get_by_text("Prompt", exact=True).click()
                body = page.locator(".hc-rail-ctx-body")
                expect(body).to_contain_text("# How to work", timeout=15000)
                expect(page.locator("textarea.hc-rail-code")).to_have_count(0)
                expect(page.locator(".hc-rail-generate")).to_have_count(0)
                expect(page.locator(".hc-rail-copy")).to_be_visible()
            finally:
                browser.close()

    def test_the_terminal_sits_under_the_watch_line_and_not_across_it(self):
        # The state line is where the build says how long it has been going
        # and what it has spent; a button beside it took the width those
        # numbers needed. Terminal opens from the foot of the panel instead,
        # and Log -- which is about the panel itself -- stays on the line.
        os.environ["STUB_HOLD"] = "8"
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
                self.go(page)
                head = page.locator(".hc-todo-watch-head")
                expect(head).to_be_visible(timeout=10_000)
                expect(head.locator("[data-hc-todo-log]")).to_have_count(1)
                expect(head.locator("[data-hc-todo-term]")).to_have_count(0)
                foot = page.locator(".hc-todo-watch-foot")
                expect(foot.locator("[data-hc-todo-term]")).to_have_text(
                    "Terminal", timeout=10_000)
                # The panel's foot is below its line, not beside it.
                line = page.locator(".hc-todo-watch-meta").bounding_box()
                under = foot.bounding_box()
                self.assertGreater(under["y"], line["y"] + line["height"] - 1)
                # And the line the terminal made room for says the tokens.
                expect(page.locator(".hc-todo-watch-meta")).to_contain_text("tok")
            finally:
                browser.close()

    def test_the_watch_panel_can_be_dismissed_and_stays_gone_for_that_build(self):
        # The log folds; the panel goes. What it holds once a build has
        # finished is a warning to restart the program by hand -- read, done,
        # and then in the way -- so the head carries an × that takes the
        # whole panel, and a reload does not bring it back.
        os.environ["STUB_HOLD"] = "8"
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.wait_for_timeout(1200)
                page.locator(".hc-todo-build").click()
                self.go(page)
                panel = page.locator(".hc-todo-watch")
                expect(panel).to_be_visible(timeout=10_000)
                page.locator("[data-hc-todo-watch-hide]").click()
                expect(panel).to_be_hidden()
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.wait_for_timeout(2000)
                expect(page.locator(".hc-todo-watch")).to_be_hidden()
            finally:
                browser.close()

    def test_a_dragged_divider_follows_the_pointer_and_is_stored_once(self):
        # The pointer reports far faster than the screen redraws, and the
        # store is a synchronous write: a drag that wrote it per report ran
        # behind the pointer. The width follows the pointer while the button
        # is down and reaches the store once, when it comes up.
        from playwright.sync_api import sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-rail-left", timeout=15000)
                page.evaluate(
                    "() => { window.__hcSaves = 0;"
                    "  const put = localStorage.setItem.bind(localStorage);"
                    "  localStorage.setItem = (k, v) => {"
                    "    if (k === 'hc-launch-layout-v2') window.__hcSaves++;"
                    "    return put(k, v); }; }")
                rail = page.locator(".hc-rail-left").bounding_box()
                width = lambda: page.evaluate(
                    "() => parseInt(getComputedStyle(document.documentElement)"
                    "  .getPropertyValue('--hc-left'), 10)")
                before = width()
                page.mouse.move(rail["x"] + rail["width"], rail["y"] + 200)
                page.mouse.down()
                page.mouse.move(rail["x"] + rail["width"] - 90,
                                rail["y"] + 200, steps=12)
                page.wait_for_timeout(120)
                dragged = width()
                self.assertLess(dragged, before - 60,
                                "the rail follows the pointer while dragging")
                self.assertEqual(0, page.evaluate("() => window.__hcSaves"),
                                 "nothing is written to the store mid-drag")
                page.mouse.up()
                page.wait_for_timeout(60)
                self.assertEqual(1, page.evaluate("() => window.__hcSaves"))
                self.assertEqual(
                    dragged,
                    page.evaluate(
                        "() => JSON.parse(localStorage"
                        "  .getItem('hc-launch-layout-v2')).left"))
            finally:
                browser.close()

    def test_the_tabs_keep_their_line_when_a_row_is_typed(self):
        # The header is the rail's navigation. A control that appeared there
        # the moment a TODO was written pushed the four tabs onto a second
        # line under the reader's pointer; Copy sits above the rows instead.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                tabs = page.locator(".hc-rail-tabs")
                before = tabs.bounding_box()
                copy = page.locator(".hc-todos-top .hc-todo-copy")
                expect(copy).to_have_text("Copy all")
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Add the route")
                page.wait_for_timeout(1200)
                after = tabs.bounding_box()
                self.assertEqual(round(before["height"]), round(after["height"]),
                                 "the tab row must not grow a second line")
                self.assertEqual(round(before["y"]), round(after["y"]))
                # Every tab is still on that one line, and none of them wrapped.
                for name in ("TODOs", "Notes", "Prompt", "Understanding"):
                    box = tabs.get_by_text(name, exact=True).bounding_box()
                    self.assertLess(box["height"], after["height"] + 1, name)
                expect(page.locator(".hc-rail-select")).to_have_count(0)
            finally:
                browser.close()

    def test_enter_on_a_building_row_opens_a_note_pane_and_the_build_is_told(self):
        # Enter on a row the build is on is not a new row: it opens the pane
        # under the row, and what is typed there goes to the build's session
        # -- the process ended and resumed on it -- which then carries on.
        if BUILD.mode() == "session":
            self.skipTest("a headless build's process is the one told mid-work")
        os.environ["STUB_HOLD"] = "8"
        log = self.root / "stub.log"
        os.environ["STUB_LOG"] = str(log)
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
                self.go(page)
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("building", timeout=10_000)
                self.tile(page, "Add the route").locator(".hc-todo-line").click()
                page.keyboard.press("Enter")
                pane = page.locator(".hc-todo-note-pane")
                expect(pane).to_be_visible()
                expect(pane).to_contain_text("Anything to add")
                # The row was not split: one row reads "Add the route".
                self.assertEqual(1, len([r for r in self.rows()
                                         if r[0] == "Add the route"]))
                page.keyboard.type("use the v2 router")
                page.keyboard.press("Enter")
                expect(page.locator(".hc-todo-note-pane")).to_have_count(0)
                deadline = time.time() + 10
                while time.time() < deadline:
                    if log.exists() and len(log.read_text().splitlines()) == 2:
                        break
                    time.sleep(0.1)
                lines = [json.loads(l) for l in log.read_text().splitlines()]
                self.assertEqual(2, len(lines), lines)
                self.assertTrue(lines[1]["resume"])
                self.assertIn("added context to a TODO row", lines[1]["prompt"])
                self.assertIn('"use the v2 router"', lines[1]["prompt"])
                # And the build went on to its end.
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("done", timeout=10_000)
                expect(page.locator(".hc-todo-error")).to_be_hidden()
            finally:
                browser.close()


    def test_a_finished_build_is_checked_for_a_restart_and_the_prompt_is_on_the_rail(self):
        # The rows land done; the same session is then asked, on sonnet at
        # high effort, whether the program needs a local restart. The rail
        # shows the check under the rows while it runs, then -- the stub
        # says yes -- the reason and the exact prompt to paste, with a copy
        # button; and a banner that asks for a hand and stays up until it
        # gets one.
        from playwright.sync_api import expect, sync_playwright
        os.environ["HC_BUILD_RESTART_CHECK"] = "1"
        os.environ["STUB_HOLD"] = "1"
        os.environ["STUB_RESTART"] = "yes"
        os.environ["STUB_CHECK_HOLD"] = "4"
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-todo-line", timeout=15000)
                page.locator(".hc-todo-line").first.click()
                page.keyboard.type("Cache goals.md per session")
                page.wait_for_timeout(1200)
                page.locator(".hc-todo-dash").first.click()
                page.locator(".hc-todo-build").click()
                self.go(page)
                expect(page.locator(".hc-todo-status").first
                       ).to_have_text("done", timeout=15_000)
                checking = page.locator(".hc-todo-restart[data-hc-todo-restart=\"checking\"]")
                expect(checking).to_be_visible(timeout=10_000)
                expect(checking).to_contain_text("sonnet · effort high")
                expect(checking).to_contain_text(
                    "checking whether these changes go stale without a local restart")
                meta = page.locator(".hc-todo-watch-meta")
                expect(meta).to_contain_text("finished")
                expect(meta).not_to_contain_text("calculating")
                yes = page.locator(".hc-todo-restart[data-hc-todo-restart=\"yes\"]")
                expect(yes).to_be_visible(timeout=15_000)
                expect(checking).to_have_count(0)
                expect(yes).to_contain_text(
                    "the session-cache change lives in a long-running process")
                expect(yes).to_contain_text("send to local claude code")
                expect(yes.locator(".hc-todo-restart-prompt")).to_contain_text(
                    "re-run `goals-ui serve --dev`")
                banner = page.locator(".hc-alert[data-hc-alert-kind=\"restart\"]")
                expect(banner).to_be_visible(timeout=10_000)
                expect(banner).to_contain_text("Restart needed")
                expect(banner).to_contain_text(
                    "the session-cache change lives in a long-running process")
                # Past the six seconds a finish banner gets: still up.
                page.wait_for_timeout(7000)
                expect(banner).to_be_visible()
                # Copy puts the prompt on the clipboard, verbatim.
                yes.locator("[data-hc-todo-restart-copy]").click()
                expect(yes.locator("[data-hc-todo-restart-copy]")).to_have_text("copied ✓")
                prompt = page.evaluate("() => navigator.clipboard.readText()")
                self.assertTrue(prompt.startswith("Restart the goals-ui dev process"), prompt)
                self.assertTrue(prompt.endswith("re-run `goals-ui serve --dev`."), prompt)
                # The notice's own × puts it away -- for good, not until the
                # next poll repaints the panel over it. What it sat under, the
                # build's state line, stays: that is still worth reading.
                # Pressed on a panel that has settled: the build is over, its
                # numbers have stopped moving, and nothing but the dismissal
                # itself is left to tell the panel it must be redrawn.
                page.wait_for_timeout(3000)
                settled = page.locator(".hc-todo-watch-meta").inner_text()
                yes.locator("[data-hc-todo-restart-hide]").click()
                expect(page.locator(".hc-todo-restart")).to_have_count(0, timeout=5_000)
                page.wait_for_timeout(3000)
                expect(page.locator(".hc-todo-restart")).to_have_count(0)
                expect(page.locator(".hc-todo-watch-head")).to_be_visible()
                self.assertEqual(settled,
                                 page.locator(".hc-todo-watch-meta").inner_text())
                # The row is the build's verdict, not the check's: still done.
                self.assertEqual(["done"], [r[2] for r in self.rows()])
                # Dismissing the banner marks it read; it does not come back.
                banner.locator(".hc-alert-close").click()
                expect(banner).to_have_count(0)
            finally:
                browser.close()


    def understanding(self):
        goals, _ = chat_state.load_goals(self.session, self.root)
        return GM.by_id(goals, "g1")["understanding"]

    def test_the_understanding_tab_keeps_a_scenario_and_its_questions(self):
        # The rail's middle tab: what this goal's work is for, and what the
        # reader wants answered about it. Both are kept on the goal and both
        # open every build of its rows -- which is what the Prompt tab, one
        # click away, is checked for here.
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-rail-tabs", timeout=15000)
                page.locator(".hc-rail-tabs").get_by_text(
                    "Understanding", exact=True).click()
                scenario = page.locator(".hc-understand-scenario")
                expect(scenario).to_be_visible()
                scenario.fill("Two people work one tree from two machines.")
                # One box is always on offer; the link under it makes more.
                expect(page.locator(".hc-understand-ask")).to_have_count(1)
                page.locator(".hc-understand-ask").first.fill(
                    "Who wins a conflict?")
                page.get_by_text("+ Add question", exact=True).click()
                expect(page.locator(".hc-understand-ask")).to_have_count(2)
                page.locator(".hc-understand-ask").nth(1).fill(
                    "Where do invites live?")
                # Each question is a TODO-shaped bullet, not a box: a dash in
                # the gutter and the words beside it, no border of their own.
                # The scenario above them is still a box -- it is one piece of
                # prose, not one of a list.
                expect(page.locator(".hc-understand-bullet")).to_have_count(2)
                self.assertEqual(
                    ["-", "-"],
                    page.locator(".hc-understand-bullet").all_inner_texts())
                self.assertEqual("0px", page.locator(".hc-understand-ask")
                                 .first.evaluate("n => getComputedStyle(n)"
                                                 ".borderBottomWidth"))
                self.assertNotEqual(
                    "0px", page.locator(".hc-understand-scenario")
                    .evaluate("n => getComputedStyle(n).borderBottomWidth"))
                page.wait_for_timeout(1500)
                held = self.understanding()
                self.assertEqual("Two people work one tree from two machines.",
                                 held["scenario"])
                self.assertEqual(["Who wins a conflict?",
                                  "Where do invites live?"],
                                 [q["text"] for q in held["questions"]])
                # What a build of this goal's rows would open on.
                page.locator(".hc-rail-tabs").get_by_text(
                    "Prompt", exact=True).click()
                body = page.locator(".hc-rail-ctx-body")
                expect(body).to_contain_text("# The scenario this goal is for",
                                             timeout=15000)
                expect(body).to_contain_text("Two people work one tree")
                expect(body).to_contain_text("- Who wins a conflict?")
                # A question dropped is dropped on the server too, and now
                # rather than 600ms after the page has gone.
                page.locator(".hc-rail-tabs").get_by_text(
                    "Understanding", exact=True).click()
                page.locator(".hc-understand-drop").first.click()
                page.wait_for_timeout(1000)
                self.assertEqual(
                    ["Where do invites live?"],
                    [q["text"] for q in self.understanding()["questions"]])
            finally:
                browser.close()

    def seed_answer(self, answer):
        """g1 with one question already answered, so the column has prose."""
        goals, _ = chat_state.load_goals(self.session, self.root)
        goal = GM.by_id(goals, "g1")
        goal["understanding"] = {
            "scenario": "The preview pane and the build pane disagree.",
            "shots": [],
            "questions": [{"id": "qaaaa0001",
                           "text": "Do the two panes share staleness?",
                           "thread": [{"q": "Do the two panes share staleness?",
                                       "a": answer}]}]}
        GM.sanitize(goals)
        chat_state.paths(self.session, self.root).goals.write_text(
            json.dumps(goals))

    def test_the_send_foot_reaches_the_bottom_of_the_column_it_pins_over(self):
        # A sticky foot is held inside its containing block, which is the
        # column's *content* box -- so a bottom padding on the column left a
        # strip of the answer sliding along under the Ask Claude button, and
        # the band the foot covered read as a hole torn in the prose.
        from playwright.sync_api import sync_playwright
        self.seed_answer("A long answer. " + ("word " * 400))
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                self.open_understanding(page, url)
                page.wait_for_selector(".hc-understand-answer", timeout=15000)
                page.evaluate(
                    "() => { document.querySelector('.hc-rail-understand')"
                    ".scrollTop = 200; }")
                page.wait_for_timeout(300)
                seen = page.evaluate("""() => {
                  const box = document.querySelector('.hc-rail-understand');
                  const foot = document.querySelector('.hc-understand-foot');
                  const b = box.getBoundingClientRect();
                  const f = foot.getBoundingClientRect();
                  return {below: (b.top + box.clientHeight) - f.bottom,
                          room: box.scrollHeight - box.clientHeight};
                }""")
                # The column really is scrolling -- otherwise nothing is
                # pinned and the check below is vacuous.
                self.assertGreater(seen["room"], 0, seen)
                self.assertLessEqual(abs(seen["below"]), 1, seen)
            finally:
                browser.close()

    def test_an_answer_is_read_as_markdown_and_not_as_its_source(self):
        # What Claude writes back is markdown, and printing the source of it
        # put the punctuation into the sentence: `preview.py` with its
        # backticks, ** around a name, a - in front of every list line.
        from playwright.sync_api import expect, sync_playwright
        self.seed_answer(
            "They do **not** share it, by the note at `preview.py:225`.\n"
            "\n"
            "- the source is hashed\n"
            "- `package.json` is not\n"
            "\n"
            "```python\n"
            "def fingerprint(path):\n"
            "    return sha(path)\n"
            "```\n")
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                self.open_understanding(page, url)
                answer = page.locator(".hc-understand-answer")
                expect(answer).to_be_visible(timeout=15000)
                expect(answer.locator("strong")).to_have_text("not")
                expect(answer.locator("code.hc-md-code").first).to_have_text(
                    "preview.py:225")
                expect(answer.locator(".hc-md-item")).to_have_count(2)
                expect(answer.locator("pre.hc-md-pre")).to_contain_text(
                    "def fingerprint(path):")
                # And none of the markup is left standing in the prose.
                shown = answer.inner_text()
                self.assertNotIn("`", shown)
                self.assertNotIn("**", shown)
            finally:
                browser.close()

    def test_the_scenario_comes_back_after_a_reload(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector(".hc-rail-tabs", timeout=15000)
                page.locator(".hc-rail-tabs").get_by_text(
                    "Understanding", exact=True).click()
                page.locator(".hc-understand-scenario").fill(
                    "The invite link is the whole of the onboarding.")
                page.wait_for_timeout(1500)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector(".hc-rail-tabs", timeout=15000)
                page.locator(".hc-rail-tabs").get_by_text(
                    "Understanding", exact=True).click()
                expect(page.locator(".hc-understand-scenario")).to_have_value(
                    "The invite link is the whole of the onboarding.",
                    timeout=15000)
            finally:
                browser.close()

    def open_understanding(self, page, url):
        from playwright.sync_api import expect
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(".hc-rail-tabs", timeout=15000)
        page.locator(".hc-rail-tabs").get_by_text(
            "Understanding", exact=True).click()
        expect(page.locator(".hc-understand-scenario")).to_be_visible()

    def test_a_question_is_answered_in_prose_and_followed_up(self):
        # The model is answered for here: what is under test is the tab --
        # that a question goes with the scenario it is about, that the answer
        # comes back under it as it was written, and that a follow-up is
        # asked with the answer above it.
        from playwright.sync_api import expect, sync_playwright
        asked = []

        def answer(route):
            asked.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "ok": True,
                              "asked": asked[-1]["question"],
                              "answer": "Both write the tree whole, so the"
                                        " later one wins."}))

        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.route("**/api/ask_scenario", answer)
                self.open_understanding(page, url)
                page.locator(".hc-understand-scenario").fill(
                    "Two people work one tree from two machines.")
                page.locator(".hc-understand-ask").first.fill(
                    "Who wins a conflict?")
                page.get_by_text("Ask Claude", exact=True).click()
                expect(page.locator(".hc-understand-answer")).to_contain_text(
                    "the later one wins", timeout=15000)
                # The question travels with the scenario it is about.
                self.assertEqual("Two people work one tree from two machines.",
                                 asked[0]["scenario"])
                self.assertEqual("Who wins a conflict?", asked[0]["question"])
                self.assertEqual([], asked[0]["turns"])
                # And the answer is the goal's now, not the panel's.
                page.wait_for_timeout(1500)
                thread = self.understanding()["questions"][0]["thread"]
                self.assertEqual(1, len(thread))
                self.assertIn("the later one wins", thread[0]["a"])
                # A follow-up is asked with what was already said.
                page.locator(".hc-understand-follow").fill(
                    "And if both are offline?")
                page.get_by_text("Follow up", exact=True).click()
                expect(page.locator(".hc-understand-answer")).to_have_count(
                    2, timeout=15000)
                self.assertEqual("And if both are offline?",
                                 asked[1]["question"])
                self.assertEqual(["Who wins a conflict?"],
                                 [t["q"] for t in asked[1]["turns"]])
                page.wait_for_timeout(1500)
                self.assertEqual(
                    2, len(self.understanding()["questions"][0]["thread"]))
                # What a build of this goal's rows would open on.
                page.locator(".hc-rail-tabs").get_by_text(
                    "Prompt", exact=True).click()
                expect(page.locator(".hc-rail-ctx-body")).to_contain_text(
                    "the later one wins", timeout=15000)
            finally:
                browser.close()

    def test_answers_landing_say_so_once_for_the_whole_send(self):
        # The tab is not somewhere to sit and wait: an answer takes as long as
        # a build does, and the reader who asked has gone back to the rows.
        # One banner for the send, naming the goal, and clicking it comes back
        # to the tab the answers are written under.
        from playwright.sync_api import expect, sync_playwright
        asked = []

        def answer(route):
            asked.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "ok": True, "asked": asked[-1]["question"],
                              "answer": "The later write wins."}))

        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.route("**/api/ask_scenario", answer)
                self.open_understanding(page, url)
                # The banner sits over the header; the clock it runs on is
                # what the assertions below would otherwise race.
                page.evaluate(
                    "() => window.__hcPromptUI.alerts.setSettings("
                    "  { seconds: 120 })")
                page.locator(".hc-understand-scenario").fill(
                    "Two people work one tree from two machines.")
                page.locator(".hc-understand-ask").first.fill(
                    "Who wins a conflict?")
                page.get_by_text("+ Add question", exact=True).click()
                page.locator(".hc-understand-ask").nth(1).fill(
                    "Where do invites live?")
                # The reader leaves for the rows and the send answers behind
                # them; the banner is what tells them it is back. The tab is
                # clicked on the node rather than at its coordinates -- the
                # banner is drawn over the header the tabs are in.
                page.get_by_text("Ask Claude", exact=True).click()
                page.eval_on_selector(
                    ".hc-rail-tabs",
                    "el => Array.prototype.slice.call(el.children).filter("
                    "  c => c.textContent.trim() === 'TODOs')[0].click()")
                banner = page.locator(".hc-alert[data-hc-alert-kind=\"understood\"]")
                expect(banner).to_have_count(1, timeout=15000)
                expect(banner).to_contain_text("Understanding answered")
                expect(banner).to_contain_text("2 answers")
                self.assertEqual(2, len(asked))
                # And it is the way back: to the tab the answers are under,
                # not to the rows.
                banner.locator(".hc-alert-detail").click()
                expect(page.locator(".hc-understand-answer")).to_have_count(
                    2, timeout=15000)
                expect(banner).to_have_count(0)
            finally:
                browser.close()

    def test_a_send_asks_its_questions_in_the_order_they_are_listed(self):
        # Sent together, the answers came back in whatever order they happened
        # to be written in: the tab filled itself in from the middle and the
        # reader could not tell what was still coming. Each question now waits
        # for the one above it, so the second is not even asked until the
        # first has been answered.
        from playwright.sync_api import expect, sync_playwright
        asked = []
        held = []

        def answer(route):
            asked.append(json.loads(route.request.post_data))
            if len(asked) == 1:
                # Kept open: what the second question does while the first is
                # out is the whole of what is under test.
                held.append(route)
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "ok": True, "asked": asked[-1]["question"],
                              "answer": "Invites live in the workspace."}))

        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.route("**/api/ask_scenario", answer)
                self.open_understanding(page, url)
                page.locator(".hc-understand-scenario").fill(
                    "Two people work one tree from two machines.")
                page.locator(".hc-understand-ask").first.fill(
                    "Who wins a conflict?")
                page.get_by_text("+ Add question", exact=True).click()
                page.locator(".hc-understand-ask").nth(1).fill(
                    "Where do invites live?")
                page.get_by_text("Ask Claude", exact=True).click()
                # One question out, and it is the first one listed. The second
                # is not out with it, however long we wait.
                expect(page.locator(".hc-understand-send")).to_have_attribute(
                    "data-hc-busy", "", timeout=15000)
                page.wait_for_timeout(2000)
                self.assertEqual(["Who wins a conflict?"],
                                 [row["question"] for row in asked])
                # It is waiting, not forgotten: one question out, one behind
                # it, and the send busy until both have had their turn.
                state = page.evaluate(
                    "() => window.__hcPromptUI.understandState()")
                self.assertEqual(1, len(state["asking"]))
                self.assertEqual(1, len(state["queued"]))
                held[0].fulfill(status=200,
                                content_type="application/json",
                                body=json.dumps({
                                    "ok": True,
                                    "asked": "Who wins a conflict?",
                                    "answer": "The later write wins."}))
                # The first answer lands, and only then does the second
                # question go -- so the answers appear down the list, in the
                # order the questions are written in.
                expect(page.locator(".hc-understand-answer")).to_have_count(
                    2, timeout=15000)
                self.assertEqual(["Who wins a conflict?",
                                  "Where do invites live?"],
                                 [row["question"] for row in asked])
                page.wait_for_timeout(1500)
                held_now = self.understanding()["questions"]
                self.assertEqual(
                    [["The later write wins."],
                     ["Invites live in the workspace."]],
                    [[turn["a"] for turn in q["thread"]] for q in held_now])
            finally:
                browser.close()

    def test_a_screenshot_pasted_into_a_question_hangs_on_that_question(self):
        # What is being asked about is often quicker shown than described. The
        # image goes on the question it was pasted into -- not on the scenario
        # above it -- and travels with that question when it is asked.
        from playwright.sync_api import expect, sync_playwright
        asked = []

        def answer(route):
            asked.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "ok": True, "asked": asked[-1]["question"],
                              "answer": "That is the done band."}))

        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.route("**/api/ask_scenario", answer)
                self.open_understanding(page, url)
                page.locator(".hc-understand-scenario").fill(
                    "Two people work one tree from two machines.")
                page.locator(".hc-understand-ask").first.fill(
                    "What is this panel showing?")
                page.locator(".hc-understand-ask").first.click()
                page.evaluate(
                    "() => { const bytes = new Uint8Array([137,80,78,71,13,10,26,10,0,0,0,0]);"
                    " const dt = new DataTransfer();"
                    " dt.items.add(new File([bytes], 'Rail.png', {type: 'image/png'}));"
                    " document.activeElement.dispatchEvent(new ClipboardEvent('paste',"
                    "   {clipboardData: dt, bubbles: true, cancelable: true})); }")
                expect(page.locator(".hc-understand-shot")).to_have_count(
                    1, timeout=15000)
                # Under the question, not on the scenario's own strip.
                expect(page.locator(".hc-understand-qshots"
                                    " .hc-understand-shot")).to_contain_text(
                    "Rail.png")
                page.wait_for_timeout(1500)
                held = self.understanding()
                self.assertEqual([], held["shots"])
                shots = held["questions"][0]["shots"]
                self.assertEqual(1, len(shots))
                path = Path(shots[0]["path"])
                self.assertEqual(self.trajdir.resolve() / "attachments",
                                 path.parent)
                self.assertEqual(b"\x89PNG", path.read_bytes()[:4])
                # And it goes with the question when it is asked.
                page.get_by_text("Ask Claude", exact=True).click()
                expect(page.locator(".hc-understand-answer")).to_have_count(
                    1, timeout=15000)
                self.assertEqual([str(path)],
                                 [str(s["path"]) for s in asked[0]["shots"]])
                # Dropped off the question, and dropped on the server too.
                page.locator(".hc-understand-qshots"
                             " [data-hc-understand-shot-rm]").click()
                expect(page.locator(".hc-understand-shot")).to_have_count(0)
                page.wait_for_timeout(1500)
                self.assertEqual(
                    [], self.understanding()["questions"][0]["shots"])
            finally:
                browser.close()

    def test_a_pasted_screenshot_is_kept_with_the_scenario(self):
        from playwright.sync_api import expect, sync_playwright
        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                self.open_understanding(page, url)
                page.locator(".hc-understand-scenario").click()
                # The paste event a screenshot fires, with the same payload:
                # playwright cannot put an image on the real clipboard.
                page.evaluate(
                    "() => { const bytes = new Uint8Array([137,80,78,71,13,10,26,10,0,0,0,0]);"
                    " const dt = new DataTransfer();"
                    " dt.items.add(new File([bytes], 'Rail.png', {type: 'image/png'}));"
                    " document.activeElement.dispatchEvent(new ClipboardEvent('paste',"
                    "   {clipboardData: dt, bubbles: true, cancelable: true})); }")
                expect(page.locator(".hc-understand-shot")).to_have_count(
                    1, timeout=15000)
                expect(page.locator(".hc-understand-shot")).to_contain_text(
                    "Rail.png")
                # The bytes are on disk, under this workspace, and the
                # scenario cites the file rather than holding the image.
                page.wait_for_timeout(1500)
                shots = self.understanding()["shots"]
                self.assertEqual(1, len(shots))
                path = Path(shots[0]["path"])
                self.assertEqual(self.trajdir.resolve() / "attachments",
                                 path.parent)
                self.assertEqual(b"\x89PNG", path.read_bytes()[:4])
                # The screenshot is the reader's own material: it stays beside
                # what they type, and a build of the goal's rows opens on it.
                page.locator(".hc-understand-scenario").fill(
                    "Two people edit one goal tree from two machines at once.")
                page.wait_for_timeout(1500)
                self.assertEqual(
                    "Two people edit one goal tree from two machines at once.",
                    self.understanding()["scenario"])
                page.locator(".hc-rail-tabs").get_by_text(
                    "Prompt", exact=True).click()
                expect(page.locator(".hc-rail-ctx-body")).to_contain_text(
                    str(path), timeout=15000)
            finally:
                browser.close()

    def test_rough_words_are_shaped_and_what_they_left_out_is_asked_back(self):
        # The reader writes the situation however they write it and the form
        # is put on it here. What their words did not say comes back as an
        # empty keyword and a line under the box -- theirs to fill, not the
        # model's to invent, because this field opens every build.
        from playwright.sync_api import expect, sync_playwright
        sent = []

        def shape(route):
            sent.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "ok": True,
                              "scenario": "GIVEN two people share one tree\n"
                                          "WHEN both save\nTHEN",
                              "asks": ["which save should win?"],
                              "shots": []}))

        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.route("**/api/draft_scenario", shape)
                self.open_understanding(page, url)
                page.locator(".hc-understand-scenario").fill(
                    "two ppl one tree, both save")
                # The control says what it does, not what form it does it in:
                # the reader is never told to write GIVEN / WHEN / THEN.
                self.assertEqual(0, page.locator(".hc-rail-understand")
                                 .get_by_text("GIVEN", exact=False).count())
                page.get_by_text("Shape it", exact=True).click()
                expect(page.locator(".hc-understand-scenario")).to_have_value(
                    "GIVEN two people share one tree\nWHEN both save\nTHEN",
                    timeout=15000)
                expect(page.locator(".hc-understand-blank")).to_contain_text(
                    "which save should win?")
                # Their own words are what was shaped, not a description of
                # them -- and the shaped lines are the goal's now.
                self.assertEqual("two ppl one tree, both save",
                                 sent[0]["text"])
                page.wait_for_timeout(1500)
                self.assertEqual(
                    "GIVEN two people share one tree\nWHEN both save\nTHEN",
                    self.understanding()["scenario"])
            finally:
                browser.close()

    def test_a_scenario_that_will_not_shape_leaves_the_box_alone(self):
        from playwright.sync_api import expect, sync_playwright

        def shape(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "ok": False,
                              "error": "that did not map onto GIVEN / WHEN /"
                                       " THEN -- write it here"}))

        with server_for(self.trajdir) as url, sync_playwright() as pw:
            browser, page = self.open(pw)
            try:
                page.route("**/api/draft_scenario", shape)
                self.open_understanding(page, url)
                page.locator(".hc-understand-scenario").fill("the thing broke")
                page.get_by_text("Shape it", exact=True).click()
                expect(page.locator(".hc-understand-err")).to_contain_text(
                    "did not map", timeout=15000)
                expect(page.locator(".hc-understand-scenario")).to_have_value(
                    "the thing broke")
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
    # they do not build; skip the build ones.
    def test_picked_rows_build_ask_and_finish_on_the_answer(self):
        self.skipTest("headless-only")

    def test_build_starts_the_build_without_asking_first(self):
        # In session mode a build is queued for the connected session's next
        # turn, so "needs you" -- the stub's question -- never comes on its
        # own; the session-mode path is covered by the queued/building test.
        self.skipTest("headless-only")

    def test_one_cmd_enter_builds_what_is_unsent_and_the_page_stays(self):
        self.skipTest("headless-only")

    def test_a_done_row_is_clicked_open_and_sent_back_out_with_the_note(self):
        # Nothing here reaches "done" without a build; the session mode's own
        # reopen is held by test_build_runs.SessionBuildTests.
        self.skipTest("headless-only")

    def test_a_build_pressed_mid_save_still_carries_the_row_it_names(self):
        self.skipTest("headless-only")

    def test_the_terminal_sits_under_the_watch_line_and_not_across_it(self):
        # The panel being placed is a headless build's; a queued row has no
        # run to watch, so there is no line and no terminal to sit under it.
        self.skipTest("headless-only")

    def test_the_watch_panel_can_be_dismissed_and_stays_gone_for_that_build(self):
        # Same reason: a queued row has no run, so there is no panel to
        # dismiss until the session takes it.
        self.skipTest("headless-only")

    def test_a_finished_build_is_checked_for_a_restart_and_the_prompt_is_on_the_rail(self):
        # The check is a second process on the build's session; there is no
        # process in session mode.
        self.skipTest("headless-only")

    def test_a_row_out_with_the_builder_comes_back_on_escape_or_its_corner(self):
        # In session mode the row waits in the queue: cancelling it from the
        # caret takes it out of the queue and back to active.
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
                expect(page.locator(".hc-todo-status").first).to_have_text("queued", timeout=10_000)
                expect(page.locator(".hc-todo-cancel")).to_have_count(1)
                self.assertEqual(1, len(BUILD.pending(self.session, self.root)))
                # From the caret inside the queued row -- not the empty row
                # the Build left above it for the next TODO.
                self.tile(page, "Add the route").locator(".hc-todo-line").click()
                page.keyboard.press("Escape")
                expect(page.locator(".hc-todo-status")).to_have_count(0)
                expect(page.locator(".hc-todo-cancel")).to_have_count(0)
                page.wait_for_timeout(600)
                self.assertEqual([], BUILD.pending(self.session, self.root))
                self.assertEqual("", self.rows()[0][2])
                expect(page.locator(".hc-todo-error")).to_be_hidden()
            finally:
                browser.close()

if __name__ == "__main__":
    unittest.main()
