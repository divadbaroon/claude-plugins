"""Where a project lives, and where its work happens.

A project made from a name used to get a folder inside the vault, beside
the records -- fine for somewhere to keep an objective, wrong for anything
anybody will open an editor on. And every build ran in the directory of
the chat it was started from, so work on another project's goals landed in
this project's repository.

Both are answered by the same idea: a project has a directory the reader
chose, and a goal remembers which project it is for.
"""
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import build as BUILD  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402


def tree(*goals):
    doc = {"version": 1, "goals": list(goals)}
    GM.sanitize(doc)
    return doc


class CreateUnderTests(unittest.TestCase):
    """A folder made where the reader pointed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        self.home = (Path(self.tmp.name) / "Projects")
        self.home.mkdir(parents=True)
        self.home = self.home.resolve()

    def test_the_folder_is_made_in_the_chosen_parent_and_named_for_the_project(self):
        where = PS.create_under(self.root, self.home, "Engelbart Visualization")
        self.assertEqual(str(self.home / "engelbart-visualization"), where)
        self.assertTrue(Path(where).is_dir())
        # The name is kept as it was typed, not as the folder spells it.
        self.assertEqual("Engelbart Visualization",
                         PS.load_project(self.root, where).get("name"))

    def test_it_is_the_project_the_switcher_then_lists(self):
        where = PS.create_under(self.root, self.home, "Engelbart")
        self.assertEqual([where], [r["cwd"] for r in PS.list_projects(self.root)])

    def test_a_parent_that_is_not_there_makes_nothing(self):
        # A picked parent is not a typo, but it can have gone away between
        # the picking and the making.
        self.assertIsNone(
            PS.create_under(self.root, self.home / "gone", "Thing"))

    def test_a_parent_that_is_a_file_makes_nothing(self):
        seat = self.home / "notafolder"
        seat.write_text("x")
        self.assertIsNone(PS.create_under(self.root, seat, "Thing"))

    def test_a_name_with_nothing_to_slug_makes_nothing(self):
        self.assertIsNone(PS.create_under(self.root, self.home, "   ///  "))

    def test_making_it_twice_is_the_same_folder(self):
        first = PS.create_under(self.root, self.home, "Engelbart")
        second = PS.create_under(self.root, self.home, "Engelbart")
        self.assertEqual(first, second)


class NewProjectParentTests(unittest.TestCase):
    """The op the switcher posts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        self.home = (Path(self.tmp.name) / "Projects")
        self.home.mkdir(parents=True)
        self.home = self.home.resolve()

    def test_a_name_and_a_parent_put_the_project_where_it_was_asked_for(self):
        out = ui.new_project("Engelbart", root=self.root, parent=str(self.home))
        self.assertTrue(out["ok"], out)
        self.assertEqual(str(self.home / "engelbart"), out["cwd"])
        self.assertTrue(Path(out["cwd"]).is_dir())

    def test_a_name_alone_still_goes_to_the_vault(self):
        # Nobody said where, so it goes where a project goes when nobody
        # says: beside the records, which is somewhere rather than nowhere.
        out = ui.new_project("Engelbart", root=self.root)
        self.assertTrue(out["ok"], out)
        self.assertIn("workspaces", out["cwd"])

    def test_a_parent_that_is_not_there_is_reported_not_invented(self):
        out = ui.new_project("Engelbart", root=self.root,
                             parent=str(self.home / "gone"))
        self.assertFalse(out["ok"])
        self.assertIn("could not make", out["error"])

    def test_a_typed_path_still_adopts_the_directory_it_names(self):
        # The other half of the picker's old job: a repository already on
        # disk becomes the project it is, and must already exist.
        here = self.home / "already-here"
        here.mkdir()
        out = ui.new_project(str(here), root=self.root)
        self.assertTrue(out["ok"], out)
        self.assertEqual(str(here.resolve()), out["cwd"])


