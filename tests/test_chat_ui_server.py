import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import ui  # noqa: E402
from human_compact.trajectory import chat_state  # noqa: E402


NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def goal(goal_id, title, prompt_ids=None):
    return {
        "id": goal_id,
        "title": title,
        "status": "active",
        "parent_goal_id": None,
        "evidence_ids": [],
        "todos": [],
        "important_item_ids": [],
        "prompt_ids": list(prompt_ids or []),
        "priority": "normal",
        "notes": "",
        "description": "",
    }


def write_scope(path, goals, prompts):
    path.mkdir(parents=True, exist_ok=True)
    (path / "goals.json").write_text(json.dumps({"version": 1, "goals": goals}))
    (path / "important.json").write_text(json.dumps({"items": []}))
    (path / "prompts.json").write_text(json.dumps({"prompts": prompts}))


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


def get_json(url):
    with NO_PROXY_OPENER.open(url, timeout=2) as response:
        return json.loads(response.read())


def post_json(url, body, headers=None):
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=request_headers,
        method="POST",
    )
    with NO_PROXY_OPENER.open(request, timeout=2) as response:
        return json.loads(response.read())


def browser_executable():
    configured = os.environ.get("HC_TEST_BROWSER")
    if configured and Path(configured).is_file():
        return configured
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    mac_chrome = Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    return chrome or (str(mac_chrome) if mac_chrome.is_file() else None)


class ChatUiServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = self.root / "chat-a"
        self.b = self.root / "chat-b"
        self.prompts_a = [
            {
                "id": "p-old",
                "role": "user",
                "text": "old human prompt",
                "created_at": "2026-08-12T08:00:00Z",
                "ordinal": 1,
            },
            {
                "id": "p-new",
                "role": "user",
                "text": "new human prompt",
                "created_at": "2026-08-12T09:00:00Z",
                "ordinal": 2,
            },
            {
                "id": "assistant",
                "role": "assistant",
                "text": "private assistant turn",
                "ordinal": 3,
            },
        ]
        write_scope(
            self.a,
            [goal("a1", "goal in chat a"), goal("a2", "another a goal")],
            self.prompts_a,
        )
        write_scope(
            self.b,
            [goal("b1", "goal in chat b")],
            [{"id": "bp", "role": "user", "text": "only in b", "ordinal": 1}],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_running_servers_keep_state_and_writes_scoped(self):
        with server_for(self.a) as url_a, server_for(self.b) as url_b:
            with NO_PROXY_OPENER.open(url_a, timeout=2) as response:
                html = response.read().decode()
            self.assertLess(
                html.index('script type="__bundler/template"'),
                html.index('<script src="/bridge.js"></script>'),
            )
            self.assertLess(
                html.index('<script src="/bridge.js"></script>'),
                html.index("</body>"),
            )
            state_a = get_json(url_a + "/api/state")
            state_b = get_json(url_b + "/api/state")
            self.assertEqual(["a1", "a2"], [g["id"] for g in state_a["goals"]])
            self.assertEqual(["b1"], [g["id"] for g in state_b["goals"]])
            self.assertEqual(["p-old", "p-new"], [p["id"] for p in state_a["prompts"]])
            self.assertEqual(["bp"], [p["id"] for p in state_b["prompts"]])
            self.assertEqual("idle", state_a["analyzer"]["status"])
            self.assertIsInstance(state_a["revision"], str)
            health = get_json(url_a + "/api/health")
            self.assertEqual(
                {"ok": True, "scope": "chat", "session_id": "chat-a"},
                {k: health[k] for k in ("ok", "scope", "session_id")},
            )
            # Reported so a launcher can tell a stale server from a current one.
            self.assertIsInstance(health["version"], str)

            self.assertEqual(
                {"ok": True},
                post_json(
                    url_a + "/api/op",
                    {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-new"},
                ),
            )
            self.assertEqual(["p-new"], get_json(url_a + "/api/state")["goals"][0]["prompt_ids"])
            self.assertEqual([], get_json(url_b + "/api/state")["goals"][0]["prompt_ids"])

    def test_scoped_state_exposes_current_analyzer_progress(self):
        chat_state.set_analyzer_state(
            "chat-a",
            status="pending",
            requested_ordinal=7,
            root=self.root,
        )
        with server_for(self.a) as url:
            analyzer = get_json(url + "/api/state")["analyzer"]
        self.assertEqual("pending", analyzer["status"])
        self.assertEqual(7, analyzer["requested_ordinal"])

    def test_http_boundary_rejects_cross_site_writes_and_dns_rebinding(self):
        with server_for(self.a) as url:
            attacks = [
                urllib.request.Request(
                    url + "/api/import",
                    data=b"[]",
                    headers={"Content-Type": "text/plain"},
                    method="POST",
                ),
                urllib.request.Request(
                    url + "/api/import",
                    data=b"[]",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://attacker.example",
                    },
                    method="POST",
                ),
                urllib.request.Request(
                    url + "/api/import",
                    data=b"[]",
                    headers={
                        "Content-Type": "application/json",
                        "Host": "attacker.example",
                    },
                    method="POST",
                ),
            ]
            for request, status in zip(attacks, (415, 403, 403)):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(status, raised.exception.code)

            # Rejected imports did not abandon the goal tree.
            state = get_json(url + "/api/state")
            self.assertTrue(all(g["status"] == "active" for g in state["goals"]))

            # A browser request from the server's exact origin still works.
            self.assertEqual(
                {"ok": True},
                post_json(
                    url + "/api/op",
                    {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-new"},
                    {"Origin": url},
                ),
            )

    def test_invalid_host_is_rejected_for_reads_too(self):
        with server_for(self.a) as url:
            request = urllib.request.Request(
                url + "/api/state", headers={"Host": "attacker.example"}
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(403, raised.exception.code)

    def test_prompt_links_are_many_to_many_deduped_and_human_only(self):
        for operation in (
            {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-old"},
            {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-old"},
            {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-new"},
            {"op": "attach_prompt", "goal_id": "a2", "prompt_id": "p-old"},
        ):
            self.assertEqual({"ok": True}, ui._apply(operation, self.a))
        state = ui._payload(self.a)
        links = {g["id"]: g["prompt_ids"] for g in state["goals"]}
        self.assertEqual(["p-old", "p-new"], links["a1"])
        self.assertEqual(["p-old"], links["a2"])

        for prompt_id in ("assistant", "missing"):
            result = ui._apply(
                {"op": "attach_prompt", "goal_id": "a1", "prompt_id": prompt_id},
                self.a,
            )
            self.assertEqual(False, result["ok"])
            self.assertIn("this chat", result["error"])
        missing_goal = ui._apply(
            {"op": "attach_prompt", "goal_id": "elsewhere", "prompt_id": "p-old"},
            self.a,
        )
        self.assertEqual(False, missing_goal["ok"])
        self.assertEqual("goal not found in this chat", missing_goal["error"])

        for _ in range(2):
            self.assertEqual(
                {"ok": True},
                ui._apply(
                    {"op": "detach_prompt", "goal_id": "a1", "prompt_id": "p-old"},
                    self.a,
                ),
            )
        self.assertEqual(["p-new"], ui._payload(self.a)["goals"][0]["prompt_ids"])

    def test_parallel_attach_requests_do_not_lose_links(self):
        prompts = [
            {"id": f"p{i}", "role": "user", "text": f"prompt {i}", "ordinal": i}
            for i in range(16)
        ]
        write_scope(self.a, [goal("a1", "parallel goal")], prompts)
        with server_for(self.a) as url:
            def attach(prompt):
                return post_json(
                    url + "/api/op",
                    {"op": "attach_prompt", "goal_id": "a1", "prompt_id": prompt["id"]},
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(attach, prompts))
            self.assertTrue(all(result == {"ok": True} for result in results))
            linked = get_json(url + "/api/state")["goals"][0]["prompt_ids"]
        self.assertEqual({p["id"] for p in prompts}, set(linked))
        self.assertEqual(len(prompts), len(linked))

    def test_analyzer_like_writer_and_http_attach_serialize_cross_process_lock(self):
        """An inference writer cannot save a stale tree over a manual link."""
        analyzer_holds_lock = threading.Event()
        release_analyzer = threading.Event()

        def analyzer_like_write():
            with chat_state.session_lock("chat-a", self.root, wait_s=2):
                goals, important = chat_state.load_goals("chat-a", self.root)
                goals["goals"][0]["description"] = "analyzer-derived description"
                analyzer_holds_lock.set()
                release_analyzer.wait(timeout=2)
                chat_state.save_goals("chat-a", goals, important, self.root)

        analyzer = threading.Thread(target=analyzer_like_write)
        analyzer.start()
        self.assertTrue(analyzer_holds_lock.wait(timeout=1))
        with server_for(self.a) as url:
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(
                    post_json,
                    url + "/api/op",
                    {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-new"},
                )
                time.sleep(0.1)
                self.assertFalse(response.done())
                release_analyzer.set()
                self.assertEqual({"ok": True}, response.result(timeout=2))
            current = get_json(url + "/api/state")["goals"][0]
        analyzer.join(timeout=2)
        self.assertFalse(analyzer.is_alive())
        self.assertEqual("analyzer-derived description", current["description"])
        self.assertEqual(["p-new"], current["prompt_ids"])
        context = (self.a / "goal_context.md").read_text()
        self.assertIn("analyzer-derived description", json.dumps(current))
        self.assertIn("new human prompt", context)

    def test_bundle_import_preserves_prompt_relationships(self):
        ui._apply(
            {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-new"},
            self.a,
        )
        nested = [
            {
                "id": "a1",
                "title": "renamed in bundle",
                "done": False,
                "status": "inprog",
                "prio": "high",
                "notes": "kept notes",
                "desc": "kept description",
                "children": [],
            },
            {
                "id": "a2",
                "title": "another a goal",
                "done": False,
                "status": "todo",
                "children": [],
            },
        ]
        imported_result = ui._import(nested, self.a)
        self.assertTrue(imported_result["ok"])
        self.assertEqual(2, imported_result["goals"])
        self.assertIsInstance(imported_result["revision"], str)
        state = ui._payload(self.a)
        imported = next(g for g in state["goals"] if g["id"] == "a1")
        self.assertEqual("renamed in bundle", imported["title"])
        self.assertEqual(["p-new"], imported["prompt_ids"])

    def test_revisioned_import_rejects_stale_browser_without_mutation(self):
        with server_for(self.a) as url:
            initial = get_json(url + "/api/state")
            stale_revision = initial["revision"]
            goals, important = chat_state.load_goals("chat-a", self.root)
            goals["goals"][0]["status"] = "completed"
            goals["goals"].append(goal("a3", "analyzer-added goal"))
            chat_state.save_goals("chat-a", goals, important, self.root)

            stale_tree = [{
                "id": "a1",
                "title": "goal in chat a",
                "done": False,
                "status": "todo",
                "prio": "high",
                "notes": "",
                "desc": "",
                "children": [],
            }]
            request = urllib.request.Request(
                url + "/api/import",
                data=json.dumps({
                    "goals": stale_tree,
                    "base_revision": stale_revision,
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(409, raised.exception.code)
            conflict = json.loads(raised.exception.read())
            self.assertTrue(conflict["conflict"])

            current = get_json(url + "/api/state")
            by_id = {g["id"]: g for g in current["goals"]}
            self.assertEqual("completed", by_id["a1"]["status"])
            self.assertEqual("analyzer-added goal", by_id["a3"]["title"])
            self.assertEqual("normal", by_id["a1"]["priority"])

    def test_ui_revision_ignores_write_timestamps_but_tracks_semantics(self):
        goals, important = chat_state.load_goals("chat-a", self.root)
        first = ui._goal_revision(goals, important)
        goals["generated_at"] = "2099-01-01T00:00:00Z"
        goals["goals"][0]["updated_at"] = "2099-01-01T00:00:01Z"
        self.assertEqual(first, ui._goal_revision(goals, important))
        goals["goals"][0]["status"] = "completed"
        self.assertNotEqual(first, ui._goal_revision(goals, important))

    def test_open_browser_merges_analyzer_update_with_unsent_manual_edit(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        self.assertEqual(
            {"ok": True},
            ui._apply({
                "op": "attach_prompt",
                "goal_id": "a1",
                "prompt_id": "p-new",
            }, self.a),
        )
        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(
                    page.get_by_text("goal in chat a", exact=True).first
                ).to_be_visible(timeout=10_000)
                # The attached prompt is on the goal before the analyzer
                # writes, and has to still be on it afterwards.
                expect(
                    page.get_by_text("new human prompt", exact=True)
                ).to_have_count(1)

                # Exact localStorage representation written by the bundled
                # app, deliberately left unsent when analysis wins the race.
                page.evaluate("""() => {
                    const key = "hc-vault-ui-v1";
                    const saved = JSON.parse(localStorage.getItem(key));
                    saved.goals[0].prio = "high";
                    localStorage.setItem(key, JSON.stringify(saved));
                }""")
                goals, important = chat_state.load_goals("chat-a", self.root)
                goals["goals"][0]["status"] = "in_progress"
                goals["goals"][0]["todos"] = [{
                    "text": "analyzer-added todo",
                    "done": False,
                    "evidence_ids": [],
                }]
                goals["goals"].append(goal("a3", "analyzer-added goal"))
                chat_state.save_goals("chat-a", goals, important, self.root)

                expect(page.get_by_text("analyzer-added goal", exact=True).first).to_be_visible(
                    timeout=10_000
                )
                expect(page.get_by_text("analyzer-added todo", exact=True).first).to_be_visible(
                    timeout=10_000
                )
                expect(
                    page.get_by_text("new human prompt", exact=True)
                ).to_have_count(1)
                current = {
                    g["id"]: g
                    for g in chat_state.load_goals("chat-a", self.root)[0]["goals"]
                }
                self.assertEqual("in_progress", current["a1"]["status"])
                self.assertEqual("high", current["a1"]["priority"])
                self.assertEqual(["p-new"], current["a1"]["prompt_ids"])
                # Inference still emits todos; the model promotes each one
                # into a child goal on load, so surviving the round trip
                # means surviving as a child of a1, not as a nested dict.
                self.assertEqual([], current["a1"]["todos"])
                self.assertIn(
                    "analyzer-added todo",
                    [g["title"] for g in current.values()
                     if g["parent_goal_id"] == "a1"],
                )
                self.assertIn("a3", current)
            finally:
                browser.close()

    def test_fresh_empty_chat_does_not_import_bundle_demo_goals(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        empty = self.root / "fresh-empty"
        write_scope(empty, [], [])
        with server_for(empty) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                # The bridge replaces the artifact's own empty-tree line: a
                # vault with nothing in it is not a dead end, so the copy
                # says where goals come from as well as offering the button.
                expect(
                    page.get_by_text(
                        "No goals yet — they are inferred from your analyzed "
                        "conversations, or add one below.",
                        exact=True,
                    )
                ).to_be_visible(timeout=10_000)
                expect(page.get_by_text("Add goal", exact=True)).to_have_count(1)
                expect(
                    page.get_by_text("Ship the goal-state foundation", exact=True)
                ).to_have_count(0)

                # The bridge polls localStorage every 800 ms. Waiting beyond
                # that boundary proves the demo seed was not imported later.
                page.wait_for_timeout(1_200)
                saved = page.evaluate(
                    "JSON.parse(localStorage.getItem('hc-vault-ui-v1'))"
                )
                self.assertEqual(7, saved["v"])
                self.assertEqual([], saved["goals"])
                self.assertEqual([], get_json(url + "/api/state")["goals"])
                self.assertEqual([], json.loads(
                    (empty / "goals.json").read_text()
                )["goals"])
            finally:
                browser.close()

    def test_a_completion_persists_and_the_filter_decides_where_it_shows(self):
        """Completing a goal, and what each filter then shows of it.

        This test was written against the bespoke UI David replaced wholesale
        in 20f20c6, and named the claim "an active completion stays crossed
        out until the filter changes". That is not a property of the artifact
        the repo ships: `keep` is `filter === 'active' ? !n.done`, applied on
        the same render as the toggle, so a goal completed under Active
        leaves the tree at once (verified in a browser before this rewrite).
        Every mechanism the old test guarded is kept -- the toggle writes
        through to disk, the row is struck through wherever it is drawn, it
        survives a reload, and the filter alone decides visibility -- and the
        name now says what the shipped UI actually does. Whether a goal
        should vanish the instant you finish it is a real question, and one
        this task did not have the standing to answer by inventing tree
        behaviour; it is raised in the report instead.
        """
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        goals_path = self.a / "goals.json"
        child_title = (
            "Completed nested subgoal explaining persistence, reload, "
            "reconciliation, and rendering without overlapping adjacent rows"
        )
        with server_for(self.a) as url, sync_playwright() as playwright:
            # The control that used to make this row is gone from the tree;
            # the op behind it is the one the browser calls either way.
            self.assertEqual({"ok": True}, post_json(
                url + "/api/op",
                {"op": "add_goal", "title": child_title,
                 "parent_goal_id": "a1"},
                {"Origin": url},
            ))
            child_id = [g["id"] for g in get_json(url + "/api/state")["goals"]
                        if g["title"] == child_title]
            self.assertEqual(1, len(child_id))
            child_id = child_id[0]

            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_text(child_title, exact=True).first
                       ).to_be_visible(timeout=10_000)

                page.get_by_text("active (3)", exact=True).click()
                toggles = page.locator('[title="Toggle complete"]')
                expect(toggles).to_have_count(3)
                at = page.evaluate(
                    """title => [...document.querySelectorAll(
                         '[title="Toggle complete"]')]
                       .findIndex(m => m.parentElement.textContent
                         .includes(title))""",
                    child_title,
                )
                self.assertGreaterEqual(at, 0)
                toggles.nth(at).click()

                # Under Active it leaves the tree, and the counts say where
                # it went rather than the row simply disappearing.
                expect(page.get_by_text(child_title, exact=True)
                       ).to_have_count(0)
                expect(page.get_by_text("done (1)", exact=True)
                       ).to_be_visible()
                expect(page.get_by_text("active (2)", exact=True)
                       ).to_be_visible()

                # It is struck through wherever it is drawn.
                page.get_by_text("all (3)", exact=True).click()
                row = page.get_by_text(child_title, exact=True).first
                expect(row).to_be_visible()
                expect(row.locator("..")).to_have_css(
                    "text-decoration-line", "line-through"
                )

                # And the completion is the server's, not the page's.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    persisted = {
                        item["id"]: item
                        for item in json.loads(goals_path.read_text())["goals"]
                    }
                    if persisted[child_id]["status"] == "completed":
                        break
                    time.sleep(0.05)
                self.assertEqual("completed", persisted[child_id]["status"])

                page.reload(wait_until="domcontentloaded")
                row = page.get_by_text(child_title, exact=True).first
                expect(row).to_be_visible(timeout=10_000)
                expect(row.locator("..")).to_have_css(
                    "text-decoration-line", "line-through", timeout=10_000
                )

                # The filter the reader picks is the only thing that hides it.
                page.get_by_text("active (2)", exact=True).click()
                expect(page.get_by_text(child_title, exact=True)
                       ).to_have_count(0)
                expect(page.locator('[title="Toggle complete"]')
                       ).to_have_count(2)
            finally:
                browser.close()

    def test_prompt_picker_close_button_and_escape_restore_trigger_focus(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                # The control is on the Context pane, under the document the
                # prompts are evidence for. Chat scope used to be refused it.
                trigger = page.locator(".hc-prompt-addbtn")
                expect(trigger).to_be_visible(timeout=10_000)
                overlay = page.locator(".hc-ask")

                trigger.click()
                expect(overlay).to_be_visible()
                page.locator(".hc-pick-close").click()
                expect(overlay).to_have_count(0)
                expect(trigger).to_be_focused()

                trigger.click()
                expect(overlay).to_be_visible()
                page.keyboard.press("Escape")
                expect(overlay).to_have_count(0)
                expect(trigger).to_be_focused()

                # Nothing about browsing your own prompts edits the goal.
                self.assertEqual(
                    [],
                    [g["prompt_ids"] for g in
                     get_json(url + "/api/state")["goals"]
                     if g["prompt_ids"]],
                )
            finally:
                browser.close()

    def test_goal_sanitize_and_merge_preserve_unique_prompt_links(self):
        from human_compact.trajectory import goals as goal_model

        goals = {
            "goals": [
                goal("a1", "source", ["p-old", "p-new"]),
                goal("a2", "destination", ["p-new"]),
                goal("a3", "legacy goal without prompt field"),
                goal("a4", "malformed links", ["p-old", "p-old", 42]),
            ]
        }
        goals["goals"][2].pop("prompt_ids")
        goal_model.sanitize(goals)
        self.assertEqual([], goals["goals"][2]["prompt_ids"])
        self.assertEqual(["p-old"], goals["goals"][3]["prompt_ids"])
        important = {"items": []}
        goal_model.apply_ops(
            goals,
            important,
            [{"op": "merge_goals", "from_id": "a1", "into_id": "a2"}],
        )
        merged = goal_model.by_id(goals, "a2")
        self.assertEqual(["p-new", "p-old"], merged["prompt_ids"])

    def test_explicit_scope_does_not_consult_global_trajectory_directory(self):
        old = os.environ.get("HC_HOME")
        os.environ["HC_HOME"] = str(self.root / "does-not-exist")
        try:
            self.assertEqual("goal in chat a", ui._payload(self.a)["goals"][0]["title"])
        finally:
            if old is None:
                os.environ.pop("HC_HOME", None)
            else:
                os.environ["HC_HOME"] = old

    def test_run_reports_bound_server_before_serving(self):
        observed = {}

        def ready(url, server):
            observed["url"] = url
            observed["scope"] = server.trajdir
            threading.Timer(0.05, server.shutdown).start()

        with mock.patch(
            "http.server.socket.getfqdn",
            side_effect=AssertionError("loopback bind must not use reverse DNS"),
        ):
            thread = threading.Thread(
                target=ui.run,
                kwargs={
                    "port": 0,
                    "open_browser": False,
                    "trajdir": self.a,
                    "ready_callback": ready,
                    "label": "Chat goals",
                },
            )
            thread.start()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(observed["url"].startswith("http://127.0.0.1:"))
        self.assertEqual(self.a.resolve(), observed["scope"].resolve())

    def test_chat_server_idle_timeout_is_extended_by_requests(self):
        observed = {}
        ready = threading.Event()

        def bound(url, _server):
            observed["url"] = url.rstrip("/")
            ready.set()

        thread = threading.Thread(
            target=ui.run,
            kwargs={
                "port": 0,
                "open_browser": False,
                "trajdir": self.a,
                "ready_callback": bound,
                # Leave enough scheduling margin for loaded CI runners while
                # still proving that requests extend the original deadline.
                "idle_timeout": 0.5,
            },
        )
        thread.start()
        self.assertTrue(ready.wait(timeout=1))
        # Keep the server active beyond its original 0.5s deadline.
        for _ in range(11):
            time.sleep(0.05)
            self.assertTrue(get_json(observed["url"] + "/api/health")["ok"])
        self.assertTrue(thread.is_alive())
        # shutdown() wakes serve_forever on its own polling interval.
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_idle_watcher_never_interrupts_an_active_request(self):
        observed = {}
        ready = threading.Event()
        request_started = threading.Event()
        release_request = threading.Event()
        real_payload = ui._payload

        def bound(url, _server):
            observed["url"] = url.rstrip("/")
            ready.set()

        def slow_payload(*args, **kwargs):
            request_started.set()
            release_request.wait(timeout=1)
            return real_payload(*args, **kwargs)

        with mock.patch.object(ui, "_payload", side_effect=slow_payload):
            thread = threading.Thread(
                target=ui.run,
                kwargs={
                    "port": 0,
                    "open_browser": False,
                    "trajdir": self.a,
                    "ready_callback": bound,
                    "idle_timeout": 0.08,
                },
            )
            thread.start()
            self.assertTrue(ready.wait(timeout=1))
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(
                    get_json, observed["url"] + "/api/state"
                )
                self.assertTrue(request_started.wait(timeout=1))
                time.sleep(0.15)
                self.assertTrue(thread.is_alive())
                self.assertFalse(response.done())
                release_request.set()
                result = response.result(timeout=1)
                self.assertEqual("a1", result["goals"][0]["id"])
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())

    @staticmethod
    def chip_style(page, label):
        """Weight and colour of one filter chip, as the reader sees them."""
        return page.evaluate(
            """label => {
              const el = [...document.querySelectorAll('span')].find(
                e => !e.children.length && e.textContent.trim() === label);
              if (!el) return null;
              const cs = getComputedStyle(el);
              return { weight: cs.fontWeight, color: cs.color };
            }""",
            label,
        )

    def test_a_chat_workspace_opens_on_its_own_tree_not_the_vault_wizard(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        # A finished goal is the case the default decides: on 'active' it is
        # filtered out of the tree, so the reader's own chat opens missing
        # the work they just completed.
        goals_path = self.a / "goals.json"
        stored = json.loads(goals_path.read_text())
        stored["goals"][1]["status"] = "completed"
        goals_path.write_text(json.dumps(stored))

        # /api/setup answers for the global vault and refuses in chat scope.
        # The artifact reads one `setup` object for its wizard, its gate and
        # its main pane, so a chat page that inherits that refusal paints
        # onboarding over a tree it already has.
        with server_for(self.a) as url, sync_playwright() as playwright:
            self.assertFalse(get_json(url + "/api/setup")["ok"])
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(
                    page.get_by_text("goal in chat a", exact=True).first
                ).to_be_visible(timeout=10_000)
                expect(
                    page.get_by_text("another a goal", exact=True).first
                ).to_be_visible()

                # Nothing that asks a chat to set up a vault, and no way back
                # into the wizard: the gate button is what re-opens it.
                for wizard in (
                    "Keep your Claude Code history",
                    "Enable local Vault",
                    "Build your Goals",
                    "No Goals yet",
                    "1 of 2",
                    "2 of 2",
                ):
                    expect(page.get_by_text(wizard, exact=True)).to_have_count(
                        0, timeout=5_000
                    )

                # The Conversations page lists a vault's whole history; this
                # scope has one conversation and no route that serves the list.
                expect(page.get_by_text("Conversations", exact=True)).to_be_hidden()
                expect(page.get_by_text("Goals", exact=True).first).to_be_visible()

                # All, on the page and not only in the store: the chip is
                # the selected one and the completed goal is in the tree.
                selected = self.chip_style(page, "all (2)")
                unselected = self.chip_style(page, "active (1)")
                self.assertEqual("700", selected["weight"])
                self.assertEqual("500", unselected["weight"])
                self.assertNotEqual(unselected["color"], selected["color"])
                self.assertEqual(
                    {"sv": 9, "storage": True, "analysis": "claude",
                     "done": True},
                    page.evaluate(
                        "JSON.parse(localStorage.getItem('hc-vault-ui-v1'))"
                        ".setup"
                    ),
                )
            finally:
                browser.close()

    def test_a_filter_the_reader_picks_here_survives_their_reload(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        goals_path = self.a / "goals.json"
        stored = json.loads(goals_path.read_text())
        stored["goals"][1]["status"] = "completed"
        goals_path.write_text(json.dumps(stored))

        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_text("all (2)", exact=True)).to_be_visible(
                    timeout=10_000
                )
                page.get_by_text("done (1)", exact=True).click()
                self.assertEqual(
                    "700", self.chip_style(page, "done (1)")["weight"]
                )

                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_text("done (1)", exact=True)).to_be_visible(
                    timeout=10_000
                )
                # The default only applies to a page with no history. A
                # choice the reader made is theirs to keep.
                self.assertEqual(
                    "700", self.chip_style(page, "done (1)")["weight"]
                )
                self.assertEqual(
                    "500", self.chip_style(page, "all (2)")["weight"]
                )
            finally:
                browser.close()

    def test_a_chat_inspector_offers_only_the_pane_it_can_serve(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        with server_for(self.a) as url, sync_playwright() as playwright:
            # Every op behind AGENT and REVIEW refuses in this scope, so a
            # tab that opens either is a control with nothing behind it.
            self.assertFalse(get_json(url + "/api/review?goal=a1")["ok"])
            self.assertFalse(post_json(
                url + "/api/op",
                {"op": "launch_agent_run", "goal_id": "a1"},
                {"Origin": url},
            )["ok"])
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_text("CONTEXT", exact=True)).to_be_visible(
                    timeout=10_000
                )
                for tab in ("AGENT", "REVIEW"):
                    expect(page.get_by_text(tab, exact=True)).to_be_hidden()
                # PROMPT is a pane this scope can serve: the assembled prompt
                # and a way to take it. Nothing in it runs anything.
                expect(page.get_by_text("PROMPT", exact=True)).to_be_visible()

                # A pane saved from a build that still offered them must not
                # restore an inspector this scope cannot draw.
                page.evaluate("""() => {
                    const key = "hc-vault-ui-v1";
                    const saved = JSON.parse(localStorage.getItem(key));
                    saved.paneTab = "agent";
                    localStorage.setItem(key, JSON.stringify(saved));
                }""")
                page.reload(wait_until="domcontentloaded")
                context_tab = page.get_by_text("CONTEXT", exact=True)
                expect(context_tab).to_be_visible(timeout=10_000)
                # What the reader sees, not what the store says: CONTEXT is
                # the tab carrying the selected underline, and the pane
                # drawn under it is the Context pane.
                expect(context_tab).not_to_have_css(
                    "border-bottom-color", "rgba(0, 0, 0, 0)"
                )
                # The Context pane is the goal's document, and the prompts
                # it was written from. The textbox pane it replaced is
                # dormant, so none of its labels are on the page at all.
                expect(page.locator('[placeholder^="Write in markdown"]')
                       ).to_be_visible()
                expect(page.get_by_text("RELATED PROMPTS", exact=True)
                       ).to_be_visible()
                for gone in ("WHERE THIS SITS", "OBJECTIVE", "CODE CONTEXT",
                             "DOCUMENT CONTEXT", "DECISIONS", "ALREADY BUILT",
                             "BLOCKERS & OPEN QUESTIONS"):
                    expect(page.get_by_text(gone, exact=True)).to_have_count(0)
                expect(page.get_by_text("AGENT", exact=True)).to_be_hidden()
                expect(page.get_by_text("REVIEW", exact=True)).to_be_hidden()
            finally:
                browser.close()


    DEFAULT_DOC = ("# Objective\n\n# In my words\n\n# Decisions\n\n"
                   "# Built\n\n# Blockers\n\n# Open questions\n")

    EDITOR = '[placeholder^="Write in markdown"]'

    def overlay_text(self, page):
        """What the reader sees rendered under their own caret."""
        return page.evaluate(
            """sel => {
                 const ta = document.querySelector(sel);
                 return ta.parentElement.firstElementChild.textContent;
               }""", self.EDITOR)

    def test_a_goal_with_no_notes_opens_on_the_documents_own_headings(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        with server_for(self.a) as url, sync_playwright() as playwright:
            self.assertEqual(
                ["", ""],
                [g["notes"] for g in get_json(url + "/api/state")["goals"]],
            )
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                editor = page.locator(self.EDITOR)
                expect(editor).to_be_visible(timeout=10_000)

                # The shape of the document is the invitation to fill it in.
                self.assertEqual(self.DEFAULT_DOC, editor.input_value())
                rendered = self.overlay_text(page)
                for head in ("# Objective", "# In my words", "# Decisions",
                             "# Built", "# Blockers", "# Open questions"):
                    self.assertIn(head, rendered)

                # Rendered, not just held: the heading is drawn bold with its
                # marker kept, which is the whole point of an inline editor.
                self.assertEqual("700", page.evaluate(
                    """sel => {
                         const ta = document.querySelector(sel);
                         const span = [...ta.parentElement.firstElementChild
                           .querySelectorAll('span')]
                           .find(s => s.textContent === 'Objective');
                         return span && getComputedStyle(span).fontWeight;
                       }""", self.EDITOR))

                # Showing it is not writing it: nothing is stored until the
                # reader types.
                page.wait_for_timeout(1_200)
                self.assertEqual(
                    ["", ""],
                    [g["notes"] for g in get_json(url + "/api/state")["goals"]],
                )

                # And the textboxes it replaced are off the page entirely.
                for gone in ("OBJECTIVE", "DECISIONS", "CODE CONTEXT",
                             "DOCUMENT CONTEXT", "ALREADY BUILT"):
                    expect(page.get_by_text(gone, exact=True)).to_have_count(0)
            finally:
                browser.close()

    def test_a_line_typed_under_a_heading_outlives_the_page_and_the_server(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        written = self.DEFAULT_DOC.replace(
            "# Decisions\n", "# Decisions\n- we chose sqlite\n")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                with server_for(self.a) as url:
                    page = browser.new_page(
                        viewport={"width": 1400, "height": 900})
                    page.goto(url, wait_until="domcontentloaded")
                    editor = page.locator(self.EDITOR)
                    expect(editor).to_be_visible(timeout=10_000)
                    editor.fill(written)

                    # It reaches the server through the same import path the
                    # rest of the tree uses; nothing new carries it.
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        notes = get_json(url + "/api/state")["goals"][0]["notes"]
                        if "- we chose sqlite" in notes:
                            break
                        time.sleep(0.1)
                    self.assertEqual(written, notes)
                    self.assertIn("- we chose sqlite", self.overlay_text(page))

                    page.reload(wait_until="domcontentloaded")
                    expect(page.locator(self.EDITOR)).to_be_visible(
                        timeout=10_000)
                    self.assertEqual(written,
                                     page.locator(self.EDITOR).input_value())

                # A different server, on a different port, over the same
                # session directory: the document is the session's, not the
                # page's and not that process's.
                with server_for(self.a) as second:
                    self.assertNotEqual(url, second)
                    self.assertEqual(
                        written,
                        get_json(second + "/api/state")["goals"][0]["notes"],
                    )
                    page = browser.new_page(
                        viewport={"width": 1400, "height": 900})
                    page.goto(second, wait_until="domcontentloaded")
                    expect(page.locator(self.EDITOR)).to_be_visible(
                        timeout=10_000)
                    self.assertEqual(written,
                                     page.locator(self.EDITOR).input_value())

                # And it is in the file the next session is handed.
                self.assertIn("- we chose sqlite",
                              (self.a / "goal_context.md").read_text())
            finally:
                browser.close()

    def test_a_prompt_can_be_tied_to_a_goal_and_untied_from_this_scope(self):
        """The whole link, both directions, through the real controls.

        Chat scope used to be refused the add control outright, so a wrong
        inference had no correction here -- while both ops it needs answer
        in this scope.
        """
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        with server_for(self.a) as url, sync_playwright() as playwright:
            self.assertEqual(
                [[], []],
                [g["prompt_ids"] for g in
                 get_json(url + "/api/state")["goals"]],
            )
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_text(
                    "No prompts of yours are tied to this goal yet.",
                    exact=True)).to_be_visible(timeout=10_000)

                page.locator(".hc-prompt-addbtn").click()
                expect(page.locator(".hc-ask")).to_be_visible()
                # Its own words only -- the assistant turn is not offered.
                self.assertEqual(2, page.locator(".hc-pick-row").count())
                page.get_by_text("new human prompt", exact=True).click()

                linked = self.wait_for_links(url, "a1", ["p-new"])
                self.assertEqual(["p-new"], linked)
                expect(page.get_by_text("new human prompt", exact=True)
                       ).to_be_visible(timeout=10_000)
                # A link the reader made is theirs, and says so.
                expect(page.get_by_text("yours", exact=True)).to_be_visible()
                expect(page.get_by_text("automatic", exact=True)
                       ).to_have_count(0)

                page.locator('[title="Unlink this prompt"]').first.click()
                self.assertEqual([], self.wait_for_links(url, "a1", []))
                expect(page.get_by_text(
                    "No prompts of yours are tied to this goal yet.",
                    exact=True)).to_be_visible(timeout=10_000)
                # Dropped by hand means the next analysis must not put it back.
                self.assertEqual(
                    ["p-new"],
                    [g for g in get_json(url + "/api/state")["goals"]
                     if g["id"] == "a1"][0]["detached_prompt_ids"],
                )
            finally:
                browser.close()

    def test_a_link_inference_made_is_labelled_as_the_machines(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        goals_path = self.a / "goals.json"
        stored = json.loads(goals_path.read_text())
        stored["goals"][0]["prompt_ids"] = ["p-new"]
        stored["goals"][0]["auto_prompt_ids"] = ["p-new"]
        goals_path.write_text(json.dumps(stored))

        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_text("new human prompt", exact=True)
                       ).to_be_visible(timeout=10_000)
                expect(page.get_by_text("automatic", exact=True)
                       ).to_be_visible()
                expect(page.get_by_text("yours", exact=True)).to_have_count(0)
            finally:
                browser.close()

    def wait_for_links(self, url, goal_id, want, seconds=8):
        deadline = time.monotonic() + seconds
        links = None
        while time.monotonic() < deadline:
            links = [g["prompt_ids"] for g in
                     get_json(url + "/api/state")["goals"]
                     if g["id"] == goal_id][0]
            if links == want:
                return links
            time.sleep(0.1)
        return links

    def test_the_prompt_tab_is_the_prompt_and_a_real_copy(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        goals_path = self.a / "goals.json"
        stored = json.loads(goals_path.read_text())
        stored["goals"][0]["notes"] = (
            "# Objective\nShip the document pane.\n\n# In my words\n\n"
            "# Decisions\n- we chose sqlite\n\n# Built\n\n# Blockers\n\n"
            "# Open questions\n"
        )
        goals_path.write_text(json.dumps(stored))

        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1400, "height": 900},
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded")
                expect(page.locator(self.EDITOR)).to_be_visible(timeout=10_000)

                page.get_by_text("PROMPT", exact=True).click()
                # One document, one place to edit it: the tab that assembles
                # the prompt does not offer a second copy of the notes.
                expect(page.locator(self.EDITOR)).to_have_count(0)
                expect(page.get_by_text("RECOMMENDED PROMPT", exact=True)
                       ).to_be_visible()

                # The one textarea in this pane, found by the control that
                # regenerates it -- "first textarea" is the goal description.
                draft = page.evaluate(
                    """() => {
                         const gen = document.querySelector(
                           '[title="Regenerate prompt"]');
                         return gen && gen.previousElementSibling.value;
                       }""")
                self.assertIn("Objective:\nShip the document pane.", draft)
                self.assertIn("Decisions:\n- we chose sqlite", draft)

                # Nothing here starts a run; every op behind one refuses.
                expect(page.get_by_text("run agent", exact=True)
                       ).to_have_count(0)
                expect(page.get_by_text("AGENT STATUS", exact=True)
                       ).to_have_count(0)

                copy = page.get_by_text("Copy prompt", exact=True)
                expect(copy).to_be_visible()
                copy.click()
                # The label only changes once the clipboard has it.
                expect(page.get_by_text("copied \u2713", exact=True)
                       ).to_be_visible()
                self.assertEqual(draft, page.evaluate(
                    "() => navigator.clipboard.readText()"))
                context.close()
            finally:
                browser.close()

    # --- notices: the workspace speaks for the terminal --------------------

    def test_state_carries_the_notices_a_chat_workspace_draws(self):
        chat_state.add_notice("chat-a", "session_stopped", "Done. Tests pass.",
                              self.root)
        chat_state.add_notice("chat-a", "subagent_returned", "Explore: found it",
                              self.root)

        with server_for(self.a) as url_a, server_for(self.b) as url_b:
            rows = get_json(url_a + "/api/state")["notices"]
            self.assertEqual([("session_stopped", "Done. Tests pass."),
                              ("subagent_returned", "Explore: found it")],
                             [(row["kind"], row["detail"]) for row in rows])
            for row in rows:
                self.assertIsInstance(row["id"], str)
                self.assertIsInstance(row["at"], str)
            # Notices are per session, like every other thing in this store.
            self.assertEqual([], get_json(url_b + "/api/state")["notices"])

    def test_a_global_vault_has_no_session_to_report_on(self):
        # The banner answers "is the chat I am attached to done?". A global
        # vault is attached to no chat, so the field is present and empty
        # rather than absent -- the bridge reads one shape in both scopes.
        self.assertEqual([], ui._payload(self.a, chat_scoped=False)["notices"])

    def test_the_workspace_says_when_a_subagent_comes_back(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        # Everything that happened before this page existed. A workspace
        # opened mid-conversation must not replay the turns it missed.
        chat_state.add_notice("chat-a", "session_stopped", "an older turn",
                              self.root)

        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(
                    page.get_by_text("goal in chat a", exact=True).first
                ).to_be_visible(timeout=10_000)
                title = page.title()
                # Two polls' worth: the state carrying the old notice has
                # certainly landed by now.
                page.wait_for_timeout(3_500)
                expect(page.locator(".hc-notice")).to_have_count(0)
                self.assertEqual(title, page.title())

                chat_state.add_notice(
                    "chat-a", "subagent_returned",
                    "Explore: Analysis complete. Found 3 potential issues",
                    self.root)

                banner = page.locator(".hc-notice")
                expect(banner).to_have_count(1, timeout=4_000)
                expect(banner.locator(".hc-notice-title")
                       ).to_have_text("A subagent returned")
                expect(banner.locator(".hc-notice-detail")).to_have_text(
                    "Explore: Analysis complete. Found 3 potential issues")
                expect(banner.locator(".hc-notice-close")).to_be_visible()
                # A workspace on another screen has to be able to say so
                # from the tab strip alone. The mark leads; what it leads is
                # whatever title the page had, which the adopted artifact's
                # runtime currently leaves empty (see the task report).
                self.assertTrue(page.title().startswith("\u25cf"), page.title())
                self.assertNotEqual(title, page.title())

                # It takes itself away; nothing here was clicked.
                expect(banner).to_have_count(0, timeout=12_000)
                self.assertEqual(title, page.title())

                # And it is not shown twice for the same event.
                page.wait_for_timeout(3_500)
                expect(banner).to_have_count(0)
            finally:
                browser.close()

    def test_a_notice_waits_while_it_is_read_and_closes_when_asked(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(
                    page.get_by_text("goal in chat a", exact=True).first
                ).to_be_visible(timeout=10_000)
                title = page.title()

                chat_state.add_notice("chat-a", "session_stopped",
                                      "Done. Tests pass.", self.root)
                banner = page.locator(".hc-notice")
                expect(banner).to_have_count(1, timeout=4_000)
                expect(banner.locator(".hc-notice-title")
                       ).to_have_text("Claude finished responding")

                # Reading it holds it open past the moment it would have gone.
                banner.hover()
                page.wait_for_timeout(9_000)
                expect(banner).to_have_count(1)

                banner.locator(".hc-notice-close").click()
                expect(banner).to_have_count(0, timeout=2_000)
                self.assertEqual(title, page.title())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()


class ConversationGoalAttributionTests(unittest.TestCase):
    """A conversation is linked to the goal that cites it, or to nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name)
        (self.trajdir / "conversations").mkdir()

    def _rows(self, goals, sessions):
        (self.trajdir / "goals.json").write_text(json.dumps(goals))
        with mock.patch("human_compact.trajectory.discover.discover",
                        return_value=sessions):
            return ui.conversation_rows(self.trajdir)

    def test_it_names_the_goal_that_cites_the_conversation(self):
        goals = {"goals": [{"id": "g1", "title": "Ship the vault",
                            "evidence_ids": ["abcdef12#003"]}]}
        rows = self._rows(goals, [{"session_id": "abcdef12-0000-0000-0000-x",
                                   "turns": [{"role": "user", "text": "hi"}]}])
        self.assertEqual(rows[0]["goalId"], "g1")
        self.assertEqual(rows[0]["goalLine"], "Goal: Ship the vault")

    def test_an_uncited_conversation_says_so_instead_of_showing_a_bare_label(self):
        rows = self._rows({"goals": []},
                          [{"session_id": "abcdef12-0000-0000-0000-x",
                            "turns": [{"role": "user", "text": "hi"}]}])
        self.assertIsNone(rows[0]["goalId"])
        self.assertEqual(rows[0]["goalLine"], "No goal drawn from this yet")
        self.assertEqual(rows[0]["goal"], "")

    def _sized(self, spec):
        sessions = [{"session_id": f"{name}0000-0000-0000-0000-x",
                     "date": date,
                     "turns": [{"role": "user", "text": "t"}] * turns}
                    for name, date, turns in spec]
        return [(row["title"], row["turns"]) for row in
                self._rows({"goals": []}, sessions)]

    def test_the_longest_conversations_come_first(self):
        # Length is the closest thing to how much a conversation carries, and
        # it is the order the extractor works in, so the list reads top-down
        # as the analysis moves through it.
        got = self._sized([("aaaaaaaa", "2026-08-15", 4),
                           ("bbbbbbbb", "2026-08-08", 40),
                           ("cccccccc", "2026-08-11", 1)])
        self.assertEqual([40, 4, 1], [turns for _, turns in got])

    def test_the_newer_of_two_equal_conversations_comes_first(self):
        got = self._sized([("aaaaaaaa", "2026-08-04", 24),
                           ("bbbbbbbb", "2026-08-15", 24)])
        self.assertEqual([24, 24], [turns for _, turns in got])
        rows = self._rows({"goals": []}, [
            {"session_id": "aaaaaaaa-0000-0000-0000-x", "date": "2026-08-04",
             "turns": [{"role": "user", "text": "t"}] * 24},
            {"session_id": "bbbbbbbb-0000-0000-0000-x", "date": "2026-08-15",
             "turns": [{"role": "user", "text": "t"}] * 24}])
        self.assertEqual("2026-08-15", rows[0]["meta"].split(" ")[0])

    def test_the_row_carries_its_length_as_a_number(self):
        # The sort needs a number; "40 messages" sorts before "4 messages"
        # as a string, which put the shorter conversation on top.
        rows = self._rows({"goals": []},
                          [{"session_id": "abcdef12-0000-0000-0000-x",
                            "turns": [{"role": "user", "text": "hi"}] * 7}])
        self.assertEqual(7, rows[0]["turns"])

    def test_the_most_specific_goal_wins_a_tie(self):
        goals = {"goals": [
            {"id": "g1", "title": "Parent", "evidence_ids": ["abcdef12#001"]},
            {"id": "g1a", "title": "Child", "parent_goal_id": "g1",
             "evidence_ids": ["abcdef12#002"]},
        ]}
        rows = self._rows(goals, [{"session_id": "abcdef12-0000-0000-0000-x",
                                   "turns": [{"role": "user", "text": "hi"}]}])
        self.assertEqual(rows[0]["goalId"], "g1a")


class ConversationThreadTests(unittest.TestCase):
    """The artifact splits the two sides on the label; it has to be right."""

    TURNS = [{"role": "user", "text": "why is it empty?"},
             {"role": "assistant", "text": "the label was wrong"},
             {"role": "user", "text": "fix it"}]

    def test_the_user_side_is_labelled_the_way_the_artifact_reads_it(self):
        rows = ui.thread_rows(self.TURNS, limit=10, chars=100)
        self.assertEqual(["YOU", "CLAUDE", "YOU"], [r[0] for r in rows])

    def test_claudes_replies_are_part_of_the_conversation(self):
        rows = ui.thread_rows(self.TURNS, limit=10, chars=100)
        self.assertIn(["CLAUDE", "the label was wrong"], rows)

    def test_order_is_preserved(self):
        rows = ui.thread_rows(self.TURNS, limit=10, chars=100)
        self.assertEqual(["why is it empty?", "the label was wrong", "fix it"],
                         [r[1] for r in rows])

    def test_empty_turns_are_dropped_rather_than_rendered_blank(self):
        rows = ui.thread_rows([{"role": "user", "text": "   "}] + self.TURNS,
                              limit=10, chars=100)
        self.assertEqual(3, len(rows))

    def test_the_preview_is_bounded(self):
        rows = ui.thread_rows(self.TURNS, limit=2, chars=3)
        self.assertEqual(2, len(rows))
        self.assertEqual("why", rows[0][1])

    def test_a_conversation_the_vault_does_not_have_reports_nothing(self):
        with mock.patch("human_compact.trajectory.discover.discover",
                        return_value=[]):
            self.assertIsNone(ui.conversation_thread(Path("/nowhere"), "nope"))

    def test_the_full_thread_comes_back_for_a_known_conversation(self):
        session = {"session_id": "abc", "turns": self.TURNS}
        with mock.patch("human_compact.trajectory.discover.discover",
                        return_value=[session]):
            rows = ui.conversation_thread(Path("/nowhere"), "abc")
        self.assertEqual(3, len(rows))
        self.assertEqual("YOU", rows[0][0])


class PlanPreviewTests(unittest.TestCase):
    """A plan shown before anything runs is a proposal, and costs one call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name)
        (self.trajdir / "goals.json").write_text(json.dumps(
            {"goals": [{"id": "g1", "title": "Ship it", "status": "active",
                        "evidence_ids": []}]}))
        (self.trajdir / "config.json").write_text(
            json.dumps({"extract_provider": "claude"}))

    def _provider(self, payload, seen):
        class Stub:
            def generate_json(_, prompt):
                seen.append(prompt)
                return payload
        return Stub()

    def test_it_proposes_steps_for_a_known_goal(self):
        seen = []
        with mock.patch("human_compact.trajectory.providers.make",
                        return_value=self._provider(
                            {"steps": ["Read the code", "Make the change"]}, seen)):
            got = ui.plan_preview(self.trajdir, "g1")
        self.assertTrue(got["ok"])
        self.assertEqual(["Read the code", "Make the change"], got["steps"])
        self.assertIn("Ship it", seen[0])

    def test_the_second_look_costs_nothing(self):
        seen = []
        with mock.patch("human_compact.trajectory.providers.make",
                        return_value=self._provider({"steps": ["One"]}, seen)):
            ui.plan_preview(self.trajdir, "g1")

        class Boom:
            def generate_json(self, prompt):
                raise AssertionError("should have been cached")

        with mock.patch("human_compact.trajectory.providers.make",
                        return_value=Boom()):
            again = ui.plan_preview(self.trajdir, "g1")
        self.assertEqual(["One"], again["steps"])

    def test_an_unknown_goal_asks_nothing(self):
        class Boom:
            def generate_json(self, prompt):
                raise AssertionError("should not reach a provider")

        with mock.patch("human_compact.trajectory.providers.make",
                        return_value=Boom()):
            self.assertFalse(ui.plan_preview(self.trajdir, "nope")["ok"])

    def test_a_path_shaped_goal_id_is_refused(self):
        self.assertFalse(ui.plan_preview(self.trajdir, "../../etc/passwd")["ok"])

    def test_a_provider_failure_is_reported_not_faked(self):
        class Broken:
            def generate_json(self, prompt):
                raise RuntimeError("no provider configured")

        with mock.patch("human_compact.trajectory.providers.make",
                        return_value=Broken()):
            got = ui.plan_preview(self.trajdir, "g1")
        self.assertFalse(got["ok"])
        self.assertNotIn("steps", got)


class GoalDocumentRoundTripTests(unittest.TestCase):
    """A goal's notes are a whole document; nothing on the way in cuts it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = self.root / "chat-a"
        write_scope(self.a, [goal("a1", "goal in chat a")], [])
        self.document = "# Decisions\n" + "\n".join(
            f"- decision {n:04d} kept in full" for n in range(220))
        self.assertGreaterEqual(len(self.document), 6000)

    def test_a_six_thousand_character_document_survives_op_and_import(self):
        with server_for(self.a) as url:
            self.assertEqual(
                {"ok": True},
                post_json(url + "/api/op", {
                    "op": "set_notes", "goal_id": "a1",
                    "notes": self.document,
                }),
            )
            state = get_json(url + "/api/state")
            self.assertEqual(self.document, state["goals"][0]["notes"])

            imported = post_json(url + "/api/import", {
                "base_revision": state["revision"],
                "goals": [{
                    "id": "a1", "title": "goal in chat a", "done": False,
                    "status": "todo", "prio": "normal",
                    "notes": self.document, "desc": "", "children": [],
                }],
            })
            self.assertTrue(imported["ok"])
            self.assertEqual(
                self.document,
                get_json(url + "/api/state")["goals"][0]["notes"],
            )
