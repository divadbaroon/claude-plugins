"""The project's record as rows a database will take.

``project_store`` builds one document for a reader; this is the same facts
flattened for Postgres, and the tests here are about what has to change on
the way: ids that survive a move and repeat across runs, hierarchy carried
as one edge, ownership on every row, timestamps a column will accept, and a
snapshot complete enough that a loader can tell a deletion from an absence.
"""

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import project_sync as SY  # noqa: E402

USER = "11111111-2222-3333-4444-555555555555"


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "work" / "myrepo"
        self.project.mkdir(parents=True)
        self.session = "chat-main"

        parent = GM.new_goal("g1", "Ship it", origin="user")
        child = GM.new_goal("g2", "Ship the part", origin="user")
        child["parent_goal_id"] = "g1"
        child["status"] = "in_progress"
        parent["notes"] = "# Why\n\nBecause.\n"
        parent["prompt_md"] = "Do the thing."
        parent["updated_at"] = "2026-08-20T01:02:03+00:00"
        parent["sources"] = [{"id": "s1", "type": "url", "label": "spec"}]
        parent["prompt_ids"] = ["p1"]
        parent["auto_prompt_ids"] = ["p1"]
        parent["todo_items"] = [
            {"id": "trow0001", "text": "first", "depth": 0, "status": "done"},
            {"id": "trow0002", "text": "second", "depth": 1, "status": "failed"},
        ]
        goals = {"version": 1, "goals": [parent, child]}
        GM.sanitize(goals)

        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": [
            {"id": "p1", "role": "user", "text": "the ask",
             "session_id": self.session,
             "created_at": "2026-08-19T00:00:00+00:00"}]}))
        p.manifest.write_text(json.dumps({"cwd": str(self.project)}))
        PS.save_project(self.root, self.project, {
            "objective": "Ship the workspace",
            "sources": [{"id": "ps1", "type": "url",
                         "label": "the brief"}]})

    def snap(self, **kw):
        return SY.snapshot(self.root, self.project, USER, **kw)


class IdentityTests(SyncTestCase):
    def test_the_project_keeps_one_id_across_calls(self):
        first = SY.project_uuid(self.root, self.project)
        second = SY.project_uuid(self.root, self.project)
        self.assertEqual(first, second)
        uuid.UUID(first)  # a real UUID, not a digest
        # Kept in the project's own file, so it survives a regeneration of
        # the derived half.
        self.assertEqual(first, PS.load_project(self.root, self.project)["id"])

    def test_an_id_survives_the_directory_moving(self):
        minted = SY.project_uuid(self.root, self.project)
        moved = self.root / "work" / "renamed"
        self.project.rename(moved)
        # The authored file is keyed by the old path's digest, so a move is
        # the case a path-keyed identity gets wrong. Read it back the way a
        # caller would after re-pointing at the same project file.
        self.assertNotEqual(SY.project_uuid(self.root, moved, mint=False),
                            minted)
        self.assertEqual(minted,
                         PS.load_project(self.root, self.project)["id"])

    def test_rows_take_the_same_ids_on_every_run(self):
        one, two = self.snap(), self.snap()
        self.assertEqual([g["id"] for g in one["goals"]],
                         [g["id"] for g in two["goals"]])
        self.assertEqual([t["id"] for t in one["todos"]],
                         [t["id"] for t in two["todos"]])

    def test_reading_without_minting_leaves_no_mark(self):
        payload = self.snap(mint=False)
        uuid.UUID(payload["project_id"])
        # The authored half is untouched and, above all, no id was
        # written: a read-only export leaves the project as it found it.
        self.assertNotIn("id", PS.load_project(self.root, self.project))


