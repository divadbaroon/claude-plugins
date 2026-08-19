"""Related prompts stay current: the server follows the chat's transcript.

The prompts the workspace offers for a goal are the chat's own user turns.
They used to reach the session's store only through the hooks; when the hooks
went quiet (a moved plugin path, a stale hook config on a reopened session)
the list froze. The server now reads the transcript itself, incrementally,
and re-finds it by session id if the recorded path is gone.
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


def user_turn(text, ts):
    return json.dumps({"type": "user", "uuid": "u-%s" % ts,
                       "timestamp": ts, "sessionId": "chat-follow",
                       "message": {"role": "user", "content": text}}) + "\n"


def assistant_turn(text, ts):
    return json.dumps({"type": "assistant", "uuid": "a-%s" % ts,
                       "timestamp": ts, "sessionId": "chat-follow",
                       "message": {"role": "assistant", "content": [
                           {"type": "text", "text": text}]}}) + "\n"


@contextmanager
def server_for(path):
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
    ui._configure_server(server, Path(path), True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.follow_stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class FollowTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-follow"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        goals = {"version": 1, "goals": [GM.new_goal("g1", "Ship it", origin="user")]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        self.trajdir = p.session_dir
        # Claude's own projects directory, where transcripts live.
        self.claude_home = self.root / "claude-home"
        self.project = self.claude_home / "projects" / "-Users-me-proj"
        self.project.mkdir(parents=True)
        self.transcript = self.project / f"{self.session}.jsonl"
        self.transcript.write_text(user_turn("first thing I asked", "2026-08-19T01:00:00Z"))
        p.manifest.write_text(json.dumps({"cwd": str(self.root),
                                          "transcript_path": str(self.transcript)}))
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "HC_CHAT_FOLLOW_SECONDS": "0.1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def prompts(self, url):
        return [p["text"] for p in get_json(url + "/api/state")["prompts"]]

    def wait_for(self, predicate, seconds=6):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_new_turns_reach_related_prompts_with_no_hook_at_all(self):
        with server_for(self.trajdir) as (server, url):
            self.assertTrue(self.wait_for(
                lambda: "first thing I asked" in self.prompts(url)),
                "the follower reads what was there before it started")
            with self.transcript.open("a") as fh:
                fh.write(assistant_turn("sure", "2026-08-19T01:00:05Z"))
                fh.write(user_turn("and now this, later", "2026-08-19T01:01:00Z"))
            self.assertTrue(self.wait_for(
                lambda: "and now this, later" in self.prompts(url)),
                "a turn written after the server started arrives on its own")
            # Assistant text is never offered as one of the user's prompts.
            self.assertNotIn("sure", self.prompts(url))
            # And the new prompt can be attached to a goal straight away.
            pid = [p for p in get_json(url + "/api/state")["prompts"]
                   if p["text"] == "and now this, later"][0]["id"]
            out = post_json(url + "/api/op", {"op": "attach_prompt",
                                              "goal_id": "g1", "prompt_id": pid})
            self.assertTrue(out["ok"], out)

    def test_a_reopened_chat_whose_file_moved_is_found_again_by_its_id(self):
        # The manifest points at a path that no longer exists (the project
        # directory was renamed); the same session id lives under a new one.
        moved = self.claude_home / "projects" / "-Users-me-proj-renamed"
        moved.mkdir()
        target = moved / f"{self.session}.jsonl"
        self.transcript.rename(target)
        with target.open("a") as fh:
            fh.write(user_turn("after the move", "2026-08-19T02:00:00Z"))
        with server_for(self.trajdir) as (server, url):
            self.assertTrue(self.wait_for(
                lambda: "after the move" in self.prompts(url)))
            manifest = chat_state.load_manifest(self.session, self.root)
            self.assertEqual(target.resolve(), Path(manifest["transcript_path"]).resolve())

    def test_a_truncated_and_rewritten_transcript_is_replayed_not_misread(self):
        with server_for(self.trajdir) as (server, url):
            self.assertTrue(self.wait_for(
                lambda: "first thing I asked" in self.prompts(url)))
            # Smaller than the cursor: the follower must start over, not
            # read garbage from an offset past the end.
            self.transcript.write_text(user_turn("a whole new start", "2026-08-19T03:00:00Z"))
            self.assertTrue(self.wait_for(
                lambda: "a whole new start" in self.prompts(url)))


class InProgressOnWorkTests(unittest.TestCase):
    """Tying a prompt to a goal, or building its rows, begins it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-begin"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        g = GM.new_goal("g1", "Ship it", origin="user")
        g["todo_items"] = [{"id": "taaaa0001", "text": "a row", "depth": 0}]
        done = GM.new_goal("g2", "Already done", origin="user", status="completed")
        goals = {"version": 1, "goals": [g, done]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": [
            {"id": "p1", "role": "user", "text": "do the thing",
             "created_at": "2026-08-19T00:00:00Z", "ordinal": 1}]}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        self.trajdir = p.session_dir
        self.env = mock.patch.dict(os.environ, {"HC_BUILD_MODE": "session"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def status(self, gid):
        goals, _ = chat_state.load_goals(self.session, self.root)
        return GM.by_id(goals, gid)["status"]

    def test_attaching_a_prompt_begins_an_active_goal_only(self):
        out = ui._apply({"op": "attach_prompt", "goal_id": "g1", "prompt_id": "p1"},
                        self.trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual("in_progress", self.status("g1"))
        out = ui._apply({"op": "attach_prompt", "goal_id": "g2", "prompt_id": "p1"},
                        self.trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual("completed", self.status("g2"), "a finished goal stays finished")

    def test_a_tombstoned_goal_cannot_be_resurrected_by_an_import(self):
        # Delete g2's tombstone case: an in-flight merge computed before a
        # delete posts the goal back as active. The tombstone wins.
        goals, important = chat_state.load_goals(self.session, self.root)
        GM.by_id(goals, "g2")["status"] = "abandoned"
        chat_state.save_goals(self.session, goals, important, self.root)
        out = ui._import([
            {"id": "g1", "title": "Ship it", "children": []},
            {"id": "g2", "title": "Already done", "children": []},
        ], self.trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual("abandoned", self.status("g2"))

    def test_building_rows_begins_the_goal(self):
        out = ui._apply({"op": "build_todos", "goal_id": "g1", "ids": ["taaaa0001"]},
                        self.trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual("in_progress", self.status("g1"))


if __name__ == "__main__":
    unittest.main()
