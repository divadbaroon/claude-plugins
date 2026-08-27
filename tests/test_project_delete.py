"""Deleting a project, and the deletion staying done.

The projects screen used to offer to *forget* a project: its record went and
everything else stayed, so the goals were still there, the records other
checkouts of the same repository had written were still there, and the next
goal save wrote the record back. The reader who pressed it saw the card
return and reasonably called that a bug.

Delete means delete. Every record the repository has, the window it pointed
at, and the vault workspace of every chat in it -- the goals, the TODO rows,
the notes, the prompts. Nothing outside the vault: the directory a project
names is the reader's repository, not this tool's to remove.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_project_json import ProjectFixture, server_for  # noqa: E402
from test_chat_ui_server import get_json, post_json  # noqa: E402


class DeleteFixture(ProjectFixture):
    """Two chats in the project, one in another, and one record each."""

    def setUp(self):
        super().setUp()
        self.save()                       # the project's record, as written
        self.save(self.away)

    def seat(self, session):
        return chat_state.paths(session, self.root).session_dir


class DeleteProjectTests(DeleteFixture):
    """What goes, and what is left standing."""

    def test_the_record_and_every_chats_workspace_go(self):
        gone = PS.delete_project(self.root, self.project)
        self.assertTrue(gone)
        self.assertFalse(PS.project_path(self.root, self.project).exists())
        self.assertFalse(self.seat(self.session).exists())
        self.assertFalse(self.seat(self.twin).exists())
        self.assertEqual(2, gone["chats"])
        # Three goals in the main chat, one in the twin.
        self.assertEqual(4, gone["goals"])

    def test_a_chat_in_another_project_is_left_alone(self):
        PS.delete_project(self.root, self.project)
        self.assertTrue(self.seat(self.away).exists())
        self.assertTrue(PS.project_path(self.root, self.other).exists())

    def test_the_directory_itself_is_not_touched(self):
        (self.project / "main.py").write_text("print('mine')\n")
        PS.delete_project(self.root, self.project)
        self.assertTrue(self.project.is_dir())
        self.assertEqual("print('mine')\n",
                         (self.project / "main.py").read_text())

    def test_a_record_another_checkout_left_behind_goes_too(self):
        """A vault written before worktrees were folded holds one file per
        checkout. Deleting only the current one left the switcher reading a
        sibling, which is the deletion that did not take."""
        checkout = self.root / "work" / "myrepo-wip"
        checkout.mkdir()
        with mock.patch.dict(PS._REPO_HOME_CACHE,
                             {PS._resolved(checkout): PS._resolved(self.project)}):
            PS.save_project(self.root, str(checkout), {"objective": "a twin"})
            stale = PS.project_path(self.root, str(checkout))
            self.assertTrue(stale.exists())
            PS.delete_project(self.root, self.project)
            self.assertFalse(stale.exists())
            self.assertEqual([], [row for row in PS.list_projects(self.root)
                                  if PS._resolved(row["cwd"])
                                  == PS._resolved(self.project)])

    def test_the_window_it_was_pointing_at_is_forgotten(self):
        PS.set_server_record(self.root, self.project, {"port": 8123})
        PS.delete_project(self.root, self.project)
        self.assertIsNone(PS.server_record(self.root, self.project))

    def test_a_project_with_nothing_left_to_delete_says_so(self):
        PS.delete_project(self.root, self.project)
        self.assertIsNone(PS.delete_project(self.root, self.project))

    def test_a_home_the_vault_made_for_a_name_goes_with_it(self):
        made = PS.create_named(self.root, "loose ends")
        self.assertTrue(Path(made).is_dir())
        self.assertTrue(PS.delete_project(self.root, made))
        self.assertFalse(Path(made).exists())


class DeletionStaysDoneTests(DeleteFixture):
    """The writers that used to put the record back."""

    def test_a_goal_save_does_not_write_the_record_back(self):
        PS.delete_project(self.root, self.other)
        self.assertTrue(PS.deleted(self.root, self.other))
        self.assertIsNone(PS.write(self.root, self.other))
        self.assertFalse(PS.project_path(self.root, self.other).exists())

    def test_the_switcher_stops_listing_it(self):
        PS.delete_project(self.root, self.other)
        listed = [PS._resolved(row["cwd"])
                  for row in PS.list_projects(self.root)]
        self.assertNotIn(PS._resolved(self.other), listed)

    def test_the_directory_this_window_sits_in_is_not_put_back_on_the_list(self):
        PS.delete_project(self.root, self.project)
        listed = [PS._resolved(row["cwd"])
                  for row in ui._all_projects(self.root, str(self.project))]
        self.assertNotIn(PS._resolved(self.project), listed)

    def test_bookkeeping_does_not_revive_it(self):
        PS.delete_project(self.root, self.other)
        PS.save_project(self.root, self.other, {"id": "x"}, revive=False)
        self.assertTrue(PS.deleted(self.root, self.other))

    def test_naming_it_again_brings_it_back(self):
        PS.delete_project(self.root, self.other)
        PS.save_project(self.root, self.other, {"objective": "a second life"})
        self.assertFalse(PS.deleted(self.root, self.other))
        listed = [PS._resolved(row["cwd"])
                  for row in PS.list_projects(self.root)]
        self.assertIn(PS._resolved(self.other), listed)


class DeleteRouteTests(DeleteFixture):
    """What the projects screen posts."""

    def test_the_route_deletes_the_project_and_frees_its_chats(self):
        manifest = json.loads(
            chat_state.paths(self.away, self.root).manifest.read_text())
        manifest["project_home"] = str(self.other)
        chat_state.paths(self.away, self.root).manifest.write_text(
            json.dumps(manifest))
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op",
                            {"op": "forget_project", "cwd": str(self.other)})
        self.assertTrue(out["ok"])
        self.assertEqual(1, out["freed"])
        self.assertEqual(1, out["chats"])
        self.assertFalse(PS.project_path(self.root, self.other).exists())
        self.assertFalse(
            chat_state.paths(self.away, self.root).session_dir.exists())

    def test_deleting_the_project_this_window_is_in_leaves_it_answering(self):
        """The reader can delete the project they are looking at. The window
        stays up -- with nothing on it, which is the truth -- rather than
        going down with the record it was serving."""
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op",
                            {"op": "forget_project", "cwd": str(self.project)})
            self.assertTrue(out["ok"])
            listed = get_json(url + "/api/projects")
            self.assertNotIn(PS._resolved(self.project),
                             [PS._resolved(row["cwd"])
                              for row in listed["projects"]])
            self.assertIn("goals", get_json(url + "/api/state"))

    def test_a_project_that_is_not_there_is_refused(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op",
                            {"op": "forget_project",
                             "cwd": str(self.root / "nowhere")})
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
