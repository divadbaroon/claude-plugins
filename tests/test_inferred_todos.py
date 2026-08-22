"""Where an inferred next action goes, and what a goal stands on.

A goal's tasks belong on its own checklist, beside it -- not in the tree.
Inference used to make each one a child goal, so a tree of a dozen goals
grew forty leaves that were really a to-do list, and the rail beside them
stayed empty. These tests pin the two halves: nothing lands in the tree,
and everything lands on the rail.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_synth as CSY  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402


class RailTests(unittest.TestCase):
    def tree(self, **over):
        t = {"version": 1, "goals": []}
        op = {"op": "new_goal", "parent_goal_id": None, "title": "Ship it",
              "distinct_because": "x"}
        op.update(over)
        GM.apply_ops(t, {"items": []}, [op])
        return t

    def test_a_goals_todos_do_not_become_goals(self):
        t = self.tree(todos=[{"text": "write it"}, {"text": "run it"}])
        self.assertEqual(1, len(t["goals"]))
        self.assertEqual(["write it", "run it"],
                         [r["text"] for r in t["goals"][0]["todo_items"]])

    def test_a_done_todo_arrives_ticked(self):
        t = self.tree(todos=[{"text": "run it", "done": True}])
        self.assertEqual("done", t["goals"][0]["todo_items"][0]["status"])

    def test_add_todo_puts_a_row_on_the_list(self):
        t = self.tree()
        gid = t["goals"][0]["id"]
        GM.apply_ops(t, {"items": []},
                     [{"op": "add_todo", "goal_id": gid, "text": "verify it"}])
        self.assertEqual(1, len(t["goals"]))
        self.assertEqual("verify it", t["goals"][0]["todo_items"][0]["text"])

    def test_the_same_line_twice_is_one_row(self):
        t = self.tree()
        gid = t["goals"][0]["id"]
        for _ in range(3):
            GM.apply_ops(t, {"items": []},
                         [{"op": "add_todo", "goal_id": gid, "text": "verify"}])
        self.assertEqual(1, len(t["goals"][0]["todo_items"]))

    def test_completing_one_ticks_the_row(self):
        t = self.tree(todos=[{"text": "write the migration"}])
        gid = t["goals"][0]["id"]
        GM.apply_ops(t, {"items": []}, [
            {"op": "complete_todo", "goal_id": gid,
             "text_match": "write the migration"}])
        self.assertEqual("done", t["goals"][0]["todo_items"][0]["status"])

    def test_an_older_tree_with_todo_subgoals_still_closes(self):
        # Trees built before this change kept next actions as children;
        # completing one must still find them there.
        t = self.tree()
        gid = t["goals"][0]["id"]
        t["goals"].append(GM.new_goal(gid + ".1", "write the migration", gid))
        GM.apply_ops(t, {"items": []}, [
            {"op": "complete_todo", "goal_id": gid,
             "text_match": "write the migration"}])
        self.assertEqual("completed", t["goals"][1]["status"])

    def test_a_blank_line_is_not_a_row(self):
        t = self.tree(todos=[{"text": "   "}, {"text": ""}])
        self.assertEqual([], t["goals"][0]["todo_items"])


class DepthTests(unittest.TestCase):
    """Every goal has its own list, however deep it sits.

    Nothing in the code branches on depth, which is exactly why it is worth
    a test: a change that special-cases top-level goals would break this
    silently, and an empty rail looks like a model that had nothing to say.
    """

    def test_a_subgoal_gets_its_own_rows(self):
        t = {"version": 1, "goals": []}
        GM.apply_ops(t, {"items": []}, [{
            "op": "new_goal", "parent_goal_id": None, "title": "Parent",
            "distinct_because": "x"}])
        pid = t["goals"][0]["id"]
        GM.apply_ops(t, {"items": []}, [{
            "op": "new_goal", "parent_goal_id": pid, "title": "Subgoal",
            "todos": [{"text": "sub task"}]}])
        GM.sanitize(t)
        sub = next(g for g in t["goals"] if g["title"] == "Subgoal")
        self.assertEqual(["sub task"], [r["text"] for r in sub["todo_items"]])

    def test_add_todo_reaches_a_subgoal(self):
        t = {"version": 1, "goals": []}
        GM.apply_ops(t, {"items": []}, [{
            "op": "new_goal", "parent_goal_id": None, "title": "Parent",
            "distinct_because": "x"}])
        pid = t["goals"][0]["id"]
        GM.apply_ops(t, {"items": []}, [{
            "op": "new_goal", "parent_goal_id": pid, "title": "Subgoal"}])
        sid = next(g["id"] for g in t["goals"] if g["title"] == "Subgoal")
        GM.apply_ops(t, {"items": []},
                     [{"op": "add_todo", "goal_id": sid, "text": "later"}])
        sub = next(g for g in t["goals"] if g["id"] == sid)
        self.assertEqual(["later"], [r["text"] for r in sub["todo_items"]])

    def test_the_initial_pass_fills_every_generation(self):
        out = CSY._normalize_initial({"goals": [
            {"id": "g1", "title": "Parent", "todos": [{"text": "a"}]},
            {"id": "g2", "title": "Child", "parent_goal_id": "g1",
             "todos": [{"text": "b"}]},
            {"id": "g3", "title": "Grandchild", "parent_goal_id": "g2",
             "todos": [{"text": "c"}]}]}, set())
        GM.sanitize(out)
        rows = {g["title"]: [r["text"] for r in g["todo_items"]]
                for g in out["goals"]}
        self.assertEqual({"Parent": ["a"], "Child": ["b"], "Grandchild": ["c"]},
                         rows)


class InitialPassTests(unittest.TestCase):
    def test_the_first_pass_no_longer_drops_them(self):
        # They were absent from the field whitelist, so a chat's very first
        # analysis produced goals with empty rails.
        out = CSY._normalize_initial({"goals": [{
            "id": "g1", "title": "Ship it",
            "todos": [{"text": "write it"}, {"text": "run it", "done": True}]}]},
            set())
        GM.sanitize(out)
        self.assertEqual(1, len(out["goals"]))
        rows = out["goals"][0]["todo_items"]
        self.assertEqual(["write it", "run it"], [r["text"] for r in rows])
        self.assertEqual("done", rows[1]["status"])

    def test_nothing_is_left_for_promote_todos_to_expand(self):
        out = CSY._normalize_initial({"goals": [{
            "id": "g1", "title": "Ship it", "todos": [{"text": "write it"}]}]},
            set())
        self.assertEqual([], out["goals"][0]["todos"])
        GM.promote_todos(out)
        self.assertEqual(1, len(out["goals"]))

    def test_the_prompt_says_which_list_they_belong_on(self):
        # Matched on fragments that do not span a line wrap -- the prompt
        # is hard-wrapped, and a phrase crossing a newline never matches.
        self.assertIn("WHAT IS A GOAL AND WHAT IS A TODO", CSY.INITIAL_PROMPT)
        self.assertIn("never become goals of their own", CSY.INITIAL_PROMPT)
        self.assertIn("checklist, beside it", CSY.INCREMENTAL_PROMPT)


class SourceTests(unittest.TestCase):
    PROJECT = [{"id": "p1", "type": "url", "label": "the spec"},
               {"id": "p2", "type": "doc", "label": "the brief"}]

    def test_a_goal_stands_on_the_projects_sources(self):
        prop = {"goals": [{"id": "g1", "sources": []}]}
        CSY._inherit_sources(prop, self.PROJECT)
        self.assertEqual(["the spec", "the brief"],
                         [s["label"] for s in prop["goals"][0]["sources"]])

    def test_its_own_sources_come_first_and_are_kept(self):
        prop = {"goals": [{"id": "g1", "sources": [
            {"id": "s1", "type": "url", "label": "its own note"}]}]}
        CSY._inherit_sources(prop, self.PROJECT)
        self.assertEqual("its own note", prop["goals"][0]["sources"][0]["label"])
        self.assertEqual(3, len(prop["goals"][0]["sources"]))

    def test_inheriting_twice_adds_nothing(self):
        prop = {"goals": [{"id": "g1", "sources": []}]}
        CSY._inherit_sources(prop, self.PROJECT)
        CSY._inherit_sources(prop, self.PROJECT)
        self.assertEqual(2, len(prop["goals"][0]["sources"]))

    def test_a_project_with_no_sources_changes_nothing(self):
        prop = {"goals": [{"id": "g1", "sources": []}]}
        CSY._inherit_sources(prop, [])
        self.assertEqual([], prop["goals"][0]["sources"])


if __name__ == "__main__":
    unittest.main()