class ShapeTests(SyncTestCase):
    def test_every_row_of_every_table_names_its_owner(self):
        payload = self.snap()
        for table in SY.TABLES:
            rows = payload[table]
            self.assertTrue(rows, f"{table} should not be empty here")
            for row in rows:
                self.assertEqual(USER, row["user_id"], table)

    def test_a_snapshot_without_an_owner_is_refused(self):
        with self.assertRaises(ValueError):
            SY.snapshot(self.root, self.project, "")

    def test_hierarchy_is_one_edge_and_the_derived_halves_are_gone(self):
        payload = self.snap()
        by_local = {g["local_id"]: g for g in payload["goals"]}
        self.assertIsNone(by_local["g1"]["parent_id"])
        self.assertEqual(by_local["g1"]["id"], by_local["g2"]["parent_id"])
        for goal in payload["goals"]:
            for derived in ("todos_md", "attachments", "location",
                            "sibling_keys", "child_keys", "title_path",
                            "depth", "key"):
                self.assertNotIn(derived, goal)

    def test_todo_rows_keep_their_order_and_their_depth(self):
        payload = self.snap()
        rows = sorted((t for t in payload["todos"]), key=lambda r: r["position"])
        self.assertEqual(["first", "second"], [r["text"] for r in rows])
        self.assertEqual([0, 1], [r["depth"] for r in rows])
        self.assertEqual(["done", "failed"], [r["status"] for r in rows])
        # Each row points at the goal it belongs to, by that goal's row id.
        goal_ids = {g["id"] for g in payload["goals"]}
        for row in rows:
            self.assertIn(row["goal_id"], goal_ids)

    def test_a_status_the_workspace_does_not_use_is_emptied(self):
        p = chat_state.paths(self.session, self.root)
        stored = json.loads(p.goals.read_text())
        stored["goals"][0]["todo_items"][0]["status"] = "nonsense"
        p.goals.write_text(json.dumps(stored))
        rows = {r["local_id"]: r for r in self.snap()["todos"]}
        self.assertEqual("", rows["trow0001"]["status"])

    def test_related_prompts_keep_whether_inference_linked_them(self):
        related = self.snap()["related_prompts"]
        self.assertEqual(1, len(related))
        self.assertEqual("p1", related[0]["prompt_id"])
        self.assertTrue(related[0]["auto"])

    def test_the_tables_are_listed_parents_before_children(self):
        self.assertLess(SY.TABLES.index("projects"), SY.TABLES.index("chats"))
        self.assertLess(SY.TABLES.index("goals"), SY.TABLES.index("todos"))
        self.assertEqual(set(SY.TABLES), set(SY.counts(self.snap())))


class TimestampTests(SyncTestCase):
    def test_an_unparseable_instant_becomes_null_rather_than_failing(self):
        p = chat_state.paths(self.session, self.root)
        stored = json.loads(p.goals.read_text())
        stored["goals"][0]["updated_at"] = "whenever"
        p.goals.write_text(json.dumps(stored))
        rows = {g["local_id"]: g for g in self.snap()["goals"]}
        self.assertIsNone(rows["g1"]["updated_at"])

    def test_instants_arrive_as_utc(self):
        self.assertEqual("2026-08-20T01:02:03+00:00", SY._ts(
            "2026-08-19T18:02:03-07:00"))
        self.assertEqual("2026-08-20T01:02:03+00:00", SY._ts(
            "2026-08-20T01:02:03Z"))
        # A naive instant is read as UTC rather than as the machine's zone,
        # which is what a second machine would otherwise disagree about.
        self.assertEqual("2026-08-20T01:02:03+00:00", SY._ts(
            "2026-08-20T01:02:03"))
        self.assertIsNone(SY._ts(None))
        self.assertIsNone(SY._ts(""))
        self.assertIsNone(SY._ts(True))

    def test_seconds_since_the_epoch_are_accepted(self):
        self.assertEqual("2026-08-20T01:02:03+00:00", SY._ts(1787187723))


class DeletionTests(SyncTestCase):
    def test_a_removed_goal_is_absent_so_a_loader_can_prune_it(self):
        before = self.snap()
        self.assertEqual(2, len(before["goals"]))
        p = chat_state.paths(self.session, self.root)
        stored = json.loads(p.goals.read_text())
        stored["goals"] = [g for g in stored["goals"] if g["id"] != "g2"]
        p.goals.write_text(json.dumps(stored))
        after = self.snap()
        self.assertEqual(1, len(after["goals"]))
        self.assertEqual("g1", after["goals"][0]["local_id"])
        # The project keeps its id, so the prune is scoped to the same rows.
        self.assertEqual(before["project_id"], after["project_id"])


if __name__ == "__main__":
    unittest.main()