class GoalDirectoryTests(unittest.TestCase):
    """Which directory a goal's work belongs in."""

    def test_a_goal_carries_the_project_it_was_made_under(self):
        doc = tree(GM.new_goal("g1", "Draw it", project_cwd="/tmp/engelbart"))
        self.assertEqual("/tmp/engelbart", BUILD._goal_cwd(doc, "g1"))

    def test_a_subgoal_inherits_it_from_above(self):
        doc = tree(GM.new_goal("g1", "Draw it", project_cwd="/tmp/engelbart"),
                   GM.new_goal("g1a", "Axes", "g1"),
                   GM.new_goal("g1b", "Ticks", "g1a"))
        self.assertEqual("/tmp/engelbart", BUILD._goal_cwd(doc, "g1b"))

    def test_an_ordinary_goal_names_nowhere(self):
        # Empty is the common case and must stay empty: it means "the
        # project this chat was started in", so a chat that moves still
        # builds where it now lives.
        doc = tree(GM.new_goal("g1", "Fix the rail"))
        self.assertEqual("", BUILD._goal_cwd(doc, "g1"))

    def test_a_goal_that_is_not_there_names_nowhere(self):
        self.assertEqual("", BUILD._goal_cwd(tree(), "nope"))

    def test_a_parent_loop_does_not_hang(self):
        doc = {"goals": [GM.new_goal("a", "a", "b"), GM.new_goal("b", "b", "a")]}
        self.assertEqual("", BUILD._goal_cwd(doc, "a"))

    def test_sanitize_keeps_the_field_and_bounds_it(self):
        doc = tree(GM.new_goal("g1", "x", project_cwd="/p" * 900),
                   GM.new_goal("g2", "y", project_cwd=None))
        self.assertEqual(1000, len(doc["goals"][0]["project_cwd"]))
        self.assertEqual("", doc["goals"][1]["project_cwd"])


