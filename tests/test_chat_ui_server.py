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
from human_compact.trajectory import project_store as PS  # noqa: E402


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


def open_prompt_tab(page):
    """The rail opens on TODOs; the assembled prompt is the other tab."""
    page.locator(".hc-rail-tabs").get_by_text("Prompt", exact=True).click()
    page.wait_for_selector(".hc-rail-copy", state="visible", timeout=10_000)


def write_scope(path, goals, prompts, bound=True):
    """A chat's scope on disk. Bound by default: a fixture with goals in it
    is a chat already in use, and an unbound chat is asked which project it
    is for before it is shown anything -- which is a different test."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "goals.json").write_text(json.dumps({"version": 1, "goals": goals}))
    (path / "important.json").write_text(json.dumps({"items": []}))
    (path / "prompts.json").write_text(json.dumps({"prompts": prompts}))
    spot = path / "manifest.json"
    manifest = {}
    if spot.exists():
        try:
            manifest = json.loads(spot.read_text())
        except ValueError:
            manifest = {}
    manifest.setdefault("schema_version", 1)
    manifest.setdefault("session_id", path.name)
    if bound:
        # The moment alone: no project_home, so the workspace still names
        # whatever it named before this flag existed.
        manifest["project_bound_at"] = "2026-01-01T00:00:00+00:00"
    else:
        manifest.pop("project_bound_at", None)
        manifest.pop("project_home", None)
    spot.write_text(json.dumps(manifest))


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


def get_json(url, timeout=2):
    with NO_PROXY_OPENER.open(url, timeout=timeout) as response:
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

    def test_no_control_carries_the_demo_copy_onto_goals_the_vault_minted(self):
        """The artifact keys its sample descriptions by g1..g4.

        Those are the ids `goals.next_goal_id` mints, so the collision is the
        common case in a chat workspace, not an edge one. Every control below
        persists -- a filter chip and the theme toggle as much as a rename --
        and each used to carry four sentences nobody wrote into goals.json,
        and from there into the prompt the reader copies.
        """
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        minted = self.root / "minted-ids"
        goals = [goal("g%d" % n, "real goal %d" % n) for n in (1, 2, 3, 4)]
        goals[1]["status"] = "in_progress"
        write_scope(minted, goals, [])
        goals_path = minted / "goals.json"

        with server_for(minted) as url, sync_playwright() as playwright:
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
                expect(page.get_by_text("real goal 2", exact=True).first
                       ).to_be_visible(timeout=10_000)

                page.get_by_text("In progress 1", exact=True).click()
                page.get_by_text("real goal 2", exact=True).first.click()
                # A chat opens dark, so the toggle on offer is the other one.
                page.locator('[title="Switch to light mode"]').click()
                # The bridge mirrors localStorage to the server on an 800 ms
                # poll; two seconds is past whichever poll each of those
                # three lands on.
                page.wait_for_timeout(2_000)

                self.assertEqual(
                    ["", "", "", ""],
                    [g["description"]
                     for g in get_json(url + "/api/state")["goals"]],
                )
                on_disk = json.loads(goals_path.read_text())["goals"]
                self.assertEqual(["g1", "g2", "g3", "g4"],
                                 [g["id"] for g in on_disk])
                self.assertEqual(["", "", "", ""],
                                 [g["description"] for g in on_disk])

                # And nothing reaches the prompt the reader takes away.
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_text("real goal 2", exact=True).first
                       ).to_be_visible(timeout=10_000)
                page.get_by_text("real goal 2", exact=True).first.click()
                open_prompt_tab(page)
                expect(page.locator(".hc-rail-ctx-body")).to_be_visible()
                copy = page.get_by_text("Copy prompt", exact=True)
                expect(copy).to_be_visible()
                copy.click()
                expect(page.get_by_text("copied \u2713", exact=True)
                       ).to_be_visible()
                copied = page.evaluate("() => navigator.clipboard.readText()")
                self.assertNotIn("Stand up the shared goal model", copied)
                self.assertNotIn("Objective:", copied)
                context.close()
            finally:
                browser.close()

    def test_a_goal_added_in_the_tree_gets_no_demo_context(self):
        """The artifact mints a new goal with no context at all.

        Its own defaults then filled the gap, so the prompt for a goal the
        reader had just named claimed an objective, a GitHub repo and a
        document that belong to the artifact's demo -- and stayed that way
        until the page was reloaded.
        """
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
                expect(page.get_by_text("goal in chat a", exact=True).first
                       ).to_be_visible(timeout=10_000)

                page.get_by_text("Add goal", exact=True).click()
                page.keyboard.type("brand new goal")
                page.keyboard.press("Enter")
                expect(page.get_by_text("brand new goal", exact=True).first
                       ).to_be_visible(timeout=10_000)
                page.get_by_text("brand new goal", exact=True).first.click()
                open_prompt_tab(page)
                expect(page.locator(".hc-rail-ctx-body")).to_be_visible()
                # What the tab prints is what Copy takes. Read the copy.
                context = page.context
                context.grant_permissions(["clipboard-read",
                                           "clipboard-write"])
                page.wait_for_timeout(1_200)
                page.get_by_text("Copy prompt", exact=True).click()
                expect(page.get_by_text("copied \u2713", exact=True)
                       ).to_be_visible()
                draft = page.evaluate("() => navigator.clipboard.readText()")
                self.assertIn("brand new goal", draft)
                self.assertNotIn("Get the drawable frame", draft)
                self.assertNotIn("divadbaroon/claude-plugins", draft)
                self.assertNotIn("design-notes.md", draft)
                self.assertNotIn("Objective:", draft)
                self.assertNotIn("Code context:", draft)
                self.assertNotIn("Document context:", draft)
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

                page.get_by_text("Active 3", exact=True).click()
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
                expect(page.get_by_text("Done 1", exact=True)
                       ).to_be_visible()
                expect(page.get_by_text("Active 2", exact=True)
                       ).to_be_visible()

                # It is struck through wherever it is drawn.
                page.get_by_text("All 3", exact=True).click()
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
                page.get_by_text("Active 2", exact=True).click()
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
        """Weight and colour of one filter chip, as the reader sees them.

        The chip is a name and, in its own element, the count: matched on
        the two together, the way the row reads.
        """
        return page.evaluate(
            """label => {
              const el = [...document.querySelectorAll('.hc-chip')].find(
                e => e.textContent.trim() === label);
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
                # The page heading went with it: this window has one page,
                # and the goal rail is where its name lives now.
                expect(page.get_by_text("GOALS", exact=True)).to_be_visible()
                self.assertEqual("goals", page.evaluate(
                    "JSON.parse(localStorage.getItem('hc-vault-ui-v1')).page"))

                # All, on the page and not only in the store: the chip is
                # the selected one and the completed goal is in the tree.
                selected = self.chip_style(page, "All 2")
                unselected = self.chip_style(page, "Active 1")
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
                expect(page.get_by_text("All 2", exact=True)).to_be_visible(
                    timeout=10_000
                )
                page.get_by_text("Done 1", exact=True).click()
                self.assertEqual(
                    "700", self.chip_style(page, "Done 1")["weight"]
                )

                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_text("Done 1", exact=True)).to_be_visible(
                    timeout=10_000
                )
                # The default only applies to a page with no history. A
                # choice the reader made is theirs to keep.
                self.assertEqual(
                    "700", self.chip_style(page, "Done 1")["weight"]
                )
                self.assertEqual(
                    "500", self.chip_style(page, "All 2")["weight"]
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
                # CONTEXT is the only tab left. The assembled prompt did not
                # go away with the tab that used to hold it -- it is on
                # screen the whole time now, in its own rail.
                #
                # The tab bar is named by a template patch, so "PROMPT is
                # hidden inside .hc-tabs" is also true of a page where the
                # patch missed and .hc-tabs does not exist. Prove the bar is
                # there before proving what is not in it.
                expect(page.locator(".hc-tabs")).to_have_count(1)
                expect(page.locator(".hc-tabs").get_by_text(
                    "PROMPT", exact=True)).to_be_hidden()
                expect(page.locator(".hc-rail-tabs").get_by_text(
                    "Prompt", exact=True)).to_be_visible()
                expect(page.locator(".hc-rail-tabs").get_by_text(
                    "TODOs", exact=True)).to_be_visible()

                # A pane saved from a build that still offered one must not
                # restore an inspector this scope no longer draws.
                page.evaluate("""() => {
                    const key = "hc-vault-ui-v1";
                    const saved = JSON.parse(localStorage.getItem(key));
                    saved.paneTab = "prompt";
                    localStorage.setItem(key, JSON.stringify(saved));
                }""")
                page.reload(wait_until="domcontentloaded")
                context_tab = page.get_by_text("CONTEXT", exact=True)
                expect(context_tab).to_be_visible(timeout=10_000)
                # What the reader sees, not what the store says: CONTEXT is
                # the tab drawn selected -- in ink, with no accent underline
                # (a lone tab has nothing to be underlined against) -- and
                # the pane drawn under it is the Context pane.
                expect(context_tab).to_have_css(
                    "border-bottom-color", "rgba(0, 0, 0, 0)"
                )
                ink = page.evaluate(
                    "() => getComputedStyle(document.querySelector('.hc'))"
                    ".getPropertyValue('--ink').trim()")
                self.assertTrue(ink)
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


    # A goal opens as an empty document: no spine of headings. What the
    # reader writes is rendered Obsidian-fashion under their own caret --
    # the marks (#, **, -) are drawn transparent so the text stays aligned
    # with the caret, headings bold, bullets as dots.
    DEFAULT_DOC = ""

    EDITOR = '[placeholder^="Write in markdown"]'

    def overlay_text(self, page):
        """What the reader sees rendered under their own caret."""
        return page.evaluate(
            """sel => {
                 const ta = document.querySelector(sel);
                 return ta.parentElement.firstElementChild.textContent;
               }""", self.EDITOR)

    def test_a_goal_with_no_notes_opens_empty_and_renders_marks_hidden(self):
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

                # Empty: no headings are put in the reader's way.
                self.assertEqual("", editor.input_value())
                for head in ("# Objective", "# TODOs", "# Decisions", "# Prompt"):
                    self.assertNotIn(head, self.overlay_text(page))

                # Type a heading and a bullet: the heading text is drawn
                # bold, its "# " transparent (present, so the caret stays
                # aligned, but not seen); the "- " is drawn as a dot.
                editor.fill("# Objective\nShip it\n- first")
                page.wait_for_timeout(300)
                self.assertEqual(["700", "rgba(0, 0, 0, 0)", "\u2022 "], page.evaluate(
                    """sel => {
                         const ta = document.querySelector(sel);
                         const spans = [...ta.parentElement.firstElementChild
                           .querySelectorAll('span')];
                         const head = spans.find(s => s.textContent === 'Objective');
                         const mark = spans.find(s => s.textContent === '# ');
                         const dot = spans.find(s => s.textContent.endsWith('\u2022 '));
                         return [head && getComputedStyle(head).fontWeight,
                                 mark && getComputedStyle(mark).color,
                                 dot && dot.textContent];
                       }""", self.EDITOR))
                editor.fill("")

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

        written = "# Decisions\n- we chose sqlite\n"
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
                    # rendered under the caret with the bullet drawn as a dot
                    self.assertIn("\u2022 we chose sqlite", self.overlay_text(page))

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

    def test_a_linked_row_ends_at_its_date_when_there_is_no_conversation(self):
        """A separator with nothing after it is not punctuation.

        `chat_state` writes a prompt record's id, ordinal, role, text and
        created_at and no session_id, so in the configuration /bart
        actually launches every linked row read `Aug 17, 2026·`.
        """
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
        goals_path.write_text(json.dumps(stored))
        self.assertNotIn(
            "session_id",
            [p for p in self.prompts_a if p["id"] == "p-new"][0],
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
                expect(page.get_by_text("new human prompt", exact=True)
                       ).to_be_visible(timeout=10_000)

                dated = page.evaluate(
                    """() => [...document.querySelectorAll('span')]
                         .map(e => e.textContent)
                         .filter(t => /^[A-Z][a-z]{2} \\d+, \\d{4}/.test(t))""")
                self.assertTrue(dated, "the row must still carry its date")
                self.assertEqual(
                    [], [t for t in dated if t.rstrip().endswith("\u00b7")])
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

    def test_the_prompt_rail_is_the_prompt_and_a_real_copy(self):
        """The prompt is a column, not a tab.

        It used to live behind PROMPT, which swapped the goal's document out
        for it; the two are read together, so the rail shows both at once and
        the tab is gone. The text is still the artifact's own assembly -- the
        rail prints the same `draft` the pane did.
        """
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

                # Both at once: the document stays editable while the prompt
                # it assembles is on screen beside it.
                rail = page.locator(".hc-rail-right")
                expect(rail).to_be_visible()
                expect(page.locator(self.EDITOR)).to_have_count(1)

                open_prompt_tab(page)
                # The tab prints the prompt and offers no box to type in:
                # what is on screen is the whole of what a build would send.
                expect(rail.locator(".hc-rail-ctx-body")).to_have_count(1)
                expect(rail.locator("textarea.hc-rail-code")).to_have_count(0)

                # Nothing here starts a run; every op behind one refuses.
                expect(page.get_by_text("run agent", exact=True)
                       ).to_have_count(0)
                expect(page.get_by_text("AGENT STATUS", exact=True)
                       ).to_have_count(0)
                # And nothing offers to rewrite it: no model call stands
                # behind a regenerate in this scope.
                expect(page.get_by_text("Regenerate", exact=True)
                       ).to_have_count(0)
                expect(page.get_by_text("RECOMMENDED EDITS", exact=True)
                       ).to_have_count(0)

                copy = page.get_by_text("Copy prompt", exact=True)
                expect(copy).to_be_visible()
                copy.click()
                # The label only changes once the clipboard has it.
                expect(page.get_by_text("copied \u2713", exact=True)
                       ).to_be_visible()
                # What leaves is the assembled prompt itself -- the same
                # string the tab is printing.
                copied = page.evaluate("() => navigator.clipboard.readText()")
                # The goal's notes ride in the tree, under the goal, as written.
                self.assertIn("Ship the document pane.", copied)
                self.assertIn("- we chose sqlite", copied)
                context.close()
            finally:
                browser.close()

    def test_the_prompt_is_assembled_from_the_document_and_copied(self):
        """It is assembled, not authored, and reading it changes nothing.

        The tab was a box to type in with the assembled prompt printed above
        it: the reader's paragraph landed in the goal's `prompt_md` and the
        string that mattered was the one they could not touch. Only the
        prompt is left. It is a rendering of the goal's document, so there is
        no edit to lose and nothing it can write back -- and what Copy takes
        is exactly what is on screen.
        """
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        goals_path = self.a / "goals.json"
        stored = json.loads(goals_path.read_text())
        stored["goals"][0]["notes"] = "# Objective\nShip the document pane.\n"
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

                open_prompt_tab(page)
                # No box to type in, and the objective from the document is
                # in what the tab prints.
                expect(page.locator("textarea.hc-rail-code")).to_have_count(0)
                body = page.locator(".hc-rail-ctx-body")
                expect(body).to_contain_text("Ship the document pane.",
                                             timeout=15_000)

                # Reading it writes nothing back: the goal's own prompt field
                # is still empty, and the document it was assembled from is
                # untouched.
                stored_goal = [g for g in get_json(url + "/api/state")["goals"]
                               if g["id"] == "a1"][0]
                self.assertEqual("", stored_goal["prompt_md"])
                self.assertNotIn("# Prompt", stored_goal["notes"])
                self.assertIn("# Objective\nShip the document pane.",
                              stored_goal["notes"])
                self.assertEqual("", stored_goal["description"])

                # Copy takes what is on screen, whole.
                copy = page.get_by_text("Copy prompt", exact=True)
                copy.click()
                expect(page.get_by_text("copied \u2713", exact=True)
                       ).to_be_visible()
                copied = page.evaluate("() => navigator.clipboard.readText()")
                # The goal's notes ride in the tree, under the goal, as written.
                self.assertIn("Ship the document pane.", copied)
                self.assertNotIn("Implement this goal for me.", copied)
                context.close()
            finally:
                browser.close()

    # --- the launch layout -------------------------------------------------

    def test_the_header_chips_count_the_tree_and_pick_the_filter(self):
        """Four chips, four counts, and the one in force is the one filled.

        The counts are the reason the chips are a header and not a menu: they
        are the only place a chat says how much work it is carrying.
        """
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        counted = self.root / "counted"
        goals = [goal("g1", "one"), goal("g2", "two"), goal("g3", "three"),
                 goal("g4", "four")]
        goals[1]["status"] = "in_progress"
        goals[2]["status"] = "completed"
        write_scope(counted, goals, [])

        with server_for(counted) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                chips = page.locator(".hc-chip")
                expect(chips).to_have_count(4, timeout=10_000)
                self.assertEqual(
                    ["All 4", "Active 3", "In progress 1", "Done 1"],
                    chips.all_inner_texts(),
                )

                # A chat opens on All, and All is the filled chip.
                self.assertEqual("700", self.chip_style(page, "All 4")["weight"])
                self.assertEqual("500",
                                 self.chip_style(page, "Done 1")["weight"])

                page.get_by_text("Done 1", exact=True).click()
                self.assertEqual("700", self.chip_style(page, "Done 1")["weight"])
                self.assertEqual("500", self.chip_style(page, "All 4")["weight"])
                # And the tree is the one goal that chip counts.
                expect(page.locator(".hc-rowtitle")).to_have_count(1)
                expect(page.locator(".hc-rowtitle")).to_have_text("three")

                # The rail counts the whole tree, whatever the filter shows.
                expect(page.locator(".hc-rail-left .hc-rail-count")
                       ).to_have_text("4")
            finally:
                browser.close()

    def test_a_finished_turn_arrives_as_a_card_with_the_builds(self):
        """A chat answering is news of the same kind as a build coming back.

        So it arrives in the same corner, counts on the same bell, and takes
        no line from the columns: the bar under the header that used to
        report this pushed the whole workspace down every time the terminal
        finished a turn.
        """
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
                expect(page.locator(".hc-rail-left")).to_be_visible(
                    timeout=10_000)
                # What the columns do while the terminal works is nothing:
                # measured before, and again while a card is up.
                rail_before = page.locator(".hc-rail-left").bounding_box()

                chat_state.add_notice("chat-a", "session_stopped",
                                      "Done. Tests pass.", self.root)
                card = page.locator(".hc-alert[data-hc-alert-kind"
                                    "=\"session_stopped\"]")
                expect(card).to_have_count(1, timeout=4_000)
                # What a Stop hook proves is that the turn ended. It does not
                # prove a goal moved or a task closed, so it does not say so.
                expect(card.locator(".hc-alert-title")).to_have_text(
                    "Claude finished responding")
                expect(card.locator(".hc-alert-detail")).to_have_text(
                    "Done. Tests pass.")
                # And it names no goal: there is no one row it reports on.
                expect(card.locator(".hc-alert-goal")).to_have_count(0)

                box = card.bounding_box()
                self.assertGreater(box["x"], 900, "a card sits on the right")
                self.assertLess(box["width"], 400, "a card is not a bar")
                self.assertLess(box["y"], 100, "under the header")
                # The columns are where they were: it is an overlay, not a
                # line the workspace has to give up.
                held = page.locator(".hc-rail-left").bounding_box()
                self.assertAlmostEqual(rail_before["y"], held["y"], delta=2)

                # It counts on the bell with the builds, and × reads it.
                count = page.locator(".hc-alerts .hc-bell-count")
                expect(count).to_have_text("1", timeout=2_000)
                card.locator(".hc-alert-close").click()
                expect(card).to_have_count(0, timeout=2_000)
                expect(count).to_have_text("0")
                back = page.locator(".hc-rail-left").bounding_box()
                self.assertAlmostEqual(rail_before["y"], back["y"], delta=2)
            finally:
                browser.close()

    def test_a_source_added_here_outlives_the_page_and_the_server(self):
        """SOURCES is one line naming the records, and a box that edits them.

        The pane that held these as three textboxes is dormant, and the rail
        of pills it became is gone too: adding and removing happen in one
        box, and the line under the title names what is attached. Nothing
        new is written -- both lists are the artifact's own, so an edit
        lands on set_sources by the path that was already there.
        """
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        def sources(url):
            return [g["sources"] for g in get_json(url + "/api/state")["goals"]
                    if g["id"] == "a1"][0]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                with server_for(self.a) as url:
                    self.assertEqual([], sources(url))
                    page = browser.new_page(
                        viewport={"width": 1400, "height": 900})
                    page.goto(url, wait_until="domcontentloaded")
                    expect(page.locator(".hc-sources")).to_be_visible(
                        timeout=10_000)
                    expect(page.locator(".hc-src")).to_have_count(0)

                    page.get_by_text("+ Add source", exact=True).click()
                    dialog = page.locator(".hc-ask-box")
                    expect(dialog).to_be_visible()
                    # Which kind, then the value: the store keeps three, and
                    # a placeholder row is not one of them.
                    dialog.get_by_text("GitHub repo", exact=True).click()
                    dialog.locator("input").fill("owner/repo")
                    page.keyboard.press("Enter")

                    chip = page.locator(".hc-src")
                    expect(chip).to_have_count(1)
                    expect(chip.locator(".hc-src-tag")).to_have_text("GITHUB")
                    expect(chip.locator(".hc-src-label")).to_have_text(
                        "owner/repo")

                    page.get_by_text("+ Add source", exact=True).click()
                    page.locator(".hc-ask-box").get_by_text(
                        "Document", exact=True).click()
                    page.locator(".hc-ask-box input").fill("design.md")
                    page.keyboard.press("Enter")
                    expect(page.locator(".hc-src")).to_have_count(2)

                    deadline = time.monotonic() + 6
                    stored = []
                    while time.monotonic() < deadline:
                        stored = sources(url)
                        if len(stored) == 2:
                            break
                        time.sleep(0.1)
                    self.assertEqual(
                        [("github", "owner/repo"), ("doc", "design.md")],
                        [(row["type"], row["label"]) for row in stored],
                    )

                    # A reload of the same page reads them back.
                    page.reload(wait_until="domcontentloaded")
                    expect(page.locator(".hc-src")).to_have_count(
                        2, timeout=10_000)

                    # And so does a browser that never saw the page that
                    # wrote them: the record is the server's, not the tab's.
                    page.close()

                with server_for(self.a) as second:
                    self.assertEqual(
                        [("github", "owner/repo"), ("doc", "design.md")],
                        [(row["type"], row["label"])
                         for row in sources(second)],
                    )
                    fresh = browser.new_context(
                        viewport={"width": 1400, "height": 900})
                    page = fresh.new_page()
                    page.goto(second, wait_until="domcontentloaded")
                    expect(page.locator(".hc-src")).to_have_count(
                        2, timeout=10_000)

                    # Removing one is the same round trip in reverse.
                    page.locator(".hc-src").first.locator(
                        ".hc-src-rm").click()
                    expect(page.locator(".hc-src")).to_have_count(1)
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline:
                        stored = sources(second)
                        if len(stored) == 1:
                            break
                        time.sleep(0.1)
                    self.assertEqual([("doc", "design.md")],
                                     [(row["type"], row["label"])
                                      for row in stored])
                    fresh.close()
            finally:
                browser.close()

    def test_the_state_reports_what_this_chat_has_been_told(self):
        """`injection` is read, never written, from files that already exist."""
        empty = ui._payload(self.a, chat_scoped=True)["injection"]
        self.assertEqual(
            {"cached": False, "last_delta_chars": None, "last_at": None,
             "active": False,
             "reads": ["session start", "prompt", "subagent", "task"]},
            empty,
        )

        chat_state.mark_goals_ui_invoked("chat-a", self.root)
        (self.a / "goal_context.md").write_text("# goals\n- one\n")
        chat_state.save_context_snapshot("chat-a", "# goals\n- one\n",
                                         self.root)
        told = ui._payload(self.a, chat_scoped=True)["injection"]
        self.assertTrue(told["cached"])
        self.assertTrue(told["active"])
        self.assertEqual(0, told["last_delta_chars"])
        self.assertIsInstance(told["last_at"], str)

        # A document the model has not seen yet is a pending change, and its
        # size is what a next message would carry.
        (self.a / "goal_context.md").write_text("# goals\n- one\n- two\n")
        pending = ui._payload(self.a, chat_scoped=True)["injection"]
        self.assertGreater(pending["last_delta_chars"], 0)

        chat_state.disable_goals_ui("chat-a", self.root)
        off = ui._payload(self.a, chat_scoped=True)["injection"]
        self.assertFalse(off["active"])
        # Disabling clears the snapshot, so there is no base left to diff.
        self.assertFalse(off["cached"])
        self.assertIsNone(off["last_delta_chars"])

    def test_the_injection_card_is_computed_after_the_lock_is_given_back(self):
        """It is two read-only file reads, and something waits on that lock.

        ``_state_access`` is chat_state's cross-process session lock, shared
        with ingestion and analysis. The hook that renders an injection into
        a turn waits half a second for the same lock before giving up and
        dropping the injection, so a poll running every 1.5s per open tab
        must not hold it for work that does not need it. Reading a snapshot
        and diffing a file does not need it.
        """
        released = []
        real_access = ui._state_access

        @contextmanager
        def watched(trajdir, chat_scoped):
            with real_access(trajdir, chat_scoped):
                yield
            released.append(True)

        def injection(session_id, root):
            self.assertTrue(released,
                            "injection computed while still under the lock")
            return {"sentinel": session_id}

        with mock.patch.object(ui, "_state_access", watched), \
                mock.patch.object(ui, "_injection_state", injection):
            payload = ui._payload(self.a, chat_scoped=True)
            unlocked = ui._payload(self.a, chat_scoped=False)

        # The key is still on the payload, still filled by the same
        # function, and still only in a chat.
        self.assertEqual([True], released[:1])
        self.assertEqual({"sentinel": "chat-a"}, payload["injection"])
        self.assertEqual(["a1", "a2"], [g["id"] for g in payload["goals"]])
        self.assertFalse(unlocked["injection"]["cached"])
        self.assertEqual(["session start", "prompt", "subagent", "task"],
                         unlocked["injection"]["reads"])

    def test_the_injection_card_is_not_drawn(self):
        """The rail carries the TODOs and the prompt; the injection status
        card that used to sit under them is gone. The state it printed is
        still on the payload (the test above) -- it just has no card."""
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        chat_state.mark_goals_ui_invoked("chat-a", self.root)
        (self.a / "goal_context.md").write_text("# goals\n- one\n")
        chat_state.save_context_snapshot("chat-a", "# goals\n- one\n",
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
                expect(page.locator(".hc-rail-right")).to_be_visible(
                    timeout=10_000)
                expect(page.locator(".hc-inject")).to_have_count(0)
                expect(page.get_by_text("context injection")).to_have_count(0)
            finally:
                browser.close()

    def test_the_launch_layout_is_a_chat_thing_only(self):
        """A global vault renders as it did: none of this is on its page."""
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")

        with server_for(self.a, chat_scoped=False) as url, \
                sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=chrome,
                headless=True,
                args=["--disable-background-networking"],
            )
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                expect(page.locator(".hc")).to_be_visible(timeout=10_000)
                page.wait_for_timeout(2_000)
                self.assertIsNone(page.evaluate(
                    "() => document.documentElement"
                    ".getAttribute('data-hc-launch')"))
                self.assertIsNone(page.evaluate(
                    "() => document.getElementById('hc-launch-style')"))
                for gone in (".hc-rail-right", ".hc-rail-left", ".hc-shell",
                             ".hc-chip", ".hc-sources", ".hc-inject"):
                    expect(page.locator(gone)).to_have_count(0)
                expect(page.get_by_text("Vault", exact=True).first
                       ).to_be_visible()
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
            # And the state says which session, because the browser has no
            # other way to learn it and the tab is named after it.
            self.assertEqual("chat-a", get_json(url_a + "/api/state")["session_id"])
            self.assertEqual("chat-b", get_json(url_b + "/api/state")["session_id"])

    def test_a_global_vault_has_no_session_to_report_on(self):
        # The banner answers "is the chat I am attached to done?". A global
        # vault is attached to no chat, so the field is present and empty
        # rather than absent -- the bridge reads one shape in both scopes.
        payload = ui._payload(self.a, chat_scoped=False)
        self.assertEqual([], payload["notices"])
        self.assertIsNone(payload["session_id"])

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
                # The artifact unpacks its template over the whole
                # documentElement, taking the bundle's own <title> with it.
                # The standing chat sweep is what puts a name back.
                title = "Engelbart \u00b7 " + "chat-a"[:8]
                expect(page).to_have_title(title, timeout=5_000)
                # Two polls' worth: the state carrying the old notice has
                # certainly landed by now.
                page.wait_for_timeout(3_500)
                expect(page.locator(".hc-alert")).to_have_count(0)
                self.assertEqual(title, page.title())

                chat_state.add_notice(
                    "chat-a", "subagent_returned",
                    "Explore: Analysis complete. Found 3 potential issues",
                    self.root)

                card = page.locator(".hc-alert")
                expect(card).to_have_count(1, timeout=4_000)
                expect(card.locator(".hc-alert-title")
                       ).to_have_text("A subagent returned")
                expect(card.locator(".hc-alert-detail")).to_have_text(
                    "Explore: Analysis complete. Found 3 potential issues")
                expect(card.locator(".hc-alert-close")).to_be_visible()
                # A workspace on another screen has to be able to say so
                # from the tab strip alone, without losing which conversation
                # it is watching.
                self.assertEqual("\u25cf " + title, page.title())

                # It takes itself away; nothing here was clicked. What it
                # leaves behind is the point of moving it here: the reader
                # who was looking at the terminal for those six seconds
                # still finds it on the bell, and the tab still says so.
                expect(card).to_have_count(0, timeout=12_000)
                expect(page.locator(".hc-alerts .hc-bell-count")
                       ).to_have_text("1")
                self.assertEqual("\u25cf " + title, page.title())

                # And it is not shown twice for the same event.
                page.wait_for_timeout(3_500)
                expect(card).to_have_count(0)
                expect(page.locator(".hc-alerts .hc-bell-count")
                       ).to_have_text("1")

                # Reading it in the center is what takes the mark off.
                page.locator(".hc-alerts .hc-bell").click()
                center = page.locator(".hc-alert-center")
                expect(center).to_have_count(1, timeout=2_000)
                expect(center.locator(".hc-alert-row")).to_have_count(1)
                center.locator(
                    ".hc-alert-center-act[data-hc-alert-act=\"read-all\"]"
                ).click()
                expect(page.locator(".hc-alerts .hc-bell-count")
                       ).to_have_text("0")
                self.assertEqual(title, page.title())
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
                title = "Engelbart \u00b7 " + "chat-a"[:8]
                expect(page).to_have_title(title, timeout=5_000)

                chat_state.add_notice("chat-a", "session_stopped",
                                      "Done. Tests pass.", self.root)
                card = page.locator(".hc-alert")
                expect(card).to_have_count(1, timeout=4_000)
                expect(card.locator(".hc-alert-title")
                       ).to_have_text("Claude finished responding")

                # Reading it holds it open past the moment it would have gone.
                card.hover()
                page.wait_for_timeout(9_000)
                expect(card).to_have_count(1)

                self.assertEqual("\u25cf " + title, page.title())

                # \u00d7 dismisses it as read, which is what clears the tab.
                card.locator(".hc-alert-close").click()
                expect(card).to_have_count(0, timeout=2_000)
                expect(page.locator(".hc-alerts .hc-bell-count")
                       ).to_have_text("0")
                self.assertEqual(title, page.title())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()


class PreHydrationMaskTests(unittest.TestCase):
    """The artifact's own first frame must never reach the reader.

    It paints a rust splash and the raw template -- unresolved bindings and
    the global onboarding dialog included -- before it swaps documentElement.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"
        write_scope(self.a, [goal("g1", "real goal one")], [])

    def test_the_page_is_served_hidden_until_the_artifact_unpacks(self):
        with server_for(self.a) as url:
            with NO_PROXY_OPENER.open(url, timeout=5) as response:
                body = response.read().decode()
        self.assertIn('id="hc-preboot"', body)
        self.assertLess(body.index('id="hc-preboot"'), body.index("</head>"),
                        "the mask must be in the head, before any body paint")
        self.assertIn("visibility:hidden", body)
        self.assertIn("hc-preboot", body[body.index("setTimeout"):],
                      "a failsafe must remove the mask if the unpack never runs")
        # Every /bart is a fresh port, so a chat workspace is always a new
        # origin with no saved theme. Following the operating system there
        # paints white in front of a dark workspace -- the flash the mask was
        # added to remove.
        self.assertIn(ui.CHAT_GROUND, body.split("</head>")[0])
        self.assertNotIn("prefers-color-scheme", body.split("</head>")[0])

    def test_a_global_vault_is_masked_on_its_own_ground(self):
        from human_compact.trajectory import ui
        self.assertIn("#fff", ui.preboot_mask(False))
        self.assertIn(ui.CHAT_GROUND, ui.preboot_mask(True))

    def test_the_mask_is_painted_in_the_workspace_s_own_ground(self):
        """Not merely dark: the colour the dressed workspace lands on.

        The mask and the workspace meet on the same pixel at reveal. A near
        miss is a seam the reader reads as a second page arriving, which is
        the whole complaint the mask exists to answer.
        """
        from human_compact.trajectory import ui
        self.assertEqual("#0d1117", ui.CHAT_GROUND,
                         "the artifact paints .hc on #0d1117")

    def test_the_reader_never_sees_the_onboarding_dialog_or_a_raw_binding(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - exercised when installed
            self.skipTest("playwright is not installed")
        browser_path = browser_executable()
        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path)
            page = browser.new_page()
            # A fast machine closes the gap between the artifact's own layout
            # and the launch skin inside one frame, which is exactly the gap
            # this test is about. Slow the page down until the gap is real.
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": 8})
            # Sample visibility from inside the page at the moment the parser
            # finishes, which is before the artifact's DOMContentLoaded unpack.
            page.add_init_script(
                "window.__hcVis = [];"
                "document.addEventListener('readystatechange', function () {"
                "  try { window.__hcVis.push(document.readyState + ':' +"
                "    getComputedStyle(document.documentElement).visibility); }"
                "  catch (e) {} }, true);"
                # Record what the document was wearing the first frame it
                # could be seen at all.
                "window.__hcFirstVisibleSkin = null;"
                "(function watch() {"
                "  try {"
                "    var root = document.documentElement;"
                "    if (root && getComputedStyle(root).visibility === 'visible'"
                "        && window.__hcFirstVisibleSkin === null) {"
                "      window.__hcFirstVisibleSkin ="
                "        root.getAttribute('data-hc-launch') || 'undressed';"
                "      return;"
                "    }"
                "  } catch (e) {}"
                "  requestAnimationFrame(watch);"
                "})();")
            page.goto(url)
            page.wait_for_selector("text=real goal one", timeout=10000)
            seen = page.evaluate("window.__hcVis || []")
            self.assertTrue(
                any(entry == "interactive:hidden" for entry in seen),
                "the parsed-but-unhydrated document must never be shown: %r" % (seen,))
            # And the first frame the reader can see is already the launch
            # workspace, not the artifact's own layout waiting to be dressed.
            self.assertEqual(
                "chat",
                page.evaluate("window.__hcFirstVisibleSkin"),
                "the page became visible before the launch skin was applied")
            # The unpack replaced the head the mask lived in, so it is gone and
            # the page is visible again.
            self.assertEqual(0, page.locator("#hc-preboot").count())
            self.assertEqual(
                "visible",
                page.evaluate("getComputedStyle(document.documentElement).visibility"))
            self.assertEqual(0, page.get_by_text("Keep your Claude Code history").count())
            browser.close()

    def test_the_hold_is_never_broken_by_the_browser_s_own_white(self):
        """The colour under a hidden document is the one the reader sees.

        `visibility:hidden` hides the element, not the viewport canvas: the
        canvas keeps painting the background propagated from the root, and
        where the root has none it falls through to the body's. The artifact
        replaces documentElement -- taking the served mask with it -- and its
        own body is white, so the hold that was meant to remove the flash
        ends up painting it: dark, then white, then the workspace.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - exercised when installed
            self.skipTest("playwright is not installed")
        browser_path = browser_executable()
        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path)
            page = browser.new_page()
            cdp = page.context.new_cdp_session(page)
            # The gap this test is about is a handful of frames wide. Make the
            # machine slow enough that it cannot close by luck.
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": 6})
            page.add_init_script(
                "window.__hcGround = [];"
                "(function sample() {"
                "  try {"
                "    var html = document.documentElement;"
                "    var vis = getComputedStyle(html).visibility;"
                "    var ground = getComputedStyle(html).backgroundColor;"
                "    if (ground === 'rgba(0, 0, 0, 0)' && document.body) {"
                "      ground = getComputedStyle(document.body).backgroundColor;"
                "    }"
                "    window.__hcGround.push(vis + ' ' + ground);"
                "  } catch (e) {}"
                "  requestAnimationFrame(sample);"
                "})();")
            page.goto(url)
            page.wait_for_selector("text=real goal one", timeout=15000)
            frames = page.evaluate("window.__hcGround || []")
            browser.close()
        held = [f for f in frames if f.startswith("hidden ")]
        self.assertTrue(held, "the document was never held: %r" % (frames[:5],))
        ground = "rgb(13, 17, 23)"
        wrong = sorted({f for f in held if not f.endswith(ground)})
        self.assertEqual(
            [], wrong,
            "every frame of the hold must be the workspace's own ground, "
            "not the browser's default: %r" % (wrong,))


class DeletedGoalBrowserTests(unittest.TestCase):
    """The row goes away; the record does not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"
        write_scope(self.a, [goal("g1", "keep this one"),
                             goal("g2", "delete this one")], [])

    def test_deleting_a_goal_takes_the_row_away_and_keeps_the_record(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - exercised when installed
            self.skipTest("playwright is not installed")
        browser_path = browser_executable()
        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path)
            page = browser.new_page()
            page.goto(url)
            page.wait_for_selector("text=delete this one", timeout=10000)
            page.evaluate(
                "() => { const t=[...document.querySelectorAll('*')]"
                ".find(e=>e.children.length===0 && e.textContent.trim()==='delete this one');"
                "  let row=t; for(let i=0;i<4 && row;i++){ const x=[...row.querySelectorAll('*')]"
                "    .find(e=>e.children.length===0 && e.textContent.trim()==='\u00d7');"
                "    if(x){ x.click(); return true; } row=row.parentElement; }"
                "  return false; }")
            page.wait_for_timeout(3000)
            self.assertEqual(0, page.get_by_text("delete this one").count(),
                             "the deleted row must not be drawn any more")
            page.reload()
            page.wait_for_selector("text=keep this one", timeout=10000)
            self.assertEqual(0, page.get_by_text("delete this one").count(),
                             "and it must not come back on reload")
            browser.close()
        state = json.loads((self.a / "goals.json").read_text())
        kept = [g for g in state["goals"] if g["id"] == "g2"]
        self.assertEqual(1, len(kept), "the record itself is kept, not erased")
        self.assertEqual("abandoned", kept[0]["status"])


class FullBleedWorkspaceBrowserTests(unittest.TestCase):
    """Three columns fill the window; the reader sizes or hides the rails."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"
        write_scope(self.a, [goal("g1", "one goal")], [])

    GEO = """() => {
        const r = (s) => { const e = document.querySelector(s);
                           return e ? e.getBoundingClientRect().toJSON() : null; };
        return { hdr: r('.hc>div:first-child'), l: r('.hc-rail-left'),
                 m: r('.hc-main'), rt: r('.hc-rail-right'),
                 pills: r('.hc-titlerow'), brand: r('.hc-brand'),
                 sub: r('.hc-subbar'), tabs: r('.hc-viewtabs'),
                 brandFont: getComputedStyle(document.querySelector('.hc-brand')).fontFamily };
    }"""

    def test_the_columns_fill_the_window_and_the_pills_ride_in_the_header(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - exercised when installed
            self.skipTest("playwright is not installed")
        chrome = browser_executable()
        if not chrome:
            self.skipTest("Chrome/Chromium is not installed")
        with server_for(self.a) as url, sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector("text=one goal", timeout=10000)
                page.wait_for_timeout(1500)
                g = page.evaluate(self.GEO)
                # Header -- two rows -- then columns from its bottom edge to
                # the window's.
                self.assertEqual((0, 0, 1440, 74), tuple(round(g["hdr"][k]) for k in ("x", "y", "width", "height")))
                for col in ("l", "m", "rt"):
                    self.assertEqual(74, round(g[col]["y"]), col)
                    self.assertEqual(900, round(g[col]["bottom"]), col)
                # Flush: left rail at 0, main starts where it ends, right
                # rail ends at the window.
                self.assertEqual(0, round(g["l"]["x"]))
                self.assertEqual(round(g["l"]["right"]), round(g["m"]["x"]))
                self.assertEqual(round(g["m"]["right"]), round(g["rt"]["x"]))
                self.assertEqual(1440, round(g["rt"]["right"]))
                # The second row is the header's own, at the header's
                # indent: the view tabs at its left end under the brand,
                # the filter counts at its right end, flush with the tools
                # above them. The brand is set in a serif.
                self.assertEqual((0, 37, 1440, 36), tuple(round(g["sub"][k]) for k in ("x", "y", "width", "height")))
                self.assertEqual(round(g["brand"]["x"]), round(g["tabs"]["x"]))
                self.assertEqual(37, round(g["pills"]["y"]))
                self.assertEqual(73, round(g["pills"]["bottom"]))
                self.assertEqual(1440 - 16, round(g["pills"]["right"]))
                self.assertIn("Georgia", g["brandFont"])

                # Drag the goals divider 80px right; the rail follows.
                edge = g["l"]["right"]
                page.mouse.move(edge, 400); page.mouse.down()
                page.mouse.move(edge + 40, 400); page.mouse.move(edge + 80, 400)
                page.mouse.up()
                self.assertEqual(round(edge) + 80, round(page.evaluate(self.GEO)["l"]["width"]))
                # Double-click the prompt divider hides that rail; the
                # document takes the space.
                g = page.evaluate(self.GEO)
                page.mouse.dblclick(g["rt"]["x"], 400)
                g = page.evaluate(self.GEO)
                self.assertEqual(0, g["rt"]["width"])
                self.assertEqual(1440, round(g["m"]["right"]))
                # The header toggle brings it back, at a quarter of the
                # window -- the width the rails open at.
                page.click('[data-hc-panel="right"]')
                self.assertEqual(360, round(page.evaluate(self.GEO)["rt"]["width"]))
                # And the layout is the reader's own: it survives a reload.
                page.click('[data-hc-panel="left"]')
                page.reload(wait_until="domcontentloaded")
                # The goals rail is hidden now, so wait on the header.
                page.wait_for_selector(".hc-brand", timeout=10000)
                page.wait_for_timeout(1500)
                g = page.evaluate(self.GEO)
                self.assertEqual(0, g["l"]["width"])
                self.assertEqual(0, round(g["m"]["x"]))
                self.assertEqual(360, round(g["rt"]["width"]))
            finally:
                browser.close()


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


class ProjectOnboardingServerTests(unittest.TestCase):
    """The server half of onboarding: is this chat bound, and bind it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"
        write_scope(self.a, [], [], bound=False)

    def test_a_fresh_chat_reports_itself_unbound(self):
        with server_for(self.a) as url:
            state = get_json(url + "/api/state")
        self.assertIn("project_bound", state)
        self.assertFalse(state["project_bound"],
                         "a chat nobody has bound must send the reader to onboarding")

    def test_binding_answers_with_the_project_and_then_reports_bound(self):
        home = str(Path(self.tmp.name) / "acme-router")
        with server_for(self.a) as url:
            done = post_json(url + "/api/op",
                             {"op": "bind_project", "cwd": home})
            self.assertTrue(done.get("ok"), done)
            self.assertEqual(PS._resolved(home), done["cwd"])
            state = get_json(url + "/api/state")
            self.assertTrue(state["project_bound"])
            # And the workspace now names that project, not the directory
            # the chat happened to start in.
            self.assertEqual(PS._resolved(home), state["project"]["cwd"])

    def test_binding_without_a_project_is_refused(self):
        with server_for(self.a) as url:
            said = post_json(url + "/api/op", {"op": "bind_project", "cwd": "  "})
        self.assertFalse(said.get("ok"))
        self.assertIn("which project", said.get("error", ""))

    def test_a_second_binding_moves_the_chat(self):
        one = str(Path(self.tmp.name) / "one")
        two = str(Path(self.tmp.name) / "two")
        with server_for(self.a) as url:
            post_json(url + "/api/op", {"op": "bind_project", "cwd": one})
            post_json(url + "/api/op", {"op": "bind_project", "cwd": two})
            state = get_json(url + "/api/state")
        self.assertEqual(PS._resolved(two), state["project"]["cwd"])


class OnboardingBrowserTests(unittest.TestCase):
    """An unbound chat is asked which project it is for, before anything else.

    The tree behind the wizard is empty either way; what an empty tree cannot
    say is whether this is a new project or one whose goals are somewhere
    else. So the question is asked first, and answering it is what binds.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"
        # Genuinely new: a chat with a tree is taken to be in its project
        # already, which is the migration's whole job.
        write_scope(self.a, [], [], bound=False)

    def open(self, page, url):
        page.goto(url)
        page.wait_for_selector(".hc", timeout=15000)

    def test_an_unbound_chat_opens_on_the_question_not_the_tree(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright is not installed")
        with server_for(self.a) as url, sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=browser_executable())
            page = b.new_page(viewport={"width": 1400, "height": 900})
            self.open(page, url)
            page.wait_for_selector(".hc-onb", timeout=10_000)
            self.assertIn("Which project is this chat for",
                          page.locator(".hc-onb-title").inner_text())
            for label in ("Start a new project", "Resume an existing project"):
                self.assertEqual(1, page.get_by_text(label, exact=True).count())
            b.close()

    def test_naming_a_new_project_binds_the_chat_and_clears_the_way(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright is not installed")
        with server_for(self.a) as url, sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=browser_executable())
            page = b.new_page(viewport={"width": 1400, "height": 900})
            self.open(page, url)
            page.wait_for_selector(".hc-onb", timeout=10_000)
            page.get_by_text("Start a new project", exact=True).click()
            page.wait_for_selector("[data-hc-onb-name]", timeout=5_000)
            page.fill("[data-hc-onb-name]", "Acme Router")
            page.fill("[data-hc-onb-why]", "Rules a newcomer can predict.")
            page.get_by_text("Continue", exact=True).click()
            # Bound: the wizard goes, and does not come back on the next sweep.
            page.wait_for_selector(".hc-onb", state="detached", timeout=15_000)
            page.wait_for_timeout(1500)
            self.assertEqual(0, page.locator(".hc-onb").count())
            state = get_json(url + "/api/state")
            b.close()
        self.assertTrue(state["project_bound"])
        self.assertEqual("Acme Router", state["project"]["name"])

    def test_an_existing_project_is_offered_and_binds_when_picked(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright is not installed")
        with server_for(self.a) as url, sync_playwright() as pw:
            made = post_json(url + "/api/op",
                             {"op": "new_project", "name": "Older Thing"})
            self.assertTrue(made.get("ok"), made)
            b = pw.chromium.launch(executable_path=browser_executable())
            page = b.new_page(viewport={"width": 1400, "height": 900})
            self.open(page, url)
            page.wait_for_selector(".hc-onb", timeout=10_000)
            page.get_by_text("Resume an existing project", exact=True).click()
            self.assertIn("used Engelbart for this project before",
                          page.locator(".hc-onb-title").inner_text())
            page.get_by_text("Yes — find it", exact=True).click()
            page.wait_for_selector(".hc-onb-item", timeout=10_000)
            page.get_by_text("Older Thing", exact=True).first.click()
            page.wait_for_selector(".hc-onb", state="detached", timeout=15_000)
            state = get_json(url + "/api/state")
            b.close()
        self.assertTrue(state["project_bound"])
        self.assertEqual("Older Thing", state["project"]["name"])


class OnboardingMigrationTests(unittest.TestCase):
    """A chat already working in a project is not asked which project it is.

    Binding arrived after these chats did. Every one of them predates it, so
    "has no binding" describes the whole existing world -- and asking someone
    mid-project which project they are in is the bug, not the feature.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"

    def test_a_chat_with_goals_is_taken_as_already_in_its_project(self):
        write_scope(self.a, [goal("g1", "real work underway")], [], bound=False)
        with server_for(self.a) as url:
            state = get_json(url + "/api/state")
        self.assertTrue(state["project_bound"],
                        "a chat with a tree is mid-project; asking is the bug")

    def test_a_chat_whose_directory_is_a_known_project_is_taken_as_bound(self):
        write_scope(self.a, [], [], bound=False)
        home = str(Path(self.tmp.name) / "acme")
        (self.a / "manifest.json").write_text(json.dumps(
            {"schema_version": 1, "session_id": self.a.name, "cwd": home}))
        from human_compact.trajectory import project_store as PS
        PS.save_project(self.a.parent, home, {"objective": "already a project"})
        with server_for(self.a) as url:
            state = get_json(url + "/api/state")
        self.assertTrue(state["project_bound"])

    def test_a_genuinely_new_chat_is_still_asked(self):
        write_scope(self.a, [], [], bound=False)
        with server_for(self.a) as url:
            state = get_json(url + "/api/state")
        self.assertFalse(state["project_bound"],
                         "an empty chat in no known project is what onboarding is for")

    def test_the_migration_is_written_down_so_it_is_asked_once(self):
        write_scope(self.a, [goal("g1", "real work underway")], [], bound=False)
        with server_for(self.a) as url:
            get_json(url + "/api/state")
        manifest = json.loads((self.a / "manifest.json").read_text())
        self.assertTrue(manifest.get("project_bound_at"),
                        "the answer should survive the next reload")


class OnboardingLooksLikeTheWorkspaceTests(unittest.TestCase):
    """The panel lives outside the artifact, so it must carry its own palette.

    The theme's variables are declared on the artifact's shell. A panel on
    documentElement inherits none of them: it had no background and drew its
    text in the browser's black, on a dark shade.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"
        write_scope(self.a, [], [], bound=False)

    def test_the_panel_wears_the_workspace_palette_and_sits_in_the_middle(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright is not installed")
        with server_for(self.a) as url, sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=browser_executable())
            page = b.new_page(viewport={"width": 1440, "height": 900})
            page.goto(url)
            page.wait_for_selector(".hc-onb", timeout=15_000)
            # The artifact applies its theme after the first paint, so this is
            # about what the reader ends up looking at, not the first frame.
            page.wait_for_timeout(2_000)
            seen = page.evaluate(
                "() => {"
                "  const b = document.querySelector('.hc-onb');"
                "  const shell = document.querySelector('.hc');"
                "  const r = b.getBoundingClientRect();"
                "  return {panel: getComputedStyle(b).backgroundColor,"
                "          want: getComputedStyle(shell).getPropertyValue('--panel').trim(),"
                "          title: getComputedStyle(document.querySelector"
                "            ('.hc-onb-title')).color,"
                "          midX: Math.abs((r.left + r.width / 2) - innerWidth / 2),"
                "          midY: Math.abs((r.top + r.height / 2) - innerHeight / 2)};}")
            b.close()
        self.assertNotEqual("rgba(0, 0, 0, 0)", seen["panel"],
                            "a panel with no background is the shade with words on it")
        self.assertNotEqual("rgb(0, 0, 0)", seen["title"],
                            "black text on a dark shade is the bug this fixes")
        self.assertEqual("#0d1117", seen["want"], "the workspace is dark here")
        self.assertEqual("rgb(13, 17, 23)", seen["panel"],
                         "the panel should wear the same panel colour as the shell")
        self.assertLess(seen["midX"], 3)
        self.assertLess(seen["midY"], 3)


class AddRootGoalTests(unittest.TestCase):
    """Adding a goal at the top of the tree, which the server used to refuse.

    The branch that decides which project a new root goal belongs to asked
    the workspace who it was, using a name that does not exist where it was
    written -- so every root goal raised before it was ever appended.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = Path(self.tmp.name) / "chat-a"
        write_scope(self.a, [goal("g1", "already here")], [])

    def test_a_root_goal_can_be_added(self):
        with server_for(self.a) as url:
            said = post_json(url + "/api/op",
                             {"op": "add_goal", "title": "a new one"})
            self.assertTrue(said.get("ok"), said)
            titles = [g["title"] for g in get_json(url + "/api/state")["goals"]]
        self.assertIn("a new one", titles)

    def test_a_subgoal_can_still_be_added(self):
        with server_for(self.a) as url:
            said = post_json(url + "/api/op", {"op": "add_goal",
                                               "title": "under it",
                                               "parent_goal_id": "g1"})
            self.assertTrue(said.get("ok"), said)
            tree = {g["title"]: g.get("parent_goal_id")
                    for g in get_json(url + "/api/state")["goals"]}
        self.assertEqual("g1", tree["under it"])
