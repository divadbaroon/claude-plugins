"""The project behind a chat: the directory it was started in.

A chat's manifest records its cwd. That directory is the project: named,
with its branch and origin where git knows them, its file tree and any text
file bounded and contained, and one objective written once per directory
so every chat started in it reads the same line. No project page of its
own -- the workspace already is the page; this is what it draws from.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import providers  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_chat_ui_server import NO_PROXY_OPENER, get_json, post_json  # noqa: E402


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
        self.assertEqual(PS._resolved(self.project), who["cwd"])
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
                          "objective": "", "description": "",
                          "sources": []}, who)
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

    def test_the_readme_route_reads_the_projects_front_page(self):
        with server_for(self.trajdir) as url:
            found = get_json(url + "/api/readme")
        self.assertEqual({"ok": True, "path": "README.md",
                          "text": "# myrepo\n\nhello\n", "truncated": False},
                         found)

    def test_a_project_with_no_readme_says_so_rather_than_reading_one(self):
        # The lowercase spelling counts; a project with neither does not get
        # an empty pane pretending to be a front page.
        (self.project / "README.md").unlink()
        with server_for(self.trajdir) as url:
            missing = get_json(url + "/api/readme")
            (self.project / "readme.md").write_text("# lower\n")
            lower = get_json(url + "/api/readme")
        self.assertEqual({"ok": False, "error": "this project has no README"},
                         missing)
        # Named as the filesystem hands it back -- a case-insensitive one
        # answers to either spelling -- so it is the text that is checked.
        self.assertEqual("# lower\n", lower["text"])

    def test_a_question_about_the_repository_is_asked_of_its_readme(self):
        seen = {}

        class Engine:
            def generate(self, prompt):
                seen["prompt"] = prompt
                return "  It says hello.  "

        out = ui.ask_source(self.root, str(self.project), None,
                            "  what   does it  say?  ", engine=Engine())
        self.assertEqual({"ok": True, "asked": "what does it say?",
                          "answer": "It says hello."}, out)
        self.assertIn("# myrepo\n\nhello", seen["prompt"])
        self.assertIn("what does it say?", seen["prompt"])
        # The context is that one file, so nothing outside the project can
        # travel with the question.
        self.assertNotIn("do not read", seen["prompt"])

    def test_a_question_with_no_words_never_reaches_the_model(self):
        class Engine:
            def generate(self, prompt):
                raise AssertionError("the model must not be asked")

        self.assertEqual({"ok": False, "error": "ask something first"},
                         ui.ask_source(self.root, str(self.project), None,
                                       "   ", engine=Engine()))

    def test_a_source_that_cannot_be_read_is_not_asked_about(self):
        class Engine:
            def generate(self, prompt):
                raise AssertionError("the model must not be asked")

        self.assertEqual(
            {"ok": False,
             "error": "that is a link, not something this pane can read"},
            ui.ask_source(self.root, str(self.project),
                          {"id": "s1", "type": "github",
                           "label": "https://github.com/acme/other"},
                          "what is it?", engine=Engine()))

    def test_a_document_of_the_project_is_asked_of_its_own_text(self):
        seen = {}

        class Engine:
            def generate(self, prompt):
                seen["prompt"] = prompt
                return "It prints hi."

        out = ui.ask_source(self.root, str(self.project),
                            {"id": "s2", "type": "doc", "label": "src/app.py"},
                            "what does it do?", engine=Engine())
        self.assertEqual({"ok": True, "asked": "what does it do?",
                          "answer": "It prints hi."}, out)
        self.assertIn("print('hi')", seen["prompt"])
        self.assertNotIn("hello", seen["prompt"], "the README is not context here")

    def test_the_ask_route_refuses_a_source_the_project_does_not_have(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/ask", {"id": "nope", "question": "hi"})
        self.assertEqual({"ok": False, "error": "no such source"}, out)

    # --- a question about a passage highlighted in the workspace -----------

    def tree(self):
        """A parent, a child under it with notes and TODO rows of its own."""
        parent = GM.new_goal("g1", "Ship the workspace", origin="user")
        child = GM.new_goal("g1a", "Ask about a highlight", parent_id="g1",
                            origin="user")
        child["notes"] = "The pill appears beside the selection."
        child["status"] = "in_progress"
        child["todo_items"] = [
            {"id": "tpill1", "text": "draw the pill", "depth": 0,
             "status": "done", "question": ""},
            {"id": "tpost1", "text": "post the question", "depth": 1,
             "status": "", "question": ""}]
        goals = {"version": 1, "goals": [parent, child]}
        GM.sanitize(goals)
        return goals

    def test_a_highlight_is_asked_about_with_the_goal_it_sits_in(self):
        seen = {}

        class Engine:
            def generate(self, prompt):
                seen["prompt"] = prompt
                return "  It is the offer beside a selection.  "

        goals = self.tree()
        out = ui.ask_selection(goals["goals"], "g1a", "  the pill  ",
                               "  what   is it? ",
                               objective="Make the workspace think aloud.",
                               engine=Engine())
        self.assertEqual({"ok": True, "asked": "what is it?",
                          "selection": "the pill",
                          "answer": "It is the offer beside a selection."}, out)
        prompt = seen["prompt"]
        self.assertIn("the pill", prompt)
        self.assertIn("Make the workspace think aloud.", prompt)
        # The goal itself, whole; its parent by title only.
        self.assertIn("title: Ask about a highlight", prompt)
        self.assertIn("status: in_progress", prompt)
        self.assertIn("The pill appears beside the selection.", prompt)
        self.assertIn("- draw the pill [done]", prompt)
        self.assertIn("  - post the question", prompt)
        self.assertIn("- Ship the workspace", prompt)
        self.assertNotIn("title: Ship the workspace", prompt)
        # It may be asked to brainstorm, which a question about a document
        # never is -- so it is told to mark what is its own suggestion.
        self.assertIn("brainstorm", prompt)

    def test_a_highlight_and_a_question_are_both_needed(self):
        class Engine:
            def generate(self, prompt):
                raise AssertionError("the model must not be asked")

        goals = self.tree()["goals"]
        self.assertEqual({"ok": False, "error": "ask something first"},
                         ui.ask_selection(goals, "g1a", "the pill", "  ",
                                          engine=Engine()))
        self.assertEqual({"ok": False, "error": "highlight something first"},
                         ui.ask_selection(goals, "g1a", "   ", "what is it?",
                                          engine=Engine()))

    def test_a_passage_under_no_goal_is_asked_about_on_its_own(self):
        seen = {}

        class Engine:
            def generate(self, prompt):
                seen["prompt"] = prompt
                return "It is a heading."

        out = ui.ask_selection(self.tree()["goals"], "nope", "GOALS",
                               "what is it?", engine=Engine())
        self.assertTrue(out["ok"])
        self.assertIn("GOALS", seen["prompt"])
        # No goal is invented to hold it, and no other goal stands in.
        self.assertNotIn("# The goal", seen["prompt"])
        self.assertNotIn("Ship the workspace", seen["prompt"])

    def test_the_selection_route_reads_the_goals_this_chat_has(self):
        session_paths = chat_state.paths(self.session, self.root)
        session_paths.goals.write_text(json.dumps(self.tree()))
        seen = {}

        def answer(lines, engine=None):
            seen["prompt"] = "\n".join(lines)
            return {"ok": True, "answer": "here is why"}

        with mock.patch.object(ui, "_answer", answer):
            with server_for(self.trajdir) as url:
                post_json(url + "/api/op", {
                    "op": "set_project_objective",
                    "objective": "Make the workspace think aloud."})
                out = post_json(url + "/api/ask_selection", {
                    "goal": "g1a", "text": "the pill",
                    "question": "why is it there?"})
        self.assertEqual({"ok": True, "asked": "why is it there?",
                          "selection": "the pill",
                          "answer": "here is why"}, out)
        self.assertIn("title: Ask about a highlight", seen["prompt"])
        self.assertIn("Make the workspace think aloud.", seen["prompt"])
        self.assertIn("why is it there?", seen["prompt"])

    def test_a_follow_up_is_asked_with_what_was_already_said(self):
        seen = {}

        class Engine:
            def generate(self, prompt):
                seen["prompt"] = prompt
                return "The second one."

        out = ui.ask_selection(
            self.tree()["goals"], "g1a", "the pill", "and the other?",
            turns=[{"question": "what is it?", "answer": "The offer."},
                   # Half a turn: asked, never answered. Quoting it back
                   # would read as an answer the model had already given.
                   {"question": "why?", "answer": ""},
                   "not a turn at all"],
            engine=Engine())
        self.assertTrue(out["ok"])
        prompt = seen["prompt"]
        self.assertIn("Q: what is it?", prompt)
        self.assertIn("A: The offer.", prompt)
        self.assertNotIn("Q: why?", prompt)
        # The question being answered is the last thing in the prompt, not
        # one of the ones already answered above it.
        self.assertLess(prompt.index("A: The offer."),
                        prompt.index("and the other?"))

    def test_a_conversation_travels_a_few_turns_deep_at_most(self):
        seen = {}

        class Engine:
            def generate(self, prompt):
                seen["prompt"] = prompt
                return "Still here."

        turns = [{"question": "q%d" % n, "answer": "a%d" % n}
                 for n in range(ui.ASK_TURN_LIMIT + 3)]
        ui.ask_selection(self.tree()["goals"], "g1a", "the pill", "and now?",
                         turns=turns, engine=Engine())
        prompt = seen["prompt"]
        self.assertNotIn("Q: q0", prompt)
        self.assertIn("Q: q%d" % (ui.ASK_TURN_LIMIT + 2), prompt)

    def test_the_selection_route_carries_the_panel_conversation(self):
        chat_state.paths(self.session, self.root).goals.write_text(
            json.dumps(self.tree()))
        seen = {}

        def answer(lines, engine=None):
            seen["prompt"] = "\n".join(lines)
            return {"ok": True, "answer": "because of the first one"}

        with mock.patch.object(ui, "_answer", answer):
            with server_for(self.trajdir) as url:
                out = post_json(url + "/api/ask_selection", {
                    "goal": "g1a", "text": "the pill",
                    "question": "and the other?",
                    "turns": [{"question": "what is it?",
                               "answer": "The offer."}]})
        self.assertTrue(out["ok"])
        self.assertIn("A: The offer.", seen["prompt"])

    def test_what_the_provider_says_went_wrong_is_what_comes_back(self):
        # One message for every failure sent the reader to look for a CLI
        # that was installed all along.
        class Engine:
            def generate(self, prompt):
                raise providers.ProviderError(
                    "claude CLI timed out after 180s")

        self.assertEqual(
            {"ok": False, "error": "claude CLI timed out after 180s"},
            ui.ask_selection(self.tree()["goals"], "g1a", "the pill",
                             "what is it?", engine=Engine()))

    def test_a_failure_the_provider_cannot_name_still_names_the_usual_one(self):
        class Engine:
            def generate(self, prompt):
                raise RuntimeError("boom")

        out = ui.ask_selection(self.tree()["goals"], "g1a", "the pill",
                               "what is it?", engine=Engine())
        self.assertFalse(out["ok"])
        self.assertIn("claude CLI on PATH", out["error"])

    def test_a_question_is_put_without_the_tools_to_go_looking(self):
        # The prompt carries the text the answer comes from. A provider that
        # would start reading the project instead spends the deadline.
        seen = {}

        class Engine:
            def generate(self, prompt):
                raise AssertionError("the agent turn must not be taken")

            def generate_plain(self, prompt):
                seen["prompt"] = prompt
                return "It is the offer."

        out = ui.ask_selection(self.tree()["goals"], "g1a", "the pill",
                               "what is it?", engine=Engine())
        self.assertEqual("It is the offer.", out["answer"])
        self.assertIn("the pill", seen["prompt"])

    def test_the_selection_route_needs_a_question(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/ask_selection",
                            {"goal": "g1", "text": "Ship it", "question": ""})
        self.assertEqual({"ok": False, "error": "ask something first"}, out)

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
        # In the file's `project` section: the flat shape it was first
        # written in is migrated on read, not written any more.
        self.assertEqual("Ship the thing, well.", record["project"]["objective"])
        self.assertEqual(PS._resolved(self.project), record["project"]["cwd"])

    def test_the_project_can_be_renamed_and_the_directory_is_the_fallback(self):
        # A directory is where a project sits today, not what it is called.
        twin_dir = chat_state.paths(self.twin, self.root).session_dir
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "set_project_meta",
                                              "name": "  The Workspace  "})
            self.assertTrue(out["ok"], out)
            self.assertEqual("The Workspace", out["name"])
            self.assertEqual("The Workspace",
                             get_json(url + "/api/state")["project"]["name"])
            bad = post_json(url + "/api/op", {"op": "set_project_meta",
                                              "name": ["not", "text"]})
            self.assertFalse(bad["ok"])
            # Blanking it is not an error: the directory's name comes back.
            back = post_json(url + "/api/op", {"op": "set_project_meta",
                                               "name": "   "})
            self.assertEqual("myrepo", back["name"])
            self.assertEqual("myrepo",
                             get_json(url + "/api/state")["project"]["name"])
        # The name belongs to the directory, so every chat of it reads it.
        with server_for(self.trajdir) as url:
            post_json(url + "/api/op", {"op": "set_project_meta",
                                        "name": "The Workspace"})
        with server_for(twin_dir) as url:
            self.assertEqual("The Workspace",
                             get_json(url + "/api/state")["project"]["name"])

    def test_a_project_is_made_and_set_up_without_a_chat_of_its_own(self):
        # The whole of onboarding over the wire: name it, then answer the
        # two questions against it. The workspace answering is this chat's,
        # and the answers land in the new project's record, not in this one.
        with server_for(self.trajdir) as url:
            made = post_json(url + "/api/op", {"op": "new_project",
                                               "name": "Fresh start"})
            self.assertTrue(made["ok"], made)
            self.assertEqual(True, made["setup"])
            out = post_json(url + "/api/op", {
                "op": "project_setup", "cwd": made["cwd"],
                "objective": "  Ship the redesign.  ",
                "description": "It replaces the rail."})
            self.assertTrue(out["ok"], out)
            self.assertEqual("Ship the redesign.", out["objective"])
            # The switcher lists it, and this chat's own project is untouched.
            listed = get_json(url + "/api/projects")
            self.assertIn("Fresh start", [r["name"] for r in listed["projects"]])
            self.assertEqual("", get_json(url + "/api/state")["project"]["objective"])
            # A second project by the same name is sent back to be renamed.
            again = post_json(url + "/api/op", {"op": "new_project",
                                               "name": "Fresh start"})
            self.assertEqual(True, again["duplicate"])
        record = PS.load_project(self.root, made["cwd"])
        self.assertEqual("Ship the redesign.", record["objective"])
        self.assertEqual("It replaces the rail.", record["description"])

    def test_setting_up_a_project_that_was_never_made_is_refused(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {
                "op": "project_setup", "cwd": str(self.root / "nowhere"),
                "objective": "Ship it."})
        self.assertFalse(out["ok"])
        self.assertIn("has not been made", out["error"])

    def test_a_long_name_is_bounded(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "set_project_meta",
                                              "name": "n" * 500})
        self.assertTrue(out["ok"])
        self.assertEqual(PS.PROJECT_NAME_LIMIT, len(out["name"]))

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


class ProjectListTests(unittest.TestCase):
    """A project is made by hand, and the switcher lists only those.

    It used to also list every directory this machine had run Claude Code
    in, read out of the transcripts under ~/.claude/projects. That is a
    list of where you have been rather than of what you are working on --
    ~/Downloads and /private/tmp were on it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_directory_with_chats_but_no_record_is_not_a_project(self):
        session = "chat-here"
        paths = chat_state.paths(session, self.root)
        paths.session_dir.mkdir(parents=True)
        paths.manifest.write_text(json.dumps(
            {"session_id": session, "cwd": str(self.root / "wandered")}))
        self.assertEqual([], PS.list_projects(self.root))

    def test_a_record_makes_it_one(self):
        PS.touch(self.root, str(self.root / "made"))
        self.assertEqual(["made"],
                         [r["name"] for r in PS.list_projects(self.root)])

    def test_touching_a_project_twice_leaves_what_was_written(self):
        PS.save_project(self.root, str(self.root / "made"),
                        {"objective": "Ship it."})
        PS.touch(self.root, str(self.root / "made"))
        self.assertEqual("Ship it.",
                         PS.load_project(self.root, str(self.root / "made"))["objective"])

    def test_the_one_being_looked_at_is_listed_even_with_no_record(self):
        here = str(self.root / "unwritten")
        rows = ui._all_projects(self.root, here)
        self.assertEqual([here], [r["cwd"] for r in rows])


