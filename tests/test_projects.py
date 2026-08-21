"""The project behind a chat: the directory it was started in.

A chat's manifest records its cwd. That directory is the project: named,
with its branch and origin where git knows them, its file tree and any text
file bounded and contained, and one objective written once per directory
so every chat started in it reads the same line. No project page of its
own -- the workspace already is the page; this is what it draws from.
"""

import json
import os
import sys
import tempfile
import threading
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


def turn(session, text, ts):
    return json.dumps({"type": "user", "uuid": "u-%s-%s" % (session, ts),
                       "timestamp": ts, "sessionId": session,
                       "message": {"role": "user", "content": text}}) + "\n"


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # The project directory: a few files, a fake .git with an origin,
        # a secret outside it that a symlink points at.
        self.project = self.root / "work" / "myrepo"
        (self.project / "src").mkdir(parents=True)
        (self.project / "README.md").write_text("# myrepo\n\nhello\n")
        (self.project / "src" / "app.py").write_text("print('hi')\n")
        (self.project / ".git").mkdir()
        (self.project / ".git" / "config").write_text(
            '[core]\n\trepositoryformatversion = 0\n'
            '[remote "origin"]\n\turl = https://github.com/acme/myrepo.git\n'
            '\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
        (self.project / ".git" / "HEAD").write_text("ref: refs/heads/feat/x\n")
        (self.project / "node_modules").mkdir()
        (self.project / "node_modules" / "junk.js").write_text("x")
        (self.root / "secret.txt").write_text("do not read")
        try:
            (self.project / "escape").symlink_to(self.root / "secret.txt")
        except OSError:
            pass
        self.session = "chat-main"
        self.twin = "chat-twin"
        self.elsewhere = "chat-elsewhere"
        self.claude_home = self.root / "claude-home"
        for session in (self.session, self.twin, self.elsewhere):
            p = chat_state.paths(session, self.root)
            p.session_dir.mkdir(parents=True)
            goals = {"version": 1,
                     "goals": [GM.new_goal("g1", "Ship it", origin="user")]}
            GM.sanitize(goals)
            p.goals.write_text(json.dumps(goals))
            p.important.write_text(json.dumps({"items": []}))
            p.prompts.write_text(json.dumps({"prompts": []}))
            directory = self.claude_home / "projects" / ("-Users-me-" + session)
            directory.mkdir(parents=True)
            (directory / f"{session}.jsonl").write_text(
                turn(session, "first in %s" % session, "2026-08-20T01:00:00Z"))
            cwd = self.project if session != self.elsewhere else self.root / "other"
            p.manifest.write_text(json.dumps({
                "cwd": str(cwd),
                "transcript_path": str(directory / f"{session}.jsonl")}))
        self.trajdir = chat_state.paths(self.session, self.root).session_dir
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "HC_CHAT_FOLLOW_SECONDS": "0.1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_state_names_the_project_from_the_manifest(self):
        with server_for(self.trajdir) as url:
            who = get_json(url + "/api/state")["project"]
        self.assertEqual(str(self.project), who["cwd"])
        self.assertEqual("myrepo", who["name"])
        self.assertEqual("feat/x", who["branch"])
        self.assertEqual("https://github.com/acme/myrepo.git", who["remote"])
        self.assertEqual("", who["objective"])

    def test_a_chat_without_a_directory_has_an_empty_project(self):
        chat_state.paths(self.session, self.root).manifest.write_text("{}")
        with server_for(self.trajdir) as url:
            who = get_json(url + "/api/state")["project"]
            tree = get_json(url + "/api/tree")
            out = post_json(url + "/api/op", {"op": "set_project_objective",
                                              "objective": "x"})
        self.assertEqual({"cwd": "", "name": "", "branch": "", "remote": "",
                          "objective": ""}, who)
        self.assertEqual([], tree["tree"])
        self.assertFalse(out["ok"])

    def test_the_tree_is_the_projects_files_and_nothing_outside_it(self):
        with server_for(self.trajdir) as url:
            out = get_json(url + "/api/tree")
        self.assertTrue(out["ok"])
        self.assertEqual(str(self.project), out["root"])
        names = [row["n"] for row in out["tree"]]
        self.assertIn("README.md", names)
        self.assertIn("src/", names)
        self.assertNotIn(".git/", names, "dot directories are not listed")
        self.assertNotIn("node_modules/", names)
        self.assertNotIn("escape", names, "a symlink out of the project is not listed")
        src = next(row for row in out["tree"] if row["n"] == "src/")
        self.assertEqual([{"n": "app.py"}], src["kids"])

    def test_a_file_is_read_only_inside_the_project(self):
        with server_for(self.trajdir) as url:
            readme = get_json(url + "/api/file?path=README.md")
            nested = get_json(url + "/api/file?path=src/app.py")
            up = get_json(url + "/api/file?path=../secret.txt")
            link = get_json(url + "/api/file?path=escape")
            missing = get_json(url + "/api/file?path=nope.md")
            folder = get_json(url + "/api/file?path=src")
            blank = get_json(url + "/api/file")
        self.assertEqual({"ok": True, "path": "README.md",
                          "text": "# myrepo\n\nhello\n", "truncated": False},
                         readme)
        self.assertEqual("print('hi')\n", nested["text"])
        for refused in (up, link, missing, folder, blank):
            self.assertFalse(refused["ok"], refused)
        self.assertNotIn("do not read", json.dumps([up, link]))

    def test_the_objective_is_kept_once_per_directory(self):
        twin_dir = chat_state.paths(self.twin, self.root).session_dir
        elsewhere_dir = chat_state.paths(self.elsewhere, self.root).session_dir
        (self.root / "other").mkdir()
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {
                "op": "set_project_objective",
                "objective": "  Ship the thing, well.  "})
            self.assertEqual({"ok": True, "objective": "Ship the thing, well."}, out)
            self.assertEqual("Ship the thing, well.",
                             get_json(url + "/api/state")["project"]["objective"])
            bad = post_json(url + "/api/op", {"op": "set_project_objective",
                                              "objective": ["not", "text"]})
            self.assertFalse(bad["ok"])
        # Another chat started in the same directory reads the same line;
        # one started elsewhere does not.
        with server_for(twin_dir) as url:
            self.assertEqual("Ship the thing, well.",
                             get_json(url + "/api/state")["project"]["objective"])
        with server_for(elsewhere_dir) as url:
            self.assertEqual("",
                             get_json(url + "/api/state")["project"]["objective"])
        # Stored under the vault base, not in any one session's directory.
        stored = list((self.root / "projects").glob("*.json"))
        self.assertEqual(1, len(stored))
        record = json.loads(stored[0].read_text())
        self.assertEqual("Ship the thing, well.", record["objective"])
        self.assertEqual(str(self.project), record["cwd"])

    def test_a_long_objective_is_bounded(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "set_project_objective",
                                              "objective": "x" * 5000})
        self.assertTrue(out["ok"])
        self.assertEqual(ui.PROJECT_OBJECTIVE_LIMIT, len(out["objective"]))

    def test_discovered_chats_say_where_they_worked(self):
        with server_for(self.trajdir) as url:
            rows = get_json(url + "/api/chats")["available"]
        by_id = {row["session_id"]: row for row in rows}
        self.assertEqual(str(self.project), by_id[self.twin]["cwd"])
        self.assertEqual(str(self.root / "other"), by_id[self.elsewhere]["cwd"])
        self.assertNotIn(self.session, by_id)

    def test_the_mock_project_page_is_gone(self):
        # The redrawn workspace shipped as a page of its own. Projects live
        # in the workspace now; the page and its script are not served.
        import urllib.error
        import urllib.request
        with server_for(self.trajdir) as url:
            for path in ("/projects", "/projects.html", "/projects.js"):
                request = urllib.request.Request(url + path)
                try:
                    with urllib.request.build_opener(
                            urllib.request.ProxyHandler({})).open(
                            request, timeout=2) as response:
                        code, body = response.status, response.read()
                except urllib.error.HTTPError as exc:
                    code, body = exc.code, exc.read()
                self.assertNotEqual(200, code, path)
                self.assertNotIn(b"__bundler", body)


if __name__ == "__main__":
    unittest.main()
