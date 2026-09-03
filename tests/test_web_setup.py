"""A chat's first /bart claims the project its reader set up on the web."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact import cli  # noqa: E402
from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import reader as READER  # noqa: E402
from human_compact.trajectory import web_setup as WS  # noqa: E402


# What the site saves at the end of its flow: the chosen direction is the one
# goal, broken into three described pieces with rows under the first only.
def web_payload(name="Signed uploads"):
    return {
        "name": name,
        "plan": {"description": "Move uploads off the API server.\nSign, then PUT."},
        "goals": [{"label": "Direct-to-storage uploads", "why": "the API is the bottleneck"}],
        "chosen": "Direct-to-storage uploads",
        "todos": [],
        "subgoals": [
            {"label": "Signing route", "description": "Mint short-lived URLs",
             "why": "nothing else can start without it",
             "todos": ["Add POST /uploads/sign", "Scope the token"]},
            {"label": "Client PUTs", "description": "Browser writes to storage",
             "why": "it is the traffic being moved", "todos": []},
            {"label": "Retire the proxy", "description": "Delete the old path",
             "why": "two paths is one too many", "todos": []},
        ],
        "reader": {"name": "Maya", "level": "expert"},
    }


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StoredAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"HUMAN_COMPACT_HOME": str(self.home)}

    def _write(self, record):
        (self.home / "auth.json").write_text(json.dumps(record), encoding="utf-8")

    def test_the_token_and_its_deployment_are_read_from_auth_json(self):
        self._write({"schema": 1, "token": "egb_x", "apiBase": "https://example.test/",
                     "email": "m@example.com"})
        self.assertEqual({"token": "egb_x", "apiBase": "https://example.test"},
                         WS.stored_account(self.env))

    def test_no_file_or_half_a_record_is_no_account(self):
        self.assertIsNone(WS.stored_account(self.env))
        self._write({"schema": 1, "apiBase": "https://example.test"})
        self.assertIsNone(WS.stored_account(self.env))
        self._write({"schema": 1, "token": "egb_x"})
        self.assertIsNone(WS.stored_account(self.env))
        (self.home / "auth.json").write_text("not json", encoding="utf-8")
        self.assertIsNone(WS.stored_account(self.env))

    def test_the_default_root_is_the_installers(self):
        with mock.patch.dict(os.environ, {"HUMAN_COMPACT_HOME": ""}), \
             mock.patch.object(Path, "home", return_value=self.home):
            self.assertEqual(self.home / ".human-compact", WS.managed_root())


class FetchPendingTests(unittest.TestCase):
    ACCOUNT = {"token": "egb_x", "apiBase": "https://example.test"}

    def _opener(self, status=200, body='{"payload": null}', raise_with=None):
        seen = {}

        def open_url(request, timeout=None):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            seen["body"] = json.loads(request.data.decode("utf-8"))
            seen["timeout"] = timeout
            if raise_with is not None:
                raise raise_with
            return FakeResponse(status, body)
        return open_url, seen

    def test_one_bearer_post_asks_for_the_pending_setup(self):
        open_url, seen = self._opener(body=json.dumps({"payload": web_payload()}))
        got = WS.fetch_pending(self.ACCOUNT, opener=open_url)
        self.assertEqual("Signed uploads", got["name"])
        self.assertEqual("https://example.test/api/engelbart-setup", seen["url"])
        self.assertEqual("POST", seen["method"])
        self.assertEqual({"action": "pending"}, seen["body"])
        self.assertEqual("Bearer egb_x", seen["headers"]["authorization"])
        self.assertEqual("application/json", seen["headers"]["content-type"])
        self.assertEqual(5, seen["timeout"])

    def test_nothing_waiting_and_every_failure_answer_none(self):
        for status, body, error in (
                (200, '{"payload": null}', None),
                (200, '{"payload": {}}', None),
                (200, '{"payload": "text"}', None),
                (200, 'not json', None),
                (401, '{"error": "bad token"}', None),
                (200, '{"payload": {}}', urllib.error.URLError("down")),
                (200, '{"payload": {}}', OSError("no route"))):
            open_url, _ = self._opener(status, body, error)
            self.assertIsNone(WS.fetch_pending(self.ACCOUNT, opener=open_url),
                              (status, body, error))


class ClaimTests(unittest.TestCase):
    """The claim, from an unbound chat's point of view."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.managed = Path(self.tmp.name) / "managed"
        self.managed.mkdir()
        self.env = {"HUMAN_COMPACT_HOME": str(self.managed)}
        (self.managed / "auth.json").write_text(json.dumps({
            "schema": 1, "token": "egb_x", "apiBase": "https://example.test"}),
            encoding="utf-8")
        self.remembered = {}
        patched = mock.patch.object(
            READER, "remember",
            side_effect=lambda value, root=None: self.remembered.update(value) or {"ok": True})
        patched.start()
        self.addCleanup(patched.stop)
        self.sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        # A chat the hooks have seen and nobody has asked about yet.
        CS.ingest_hook({"session_id": self.sid, "hook_event_name": "SessionStart",
                        "cwd": str(Path(self.tmp.name) / "repo")}, root=self.root)

    def _fetch(self, payload):
        calls = []

        def fetch(account):
            calls.append(account)
            return payload
        return fetch, calls

    def test_the_web_project_is_made_and_this_chat_is_bound_to_it(self):
        fetch, calls = self._fetch(web_payload())
        said = WS.claim_for_chat(self.sid, self.root, self.env, fetch=fetch)
        self.assertEqual([{"token": "egb_x", "apiBase": "https://example.test"}], calls)
        self.assertEqual('created "Signed uploads" from your web setup; this chat is in it', said)
        self.assertTrue(CS.project_bound(self.sid, self.root))
        row = PS.project_named(self.root, "Signed uploads")
        self.assertIsNotNone(row)
        self.assertEqual(CS.bound_project(self.sid, self.root), row["cwd"])
        # The tree is the project's, read through the chat that joined it:
        # one goal, three pieces, rows under the first only.
        goals = CS.load_goals(self.sid, self.root)[0]["goals"]
        parents = [g for g in goals if not g.get("parent_goal_id")]
        self.assertEqual(["Direct-to-storage uploads"], [g["title"] for g in parents])
        kids = [g for g in goals if g.get("parent_goal_id") == parents[0]["id"]]
        self.assertEqual(["Signing route", "Client PUTs", "Retire the proxy"],
                         [k["title"] for k in kids])
        self.assertEqual([2, 0, 0], [len(k["todo_items"]) for k in kids])
        self.assertEqual(["Mint short-lived URLs", "Browser writes to storage",
                          "Delete the old path"], [k["description"] for k in kids])
        self.assertEqual("Maya", self.remembered["name"])

    def test_a_second_bart_does_not_ask_the_site_again(self):
        fetch, calls = self._fetch(web_payload())
        WS.claim_for_chat(self.sid, self.root, self.env, fetch=fetch)
        self.assertEqual("", WS.claim_for_chat(self.sid, self.root, self.env, fetch=fetch))
        self.assertEqual(1, len(calls))

    def test_a_chat_that_is_not_about_to_be_asked_is_left_alone(self):
        fetch, calls = self._fetch(web_payload())
        # Bound already.
        CS.bind_project(self.sid, str(Path(self.tmp.name) / "repo"), self.root)
        self.assertEqual("", WS.claim_for_chat(self.sid, self.root, self.env, fetch=fetch))
        # A tree of its own: a chat from before binding, which the workspace
        # migrates rather than asks.
        other = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        CS.ingest_hook({"session_id": other, "hook_event_name": "SessionStart",
                        "cwd": str(Path(self.tmp.name) / "repo")}, root=self.root)
        CS.save_goals(other, {"version": 1, "goals": [
            {"id": "g1", "title": "old work", "status": "active",
             "parent_goal_id": None}]}, {"items": []}, root=self.root)
        self.assertEqual("", WS.claim_for_chat(other, self.root, self.env, fetch=fetch))
        # A session this vault minted for a directory: no chat behind it.
        minted = CS.open_workspace_for(str(Path(self.tmp.name) / "repo"), self.root)
        self.assertEqual("", WS.claim_for_chat(minted, self.root, self.env, fetch=fetch))
        # A session nobody has recorded at all.
        self.assertEqual("", WS.claim_for_chat("cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                                               self.root, self.env, fetch=fetch))
        self.assertEqual([], calls)

    def test_no_account_and_nothing_pending_fall_through_quietly(self):
        fetch, calls = self._fetch(web_payload())
        self.assertEqual("", WS.claim_for_chat(
            self.sid, self.root, {"HUMAN_COMPACT_HOME": str(Path(self.tmp.name) / "nowhere")},
            fetch=fetch))
        self.assertEqual([], calls)
        fetch, calls = self._fetch(None)
        self.assertEqual("", WS.claim_for_chat(self.sid, self.root, self.env, fetch=fetch))
        self.assertEqual(1, len(calls))
        self.assertFalse(CS.project_bound(self.sid, self.root))

    def test_a_site_that_blows_up_is_the_same_as_nothing(self):
        def fetch(account):
            raise RuntimeError("boom")
        self.assertEqual("", WS.claim_for_chat(self.sid, self.root, self.env, fetch=fetch))
        self.assertFalse(CS.project_bound(self.sid, self.root))

    def test_a_claimed_payload_that_cannot_be_made_is_saved_with_its_retry(self):
        # The claim was single-use, so the file is the only copy left.
        PS.create_named(self.root, "Signed uploads")
        fetch, _ = self._fetch(web_payload())
        said = WS.claim_for_chat(self.sid, self.root, self.env, fetch=fetch)
        saved = self.managed / "pending-setup.json"
        self.assertTrue(saved.is_file())
        self.assertEqual("Signed uploads", json.loads(saved.read_text())["name"])
        self.assertIn("a project is already called that", said)
        self.assertIn(f"hc setup-import --file {saved}", said)
        self.assertFalse(CS.project_bound(self.sid, self.root))


class ChatUiTests(unittest.TestCase):
    """The claim rides in /bart: a note beside the URL the hook prints."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.managed = Path(self.tmp.name) / "managed"
        self.managed.mkdir()
        (self.managed / "auth.json").write_text(json.dumps({
            "schema": 1, "token": "egb_x", "apiBase": "https://example.test"}),
            encoding="utf-8")
        env = mock.patch.dict(os.environ, {"HC_CHAT_STATE_DIR": str(self.root),
                                           "HUMAN_COMPACT_HOME": str(self.managed)})
        env.start()
        self.addCleanup(env.stop)
        patched = mock.patch.object(READER, "remember", return_value={"ok": True})
        patched.start()
        self.addCleanup(patched.stop)
        self.sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.cwd = str(Path(self.tmp.name) / "repo")
        CS.ingest_hook({"session_id": self.sid, "hook_event_name": "SessionStart",
                        "cwd": self.cwd})

    def _open(self, fetched):
        record = {"schema_version": 1, "session_id": self.sid, "pid": os.getpid(),
                  "url": "http://127.0.0.1:9012/", "started_at": 0}
        out = io.StringIO()
        with (mock.patch.object(WS, "fetch_pending", side_effect=lambda account: fetched),
              mock.patch.object(cli, "_healthy_chat_server", return_value=True),
              mock.patch.object(cli, "_read_server_registry", return_value=record),
              mock.patch.object(cli, "_request_chat_refresh"),
              contextlib.redirect_stdout(out)):
            code = cli.chat_ui_main(["--session", self.sid, "--cwd", self.cwd, "--no-open"])
        return code, out.getvalue().splitlines()

    def test_the_first_bart_names_the_project_it_made_above_the_url(self):
        code, lines = self._open(web_payload())
        self.assertEqual(0, code)
        self.assertEqual('note: created "Signed uploads" from your web setup; this chat is in it',
                         lines[-2])
        self.assertEqual("http://127.0.0.1:9012/", lines[-1])
        self.assertTrue(CS.project_bound(self.sid))
        self.assertEqual(3, len([g for g in CS.load_goals(self.sid)[0]["goals"]
                                 if g.get("parent_goal_id")]))

    def test_with_nothing_pending_bart_opens_as_it_always_did(self):
        code, lines = self._open(None)
        self.assertEqual(0, code)
        self.assertEqual(["http://127.0.0.1:9012/"], lines)
        self.assertFalse(CS.project_bound(self.sid))


class SetupImportTests(unittest.TestCase):
    """`hc setup-import` and the claim share one commit path."""

    def test_setup_import_makes_the_same_project_from_the_same_payload(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"HC_CHAT_STATE_DIR": str(Path(tmp) / "state")}), \
             mock.patch.object(READER, "remember", return_value={"ok": True}), \
             mock.patch.object(cli, "chat_ui_main",
                               side_effect=lambda argv: print("http://127.0.0.1:1/")), \
             mock.patch("sys.stdin", new=io.StringIO(json.dumps(web_payload("Imported")))), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.setup_import_main(["--stdin", "--no-open"])
            row = PS.project_named(None, "Imported")
            self.assertIsNotNone(row)
            tree = PS.tree_session(None, row["cwd"])
            goals = CS.load_goals(tree)[0]["goals"]
            self.assertEqual(3, len([g for g in goals if g.get("parent_goal_id")]))


if __name__ == "__main__":
    unittest.main()
