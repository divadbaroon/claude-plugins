"""Linked chats: other sessions as prompt sources, nothing more.

A linked chat's user turns join the related-prompts pool, tagged with the
chat's label, and its transcript is followed with the same cursor machinery
as the workspace's own. Its goals are never read; nothing is written to it.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_chat_ui_server import get_json, post_json  # noqa: E402


def turn(session, text, ts):
    return json.dumps({"type": "user", "uuid": "u-%s-%s" % (session, ts),
                       "timestamp": ts, "sessionId": session,
                       "message": {"role": "user", "content": text}}) + "\n"


@contextmanager
def server_for(path):
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
    ui._configure_server(server, Path(path), True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.follow_stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LinkedChatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-main"
        self.other = "chat-other"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        goals = {"version": 1, "goals": [GM.new_goal("g1", "Ship it", origin="user")]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        self.trajdir = p.session_dir
        self.claude_home = self.root / "claude-home"
        for session, project in ((self.session, "-Users-me-main"),
                                 (self.other, "-Users-me-otherproj")):
            directory = self.claude_home / "projects" / project
            directory.mkdir(parents=True)
            (directory / f"{session}.jsonl").write_text(
                turn(session, "first in %s" % session, "2026-08-20T01:00:00Z"))
        p.manifest.write_text(json.dumps({
            "cwd": str(self.root),
            "transcript_path": str(self.claude_home / "projects" / "-Users-me-main"
                                   / f"{self.session}.jsonl")}))
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "HC_CHAT_FOLLOW_SECONDS": "0.1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def wait_for(self, predicate, seconds=6):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def prompts(self, url):
        return get_json(url + "/api/state")["prompts"]

    def texts(self, url):
        return [p["text"] for p in self.prompts(url)]

    def test_linking_a_chat_brings_its_prompts_tagged_and_in_sync(self):
        with server_for(self.trajdir) as url:
            chats = get_json(url + "/api/chats")
            self.assertTrue(chats["ok"])
            self.assertIn(self.other,
                          [c["session_id"] for c in chats["available"]])
            self.assertNotIn(self.session,
                             [c["session_id"] for c in chats["available"]],
                             "a workspace does not offer its own chat")
            out = post_json(url + "/api/op", {"op": "link_chat",
                                              "session_id": self.other,
                                              "label": "otherproj"})
            self.assertTrue(out["ok"], out)
            self.assertTrue(self.wait_for(
                lambda: "first in chat-other" in self.texts(url)))
            row = [p for p in self.prompts(url)
                   if p["text"] == "first in chat-other"][0]
            self.assertEqual("otherproj", row.get("chat"))
            own = [p for p in self.prompts(url)
                   if p["text"] == "first in chat-main"][0]
            self.assertNotIn("chat", own, "own prompts carry no tag")

            # It stays in sync: a new turn in the linked chat arrives alone.
            transcript = (self.claude_home / "projects" / "-Users-me-otherproj"
                          / f"{self.other}.jsonl")
            with transcript.open("a") as fh:
                fh.write(turn(self.other, "later in the other chat",
                              "2026-08-20T02:00:00Z"))
            self.assertTrue(self.wait_for(
                lambda: "later in the other chat" in self.texts(url)))

            # And a linked prompt can be attached to a goal.
            pid = [p for p in self.prompts(url)
                   if p["text"] == "later in the other chat"][0]["id"]
            out = post_json(url + "/api/op", {"op": "attach_prompt",
                                              "goal_id": "g1",
                                              "prompt_id": pid})
            self.assertTrue(out["ok"], out)

    def test_unlinking_removes_the_prompts_and_nothing_of_the_chat_was_touched(self):
        with server_for(self.trajdir) as url:
            post_json(url + "/api/op", {"op": "link_chat",
                                        "session_id": self.other,
                                        "label": "otherproj"})
            self.assertTrue(self.wait_for(
                lambda: "first in chat-other" in self.texts(url)))
            out = post_json(url + "/api/op", {"op": "unlink_chat",
                                              "session_id": self.other})
            self.assertTrue(out["ok"], out)
            self.assertTrue(self.wait_for(
                lambda: "first in chat-other" not in self.texts(url)))
            # The linked chat's own store holds only ingestion artifacts --
            # no goals were created for it, nothing else written.
            other_dir = chat_state.paths(self.other, self.root).session_dir
            self.assertFalse((other_dir / "goals.json").exists())

    def test_a_chat_linked_from_a_goal_is_scoped_to_that_goal(self):
        # Linked from a goal's pane, the chat's prompts are tagged with that
        # goal: the picker offers them there and in the goals under it,
        # never above. Linked from the header as well, the tag comes off --
        # a global link covers every goal. Unlinking one scope leaves the
        # other standing, and the session is followed once throughout.
        def other_row(url):
            rows = [p for p in self.prompts(url)
                    if p["text"] == "first in chat-other"]
            return rows[0] if len(rows) == 1 else None

        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "link_chat",
                                              "session_id": self.other,
                                              "label": "otherproj",
                                              "goal_id": "g1"})
            self.assertTrue(out["ok"], out)
            self.assertTrue(self.wait_for(lambda: other_row(url) is not None))
            self.assertEqual(["g1"], other_row(url).get("chat_goals"))
            self.assertEqual("otherproj", other_row(url).get("chat"))
            chats = get_json(url + "/api/chats")["linked"]
            self.assertEqual([{"session_id": self.other, "label": "otherproj",
                               "goal_id": "g1"}], chats)

            out = post_json(url + "/api/op", {"op": "link_chat",
                                              "session_id": self.other})
            self.assertTrue(out["ok"], out)
            self.assertTrue(self.wait_for(
                lambda: other_row(url) is not None
                and "chat_goals" not in other_row(url)))
            self.assertEqual(
                1, len([p for p in self.prompts(url)
                        if p["text"] == "first in chat-other"]),
                "two scopes, one session, one copy of each prompt")
            # The label was kept from the first link rather than reset.
            chats = get_json(url + "/api/chats")["linked"]
            self.assertEqual(["otherproj", "otherproj"],
                             [c["label"] for c in chats])

            out = post_json(url + "/api/op", {"op": "unlink_chat",
                                              "session_id": self.other})
            self.assertTrue(out["ok"], out)
            self.assertTrue(self.wait_for(
                lambda: other_row(url) is not None
                and other_row(url).get("chat_goals") == ["g1"]))
            out = post_json(url + "/api/op", {"op": "unlink_chat",
                                              "session_id": self.other,
                                              "goal_id": "g1"})
            self.assertTrue(out["ok"], out)
            self.assertTrue(self.wait_for(
                lambda: "first in chat-other" not in self.texts(url)))

    def test_a_goal_scoped_link_must_name_a_goal_this_tree_has(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "link_chat",
                                              "session_id": self.other,
                                              "goal_id": "nope"})
            self.assertFalse(out["ok"])
            self.assertEqual([], get_json(url + "/api/chats")["linked"])

    def test_link_refuses_self_and_junk(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "link_chat",
                                              "session_id": self.session})
            self.assertFalse(out["ok"])
            out = post_json(url + "/api/op", {"op": "link_chat",
                                              "session_id": "../escape"})
            self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
