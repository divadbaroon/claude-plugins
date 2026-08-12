import json
import os
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
    with urllib.request.urlopen(url, timeout=2) as response:
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
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read())


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
            state_a = get_json(url_a + "/api/state")
            state_b = get_json(url_b + "/api/state")
            self.assertEqual(["a1", "a2"], [g["id"] for g in state_a["goals"]])
            self.assertEqual(["b1"], [g["id"] for g in state_b["goals"]])
            self.assertEqual(["p-old", "p-new"], [p["id"] for p in state_a["prompts"]])
            self.assertEqual(["bp"], [p["id"] for p in state_b["prompts"]])
            self.assertEqual(
                {"ok": True, "scope": "chat", "session_id": "chat-a"},
                get_json(url_a + "/api/health"),
            )

            self.assertEqual(
                {"ok": True},
                post_json(
                    url_a + "/api/op",
                    {"op": "attach_prompt", "goal_id": "a1", "prompt_id": "p-new"},
                ),
            )
            self.assertEqual(["p-new"], get_json(url_a + "/api/state")["goals"][0]["prompt_ids"])
            self.assertEqual([], get_json(url_b + "/api/state")["goals"][0]["prompt_ids"])

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
        self.assertEqual({"ok": True, "goals": 2}, ui._import(nested, self.a))
        state = ui._payload(self.a)
        imported = next(g for g in state["goals"] if g["id"] == "a1")
        self.assertEqual("renamed in bundle", imported["title"])
        self.assertEqual(["p-new"], imported["prompt_ids"])

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
                "idle_timeout": 0.12,
            },
        )
        thread.start()
        self.assertTrue(ready.wait(timeout=1))
        for _ in range(5):
            time.sleep(0.05)
            self.assertTrue(get_json(observed["url"] + "/api/health")["ok"])
        self.assertTrue(thread.is_alive())
        thread.join(timeout=1)
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
