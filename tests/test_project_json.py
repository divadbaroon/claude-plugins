"""One JSON file per project, holding everything known about it.

A project is the directory a chat was started in, so the file is keyed by
that directory and holds the goals of EVERY chat started there -- each goal
with where it sits in its tree, its notes, its TODO rows and their statuses,
its prompt and the prompts marked related to it -- beside the project's own
metadata: its name, what the reader wrote about it, and the sources they
saved to it. The goals are regenerated on every save; the reader's own lines
survive that regeneration untouched.
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
from human_compact.trajectory import project_store as PS  # noqa: E402
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


class ProjectFixture(unittest.TestCase):
    """Two chats in one directory, a third in another, and one furnished
    goal tree: a parent, its children, notes, TODO rows, prompts, sources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "work" / "myrepo"
        self.project.mkdir(parents=True)
        self.other = self.root / "work" / "elsewhere"
        self.other.mkdir(parents=True)
        self.session, self.twin, self.away = "chat-main", "chat-twin", "chat-away"

        # The main chat: a parent with two children, notes, a TODO list whose
        # rows carry statuses, the reader's prompt, and two marked prompts --
        # one the reader chose, one inference linked from its own evidence.
        parent = GM.new_goal("g1", "Ship the workspace", origin="user")
        parent.update({
            "notes": "# Objective\nOne file per project.\n",
            "description": "the trunk",
            "priority": "high",
            "todo_items": [
                {"id": "t1111aaa", "text": "Write the record", "depth": 0,
                 "status": "done"},
                {"id": "t2222bbb", "text": "Write the tests", "depth": 1,
                 "status": "building"},
                {"id": "t3333ccc", "text": "Which shape?", "depth": 1,
                 "status": "asking", "question": "one file or two?"}],
            "prompt_md": "Store the goals as JSON.\n",
            "prompt_ids": ["p1", "p2"],
            "auto_prompt_ids": ["p2"],
            "sources": [{"id": "s1", "type": "github", "label": "acme/myrepo"}],
            "important_item_ids": ["i1"],
            "evidence_ids": ["e1"],
        })
        first = GM.new_goal("g1a", "Decide the shape", "g1")
        second = GM.new_goal("g1b", "Write it down", "g1")
        self.goals = {"version": 1, "goals": [parent, first, second]}
        self.important = {"items": [{"id": "i1", "text": "keep it one file",
                                     "goal_id": "g1", "origin": "user"}]}

        for session in (self.session, self.twin, self.away):
            p = chat_state.paths(session, self.root)
            p.session_dir.mkdir(parents=True)
            if session == self.session:
                goals = json.loads(json.dumps(self.goals))
                important = self.important
            else:
                goals = {"version": 1, "goals": [
                    GM.new_goal("g1", "A goal of %s" % session)]}
                important = {"items": []}
            GM.sanitize(goals)
            p.goals.write_text(json.dumps(goals))
            p.important.write_text(json.dumps(important))
            p.prompts.write_text(json.dumps({"prompts": [
                {"id": "p1", "role": "user", "text": "chosen prompt",
                 "session_id": session, "created_at": "2026-08-01"},
                {"id": "p2", "role": "user", "text": "linked prompt",
                 "session_id": session, "created_at": "2026-08-02"},
                {"id": "p3", "role": "user", "text": "unmarked prompt",
                 "session_id": session, "created_at": "2026-08-03"}]}))
            cwd = self.other if session == self.away else self.project
            p.manifest.write_text(json.dumps({
                "session_id": session, "cwd": str(cwd),
                "created_at": "2026-08-01T00:00:00+00:00",
                "updated_at": "2026-08-02T00:00:00+00:00"}))
        self.trajdir = chat_state.paths(self.session, self.root).session_dir
        self.env = mock.patch.dict(os.environ, {"HC_CHAT_FOLLOW_SECONDS": "0.1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    # --- helpers ---------------------------------------------------------

    def written(self, cwd=None):
        path = PS.project_path(self.root, cwd or self.project)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, session=None):
        session = session or self.session
        goals, important = chat_state.load_goals(session, self.root)
        self.assertTrue(
            chat_state.save_goals(session, goals, important, self.root))

    def goal(self, record, key):
        return next(g for g in record["goals"] if g["key"] == key)


class ProjectJsonTests(ProjectFixture):
    """The file itself: where it is, and what it holds."""

    def test_a_save_writes_one_file_for_the_whole_project(self):
        self.save()
        stored = sorted((self.root / "projects").glob("*.json"))
        self.assertEqual(1, len(stored), "one file per directory, not per chat")
        record = self.written()
        self.assertEqual(PS.SCHEMA_VERSION, record["schema_version"])
        self.assertTrue(record["generated_at"])
        # Every chat started in the directory is in it; one started elsewhere
        # is not, however recently it saved.
        self.assertEqual([self.session, self.twin],
                         sorted(row["session_id"] for row in record["chats"]))
        keys = {g["key"] for g in record["goals"]}
        self.assertEqual({"chat-main:g1", "chat-main:g1a", "chat-main:g1b",
                          "chat-twin:g1"}, keys)

    def test_the_other_directory_keeps_its_own_file(self):
        self.save(self.away)
        away = self.written(self.other)
        self.assertEqual([self.away],
                         [row["session_id"] for row in away["chats"]])
        self.assertEqual(["chat-away:g1"], [g["key"] for g in away["goals"]])
        self.assertEqual(str(self.other), away["project"]["cwd"])

    def test_every_goal_is_in_the_file_whatever_its_status(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        for goal, status in zip(goals["goals"],
                                ("completed", "abandoned", "in_progress")):
            goal["status"] = status
        self.assertTrue(
            chat_state.save_goals(self.session, goals, important, self.root))
        self.assertEqual({"chat-main:g1": "completed",
                          "chat-main:g1a": "abandoned",
                          "chat-main:g1b": "in_progress"},
                         {g["key"]: g["status"] for g in self.written()["goals"]
                          if g["session_id"] == self.session})

    def test_a_chat_with_no_directory_writes_no_file(self):
        chat_state.paths(self.session, self.root).manifest.write_text("{}")
        self.save()
        self.assertEqual([], list((self.root / "projects").glob("*.json")))

    # --- what each goal carries -----------------------------------------

    def test_a_goal_says_where_it_sits_in_its_tree(self):
        self.save()
        record = self.written()
        parent = self.goal(record, "chat-main:g1")
        child = self.goal(record, "chat-main:g1a")
        self.assertEqual(
            {"parent_id": None, "parent_key": None,
             "child_ids": ["g1a", "g1b"],
             "child_keys": ["chat-main:g1a", "chat-main:g1b"],
             "sibling_ids": [], "sibling_keys": [], "depth": 1,
             "title_path": ["Ship the workspace"]}, parent["location"])
        self.assertEqual(
            {"parent_id": "g1", "parent_key": "chat-main:g1",
             "child_ids": [], "child_keys": [],
             "sibling_ids": ["g1b"], "sibling_keys": ["chat-main:g1b"],
             "depth": 2,
             "title_path": ["Ship the workspace", "Decide the shape"]},
            child["location"])

    def test_a_goal_carries_its_notes_and_its_own_facts(self):
        self.save()
        parent = self.goal(self.written(), "chat-main:g1")
        self.assertEqual("# Objective\nOne file per project.\n", parent["notes"])
        self.assertEqual("Ship the workspace", parent["title"])
        self.assertEqual("active", parent["status"])
        self.assertEqual("high", parent["priority"])
        self.assertEqual("the trunk", parent["description"])
        self.assertEqual("user", parent["origin"])
        self.assertEqual(["e1"], parent["evidence_ids"])
        self.assertEqual([{"id": "s1", "type": "github", "label": "acme/myrepo"}],
                         parent["sources"])
        self.assertEqual("keep it one file", parent["important"][0]["text"])

    def test_a_goal_carries_its_todos_and_the_status_of_each(self):
        self.save()
        parent = self.goal(self.written(), "chat-main:g1")
        self.assertEqual(
            [("Write the record", 0, "done", ""),
             ("Write the tests", 1, "building", ""),
             ("Which shape?", 1, "asking", "one file or two?")],
            [(row["text"], row["depth"], row["status"], row["question"])
             for row in parent["todos"]])
        self.assertEqual("- Write the record\n    - Write the tests\n"
                         "    - Which shape?\n", parent["todos_md"])
        self.assertEqual([], self.goal(self.written(),
                                       "chat-main:g1a")["todos"])

    def test_a_goal_carries_its_prompt_and_the_prompts_marked_related(self):
        self.save()
        parent = self.goal(self.written(), "chat-main:g1")
        self.assertEqual("Store the goals as JSON.\n", parent["prompt"])
        self.assertEqual(
            [("p1", "chosen prompt", False), ("p2", "linked prompt", True)],
            [(row["id"], row["text"], row["auto"])
             for row in parent["related_prompts"]])
        self.assertEqual("2026-08-01", parent["related_prompts"][0]["created_at"])
        # A prompt nobody marked stays out of every goal.
        self.assertNotIn("unmarked prompt", json.dumps(self.written()))

    # --- the project's own metadata --------------------------------------

    def test_the_project_metadata_is_the_directory_and_the_readers_words(self):
        PS.save_project(self.root, self.project, {
            "objective": "Ship it well.",
            "description": "The workspace and everything under it.",
            "sources": ["acme/myrepo", "https://example.com/spec"]})
        self.save()
        section = self.written()["project"]
        self.assertEqual(str(self.project), section["cwd"])
        self.assertEqual("myrepo", section["name"])
        self.assertEqual("Ship it well.", section["objective"])
        self.assertEqual("The workspace and everything under it.",
                         section["description"])
        self.assertEqual([{"id": "s1", "type": "github", "label": "acme/myrepo"},
                          {"id": "s2", "type": "doc",
                           "label": "https://example.com/spec"}],
                         section["sources"])

    def test_the_description_falls_back_to_the_objective(self):
        PS.save_project(self.root, self.project, {"objective": "Ship it well."})
        self.save()
        self.assertEqual("Ship it well.", self.written()["project"]["description"])

    def test_the_readers_lines_and_the_goals_never_erase_each_other(self):
        self.save()
        PS.save_project(self.root, self.project, {"objective": "Ship it well."})
        record = self.written()
        self.assertEqual("Ship it well.", record["project"]["objective"])
        self.assertEqual(4, len(record["goals"]),
                         "writing the objective keeps the goals")
        self.save()
        self.assertEqual("Ship it well.", self.written()["project"]["objective"],
                         "regenerating the goals keeps the objective")

    def test_the_first_flat_record_is_read_as_the_projects_section(self):
        # The file was first written as {"cwd": ..., "objective": ...}.
        path = PS.project_path(self.root, self.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cwd": str(self.project),
                                    "objective": "Written before the goals."}))
        self.assertEqual({"objective": "Written before the goals."},
                         PS.load_project(self.root, self.project))
        self.save()
        self.assertEqual("Written before the goals.",
                         self.written()["project"]["objective"])

    def test_a_broken_file_costs_nothing(self):
        path = PS.project_path(self.root, self.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual({}, PS.load_project(self.root, self.project))
        self.save()
        self.assertEqual(4, len(self.written()["goals"]))


class ProjectMetaOpTests(ProjectFixture):
    """The workspace's way to write the project's own metadata."""

    def test_the_workspace_writes_the_description_and_the_sources(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {
                "op": "set_project_meta",
                "description": "  The workspace and everything under it.  ",
                "sources": [{"type": "doc", "label": "https://example.com/spec"}]})
            self.assertTrue(out["ok"], out)
            self.assertEqual("The workspace and everything under it.",
                             out["description"])
            self.assertEqual(["https://example.com/spec"],
                             [s["label"] for s in out["sources"]])
            # The objective is written by its own op and is not disturbed.
            self.assertTrue(post_json(url + "/api/op", {
                "op": "set_project_objective", "objective": "Ship it well."})["ok"])
            self.assertEqual("Ship it well.",
                             get_json(url + "/api/state")["project"]["objective"])
        section = self.written()["project"]
        self.assertEqual("Ship it well.", section["objective"])
        self.assertEqual("The workspace and everything under it.",
                         section["description"])
        self.assertEqual(1, len(section["sources"]))

    def test_the_op_refuses_what_is_not_text_or_a_list(self):
        with server_for(self.trajdir) as url:
            for bad in ({"description": ["no"]}, {"sources": "no"}):
                out = post_json(url + "/api/op", dict(bad, op="set_project_meta"))
                self.assertFalse(out["ok"], bad)

    def test_a_chat_without_a_directory_has_nothing_to_write_to(self):
        chat_state.paths(self.session, self.root).manifest.write_text("{}")
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "set_project_meta",
                                              "description": "x"})
        self.assertFalse(out["ok"])


