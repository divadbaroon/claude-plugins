"""The goal page: what a chat workspace opens on.

The page is the design's interaction model on local state. These tests
cover the half the server owns -- the root serves it, its files are served
by name and nothing else is, and the workspace it replaced still answers at
/legacy -- and, where a browser is available, the interactions themselves.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import ui  # noqa: E402

NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GOAL_DIR = ROOT / "hc" / "src" / "human_compact" / "trajectory" / "web" / "goal"


@contextmanager
def server_for(path, chat_scoped=True):
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
    ui._configure_server(server, Path(path), chat_scoped)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def fetch(url):
    """Status, headers and body of one GET; a 404 is an answer, not an error."""
    try:
        with NO_PROXY_OPENER.open(url, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, dict(error.headers), error.read()


def browser_executable():
    configured = os.environ.get("HC_TEST_BROWSER")
    if configured and Path(configured).is_file():
        return configured
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    mac_chrome = Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    return chrome or (str(mac_chrome) if mac_chrome.is_file() else None)


class GoalPageRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = Path(self.tmp.name) / "chat"
        self.chat.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_root_is_the_goal_page(self):
        with server_for(self.chat) as url:
            for path in ("/", "/index.html", "/?quick=1"):
                status, headers, body = fetch(url + path)
                text = body.decode("utf-8")
                self.assertEqual(200, status, path)
                self.assertTrue(headers["Content-Type"].startswith("text/html"), path)
                self.assertEqual("no-store, must-revalidate", headers["Cache-Control"])
                self.assertIn("<title>Engelbart</title>", text)
                self.assertIn('<script type="module" src="/goal/app.js"></script>', text)
                self.assertIn('<link rel="stylesheet" href="/goal/styles.css">', text)
                # Nothing of the workspace it replaced comes along.
                self.assertNotIn("bridge.js", text)
                self.assertNotIn("hc-preboot", text)
                self.assertNotIn("__bundler", text)

    def test_every_file_of_the_page_is_served_by_name(self):
        expected = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
        }
        files = sorted(p for p in GOAL_DIR.rglob("*")
                       if p.is_file() and p.suffix in expected)
        self.assertGreater(len(files), 10)
        with server_for(self.chat) as url:
            for file in files:
                name = file.relative_to(GOAL_DIR).as_posix()
                status, headers, body = fetch(f"{url}/goal/{name}")
                self.assertEqual(200, status, name)
                self.assertEqual(expected[file.suffix], headers["Content-Type"], name)
                self.assertEqual(file.read_bytes(), body, name)
                self.assertEqual("no-store, must-revalidate", headers["Cache-Control"])

    def test_nothing_outside_the_page_is_served(self):
        refused = [
            "/goal/",
            "/goal/missing.js",
            "/goal/components/",
            "/goal/components/missing.js",
            "/goal/../ui.py",
            "/goal/../setup.html",
            "/goal/components/../../setup.js",
            "/goal/%2e%2e/setup.html",
            "/goal/.hidden.js",
            "/goal/app.js.bak",
            "/goal/README.md",
            "/goal//app.js/../../setup.js",
        ]
        with server_for(self.chat) as url:
            for path in refused:
                status, _headers, body = fetch(url + path)
                self.assertEqual(404, status, path)
                self.assertIn(b"not found", body, path)

    def test_the_helper_refuses_every_shape_of_escape(self):
        for relpath in ("", "/", "..", "../ui.py", "goal/../../ui.py",
                        "/etc/passwd", "app.py", "styles.css/", ".env.js",
                        "app.js\x00.html"):
            self.assertIsNone(ui.goal_page_asset(relpath), relpath)
        served = ui.goal_page_asset("components/todos.js")
        self.assertIsNotNone(served)
        self.assertEqual("application/javascript; charset=utf-8", served[1])
        self.assertEqual((GOAL_DIR / "components" / "todos.js").read_bytes(), served[0])

    def test_the_legacy_workspace_still_answers_whole(self):
        with server_for(self.chat) as url:
            for path in ("/legacy", "/legacy/"):
                status, headers, body = fetch(url + path)
                text = body.decode("utf-8")
                self.assertEqual(200, status, path)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertIn('id="hc-preboot"', text)
                self.assertLess(text.index('script type="__bundler/template"'),
                                text.index('<script src="/bridge.js"></script>'))
                self.assertLess(text.index('<script src="/bridge.js"></script>'),
                                text.index("</body>"))
            status, _headers, body = fetch(url + "/bridge.js")
            self.assertEqual(200, status)
            self.assertIn(b"window.__hcServerStale", body)


class AccountRouteTests(unittest.TestCase):
    """What the page's loadAccount relies on: the machine's own account, as
    the installer wrote it, read by the server and never by the page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = Path(self.tmp.name) / "chat"
        self.chat.mkdir()
        self.home = Path(self.tmp.name) / "human-compact"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        with mock.patch.dict(os.environ, {"HUMAN_COMPACT_HOME": str(self.home)}):
            with server_for(self.chat) as url:
                _status, _headers, body = fetch(url + "/api/supabase")
        return json.loads(body)

    def test_a_machine_nobody_connected_says_so(self):
        answer = self.status()
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["connected"])
        self.assertEqual("", answer["email"])

    def test_a_connected_machine_answers_with_its_account(self):
        (self.home / "auth.json").write_text(json.dumps({
            "apiBase": "http://127.0.0.1:9", "token": "machine-token",
            "email": "someone@example.com"}), encoding="utf-8")
        answer = self.status()
        self.assertTrue(answer["connected"])
        self.assertEqual("someone@example.com", answer["email"])


