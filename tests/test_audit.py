import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.audit import audit_summary  # noqa: E402


class AuditTests(unittest.TestCase):
    def test_flags_missing_item_without_claiming_semantic_verification(self):
        review = {
            "items": [
                {
                    "id": "kept",
                    "title": "Nonlinear timestamp drift remains open",
                    "summary": "Sparse anchors do not identify nonlinear timestamp drift.",
                    "next_step": "Collect a late anchor.",
                    "retention": "preserve",
                },
                {
                    "id": "missing",
                    "title": "Package install instructions",
                    "summary": "Publish marketplace setup and cache-safe update steps.",
                    "next_step": "",
                    "retention": "summarize",
                },
                {
                    "id": "demoted",
                    "title": "Obsolete terminal experiment",
                    "summary": "Do not carry this forward.",
                    "next_step": "",
                    "retention": "demote",
                },
            ]
        }
        summary = "Nonlinear timestamp drift remains open; collect another late anchor."
        result = audit_summary(review, summary)
        self.assertEqual(2, result["checked_items"])
        self.assertEqual(1, result["possible_omissions"])
        self.assertFalse(next(item for item in result["items"] if item["item_id"] == "kept")["possible_omission"])
        self.assertTrue(next(item for item in result["items"] if item["item_id"] == "missing")["possible_omission"])
        self.assertIn("not semantic verification", result["method"])

    def test_binding_precommit_is_audited_independently(self):
        review = {
            "precommit": "CATACLYSM-SENTINEL-7Q9 means latency, never throughput.",
            "items": [],
        }
        missing = audit_summary(review, "The reviewed items were retained.")
        self.assertTrue(missing["checked_precommit"])
        self.assertTrue(missing["precommit"]["possible_omission"])
        self.assertEqual(1, missing["possible_omissions"])

        present = audit_summary(
            review,
            "CATACLYSM-SENTINEL-7Q9 means latency, never throughput.",
        )
        self.assertTrue(present["precommit"]["exact_present"])
        self.assertEqual(0, present["possible_omissions"])

        reworded = audit_summary(
            review,
            "CATACLYSM-SENTINEL-7Q9 means latency rather than throughput.",
        )
        self.assertGreater(reworded["precommit"]["lexical_coverage"], 0.5)
        self.assertTrue(reworded["precommit"]["possible_omission"])

    def test_approved_draft_reports_exact_carriage_separately(self):
        review = {
            "approved_summary": "Latency is binding; throughput stays out of scope.",
            "items": [],
        }
        exact = audit_summary(
            review,
            "Latency is binding; throughput stays out of scope.",
        )
        paraphrased = audit_summary(
            review,
            "Keep latency as the metric and exclude throughput.",
        )
        self.assertTrue(exact["approved_draft"]["exact_present"])
        self.assertFalse(paraphrased["approved_draft"]["exact_present"])
        self.assertTrue(paraphrased["checked_approved_draft"])


if __name__ == "__main__":
    unittest.main()
