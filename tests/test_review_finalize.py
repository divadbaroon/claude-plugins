import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.finalize import FinalizeError, finalize_cycle, recall, search  # noqa: E402
from compact_focus.review import (  # noqa: E402
    create_item,
    merge_items,
    move_source,
    new_review,
    resolve_item,
    review_errors,
    set_item_field,
    split_source,
)
from compact_focus.state import StatePaths  # noqa: E402


def fixture():
    trace = {
        "source_hash": "hash",
        "transcript_path": "/tmp/session.jsonl",
        "episodes": [
            {
                "id": "e1",
                "sources": [
                    {"id": "s1", "kind": "user_prompt", "class": "other", "text": "keep this", "artifacts": {"paths": [], "commits": []}},
                    {"id": "s2", "kind": "tool_result", "class": "other", "text": "drop this", "artifacts": {"paths": ["/tmp/a.py"], "commits": []}},
                ],
            }
        ],
    }
    proposal = {
        "source_hash": "hash",
        "class_rules": [],
        "items": [
            {
                "id": "i1",
                "title": "open decision",
                "summary": "keep this faithfully",
                "type": "decision",
                "status": "active",
                "retention": "preserve",
                "model_retention": "preserve",
                "confidence": "low",
                "needs_review": True,
                "source_ids": ["s1"],
                "rationale": "",
                "next_step": "test it",
                "rival_interpretations": ["one", "two"],
            },
            {
                "id": "i2",
                "title": "old output",
                "summary": "old output",
                "type": "mechanical",
                "status": "resolved",
                "retention": "demote",
                "model_retention": "demote",
                "confidence": "high",
                "needs_review": False,
                "source_ids": ["s2"],
                "rationale": "",
                "next_step": "",
                "rival_interpretations": [],
            },
        ],
    }
    return trace, proposal


class ReviewFinalizeTests(unittest.TestCase):
    def test_contested_blocks_until_resolved(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        self.assertTrue(any("contested" in value for value in review_errors(trace, review)))
        resolve_item(review, 0, 1)
        self.assertEqual([], review_errors(trace, review))

    def test_move_split_merge_preserve_partition(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        resolve_item(review, 0)
        created = create_item(review, "new bucket", after=0)
        move_source(review, "s2", created)
        self.assertEqual([], review_errors(trace, review))
        split = split_source(review, "s1", "split", 0)
        self.assertEqual([], review_errors(trace, review))
        merge_items(review, split, created)
        self.assertEqual([], review_errors(trace, review))

    def test_finalize_is_idempotent_and_recovery_searches(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        resolve_item(review, 0)
        with tempfile.TemporaryDirectory() as directory:
            paths = StatePaths(Path(directory), "session", "project", "/tmp")
            paths.ensure()
            result = finalize_cycle(paths, "cycle", trace, proposal, review)
            second = finalize_cycle(paths, "cycle", trace, proposal, review)
            self.assertFalse(result["reused"])
            self.assertTrue(second["reused"])
            demoted = json.loads((paths.cycle("cycle") / "demoted.json").read_text())
            self.assertEqual(1, len(demoted))
            restored = recall(paths.project / "recovery.sqlite3", demoted[0]["id"])
            self.assertEqual("drop this", restored["text"])
            self.assertEqual(1, len(search(paths.project / "recovery.sqlite3", "drop")))
            self.assertIn("HUMAN-REVIEWED", result["directive"])


if __name__ == "__main__":
    unittest.main()