class NewProjectTests(unittest.TestCase):
    """A project is made by naming it.

    Everything here is keyed by a directory, but having one is not something
    a reader should have to arrange first: a project is somewhere to keep an
    objective, its goals and its sources, and most are named before there is
    any code to point at. A name gets a home of its own inside the vault. A
    path is still taken when one is typed, so a repository already on disk
    becomes the project it is.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_name_alone_makes_a_project(self):
        made = ui.new_project("Redesign the onboarding", root=self.root)
        self.assertTrue(made["ok"], made)
        self.assertEqual("Redesign the onboarding", made["name"])
        self.assertTrue(Path(made["cwd"]).is_dir())
        # And the switcher lists it, under the name rather than the folder.
        self.assertEqual(["Redesign the onboarding"],
                         [r["name"] for r in PS.list_projects(self.root)])

    def test_the_folder_it_is_given_is_a_slug_of_the_name(self):
        made = ui.new_project("Redesign the Onboarding!", root=self.root)
        self.assertEqual("redesign-the-onboarding", Path(made["cwd"]).name)
        self.assertEqual("workspaces", Path(made["cwd"]).parent.name)

    def test_a_name_somebody_already_used_is_sent_back_to_be_renamed(self):
        # Two projects with one name are two answers to "which one did I
        # mean". The second is refused rather than folded into the first:
        # the reader is making a second thing, and the fix is a name.
        first = ui.new_project("Same thing", root=self.root)
        PS.save_project(self.root, first["cwd"], {"objective": "Ship it."})
        again = ui.new_project("Same thing", root=self.root)
        self.assertEqual(False, again["ok"])
        self.assertEqual(True, again["duplicate"])
        self.assertIn("already called", again["error"])
        self.assertEqual(1, len(PS.list_projects(self.root)))
        # And nothing written about the first one was touched.
        self.assertEqual("Ship it.", PS.load_project(
            self.root, first["cwd"])["objective"])

    def test_the_same_name_spelled_differently_is_still_the_same_name(self):
        ui.new_project("Same thing", root=self.root)
        for text in ("same thing", "SAME  THING", "Same thing"):
            got = ui.new_project(text, root=self.root)
            self.assertEqual(True, got.get("duplicate"), text)
        self.assertEqual(1, len(PS.list_projects(self.root)))

    def test_a_project_renamed_since_is_still_found_by_its_home(self):
        made = ui.new_project("Same thing", root=self.root)
        PS.save_project(self.root, made["cwd"], {"name": "Something else"})
        again = ui.new_project("Same thing", root=self.root)
        self.assertEqual(True, again["duplicate"])
        self.assertEqual("Something else", again["name"])

    def test_a_project_with_nothing_in_it_yet_asks_to_be_set_up(self):
        # A project made from a name has no chat behind it and no objective:
        # it knows nothing about itself, which is what onboarding is for.
        made = ui.new_project("Fresh start", root=self.root)
        self.assertEqual(True, made["setup"])
        self.assertEqual(0, made["chats"])

    def test_a_directory_that_has_been_worked_in_is_not_asked_again(self):
        where = self.root / "worked"
        where.mkdir()
        session = "chat-there"
        paths = chat_state.paths(session, self.root)
        paths.session_dir.mkdir(parents=True)
        paths.manifest.write_text(json.dumps(
            {"session_id": session, "cwd": str(where)}))
        made = ui.new_project(str(where), root=self.root)
        self.assertEqual(1, made["chats"])
        self.assertEqual(False, made["setup"])

    def test_a_project_that_has_answered_already_is_not_asked_again(self):
        made = ui.new_project("Answered", root=self.root)
        PS.save_project(self.root, made["cwd"], {"objective": "Ship it."})
        # Made once, the record stands; what a second look reports is what
        # the switcher would ask, and it has nothing left to ask.
        self.assertEqual(False, ui._made(
            self.root, made["cwd"], "Answered")["setup"])

    def test_a_name_with_nothing_nameable_in_it_is_refused(self):
        for text in ("", "   ", "!!!"):
            got = ui.new_project(text, root=self.root)
            self.assertEqual(False, got["ok"], text)
            self.assertEqual("give the project a name", got["error"])
        self.assertEqual([], PS.list_projects(self.root))

    def test_a_path_that_is_typed_is_still_a_directory(self):
        where = self.root / "onefolder"
        where.mkdir()
        made = ui.new_project(str(where), root=self.root)
        self.assertTrue(made["ok"], made)
        self.assertEqual("onefolder", made["name"])
        self.assertEqual(str(where.resolve()), made["cwd"])

    def test_a_path_that_is_not_there_is_reported_rather_than_created(self):
        missing = self.root / "nowhere" / "deep"
        got = ui.new_project(str(missing), root=self.root)
        self.assertEqual(False, got["ok"])
        self.assertIn("no such directory", got["error"])
        self.assertFalse(missing.exists())

    def test_the_browsers_older_field_is_still_understood(self):
        where = self.root / "legacy"
        where.mkdir()
        made = ui.new_project(None, str(where), root=self.root)
        self.assertEqual(str(where.resolve()), made["cwd"])


class CloneProjectTests(unittest.TestCase):
    """A project made out of a repository that already exists.

    The clone lands in the home the project is given, beside the other
    projects, so the code is there from the first moment rather than after
    an errand in a terminal. Git is stood in for here: what matters is what
    it is asked, and what is written once it answers.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @contextmanager
    def git(self, returncode=0, stderr="", raises=None):
        """Stand in for git clone: what it was asked, what it said."""
        seen = {"commands": []}

        def run(command, **kwargs):
            seen["commands"].append(list(command))
            seen.setdefault("envs", []).append(kwargs.get("env") or {})
            seen["env"] = seen["envs"][0]
            if raises is not None:
                raise raises
            if not returncode:
                # A real clone leaves the directory it was given.
                Path(command[-1]).mkdir(parents=True, exist_ok=True)
                (Path(command[-1]) / "README.md").write_text("hi")
            return mock.Mock(stdout="", stderr=stderr, returncode=returncode)

        with mock.patch("subprocess.run", run):
            yield seen

    def test_a_repository_url_is_cloned_into_the_projects_home(self):
        with self.git() as seen:
            made = ui.new_project("https://github.com/me/widget.git",
                                  root=self.root)
        self.assertEqual(True, made["ok"], made)
        # Named after the repository, in a home of its own beside the rest.
        self.assertEqual("widget", made["name"])
        self.assertEqual("widget", Path(made["cwd"]).name)
        self.assertEqual("workspaces", Path(made["cwd"]).parent.name)
        self.assertEqual("https://github.com/me/widget.git", made["cloned"])
        self.assertTrue((Path(made["cwd"]) / "README.md").is_file())
        # Git's own argument list, never a shell's, with -- ending the flags.
        self.assertEqual(["git", "clone", "--",
                          "https://github.com/me/widget.git", made["cwd"]],
                         seen["commands"][0])
        # And no credential prompt: nobody is watching this terminal.
        self.assertEqual("0", seen["env"].get("GIT_TERMINAL_PROMPT"))
        # It is a project of the vault, listed under the name it was given.
        self.assertEqual(["widget"],
                         [r["name"] for r in PS.list_projects(self.root)])

    def test_a_name_typed_beside_the_repository_is_what_it_is_called(self):
        with self.git():
            made = ui.new_project("The widget",
                                  repo="git@github.com:me/widget.git",
                                  root=self.root)
        self.assertEqual("The widget", made["name"])
        self.assertEqual("the-widget", Path(made["cwd"]).name)

    def test_a_clone_that_failed_is_reported_and_makes_no_project(self):
        with self.git(returncode=128,
                      stderr="fatal: repository not found\n"):
            got = ui.new_project("https://github.com/me/nope.git",
                                 root=self.root)
        self.assertEqual(False, got["ok"])
        self.assertEqual("fatal: repository not found", got["error"])
        self.assertEqual([], PS.list_projects(self.root))

    def test_a_machine_without_git_says_so(self):
        with self.git(raises=FileNotFoundError("git")):
            got = ui.new_project("https://github.com/me/widget.git",
                                 root=self.root)
        self.assertEqual(False, got["ok"])
        self.assertIn("git is not installed", got["error"])

    def test_a_clone_left_running_forever_gives_up(self):
        with self.git(raises=subprocess.TimeoutExpired("git", 1)):
            got = ui.new_project("https://github.com/me/widget.git",
                                 root=self.root)
        self.assertEqual(False, got["ok"])
        self.assertIn("too long", got["error"])

    def test_cloning_over_a_project_that_exists_asks_for_another_name(self):
        with self.git():
            ui.new_project("https://github.com/me/widget.git", root=self.root)
            again = ui.new_project("https://github.com/other/widget.git",
                                   root=self.root)
        self.assertEqual(True, again["duplicate"])
        self.assertEqual(1, len(PS.list_projects(self.root)))

    def test_only_the_transports_git_is_asked_to_speak_are_repositories(self):
        for text in ("https://github.com/me/widget.git",
                     "http://host/me/widget",
                     "ssh://git@host/me/widget.git",
                     "git://host/me/widget.git",
                     "git@github.com:me/widget.git"):
            self.assertEqual(True, ui._looks_like_a_repo(text), text)
        # A local path is a folder to point at, and the two URLs that would
        # have git run a command of the URL's choosing are not repositories.
        for text in ("My redesign", "~/Projects/widget", "/tmp/widget",
                     "ext::sh -c whoami", "file:///tmp/widget",
                     "--upload-pack=touch /tmp/x"):
            self.assertEqual(False, ui._looks_like_a_repo(text), text)

    def test_a_url_that_is_not_one_is_refused_before_git_is_run(self):
        with self.git() as seen:
            got = ui.clone_project("not a url", root=self.root)
        self.assertEqual(False, got["ok"])
        self.assertIn("not a repository URL", got["error"])
        self.assertEqual([], seen["commands"])


