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

    def test_one_cmd_enter_builds_the_sole_row_and_the_page_stays(self):
        # Nothing picked and one row to pick: Cmd+Enter builds it -- once,
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
                # Two unsent rows is a choice: with nothing picked, Cmd+Enter
                # from the Build control's side of the page builds nothing.
                page.keyboard.type("Also this")
                page.wait_for_timeout(1200)
                page.locator(".hc-rail-select").click()  # picks both
                page.locator(".hc-rail-select").click()  # releases both; focus is off the list
                expect(page.locator(".hc-todo-build")).to_have_text("Build")
                page.keyboard.press("Meta+Enter")
                page.wait_for_timeout(600)
                # Nothing to build is nothing to price: no dialog either.
                expect(page.locator(".hc-ask")).to_have_count(0)
                self.assertEqual(["", ""], [r[2] for r in self.rows()
                                            if r[0] in ("Write the tests", "Also this")])
                # And with all three unsent rows picked ("Update the docs"
                # was never sent), from off the list: one press builds.
                page.locator(".hc-rail-select").click()
                expect(page.locator(".hc-todo-build")).to_have_text("Build 3")
                page.keyboard.press("Meta+Enter")
                self.go(page)
                expect(self.tile(page, "Also this").locator(".hc-todo-status")
                       ).to_have_text("done", timeout=10_000)
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

    def test_one_cmd_enter_builds_the_sole_row_and_the_page_stays(self):
        self.skipTest("headless-only")

    def test_a_done_row_is_clicked_open_and_sent_back_out_with_the_note(self):
        # Nothing here reaches "done" without a build; the session mode's own
        # reopen is held by test_build_runs.SessionBuildTests.
        self.skipTest("headless-only")

    def test_a_build_pressed_mid_save_still_carries_the_row_it_names(self):
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