class BuildCwdTests(unittest.TestCase):
    """Where the build actually runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = Path(self.tmp.name) / "engelbart"
        self.here.mkdir()

    def test_the_goals_project_wins_over_the_chats_directory(self):
        doc = tree(GM.new_goal("g1", "Draw it", project_cwd=str(self.here)))
        got = BUILD._cwd_for("chat-x", None, doc, "g1")
        self.assertEqual(str(self.here), got)

    def test_a_goal_that_names_nowhere_falls_back_to_the_chat(self):
        doc = tree(GM.new_goal("g1", "Fix the rail"))
        with mock.patch.object(
                BUILD.CS, "load_manifest", return_value={"cwd": self.tmp.name}):
            self.assertEqual(self.tmp.name,
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_a_project_directory_that_has_gone_is_not_used(self):
        # A build in a path that no longer exists fails in a way nobody can
        # read; the chat's own directory is at least somewhere.
        doc = tree(GM.new_goal("g1", "x", project_cwd=str(self.here / "gone")))
        with mock.patch.object(
                BUILD.CS, "load_manifest", return_value={"cwd": self.tmp.name}):
            self.assertEqual(self.tmp.name,
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_the_bound_project_wins_over_the_chats_directory(self):
        # A chat is bound to a project; the directory it happened to start
        # in is only where it was opened. Every checkout of a repository is
        # one project, so the project's home is the one answer that does not
        # change when the reader opens the same work from a second worktree.
        doc = tree(GM.new_goal("g1", "Fix the rail"))
        with mock.patch.object(
                BUILD.CS, "load_manifest",
                return_value={"cwd": self.tmp.name,
                              "project_home": str(self.here)}):
            self.assertEqual(str(self.here),
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_a_goal_that_names_a_project_still_outranks_the_binding(self):
        # The goal is the narrower statement: a goal made under another
        # project says so on itself, and that is the whole point of saying it.
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        doc = tree(GM.new_goal("g1", "Draw it", project_cwd=str(other)))
        with mock.patch.object(
                BUILD.CS, "load_manifest",
                return_value={"cwd": self.tmp.name,
                              "project_home": str(self.here)}):
            self.assertEqual(str(other),
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_a_bound_project_that_has_gone_falls_back_to_the_chat(self):
        # Same reason the goal's own directory is checked: a build in a path
        # that no longer exists fails in a way nobody can read.
        doc = tree(GM.new_goal("g1", "Fix the rail"))
        with mock.patch.object(
                BUILD.CS, "load_manifest",
                return_value={"cwd": self.tmp.name,
                              "project_home": str(self.here / "gone")}):
            self.assertEqual(self.tmp.name,
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_the_checkout_the_project_chose_wins_over_its_home(self):
        # The whole point of choosing one: the project is the repository,
        # and the reader says which of its checkouts the builds run in so
        # that Engelbart and their own Claude Code sit on one branch.
        doc = tree(GM.new_goal("g1", "Fix the rail"))
        with mock.patch.object(
                BUILD.CS, "load_manifest",
                return_value={"cwd": self.tmp.name,
                              "project_home": self.tmp.name}), \
             mock.patch.object(
                BUILD.PS, "load_project",
                return_value={"working_dir": str(self.here)}):
            self.assertEqual(str(self.here),
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_with_no_checkout_chosen_the_project_home_still_answers(self):
        doc = tree(GM.new_goal("g1", "Fix the rail"))
        with mock.patch.object(
                BUILD.CS, "load_manifest",
                return_value={"cwd": self.tmp.name,
                              "project_home": str(self.here)}), \
             mock.patch.object(
                BUILD.PS, "load_project", return_value={}):
            self.assertEqual(str(self.here),
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_an_unbound_chat_is_the_chat_as_before(self):
        doc = tree(GM.new_goal("g1", "Fix the rail"))
        with mock.patch.object(
                BUILD.CS, "load_manifest", return_value={"cwd": self.tmp.name}):
            self.assertEqual(self.tmp.name,
                             BUILD._cwd_for("chat-x", None, doc, "g1"))

    def test_called_without_a_goal_it_is_the_chat_as_before(self):
        with mock.patch.object(
                BUILD.CS, "load_manifest", return_value={"cwd": self.tmp.name}):
            self.assertEqual(self.tmp.name, BUILD._cwd_for("chat-x", None))


class WorktreeChoiceTests(unittest.TestCase):
    """Which checkout of the repository the project builds in.

    A project is its repository, not one directory of it: repo_home folds
    every worktree onto the main one so the goal tree stays whole. But the
    reader runs Claude Code in whichever checkout holds the branch they are
    working, and a build that lands in a different checkout writes onto a
    different branch. The project therefore names one working directory,
    and it may be any checkout of the same repository.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.repo / "a.txt").write_text("a\n")
        self._git("add", "a.txt")
        self._git("commit", "-m", "one")
        self.tree = self.root / "side"
        self._git("worktree", "add", str(self.tree), "-b", "side")

    def _git(self, *args):
        subprocess.run(("git",) + args, cwd=self.repo, check=True,
                       capture_output=True, text=True)

    def test_every_checkout_is_offered_with_the_branch_it_holds(self):
        # The reader picks by branch, not by path: the path is where the
        # checkout sits and the branch is what they are working on.
        found = PS.worktrees(self.repo)
        by_path = {w["path"]: w for w in found}
        self.assertEqual({PS._resolved(self.repo), PS._resolved(self.tree)},
                         set(by_path))
        self.assertEqual("main", by_path[PS._resolved(self.repo)]["branch"])
        self.assertEqual("side", by_path[PS._resolved(self.tree)]["branch"])
        self.assertTrue(by_path[PS._resolved(self.repo)]["main"])
        self.assertFalse(by_path[PS._resolved(self.tree)]["main"])

    def test_a_directory_git_never_heard_of_offers_only_itself(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual([PS._resolved(plain)],
                         [w["path"] for w in PS.worktrees(plain)])

    def test_the_project_keeps_the_checkout_the_reader_chose(self):
        PS.save_project(self.root, self.repo,
                        {"name": "Repo", "working_dir": str(self.tree)})
        self.assertEqual(PS._resolved(self.tree),
                         PS.load_project(self.root, self.repo)["working_dir"])

    def test_a_later_unrelated_edit_does_not_drop_the_choice(self):
        # _project_section rebuilds from a whitelist on every write, so a
        # field merely present in what was read is lost by the next edit.
        PS.save_project(self.root, self.repo, {"working_dir": str(self.tree)})
        held = PS.load_project(self.root, self.repo)
        held["objective"] = "ship it"
        PS.save_project(self.root, self.repo, held)
        self.assertEqual(PS._resolved(self.tree),
                         PS.load_project(self.root, self.repo)["working_dir"])

    def test_the_store_holding_the_tree_survives_a_regeneration(self):
        # _project_section writes tree_session, but load_project did not read
        # it back, so build() -- the regeneration every goal save triggers --
        # rebuilt the record without it and the project forgot where its
        # goals were. The comment beside the key had warned about exactly
        # this: a field written on one side of the whitelist and not the
        # other is dropped by the next unrelated write.
        PS.save_project(self.root, self.repo, {"name": "Repo"})
        PS.set_tree_session(self.root, self.repo, "hcws-abc123")
        PS.write(self.root, self.repo)
        self.assertEqual("hcws-abc123",
                         PS.tree_session(self.root, self.repo))

    def test_a_checkout_of_another_repository_is_refused(self):
        # The choice decides where a build writes. A path from elsewhere is
        # not a view of this project and is not taken on its word.
        other = self.root / "other"
        other.mkdir()
        subprocess.run(("git", "init"), cwd=other, check=True,
                       capture_output=True, text=True)
        PS.save_project(self.root, self.repo, {"working_dir": str(other)})
        self.assertEqual("", PS.load_project(self.root, self.repo)
                         .get("working_dir", ""))

    def test_a_checkout_that_has_gone_away_is_refused(self):
        PS.save_project(self.root, self.repo,
                        {"working_dir": str(self.root / "never")})
        self.assertEqual("", PS.load_project(self.root, self.repo)
                         .get("working_dir", ""))

    def test_choosing_nowhere_returns_the_project_to_its_own_home(self):
        PS.save_project(self.root, self.repo, {"working_dir": str(self.tree)})
        PS.save_project(self.root, self.repo, {"working_dir": ""})
        self.assertEqual("", PS.load_project(self.root, self.repo)
                         .get("working_dir", ""))


class ImportKeepsTheProjectTests(unittest.TestCase):
    """The whitelist hazard, for the third time.

    The browser posts the whole tree back on every edit, and _import
    rebuilds each goal from a fixed list of fields. A field missing from
    that list is not merely unsaved -- it is erased by the next thing the
    reader types. relevance was lost this way once already.
    """

    def test_project_cwd_is_among_the_fields_carried_from_the_previous_goal(self):
        source = (ROOT / "hc" / "src" / "human_compact" / "trajectory"
                  / "ui.py").read_text()
        self.assertIn('"project_cwd": prev.get("project_cwd", "")', source)


if __name__ == "__main__":
    unittest.main()


class OpenProjectTests(unittest.TestCase):
    """Clicking a project takes you to it.

    A project nobody has worked in has no chat, and a workspace serves one
    chat's goals -- so this used to refuse and say "run claude there",
    which made creating a project a dead end: you clicked the thing you had
    just made and were told to go somewhere else and do something first.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        self.chat = self.root / "chat-here"
        self.chat.mkdir(parents=True)
        (self.chat / "manifest.json").write_text(json.dumps(
            {"session_id": "chat-here", "cwd": self.tmp.name}))
        self.work = Path(self.tmp.name) / "Projects" / "engelbart"
        self.work.mkdir(parents=True)
        self.served = []

        def serve(session_id, root):
            self.served.append(session_id)
            return {"url": "http://127.0.0.1:8871/", "thread": None}

        patch = mock.patch.object(ui, "_serve_session", serve)
        patch.start()
        self.addCleanup(patch.stop)
        # Each test gets its own registry: a server held from a previous one
        # would be handed back instead of a decision being made.
        held = mock.patch.dict(ui._PROJECT_SERVERS, {}, clear=True)
        held.start()
        self.addCleanup(held.stop)

    def test_a_project_with_no_chat_gets_one_and_opens(self):
        out = ui.open_project(str(self.work), self.chat)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["fresh"])
        self.assertEqual([out["session_id"]], self.served)
        # And the session it made is a chat of that directory from now on.
        self.assertEqual([out["session_id"]],
                         PS.project_sessions(self.root, str(self.work)))

    def test_the_second_click_opens_the_one_it_made_rather_than_another(self):
        first = ui.open_project(str(self.work), self.chat)
        ui._PROJECT_SERVERS.clear()
        second = ui.open_project(str(self.work), self.chat)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertFalse(second["fresh"])

    def test_a_directory_that_has_gone_is_reported_not_recreated(self):
        out = ui.open_project(str(self.work / "gone"), self.chat)
        self.assertFalse(out["ok"])
        self.assertIn("not there any more", out["error"])
        self.assertFalse((self.work / "gone").exists())
        self.assertEqual([], self.served)

    def test_a_project_that_already_has_a_chat_opens_that_one(self):
        theirs = self.root / "chat-theirs"
        theirs.mkdir(parents=True)
        (theirs / "manifest.json").write_text(json.dumps(
            {"session_id": "chat-theirs", "cwd": str(self.work)}))
        out = ui.open_project(str(self.work), self.chat)
        self.assertEqual("chat-theirs", out["session_id"])
        self.assertFalse(out["fresh"])