class ProjectSetupTests(unittest.TestCase):
    """The two questions a project nobody has worked in yet is asked.

    A project made from the switcher has no chat behind it and so no
    workspace of its own to answer in: what it is for and what is worth
    knowing first are written against that project's own record, from the
    workspace that made it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_answers_are_written_to_the_project_that_was_made(self):
        made = ui.new_project("Fresh start", root=self.root)
        got = ui.project_setup(made["cwd"], "Ship the redesign.",
                               "It replaces the old rail.", self.root)
        self.assertEqual(True, got["ok"], got)
        record = PS.load_project(self.root, made["cwd"])
        self.assertEqual("Ship the redesign.", record["objective"])
        self.assertEqual("It replaces the old rail.", record["description"])
        # And the name it was made with is still its own.
        self.assertEqual("Fresh start", record["name"])

    def test_answering_one_question_leaves_the_other_alone(self):
        made = ui.new_project("Half answered", root=self.root)
        ui.project_setup(made["cwd"], "Ship it.", None, self.root)
        ui.project_setup(made["cwd"], None, "Some context.", self.root)
        record = PS.load_project(self.root, made["cwd"])
        self.assertEqual("Ship it.", record["objective"])
        self.assertEqual("Some context.", record["description"])

    def test_a_project_that_was_never_made_is_not_written_to(self):
        got = ui.project_setup(str(self.root / "nowhere"), "Ship it.",
                               root=self.root)
        self.assertEqual(False, got["ok"])
        self.assertIn("has not been made", got["error"])
        self.assertEqual([], PS.list_projects(self.root))

    def test_a_missing_directory_is_refused_rather_than_guessed(self):
        got = ui.project_setup("", "Ship it.", root=self.root)
        self.assertEqual(False, got["ok"])
        self.assertEqual("which project?", got["error"])

    def test_an_answer_that_is_not_text_is_refused(self):
        made = ui.new_project("Typed wrong", root=self.root)
        for field in ({"objective": 7}, {"description": []}):
            got = ui.project_setup(made["cwd"], root=self.root, **field)
            self.assertEqual(False, got["ok"], field)
        self.assertEqual("", PS.load_project(
            self.root, made["cwd"]).get("objective", ""))


class PickDirectoryTests(unittest.TestCase):
    """Pointing at a folder instead of spelling one.

    A path is the one thing in the new-project box nobody wants to type, so
    the machine's own folder chooser is opened and what it answers with comes
    back as text. Closing the dialog is an ordinary outcome, not an error.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    @contextmanager
    def chooser(self, stdout="", returncode=0, raises=None, stderr=""):
        """Stand in for the platform's dialog: what it was asked, what it said.

        Which program that is belongs to :func:`ui._chooser_command` and is
        tested there; here only the answer matters, so every platform runs
        the same test.
        """
        seen = {"starts": []}

        def run(command, **kwargs):
            if raises is not None:
                raise raises
            return mock.Mock(stdout=stdout, stderr=stderr,
                             returncode=returncode)

        def command(here):
            seen["starts"].append(here)
            return ["chooser"]

        with mock.patch.object(ui, "_chooser_command", command), \
                mock.patch("subprocess.run", run):
            yield seen

    def test_what_was_picked_comes_back_as_a_path(self):
        where = self.root / "picked"
        where.mkdir()
        with self.chooser(stdout=str(where) + "\n"):
            got = ui.pick_directory()
        self.assertEqual(True, got["ok"], got)
        self.assertEqual(str(where.resolve()), got["cwd"])
        self.assertEqual("picked", got["name"])

    def test_closing_the_dialog_is_not_a_failure(self):
        # Every one of these choosers says "never mind" the same way: an
        # empty answer, whatever it does with its exit code.
        for code in (0, 1):
            with self.chooser(stdout="", returncode=code):
                got = ui.pick_directory()
            self.assertEqual({"ok": True, "cancelled": True}, got)
        # macOS says so in words on the way out; that is still a cancel.
        with self.chooser(returncode=1, stderr="execution error: User "
                          "canceled. (-128)"):
            self.assertEqual({"ok": True, "cancelled": True},
                             ui.pick_directory())

    def test_a_chooser_that_could_not_open_says_why(self):
        with self.chooser(returncode=1,
                          stderr="Unable to access the display"):
            got = ui.pick_directory()
        self.assertEqual(False, got["ok"])
        self.assertEqual("Unable to access the display", got["error"])

    def test_a_directory_that_is_not_there_is_reported(self):
        gone = self.root / "gone"
        with self.chooser(stdout=str(gone)):
            got = ui.pick_directory()
        self.assertEqual(False, got["ok"])
        self.assertIn("no such directory", got["error"])

    def test_the_dialog_opens_where_the_reader_already_is(self):
        with self.chooser(stdout=str(self.root)) as seen:
            ui.pick_directory(str(self.root))
            # A start that is not a directory is simply not passed on.
            ui.pick_directory(str(self.root / "nowhere"))
            ui.pick_directory()
        self.assertEqual([str(self.root), "", ""], seen["starts"])

    def test_a_machine_with_no_chooser_says_so(self):
        with mock.patch.object(ui, "_chooser_command", lambda here: None):
            got = ui.pick_directory()
        self.assertEqual(False, got["ok"])
        self.assertIn("type a path", got["error"])
        # And so does one whose chooser turns out not to be installed.
        with self.chooser(raises=FileNotFoundError("zenity")):
            got = ui.pick_directory()
        self.assertEqual(False, got["ok"])
        self.assertIn("type a path", got["error"])

    def test_a_dialog_left_open_forever_gives_up_quietly(self):
        with self.chooser(raises=subprocess.TimeoutExpired("osascript", 1)):
            got = ui.pick_directory()
        self.assertEqual({"ok": True, "cancelled": True}, got)

    def test_each_desktop_is_asked_in_its_own_language(self):
        with mock.patch.object(sys, "platform", "darwin"):
            command = ui._chooser_command("/Users/me/work")
        self.assertEqual("osascript", command[0])
        self.assertIn("choose folder", command[-1])
        self.assertIn('POSIX file "/Users/me/work"', command[-1])
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(ui.os, "name", "posix"), \
                mock.patch("shutil.which", lambda name: name == "zenity"):
            command = ui._chooser_command("/home/me/work")
        self.assertEqual("zenity", command[0])
        self.assertIn("--directory", command)
        # A desktop with none of them installed has no question to ask.
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(ui.os, "name", "posix"), \
                mock.patch("shutil.which", lambda name: None):
            self.assertIsNone(ui._chooser_command(""))


