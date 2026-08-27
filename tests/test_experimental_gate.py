"""The launch product exposes one workspace; everything else is disconnected.

The audit's "built but not exposed" capabilities keep their implementations,
but their entry points answer with a refusal unless ``HC_EXPERIMENTAL=1`` is
set.  The CLI half of that boundary lives in ``cli.EXPERIMENTAL_COMMANDS``;
these tests hold the HTTP half, which is reachable from any running server —
including the per-chat server ``/bart`` starts.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402


NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REFUSAL = "experimental in this release; set HC_EXPERIMENTAL=1"


@contextmanager
def flag(enabled):
    """Run with HC_EXPERIMENTAL forced on or forced absent.

    Forced absent matters: the developer running these may export it, and the
    launch configuration is precisely the one without it.
    """
    with mock.patch.dict(os.environ, {}):
        if enabled:
            os.environ["HC_EXPERIMENTAL"] = "1"
        else:
            os.environ.pop("HC_EXPERIMENTAL", None)
        yield


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
    with NO_PROXY_OPENER.open(url, timeout=5) as response:
        return json.loads(response.read())


def goal(goal_id, title):
    return {"id": goal_id, "title": title, "status": "active",
            "parent_goal_id": None, "evidence_ids": [], "todos": [],
            "important_item_ids": [], "prompt_ids": [], "priority": "normal",
            "notes": "", "description": ""}


def write_scope(path, goals):
    path.mkdir(parents=True, exist_ok=True)
    (path / "goals.json").write_text(json.dumps({"version": 1, "goals": goals}))
    (path / "important.json").write_text(json.dumps({"items": []}))
    (path / "prompts.json").write_text(json.dumps({"prompts": []}))


class ExperimentalOpTests(unittest.TestCase):
    """`POST /api/op` refuses the disconnected ops, and only those."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chat = self.root / "chat-a"
        write_scope(self.chat, [goal("a1", "goal in chat a")])

    def opening(self):
        goals, _ = CS.load_goals(self.chat.name, self.root)
        return GM.by_id(goals, "a1").get("opening")

    def test_set_opening_is_refused_without_the_flag(self):
        with flag(False):
            result = ui._apply({"op": "set_opening", "goal_id": "a1",
                                "opening": "start here"},
                               self.chat, chat_scoped=True)
        self.assertFalse(result["ok"])
        self.assertIn("experimental", result["error"])
        self.assertEqual(REFUSAL, result["error"])
        # A refusal that quietly wrote anyway would be the worse bug.
        self.assertFalse(self.opening())

    def test_set_opening_still_works_with_the_flag(self):
        with flag(True):
            result = ui._apply({"op": "set_opening", "goal_id": "a1",
                                "opening": "start here"},
                               self.chat, chat_scoped=True)
        self.assertTrue(result["ok"])
        self.assertEqual("start here", self.opening())

    def test_every_disconnected_op_answers_the_same_way(self):
        for kind in sorted(ui.EXPERIMENTAL_OPS):
            with self.subTest(op=kind), flag(False):
                result = ui._apply({"op": kind, "goal_id": "a1"},
                                   self.chat, chat_scoped=True)
                self.assertFalse(result["ok"])
                self.assertEqual(REFUSAL, result["error"])

    def test_the_launch_ops_are_untouched(self):
        with flag(False):
            result = ui._apply({"op": "rename_goal", "goal_id": "a1",
                                "title": "renamed"},
                               self.chat, chat_scoped=True)
        self.assertTrue(result["ok"])
        goals, _ = CS.load_goals(self.chat.name, self.root)
        self.assertEqual("renamed", GM.by_id(goals, "a1")["title"])


class ExperimentalRouteTests(unittest.TestCase):
    """`GET` on the disconnected read routes answers the same refusal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chat = self.root / "chat-a"
        write_scope(self.chat, [goal("a1", "goal in chat a")])

    def test_the_disconnected_routes_refuse_without_the_flag(self):
        paths = ("/api/briefing?goal=a1", "/api/briefings", "/api/plan?goal=a1",
                 "/api/review?goal=a1", "/api/setup", "/api/conversation?id=a1")
        with flag(False), server_for(self.chat) as url:
            for path in paths:
                with self.subTest(path=path):
                    body = get_json(url + path)
                    self.assertFalse(body["ok"])
                    self.assertEqual(REFUSAL, body["error"])

    def test_a_prefixed_path_cannot_slip_past_the_gate(self):
        # The router reaches these handlers by prefix, so a path the gate
        # missed would still be served by the handler behind it.
        with flag(False), server_for(self.chat) as url:
            for path in ("/api/plan/anything", "/api/reviewx?goal=a1",
                         "/api/conversation/1"):
                with self.subTest(path=path):
                    body = get_json(url + path)
                    self.assertFalse(body["ok"])
                    self.assertEqual(REFUSAL, body["error"])

    def test_the_launch_routes_stay_open_without_the_flag(self):
        with flag(False), server_for(self.chat) as url:
            state = get_json(url + "/api/state")
            health = get_json(url + "/api/health")
            with NO_PROXY_OPENER.open(url + "/", timeout=5) as response:
                html = response.read().decode()
        self.assertEqual(["a1"], [g["id"] for g in state["goals"]])
        self.assertTrue(health["ok"])
        self.assertIn('<script src="/bridge.js"></script>', html)


class GlobalSetupRouteTests(unittest.TestCase):
    """With the flag on, the global onboarding route behaves as before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.trajdir = self.vault / "trajectory"
        self.trajdir.mkdir(parents=True)
        from human_compact.trajectory import discover
        patch = mock.patch.object(discover, "VAULT", self.vault)
        patch.start()
        self.addCleanup(patch.stop)
        home = mock.patch.dict(os.environ, {"HC_HOME": self.tmp.name,
                                            "CLAUDE_VAULT_DIR": str(self.vault)})
        home.start()
        self.addCleanup(home.stop)
        # is_enabled() honours a legacy CLAUDE_VAULT=1 export when nothing is
        # recorded, and the developer running these may have one.
        os.environ.pop("CLAUDE_VAULT", None)
        GM.save(self.trajdir, {"version": 1, "goals": []}, {"items": []})

    def test_setup_answers_normally_with_the_flag(self):
        with flag(True):
            os.environ["HC_HOME"] = self.tmp.name
            os.environ["CLAUDE_VAULT_DIR"] = str(self.vault)
            os.environ.pop("CLAUDE_VAULT", None)
            with server_for(self.trajdir, chat_scoped=False) as url:
                body = get_json(url + "/api/setup")
        self.assertTrue(body["ok"])
        self.assertIn("storage", body)

    def test_setup_refuses_on_a_global_server_without_the_flag(self):
        with flag(False), server_for(self.trajdir, chat_scoped=False) as url:
            body = get_json(url + "/api/setup")
        self.assertFalse(body["ok"])
        self.assertEqual(REFUSAL, body["error"])


if __name__ == "__main__":
    unittest.main()
