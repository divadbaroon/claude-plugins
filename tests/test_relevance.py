"""Goals judged against the objective the project states.

Three answers, not two: work that unblocks the objective is not the same as
work that has nothing to do with it, and only the last is worth folding
away. A binary verdict would hide the plumbing that makes the objective
reachable -- which is usually the part you most need to see.

Nothing here hides anything yet. The verdict is recorded so its quality can
be judged before the tree starts acting on it.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_synth as CSY  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402


class ShapeTests(unittest.TestCase):
    def test_a_goal_carries_a_verdict_from_the_start(self):
        goal = GM.new_goal("g1", "t", origin="user")
        self.assertEqual("core", goal["relevance"])
        self.assertEqual("", goal["relevance_why"])
        self.assertEqual("", goal["relevance_for"])

    def test_an_unrecognised_verdict_becomes_core(self):
        # The fold hides work, so failing to understand a verdict must not
        # be the thing that hides it.
        tree = {"version": 1, "goals": [
            dict(GM.new_goal("g1", "t"), relevance="nonsense"),
            dict(GM.new_goal("g2", "t"), relevance=None)]}
        GM.sanitize(tree)
        self.assertEqual(["core", "core"],
                         [g["relevance"] for g in tree["goals"]])

    def test_a_real_verdict_survives(self):
        tree = {"version": 1, "goals": [dict(
            GM.new_goal("g1", "t"), relevance="supporting",
            relevance_why="unblocks the objective")]}
        GM.sanitize(tree)
        self.assertEqual("supporting", tree["goals"][0]["relevance"])
        self.assertEqual("unblocks the objective",
                         tree["goals"][0]["relevance_why"])


class OpTests(unittest.TestCase):
    def tree(self):
        t = {"version": 1, "goals": [GM.new_goal("g1", "Fix the hook")]}
        GM.sanitize(t)
        return t

    def test_the_op_changes_the_standing(self):
        t = self.tree()
        changes = GM.apply_ops(t, {"items": []}, [
            {"op": "set_relevance", "goal_id": "g1", "relevance": "supporting",
             "relevance_why": "unblocks it"}])
        self.assertEqual(["relevance g1 -> supporting"], changes)
        self.assertEqual("supporting", t["goals"][0]["relevance"])

    def test_restating_the_same_verdict_changes_nothing(self):
        # A verdict repeated every pass would rewrite updated_at and churn
        # the tree for no reason.
        t = self.tree()
        GM.apply_ops(t, {"items": []}, [
            {"op": "set_relevance", "goal_id": "g1", "relevance": "unrelated"}])
        stamp = t["goals"][0]["updated_at"]
        changes = GM.apply_ops(t, {"items": []}, [
            {"op": "set_relevance", "goal_id": "g1", "relevance": "unrelated"}])
        self.assertEqual([], changes)
        self.assertEqual(stamp, t["goals"][0]["updated_at"])

    def test_a_verdict_the_schema_does_not_know_is_refused(self):
        t = self.tree()
        GM.apply_ops(t, {"items": []}, [
            {"op": "set_relevance", "goal_id": "g1", "relevance": "sort of"}])
        self.assertEqual("core", t["goals"][0]["relevance"])


class BornFinishedTests(unittest.TestCase):
    """A goal can be created already closed.

    Each pass sees one window of evidence. Work that starts and finishes
    inside that window is noticed exactly once -- the evidence that would
    close the goal is the same evidence that created it -- so a goal born
    active there stays active for good. This is why a long catch-up leaves
    a tree full of finished work marked open.
    """

    def make(self, **over):
        tree = {"version": 1, "goals": []}
        op = {"op": "new_goal", "parent_goal_id": None,
              "title": "Work already done", "distinct_because": "x"}
        op.update(over)
        GM.apply_ops(tree, {"items": []}, [op])
        return tree["goals"][0]

    def test_a_goal_can_be_born_completed(self):
        self.assertEqual("completed", self.make(status="completed")["status"])

    def test_and_born_abandoned(self):
        self.assertEqual("abandoned", self.make(status="abandoned")["status"])

    def test_without_a_status_it_is_open_as_before(self):
        self.assertEqual("active", self.make()["status"])

    def test_a_status_the_tree_does_not_use_is_ignored(self):
        self.assertEqual("active", self.make(status="finished-ish")["status"])

    def test_the_prompt_tells_the_model_it_may_do_this(self):
        # Allowing it in code is not enough; nothing would ever send it.
        self.assertIn("stays active for good", CSY.INCREMENTAL_PROMPT)
        self.assertIn('"status":"active|in_progress|completed|abandoned"',
                      CSY.INCREMENTAL_PROMPT)


class NewGoalRelevanceTests(unittest.TestCase):
    """A verdict the model sends with a new goal has to survive the op.

    It did not: apply_ops built the goal without it, so every goal an
    incremental pass created came out "core" whatever the model judged. A
    rebuilt tree then looked uniformly aligned, which reads as the feature
    working rather than the verdict being discarded.
    """

    def make(self, **over):
        t = {"version": 1, "goals": []}
        op = {"op": "new_goal", "parent_goal_id": None, "title": "Fix the hook",
              "distinct_because": "x"}
        op.update(over)
        GM.apply_ops(t, {"items": []}, [op])
        return t["goals"][0]

    def test_the_verdict_survives(self):
        g = self.make(relevance="supporting", relevance_why="unblocks it")
        self.assertEqual("supporting", g["relevance"])
        self.assertEqual("unblocks it", g["relevance_why"])

    def test_unrelated_survives_too(self):
        self.assertEqual("unrelated", self.make(relevance="unrelated")["relevance"])

    def test_no_verdict_means_core(self):
        self.assertEqual("core", self.make()["relevance"])

    def test_a_verdict_the_schema_does_not_know_is_refused(self):
        self.assertEqual("core", self.make(relevance="sort of")["relevance"])


class ImportKeepsVerdictTests(unittest.TestCase):
    """The page posts the whole tree back; the verdict has to survive it.

    _import rebuilds every goal from a fixed field list. A field missing
    from that list is not merely unsaved -- it is erased by the next thing
    the reader types, and the tags quietly fall back to "core" while
    looking like inference simply judged everything aligned.
    """

    def test_the_field_list_carries_relevance(self):
        import inspect
        from human_compact.trajectory import ui
        src = inspect.getsource(ui._import)
        for field in ("relevance", "relevance_why", "relevance_for"):
            self.assertIn('"%s": prev.get' % field, src,
                          "_import drops %s" % field)


class ObjectiveTests(unittest.TestCase):
    def test_no_objective_says_so_rather_than_going_blank(self):
        # A blank here would read as "nothing matters"; the model needs to
        # be told there is no opinion to judge against.
        block = CSY.objective_block("")
        self.assertIn("none given", block)
        self.assertIn("core", block)

    def test_an_objective_is_passed_through_bounded(self):
        self.assertEqual("Ship it", CSY.objective_block("  Ship it  "))
        self.assertEqual(2000, len(CSY.objective_block("x" * 5000)))

    def test_both_prompts_ask_for_the_verdict(self):
        for prompt in (CSY.INITIAL_PROMPT, CSY.INCREMENTAL_PROMPT):
            self.assertIn("<<OBJECTIVE>>", prompt)
            self.assertIn("supporting", prompt)
            self.assertIn("unrelated", prompt)

    def test_the_prompt_says_what_supporting_is_for(self):
        # The distinction the whole design rests on: a blocker is not the
        # objective and is not unrelated either.
        self.assertIn("unblocks", CSY.INITIAL_PROMPT)


class StampTests(unittest.TestCase):
    def test_a_fresh_verdict_records_the_objective_it_was_made_against(self):
        proposed = {"goals": [{"id": "g1", "relevance": "unrelated"}]}
        CSY._stamp_relevance_for(proposed, {"g1": "core"}, "Ship trees")
        self.assertEqual("Ship trees", proposed["goals"][0]["relevance_for"])

    def test_a_goal_the_pass_did_not_revisit_is_left_alone(self):
        # It was judged against whatever objective stood then; restamping
        # would make a stale verdict look freshly considered.
        proposed = {"goals": [{"id": "g1", "relevance": "core",
                               "relevance_for": "An older objective"}]}
        CSY._stamp_relevance_for(proposed, {"g1": "core"}, "Ship trees")
        self.assertEqual("An older objective",
                         proposed["goals"][0]["relevance_for"])

    def test_a_new_goal_is_stamped(self):
        proposed = {"goals": [{"id": "g9", "relevance": "core"}]}
        CSY._stamp_relevance_for(proposed, {}, "Ship trees")
        self.assertEqual("Ship trees", proposed["goals"][0]["relevance_for"])


if __name__ == "__main__":
    unittest.main()
