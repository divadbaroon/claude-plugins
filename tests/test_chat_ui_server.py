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
                expect(page.locator("#hc-prompt-links")).to_be_visible(
                    timeout=10_000
                )
                expect(page.locator("#hc-prompt-links .hc-pa-card")).to_have_count(1)

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
                expect(page.locator("#hc-prompt-links .hc-pa-card")).to_have_count(1)
                current = {
                    g["id"]: g
                    for g in chat_state.load_goals("chat-a", self.root)[0]["goals"]
                }
                self.assertEqual("in_progress", current["a1"]["status"])
                self.assertEqual("high", current["a1"]["priority"])
                self.assertEqual(["p-new"], current["a1"]["prompt_ids"])
                self.assertEqual(
                    ["analyzer-added todo"],
                    [t["text"] for t in current["a1"]["todos"]],
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
                expect(
                    page.get_by_text("No goals yet — add one below.", exact=True)
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

    def test_active_completion_stays_crossed_out_until_filter_changes(self):
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        goals_path = self.a / "goals.json"
        saved = json.loads(goals_path.read_text())
        root_title = (
            "Root goal with enough implementation detail to wrap across lines "
            "while its selected background and controls stay aligned"
        )
        child_title_text = (
            "Completed nested subgoal explaining persistence, reload, "
            "reconciliation, and rendering without overlapping adjacent rows"
        )
        saved["goals"][0]["title"] = root_title
        saved["goals"][1]["title"] = child_title_text
        saved["goals"][1]["parent_goal_id"] = "a1"
        goals_path.write_text(json.dumps(saved))

        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_text("+ Add subgoal", exact=True)).to_have_count(
                    2, timeout=10_000
                )
                expect(page.get_by_text("Add goal", exact=True)).to_have_count(1)
                self.assertEqual(
                    ["+ Add subgoal", "+ Add subgoal"],
                    page.locator('[title="Add subgoal"]').all_text_contents(),
                )
                expect(page.get_by_text("SELECTED GOAL", exact=True)).to_have_count(0)
                expect(
                    page.get_by_text("Copy appends goal metadata", exact=True)
                ).to_have_count(0)
                expect(
                    page.get_by_text(
                        "Markdown formats as you type · auto-saved with this goal",
                        exact=True,
                    )
                ).to_have_count(0)
                expect(page.locator('[placeholder^="Plan in markdown"]')).to_have_count(0)
                page.get_by_text("NOTES", exact=True).first.click()
                notes = page.locator('textarea[aria-label="Goal notes"]')
                expect(notes).to_be_visible()
                self.assertIn(notes.get_attribute("placeholder"), (None, ""))
                expect(
                    page.get_by_text(
                        "Markdown formats as you type · auto-saved with this goal",
                        exact=True,
                    )
                ).to_have_count(0)
                page.get_by_text("PROMPT", exact=True).first.click()

                metrics = page.evaluate(
                    """titles => {
                      const measure = text => {
                        const inner = [...document.querySelectorAll('.sc-interp')]
                          .find(el => el.textContent === text && el.parentElement &&
                            el.parentElement.parentElement.querySelector('[title="Add subgoal"]'));
                        const title = inner.parentElement;
                        const row = title.parentElement;
                        const add = row.querySelector('[title="Add subgoal"]');
                        const guide = row.firstElementChild;
                        const rr = row.getBoundingClientRect();
                        const tr = title.getBoundingClientRect();
                        const ar = add.getBoundingClientRect();
                        const gr = guide.getBoundingClientRect();
                        return {
                          row: { top: rr.top, bottom: rr.bottom, height: rr.height },
                          title: { left: tr.left, right: tr.right, height: tr.height },
                          add: { left: ar.left, top: ar.top, bottom: ar.bottom },
                          guide: { width: gr.width, height: gr.height },
                          background: getComputedStyle(row).backgroundColor
                        };
                      };
                      return { root: measure(titles[0]), child: measure(titles[1]) };
                    }""",
                    [root_title, child_title_text],
                )
                self.assertGreater(metrics["root"]["row"]["height"], 29)
                self.assertGreater(metrics["child"]["row"]["height"], 29)
                self.assertLessEqual(
                    metrics["root"]["row"]["bottom"],
                    metrics["child"]["row"]["top"] + 0.5,
                )
                for measured in metrics.values():
                    self.assertLessEqual(
                        measured["title"]["right"], measured["add"]["left"] + 0.5
                    )
                    self.assertGreaterEqual(
                        measured["add"]["top"], measured["row"]["top"] - 0.5
                    )
                    self.assertLessEqual(
                        measured["add"]["bottom"], measured["row"]["bottom"] + 0.5
                    )
                    self.assertAlmostEqual(
                        measured["guide"]["height"],
                        measured["row"]["height"],
                        delta=0.5,
                    )
                self.assertGreater(metrics["child"]["guide"]["width"], 0)
                self.assertGreater(
                    metrics["child"]["title"]["left"],
                    metrics["root"]["title"]["left"],
                )
                self.assertNotIn(
                    metrics["root"]["background"],
                    ("rgba(0, 0, 0, 0)", "transparent"),
                )

                toggles = page.locator('[title="Toggle complete"]')
                expect(toggles).to_have_count(2)
                toggles.nth(1).click()
                child_title = page.get_by_text(child_title_text, exact=True).first
                expect(child_title.locator("..")).to_have_css(
                    "text-decoration-line", "line-through"
                )
                child_title.click()
                completed_row = child_title.locator("..").locator("..")
                self.assertNotIn(
                    completed_row.evaluate("e => getComputedStyle(e).backgroundColor"),
                    ("rgba(0, 0, 0, 0)", "transparent"),
                )
                completed_bounds = toggles.evaluate_all("""marks => marks.map(mark => {
                    const row = mark.parentElement;
                    const rect = row.getBoundingClientRect();
                    const title = row.querySelector('[data-dc-tpl="42"]') ||
                      [...row.children].find(el => el.textContent.includes('subgoal'));
                    const add = row.querySelector('[title="Add subgoal"]');
                    const tr = title.getBoundingClientRect();
                    const ar = add.getBoundingClientRect();
                    return { top: rect.top, bottom: rect.bottom, height: rect.height,
                      titleRight: tr.right, addLeft: ar.left };
                  })""")
                self.assertLessEqual(
                    completed_bounds[0]["bottom"],
                    completed_bounds[1]["top"] + 0.5,
                )
                self.assertGreater(completed_bounds[1]["height"], 29)
                self.assertLessEqual(
                    completed_bounds[1]["titleRight"],
                    completed_bounds[1]["addLeft"] + 0.5,
                )
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    persisted = {
                        item["id"]: item
                        for item in json.loads(goals_path.read_text())["goals"]
                    }
                    if persisted["a2"]["status"] == "completed":
                        break
                    time.sleep(0.05)
                self.assertEqual("completed", persisted["a2"]["status"])

                page.reload(wait_until="domcontentloaded")
                child_title = page.get_by_text(child_title_text, exact=True).first
                expect(child_title.locator("..")).to_have_css(
                    "text-decoration-line", "line-through", timeout=10_000
                )

                page.get_by_text("done (1)", exact=True).click()
                page.get_by_text("active (1)", exact=True).click()
                expect(page.locator('[title="Toggle complete"]')).to_have_count(1)
                self.assertNotIn(
                    child_title_text,
                    page.locator('[title="Toggle complete"]')
                    .locator("..")
                    .all_text_contents(),
                )
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
                trigger = page.locator("#hc-prompt-links .hc-pa-add")
                expect(trigger).to_be_visible(timeout=10_000)
                overlay = page.locator("#hc-prompt-picker")

                trigger.click()
                expect(overlay).to_be_visible()
                page.locator("#hc-prompt-picker .hc-pa-close").click()
                expect(overlay).to_be_hidden()
                expect(trigger).to_be_focused()

                trigger.click()
                expect(overlay).to_be_visible()
                page.keyboard.press("Escape")
                expect(overlay).to_be_hidden()
                expect(trigger).to_be_focused()
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
