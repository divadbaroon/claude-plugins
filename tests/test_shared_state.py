"""Two people's goals, in the shape the workspace already draws.

The point of this mapping is that a shared workspace needs no second
renderer: if the payload matches what goals.json produces, the existing
tree draws it. These tests are about the places where two trees meeting
is different from one -- colliding local ids, whose goal is whose, and
what a reader is allowed to touch.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import shared_state as SS  # noqa: E402

from test_goal_ui_bridge import NODE, BridgeTestCase  # noqa: E402
from test_project_ui import PRELUDE  # noqa: E402

ME = "11111111-1111-1111-1111-111111111111"
THEM = "22222222-2222-2222-2222-222222222222"


def rows():
    return {
        "projects": [{"id": "p1", "cwd": "/work/repo", "name": "repo",
                      "objective": "ship it", "description": "",
                      "generated_at": "2026-08-21T00:00:00+00:00"}],
        "chats": [],
        "goals": [
            {"id": "u-mine", "user_id": ME, "session_id": "chat-a",
             "local_id": "g1", "parent_id": None, "title": "Mine",
             "status": "active", "priority": "normal", "origin": "user",
             "description": "", "notes": "my notes", "prompt": "do it",
             "evidence_ids": [], "updated_at": "2026-08-21T02:00:00+00:00"},
            {"id": "u-kid", "user_id": ME, "session_id": "chat-a",
             "local_id": "g2", "parent_id": "u-mine", "title": "My child",
             "status": "completed", "priority": "normal", "origin": "user",
             "description": "", "notes": "", "prompt": "",
             "evidence_ids": [], "updated_at": "2026-08-21T01:00:00+00:00"},
            # Same local id as mine: two trees each have a g1.
            {"id": "u-theirs", "user_id": THEM, "session_id": "chat-b",
             "local_id": "g1", "parent_id": None, "title": "Theirs",
             "status": "active", "priority": "normal", "origin": "user",
             "description": "", "notes": "", "prompt": "",
             "evidence_ids": [], "updated_at": "2026-08-21T03:00:00+00:00"},
        ],
        "todos": [
            {"id": "t2", "user_id": ME, "goal_id": "u-mine",
             "local_id": "trow0002", "position": 1, "depth": 1,
             "text": "second", "status": "done", "question": ""},
            {"id": "t1", "user_id": ME, "goal_id": "u-mine",
             "local_id": "trow0001", "position": 0, "depth": 0,
             "text": "first", "status": "", "question": ""},
        ],
        "goal_sources": [{"id": "s1", "user_id": ME, "goal_id": "u-mine",
                          "local_id": "s1", "type": "url", "label": "spec",
                          "position": 0}],
        "related_prompts": [{"id": "r1", "user_id": ME, "goal_id": "u-mine",
                             "prompt_id": "p1", "text": "the ask",
                             "session_id": "chat-a", "auto": True,
                             "created_at": None, "position": 0}],
    }


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.payload = SS.build(rows(), ME, {THEM: "hudson@example.com"})

    def test_it_matches_the_shape_a_local_goal_has(self):
        # The browser reads one shape in every scope; a key the local tree
        # has and this one lacks is a key the page will read as undefined.
        local = set(GM.new_goal("g1", "t", origin="user"))
        for goal in self.payload["goals"]:
            missing = local - set(goal)
            self.assertEqual(set(), missing, f"missing {missing}")

    def test_it_presents_as_the_workspace_the_reader_knows(self):
        # Every modern control in bridge.js is gated on scope === "chat";
        # any other value falls through to the old vault page. A shared
        # project says "chat" and carries its own marker beside it.
        self.assertEqual("chat", self.payload["scope"])
        self.assertTrue(self.payload["shared"]["readonly"])

    def test_a_contributor_is_not_told_the_whole_thing_is_read_only(self):
        payload = SS.build(rows(), ME, {}, can_write=True)
        self.assertFalse(payload["shared"]["readonly"])
        self.assertTrue(payload["shared"]["can_write"])
        # Narrower per goal: their own rows, never anyone else's.
        by_id = {g["id"]: g for g in payload["goals"]}
        self.assertFalse(by_id["u-mine"]["shared_readonly"])
        self.assertTrue(by_id["u-theirs"]["shared_readonly"])
        self.assertEqual("p1", self.payload["shared"]["project_id"])

    def test_colliding_local_ids_do_not_collide(self):
        ids = [g["id"] for g in self.payload["goals"]]
        self.assertEqual(len(ids), len(set(ids)))
        locals_ = sorted(g["shared_local_id"] for g in self.payload["goals"])
        self.assertEqual(["g1", "g1", "g2"], locals_)

    def test_the_tree_survives_the_trip(self):
        by_id = {g["id"]: g for g in self.payload["goals"]}
        self.assertEqual("u-mine", by_id["u-kid"]["parent_goal_id"])
        self.assertIsNone(by_id["u-mine"]["parent_goal_id"])

    def test_whose_goal_is_whose(self):
        by_id = {g["id"]: g for g in self.payload["goals"]}
        self.assertTrue(by_id["u-mine"]["shared_mine"])
        self.assertFalse(by_id["u-mine"]["shared_readonly"])
        self.assertFalse(by_id["u-theirs"]["shared_mine"])
        self.assertTrue(by_id["u-theirs"]["shared_readonly"])

    def test_everyone_is_named_including_the_reader(self):
        # An untagged row in a merged tree is ambiguous between "mine" and
        # "nobody knows", so every row says who wrote it.
        for goal in self.payload["goals"]:
            self.assertTrue(goal["shared_author"], goal["title"])

    def test_a_chosen_name_is_used_over_an_email(self):
        payload = SS.build(rows(), ME, {ME: "David", THEM: "Hudson"})
        by_id = {g["id"]: g for g in payload["goals"]}
        self.assertEqual("David", by_id["u-mine"]["shared_author"])
        self.assertEqual("Hudson", by_id["u-theirs"]["shared_author"])

    def test_an_email_is_only_a_fallback(self):
        # "dbarron410" is not what anyone calls him, which is why the name
        # is asked for -- but it beats nothing.
        payload = SS.build(rows(), ME, {THEM: "hudson@example.com"})
        theirs = next(g for g in payload["goals"] if g["id"] == "u-theirs")
        self.assertEqual("hudson", theirs["shared_author"])

    def test_a_contributor_with_no_name_is_still_named(self):
        payload = SS.build(rows(), ME, {})
        theirs = next(g for g in payload["goals"] if not g["shared_mine"])
        self.assertTrue(theirs["shared_author"])
        self.assertIn("contributor", theirs["shared_author"])

    def test_todo_rows_keep_the_order_the_rail_needs(self):
        mine = next(g for g in self.payload["goals"] if g["id"] == "u-mine")
        self.assertEqual(["first", "second"],
                         [t["text"] for t in mine["todo_items"]])
        self.assertEqual([0, 1], [t["depth"] for t in mine["todo_items"]])
        self.assertEqual("trow0001", mine["todo_items"][0]["id"])

    def test_sources_and_prompts_come_across(self):
        mine = next(g for g in self.payload["goals"] if g["id"] == "u-mine")
        self.assertEqual([{"id": "s1", "type": "url", "label": "spec"}],
                         mine["sources"])
        self.assertEqual(["p1"], mine["prompt_ids"])
        self.assertEqual(["p1"], mine["auto_prompt_ids"])
        self.assertEqual(1, len(self.payload["prompts"]))
        self.assertEqual("user", self.payload["prompts"][0]["role"])

    def test_the_reader_is_told_who_else_is_here(self):
        shared = self.payload["shared"]
        self.assertEqual(2, shared["mine"])
        self.assertEqual([{"user_id": THEM, "name": "hudson", "goals": 1}],
                         shared["contributors"])

    def test_an_empty_project_still_draws(self):
        payload = SS.build({}, None, {})
        self.assertEqual([], payload["goals"])
        self.assertEqual("chat", payload["scope"])
        self.assertEqual("shared project", payload["project"]["name"])


SHARED_STATE = ("P.acceptState({scope:'chat', goals:[], items:[], prompts:[],"
                " generated_at:'', shared:{readonly:false, can_write:true,"
                " contributors:[], me:'u1'}});")


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SaveRefusedTests(BridgeTestCase):
    """What a shared save says when it would not take an edit.

    The tree snaps back to whatever the server holds, so the reader has to
    be told why -- otherwise their typing simply vanishes and the page
    looks broken rather than governed.
    """

    def said(self, result):
        return json.loads(self.run_js(
            PRELUDE + "var P = window.__hcPromptUI;" + SHARED_STATE
            + "JSON.stringify(P.reportSharedSave(%s));" % json.dumps(result)))

    def test_someone_elses_goal_is_named_and_so_are_they(self):
        said = self.said({"ok": False, "conflicts": [], "refused": [
            {"id": "g1", "title": "read the shared tree", "author": "Hudson"}]})
        self.assertIn("Hudson", said)
        self.assertIn("read the shared tree", said)
        self.assertIn("not yours to change", said)

    def test_a_conflict_says_the_edit_was_not_saved(self):
        said = self.said({"ok": False, "refused": [], "conflicts": [
            {"id": "g2", "title": "Fix the goal UI"}]})
        self.assertIn("Fix the goal UI", said)
        self.assertIn("not saved", said)

    def test_several_are_counted_rather_than_listed(self):
        said = self.said({"ok": False, "conflicts": [], "refused": [
            {"id": "a", "title": "one", "author": "Hudson"},
            {"id": "b", "title": "two", "author": "Hudson"}]})
        self.assertIn("2 goals", said)

    def test_a_clean_save_says_nothing(self):
        self.assertEqual("", self.said({"ok": True, "refused": [],
                                        "conflicts": []}))

    def test_a_personal_workspace_is_never_told_any_of_this(self):
        # No shared block, so the reporter must not engage at all.
        said = json.loads(self.run_js(
            PRELUDE + "var P = window.__hcPromptUI;"
            "P.acceptState({scope:'chat', goals:[], items:[], prompts:[],"
            " generated_at:''});"
            "JSON.stringify(P.reportSharedSave({ok:false, refused:["
            "{id:'g1', title:'t', author:'Hudson'}], conflicts:[]}));"))
        self.assertEqual("", said)


if __name__ == "__main__":
    unittest.main()
