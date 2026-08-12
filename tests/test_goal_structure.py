import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import goals as GM  # noqa: E402


def goal(gid, parent=None):
    return {
        "id": gid,
        "title": gid,
        "status": "active",
        "parent_goal_id": parent,
        "evidence_ids": [],
        "todos": [],
    }


class GoalStructureTests(unittest.TestCase):
    def test_sanitize_breaks_parent_cycles_into_a_reachable_tree(self):
        goals = {"version": 1, "goals": [
            goal("g1", "g2"),
            goal("g2", "g1"),
            goal("g3", "g2"),
        ]}

        GM.sanitize(goals)

        self.assertIsNone(GM.by_id(goals, "g1")["parent_goal_id"])
        self.assertEqual("g1", GM.by_id(goals, "g2")["parent_goal_id"])
        self.assertEqual("g2", GM.by_id(goals, "g3")["parent_goal_id"])
        self.assertTrue(all(GM.depth(goals, g["id"]) <= 3 for g in goals["goals"]))

    def test_incremental_subgoal_keeps_description_and_parent(self):
        goals = {"version": 1, "goals": [goal("g1")]}

        GM.apply_ops(goals, {"items": []}, [{
            "op": "new_goal",
            "parent_goal_id": "g1",
            "title": "Search prior prompts",
            "description": "Offer typo-tolerant, newest-first lookup.",
            "todos": [],
            "evidence_ids": ["event:1"],
        }])

        child = GM.by_id(goals, "g2")
        self.assertEqual("g1", child["parent_goal_id"])
        self.assertEqual(
            "Offer typo-tolerant, newest-first lookup.", child["description"]
        )
        self.assertEqual("inferred", child["origin"])


if __name__ == "__main__":
    unittest.main()
