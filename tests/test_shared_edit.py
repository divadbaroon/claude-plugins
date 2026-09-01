"""One edit, in the two places the goal lives.

A goal edited from the shared workspace has a row in Postgres and a goal in
its author's own chat. These tests are about the seam: that both take the
edit, that a refusal upstream leaves the vault alone, and that a vault that
will not take it is said out loud rather than swallowed.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import shared_edit as SE  # noqa: E402
from human_compact.trajectory import supabase_client as SB  # noqa: E402


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-one"
        first = GM.new_goal("g1", "First", origin="user")
        second = GM.new_goal("g2", "Second", origin="user")
        first["notes"] = "before"
        goals = {"version": 1, "goals": [first, second]}
        GM.sanitize(goals)
        p = CS.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))

    def accepted(self, **over):
        out = {"ok": True, "updated_at": "2026-08-22T00:00:00+00:00",
               "session_id": self.session, "local_id": "g1"}
        out.update(over)
        return out

    def run_update(self, reply, fields=None, expect=None):
        with mock.patch.object(SB, "load_config",
                               lambda root=None: {"url": "https://x",
                                                  "anon_key": "k",
                                                  "email": ""}), \
             mock.patch.object(SB, "current_session",
                               lambda root=None: {"access_token": "t",
                                                  "user_id": "u"}), \
             mock.patch.object(SB, "_rpc", lambda *a, **k: reply):
            return SE.update_goal("row-1", expect,
                                  fields or {"notes": "after"},
                                  self.root)


class BothPlacesTests(EditTests):
    def test_an_accepted_edit_reaches_the_chats_own_copy(self):
        out = self.run_update(self.accepted())
        self.assertTrue(out["ok"])
        self.assertTrue(out["local_ok"])
        self.assertEqual("after",
                         SE.local_goal(self.session, "g1", self.root)["notes"])

    def test_only_the_goal_named_is_touched(self):
        self.run_update(self.accepted())
        self.assertEqual("", SE.local_goal(self.session, "g2",
                                           self.root)["notes"])
        self.assertEqual("Second", SE.local_goal(self.session, "g2",
                                                 self.root)["title"])

    def test_a_prompt_lands_in_the_field_the_vault_calls_it(self):
        # Postgres calls it `prompt`; goals.json calls it `prompt_md`. A
        # rename between the two is exactly where an edit goes missing.
        self.run_update(self.accepted(), fields={"prompt": "do the thing"})
        self.assertEqual("do the thing",
                         SE.local_goal(self.session, "g1",
                                       self.root)["prompt_md"])

    def test_a_status_the_tree_does_not_use_is_ignored(self):
        self.run_update(self.accepted(), fields={"status": "nonsense"})
        self.assertEqual("active",
                         SE.local_goal(self.session, "g1", self.root)["status"])


class RefusalTests(EditTests):
    def test_a_conflict_leaves_the_vault_alone(self):
        out = self.run_update({"ok": False, "conflict": True,
                               "error": "this goal changed while you were "
                                        "editing", "notes": "theirs"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["conflict"])
        # Postgres refused, so the local copy must not have moved either.
        self.assertEqual("before",
                         SE.local_goal(self.session, "g1", self.root)["notes"])

    def test_someone_elses_goal_is_refused_upstream(self):
        out = self.run_update({"ok": False,
                               "error": "that goal belongs to someone else"})
        self.assertFalse(out["ok"])
        self.assertEqual("before",
                         SE.local_goal(self.session, "g1", self.root)["notes"])

    def test_a_field_that_is_not_editable_here_is_not_sent(self):
        out = self.run_update(self.accepted(), fields={"parent_goal_id": "g2"})
        self.assertFalse(out["ok"])
        self.assertIn("nothing to change", out["error"])

    def test_a_vault_that_will_not_take_it_is_said_out_loud(self):
        # The shared copy moved and the local one did not. That is a state
        # worth surfacing: only the reader can decide what to do about it.
        out = self.run_update(self.accepted(local_id="gone"))
        self.assertTrue(out["ok"])
        self.assertFalse(out["local_ok"])
        self.assertIn("did not take it", out["warning"])


class TreeSaveTests(unittest.TestCase):
    """The artifact saves by posting the whole tree; a shared tree is two
    people's. Only what changed, and only what is the reader's, may land."""

    def served(self):
        return {"goals": [
            {"id": "row-mine", "title": "Mine", "notes": "before",
             "description": "", "prompt_md": "", "status": "active",
             "priority": "normal", "shared_readonly": False,
             "shared_author": "David",
             "updated_at": "2026-08-22T00:00:00+00:00"},
            {"id": "row-theirs", "title": "Theirs", "notes": "",
             "description": "", "prompt_md": "", "status": "active",
             "priority": "normal", "shared_readonly": True,
             "shared_author": "Hudson",
             "updated_at": "2026-08-22T00:00:00+00:00"}]}

    def node(self, gid, **over):
        n = {"id": gid, "title": "Mine" if gid == "row-mine" else "Theirs",
             "notes": "before" if gid == "row-mine" else "", "desc": "",
             "prompt_md": "", "done": False, "status": "todo",
             "prio": "normal", "children": []}
        n.update(over)
        return n

    def apply(self, tree, updates=None, rpcs=None, served=None):
        seen, calls = [], []

        def fake_update(gid, expect, fields, root=None, project_id=None):
            seen.append((gid, fields))
            return (updates or {}).get(gid, {"ok": True})

        def fake_rpc(name, body, root):
            calls.append((name, body))
            return (rpcs or {}).get(name, {"ok": True, "id": "row-new"})

        state = served or self.served()
        with mock.patch.object(SE.SB, "shared_payload", lambda pid, root=None, force=False: state), mock.patch.object(SE.SB, "forget_shared", lambda pid: None), mock.patch.object(SE, "_rpc", fake_rpc), mock.patch.object(SE, "update_goal", fake_update):
            out = SE.apply_tree("p1", tree)
        self.calls = calls
        return out, seen

    def test_only_what_changed_is_written(self):
        out, seen = self.apply([self.node("row-mine"),
                                self.node("row-theirs")])
        self.assertEqual([], seen)
        self.assertTrue(out["ok"])

    def test_a_changed_field_of_mine_is_written(self):
        out, seen = self.apply([self.node("row-mine", notes="after"),
                                self.node("row-theirs")])
        self.assertEqual([("row-mine", {"notes": "after"})], seen)
        self.assertEqual(["row-mine"], out["changed"])

    def test_someone_elses_row_is_refused_by_name_not_dropped(self):
        # The whole tree is posted, as the artifact posts it -- leaving a
        # goal out now means deleting it, which is a different test.
        out, seen = self.apply([self.node("row-mine"),
                                self.node("row-theirs", title="mine now")])
        self.assertEqual([], seen)
        self.assertFalse(out["ok"])
        self.assertEqual("Hudson", out["refused"][0]["author"])

    def test_the_artifacts_own_words_are_translated(self):
        # done/inprog and prio are the artifact's names for status and
        # priority; a mistranslation here means an edit meaning one thing
        # on the way out and another coming back.
        out, seen = self.apply([self.node("row-mine", done=True),
                                self.node("row-theirs")])
        self.assertEqual([("row-mine", {"status": "completed"})], seen)
        out, seen = self.apply([self.node("row-mine", status="inprog"),
                                self.node("row-theirs")])
        self.assertEqual([("row-mine", {"status": "in_progress"})], seen)

    def test_a_conflict_is_reported_rather_than_counted_as_saved(self):
        out, _ = self.apply([self.node("row-mine", notes="after"),
                             self.node("row-theirs")],
                            {"row-mine": {"ok": False, "conflict": True,
                                          "error": "changed while editing"}})
        self.assertTrue(out["conflict"])
        self.assertEqual([], out["changed"])

    def test_a_goal_the_server_never_served_is_created(self):
        # The artifact mints an id locally when the reader adds a row, so a
        # node nobody served is a new goal. It used to be skipped -- the
        # save reported success and wrote nothing.
        out, _ = self.apply([self.node("row-mine"), self.node("row-theirs"),
                             self.node("row-unknown", title="brand new")])
        made = [c for c in self.calls if c[0] == "hc_create_goal"]
        self.assertEqual(1, len(made))
        self.assertEqual("brand new", made[0][1]["p_title"])
        self.assertEqual([{"posted_id": "row-unknown", "id": "row-new"}],
                         out["created"])

    def test_an_untitled_new_row_is_not_created(self):
        # The rail always carries a blank row to type into.
        self.apply([self.node("row-mine"), self.node("row-theirs"),
                    self.node("row-unknown", title="   ")])
        self.assertEqual([], [c for c in self.calls
                              if c[0] == "hc_create_goal"])

    def test_a_move_of_someone_elses_goal_is_not_attempted(self):
        self.apply([self.node("row-mine",
                              children=[self.node("row-theirs")])])
        self.assertEqual([], [c for c in self.calls
                              if c[0] == "hc_move_goal"])

    def test_a_move_of_my_own_goal_is_sent(self):
        served = self.served()
        served["goals"][1]["shared_readonly"] = False
        self.apply([self.node("row-mine",
                              children=[self.node("row-theirs")])],
                   served=served)
        moves = [c for c in self.calls if c[0] == "hc_move_goal"]
        self.assertEqual(1, len(moves))
        self.assertEqual("row-mine", moves[0][1]["p_parent_id"])

    def test_a_changed_rail_is_replaced(self):
        node = self.node("row-mine")
        node["todo_items"] = [{"id": "r1", "text": "first", "depth": 0,
                               "status": "", "question": ""}]
        self.apply([node, self.node("row-theirs")])
        rails = [c for c in self.calls if c[0] == "hc_replace_todos"]
        self.assertEqual(1, len(rails))
        self.assertEqual("first", rails[0][1]["p_rows"][0]["text"])

    def test_a_goal_left_out_of_the_tree_is_tombstoned(self):
        # The artifact deletes by omission, and the local store answers by
        # marking the goal archived and keeping it. Same answer here.
        out, seen = self.apply([self.node("row-theirs")])
        self.assertEqual([("row-mine", {"status": "archived"})], seen)
        self.assertEqual(["row-mine"], out["removed"])

    def test_someone_elses_goal_is_never_tombstoned_by_omission(self):
        # Their rows are simply not in a tree this reader posts about.
        out, seen = self.apply([self.node("row-mine")])
        self.assertEqual([], seen)
        self.assertEqual([], out["removed"])

    def test_an_empty_tree_buries_nothing(self):
        # A page in a bad state posts no goals; that is not an instruction
        # to abandon the project.
        out, seen = self.apply([])
        self.assertEqual([], seen)
        self.assertEqual([], out["removed"])

    def test_an_already_archived_goal_is_not_re_tombstoned(self):
        served = self.served()
        served["goals"][0]["status"] = "archived"
        out, seen = self.apply([self.node("row-theirs")], served=served)
        self.assertEqual([], seen)

    def test_nor_one_a_stale_row_still_calls_abandoned(self):
        # Rows written before the rename are still up there.
        served = self.served()
        served["goals"][0]["status"] = "abandoned"
        out, seen = self.apply([self.node("row-theirs")], served=served)
        self.assertEqual([], seen)

    def test_an_unchanged_rail_is_left_alone(self):
        node = self.node("row-mine")
        node["todo_items"] = []
        self.apply([node, self.node("row-theirs")])
        self.assertEqual([], [c for c in self.calls
                              if c[0] == "hc_replace_todos"])


if __name__ == "__main__":
    unittest.main()