class GoalPageModuleTests(unittest.TestCase):
    """The page's modules, read as files: what a browser would refuse."""

    IMPORT = re.compile(r'^import\s.*?\sfrom\s+"([^"]+)";', re.M)

    def modules(self):
        return sorted(GOAL_DIR.rglob("*.js"))

    def test_every_import_names_a_file_that_exists(self):
        for module in self.modules():
            specifiers = self.IMPORT.findall(module.read_text(encoding="utf-8"))
            for specifier in specifiers:
                self.assertTrue(specifier.startswith(("./", "../")),
                                f"{module.name} imports {specifier}: bare "
                                "specifiers do not resolve in a browser")
                target = (module.parent / specifier).resolve()
                self.assertTrue(target.is_file(), f"{module.name}: {specifier}")
                self.assertTrue(str(target).startswith(str(GOAL_DIR)), specifier)

    def test_the_html_names_only_files_the_server_has(self):
        html = (GOAL_DIR / "index.html").read_text(encoding="utf-8")
        for match in re.finditer(r'(?:src|href)="(/goal/[^"]+)"', html):
            self.assertTrue((GOAL_DIR / match.group(1)[len("/goal/"):]).is_file(),
                            match.group(1))

    def test_every_module_parses_as_an_es_module(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for module in self.modules():
            run = subprocess.run(
                [node, "--input-type=module", "--check", "-"],
                input=module.read_bytes(), capture_output=True, timeout=30,
            )
            self.assertEqual(0, run.returncode,
                             f"{module.relative_to(GOAL_DIR)}: "
                             f"{run.stderr.decode('utf-8', 'replace')}")


class GoalPageBrowserTests(unittest.TestCase):
    """The design's interactions, exercised on the page's local state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = Path(self.tmp.name) / "chat"
        self.chat.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_design_s_interactions_hold_on_local_state(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")
        active = re.compile(r"\bis-active\b")
        with server_for(self.chat) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text)
                        if message.type == "error" else None)
                page.goto(url, wait_until="domcontentloaded")

                # The goal, its breakdown, and the first subgoal selected
                # with its own two todos.
                expect(page.get_by_role("heading", name="Create an interface to import the dataset")).to_be_visible()
                subs = page.locator(".rail .sub")
                expect(subs).to_have_count(3)
                expect(subs.nth(0)).to_have_class(active)
                rows = page.locator(".todo-list .todo:not(.todo-new)")
                expect(rows).to_have_count(2)
                expect(page.get_by_role("button", name="Build all")).to_be_enabled()

                # Notes belong to the subgoal they were written on.
                page.locator(".notes-input").fill("first notes")
                subs.nth(1).click()
                expect(subs.nth(1)).to_have_class(active)
                expect(page.locator(".notes-input")).to_have_value("")
                # No todos yet, so the pane is folded away behind its button.
                expect(page.get_by_role("button", name="Show todos")).to_be_visible()
                expect(page.locator(".todo-list")).to_have_count(0)

                # Bart proposes a todo from the first message on an empty
                # subgoal, and Add puts it on the list.
                composer = page.get_by_label("Message Bart")
                composer.fill("I want to save the file as parquet")
                composer.press("Enter")
                expect(page.locator(".msg.from-you .bubble")).to_have_text("I want to save the file as parquet")
                expect(composer).to_have_value("")
                expect(page.locator(".proposal-text")).to_have_text("save the file as parquet", timeout=5_000)
                page.get_by_role("button", name="Add", exact=True).click()
                expect(page.locator(".proposal-note")).to_have_text("added to todos")
                expect(rows).to_have_count(1)
                expect(rows.nth(0).locator(".todo-text")).to_have_value("save the file as parquet")

                # A todo typed, toggled, edited in place, and removed.
                new_todo = page.get_by_label("New todo")
                new_todo.fill("write the tests")
                new_todo.press("Enter")
                expect(rows).to_have_count(2)
                expect(new_todo).to_have_value("")
                rows.nth(1).get_by_role("button", name="Mark as done").click()
                expect(rows.nth(1)).to_have_class(re.compile(r"\bis-done\b"))
                rows.nth(0).locator(".todo-text").fill("save as parquet")
                expect(rows.nth(0).locator(".todo-text")).to_have_value("save as parquet")
                expect(rows.nth(0).locator(".todo-text")).to_be_focused()
                rows.nth(1).get_by_role("button", name="Remove todo").click()
                expect(rows).to_have_count(1)

                # With todos on the subgoal, Bart asks instead of proposing.
                composer.fill("and then?")
                page.get_by_role("button", name="Send").click()
                expect(page.locator(".msg.from-bart .bubble")).to_have_text(
                    re.compile("Imagine this subgoal is done"), timeout=5_000)
                expect(composer).to_be_focused()

                # Back on the first subgoal: its notes, its todos, none of
                # the second's conversation.
                subs.nth(0).click()
                expect(page.locator(".notes-input")).to_have_value("first notes")
                expect(rows).to_have_count(2)
                expect(page.locator(".msg")).to_have_count(0)

                # The other two panes, and the host beside them.
                page.get_by_role("tab", name="Live preview").click()
                expect(page.get_by_role("button", name="Import dataset")).to_be_visible()
                expect(page.get_by_text("CSV only · up to 100mb")).to_be_visible()
                expect(page.locator(".host")).to_have_text("localhost:5173")
                page.get_by_role("tab", name="Terminal").click()
                expect(page.locator(".terminal")).to_contain_text("$ npm run dev")
                expect(page.locator(".terminal")).to_contain_text("ready · http://localhost:5173")
                expect(page.locator(".host")).to_have_text("localhost:5173")
                page.get_by_role("tab", name="Plan").click()
                expect(page.locator(".host")).to_have_count(0)
                expect(page.locator(".notes-input")).to_have_value("first notes")

                # A subgoal added from the rail is selected as it lands.
                page.get_by_role("button", name="+ Add subgoal").click()
                added = page.get_by_label("New subgoal")
                expect(added).to_be_focused()
                added.fill("Export the dataset to parquet")
                added.press("Enter")
                expect(subs).to_have_count(4)
                expect(subs.nth(3)).to_have_class(active)
                expect(subs.nth(3)).to_have_text("Export the dataset to parquet")
                expect(page.locator(".todo-list")).to_have_count(0)
                # Escape drops an empty one.
                page.get_by_role("button", name="+ Add subgoal").click()
                page.get_by_label("New subgoal").press("Escape")
                expect(subs).to_have_count(4)

                # Build all finishes the subgoal's todos and moves to the next.
                subs.nth(0).click()
                page.get_by_role("button", name="Build all").click()
                expect(subs.nth(1)).to_have_class(active, timeout=5_000)
                subs.nth(0).click()
                expect(page.locator(".todo-list .todo.is-done")).to_have_count(2)
                expect(page.get_by_role("button", name="Build all")).to_be_disabled()

                self.assertEqual([], errors)
            finally:
                browser.close()

    def test_the_account_icon_says_who_the_machine_is_connected_as(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")
        home = Path(self.tmp.name) / "human-compact"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HUMAN_COMPACT_HOME": str(home)}), \
                server_for(self.chat) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome, headless=True,
                args=["--disable-background-networking"])
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                account = page.get_by_role("button", name=re.compile("connected|account", re.I))

                # Nobody has connected this machine: the avatar says so and
                # the menu points at the command that does.
                page.goto(url, wait_until="domcontentloaded")
                expect(account).to_have_attribute("aria-label", "Not connected")
                menu = page.get_by_role("menu", name="Account")
                expect(menu).to_have_count(0)
                account.click()
                expect(menu).to_be_visible()
                expect(menu).to_contain_text("Not connected")
                expect(menu).to_contain_text("engelbart auth")
                expect(menu.get_by_role("menuitem", name="Sign out")).to_have_count(0)
                page.keyboard.press("Escape")
                expect(menu).to_have_count(0)
                account.click()
                expect(menu).to_be_visible()
                page.locator(".goal-title").click()
                expect(menu).to_have_count(0)

                # Connected: the account the installer wrote, by email, and
                # a way out.
                (home / "auth.json").write_text(json.dumps({
                    "apiBase": "http://127.0.0.1:9", "token": "machine-token",
                    "email": "someone@example.com"}), encoding="utf-8")
                page.reload(wait_until="domcontentloaded")
                expect(account).to_have_attribute(
                    "aria-label", "Connected as someone@example.com")
                account.click()
                expect(menu).to_contain_text("someone@example.com")
                menu.get_by_role("menuitem", name="Sign out").click()
                expect(menu).to_have_count(0)
                expect(account).to_have_attribute("aria-label", "Not connected")
                self.assertEqual([], errors)
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