class StaleServerTests(unittest.TestCase):
    """A page newer than the process answering it, and how that reads.

    ``/bridge.js`` is read from disk on every load; the Python behind it was
    read once, when the workspace started. Editing the plugin with a
    workspace open therefore leaves the two halves a version apart, and the
    controls added by the edit reach a server that has never heard of them.
    That is a restart, not a bug, and both ends say so.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        paths = chat_state.paths("chat-stale", self.root)
        paths.session_dir.mkdir(parents=True)
        goals = {"version": 1,
                 "goals": [GM.new_goal("g1", "Ship it", origin="user")]}
        GM.sanitize(goals)
        paths.goals.write_text(json.dumps(goals))
        paths.important.write_text(json.dumps({"items": []}))
        paths.prompts.write_text(json.dumps({"prompts": []}))
        paths.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        self.trajdir = paths.session_dir

    def script(self, url):
        request = urllib.request.Request(url + "/bridge.js")
        with NO_PROXY_OPENER.open(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def test_an_operation_this_build_never_had_is_named(self):
        # The old answer -- "unknown or invalid op" -- was the same sentence
        # for a control that does not exist and for one whose goal is gone,
        # and neither could be acted on.
        with server_for(self.trajdir) as url:
            got = post_json(url + "/api/op", {"op": "pick_a_folder"})
        self.assertEqual(False, got["ok"])
        self.assertEqual("unknown operation: pick_a_folder", got["error"])

    def test_a_goal_that_is_not_here_says_that_instead(self):
        with server_for(self.trajdir) as url:
            got = post_json(url + "/api/op", {"op": "set_status",
                                              "goal_id": "gone",
                                              "status": "completed"})
        self.assertEqual(False, got["ok"])
        self.assertEqual("goal not found in this workspace", got["error"])

    def test_the_page_is_told_the_server_is_current(self):
        with server_for(self.trajdir) as url:
            self.assertIn("window.__hcServerStale = false;",
                          self.script(url))

    def test_the_page_is_told_when_the_plugin_was_edited_since(self):
        # Standing in for the edit: a process that read its code long enough
        # ago that everything on disk is newer than it.
        with mock.patch.object(ui, "_CODE_STAMP", 1.0), \
                server_for(self.trajdir) as url:
            self.assertIn("window.__hcServerStale = true;", self.script(url))

    def test_a_package_with_no_readable_files_claims_nothing(self):
        # An installed copy whose timestamps cannot be read is not evidence
        # of an edit; a workspace must not open under a warning it invented.
        with mock.patch.object(ui.Path, "glob",
                               mock.Mock(side_effect=OSError)):
            self.assertEqual(0.0, ui._code_stamp())
            self.assertFalse(ui._server_is_stale())