class ProjectJsonRouteTests(ProjectFixture):
    """The route behind the overview's project.json pane: the file itself,
    read out of the vault base rather than out of the project directory."""

    def test_the_route_hands_back_the_file_as_it_was_written(self):
        self.save()
        path = PS.project_path(self.root, self.project)
        with server_for(self.trajdir) as url:
            out = get_json(url + "/api/project.json")
        self.assertTrue(out["ok"], out)
        self.assertEqual(str(path), out["path"])
        self.assertTrue(out["written"])
        self.assertFalse(out["truncated"])
        # Byte for byte: what a reader opening the file would see.
        self.assertEqual(path.read_text(encoding="utf-8"), out["text"])
        self.assertEqual(self.written(), json.loads(out["text"]))

    def test_before_any_save_it_builds_the_record_the_file_would_hold(self):
        with server_for(self.trajdir) as url:
            out = get_json(url + "/api/project.json")
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["written"])
        self.assertFalse(PS.project_path(self.root, self.project).exists(),
                         "reading the record does not write it")
        self.assertEqual(4, len(json.loads(out["text"])["goals"]))

    def test_a_chat_without_a_directory_has_no_record_to_read(self):
        chat_state.paths(self.session, self.root).manifest.write_text("{}")
        with server_for(self.trajdir) as url:
            out = get_json(url + "/api/project.json")
        self.assertFalse(out["ok"])

    def test_a_record_past_the_pane_limit_says_it_was_cut(self):
        self.save()
        path = PS.project_path(self.root, self.project)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["goals"][0]["notes"] = "x" * (ui.PROJECT_FILE_LIMIT + 100)
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        with server_for(self.trajdir) as url:
            out = get_json(url + "/api/project.json")
        self.assertTrue(out["ok"])
        self.assertTrue(out["truncated"])
        self.assertEqual(ui.PROJECT_FILE_LIMIT, len(out["text"]))


if __name__ == "__main__":
    unittest.main()
