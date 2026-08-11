import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.draft import (  # noqa: E402
    apply_generated_summary,
    apply_revision,
    approve_draft,
    build_draft,
    build_summary_prompt,
    ensure_draft,
    run_revision_worker,
    run_summary_worker,
)
from compact_focus.finalize import finalize_cycle  # noqa: E402
from compact_focus.review import (  # noqa: E402
    new_review,
    set_source_retention,
)
from compact_focus.state import StatePaths  # noqa: E402


def fixture(platform="claude"):
    trace = {
        "platform": platform,
        "source_hash": "source-hash",
        "transcript_path": "/tmp/session.jsonl",
        "context": {"window_tokens": 1000},
        "episodes": [
            {
                "id": "e1",
                "sources": [
                    {
                        "id": "s1",
                        "kind": "user_prompt",
                        "class": "other",
                        "text": "The latency distinction must survive.",
                        "tokens_estimate": 10,
                        "artifacts": {"paths": [], "commits": []},
                    },
                    {
                        "id": "s2",
                        "kind": "tool_result",
                        "class": "file_changes",
                        "text": "An obsolete diagnostic output.",
                        "tokens_estimate": 10,
                        "artifacts": {"paths": ["/tmp/old.log"], "commits": []},
                    },
                ],
            }
        ],
    }
    proposal = {
        "source_hash": "source-hash",
        "generator": "test",
        "class_rules": [],
        "items": [
            {
                "id": "cluster-1",
                "title": "Latency contract",
                "summary": "Latency is the target, not throughput.",
                "type": "constraint",
                "status": "active",
                "retention": "preserve",
                "model_retention": "preserve",
                "confidence": "high",
                "needs_review": False,
                "source_ids": ["s1", "s2"],
                "rationale": "This controls the next implementation pass.",
                "next_step": "Measure tail latency.",
                "rival_interpretations": [],
            }
        ],
    }
    return trace, proposal


class DraftTests(unittest.TestCase):
    def test_oversized_draft_can_be_explicitly_approved(self):
        _trace, proposal = fixture()
        review = new_review(proposal)
        review["draft_review"] = {"draft": "x" * 24001, "revision_count": 0}

        with self.assertRaisesRegex(Exception, "explicitly approve"):
            approve_draft(review)
        approved = approve_draft(review, allow_oversized=True)

        self.assertEqual(24002, len(approved))
        self.assertTrue(review["draft_review"]["approved"])
        self.assertTrue(review["actions"][-1]["oversized"])

    def test_generated_summary_replaces_long_fallback_without_changing_contract(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        state = ensure_draft(trace, review)
        fallback = state["draft"]

        generated = apply_generated_summary(
            review,
            {"draft": "# Current objective\n\nPreserve the latency distinction and measure tail latency."},
        )

        self.assertLess(len(generated), len(fallback))
        self.assertEqual("model", review["draft_review"]["generated_by"])
        self.assertEqual("preserve", review["items"][0]["retention"])
        self.assertEqual([], review["draft_review"]["messages"])

    def test_summary_prompt_requests_dense_handoff_without_raw_ledger(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        prompt = build_summary_prompt(trace, review)

        self.assertIn("300-900 words", prompt)
        self.assertIn("Latency is the target", prompt)
        self.assertIn("The latency distinction must survive", prompt)
        self.assertNotIn("An obsolete diagnostic output", prompt)

    def test_claude_summary_worker_is_bounded_and_schema_driven(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["prompt"] = kwargs["input"]
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"structured_output": {"draft": "# Concise handoff"}}),
                "",
            )

        with patch("compact_focus.draft.shutil.which", return_value="/usr/local/bin/claude"):
            result = run_summary_worker(trace, review, runner=runner)

        self.assertEqual({"draft": "# Concise handoff"}, result)
        self.assertIn("--safe-mode", captured["command"])
        self.assertIn("--no-session-persistence", captured["command"])
        self.assertIn("--json-schema", captured["command"])
        self.assertIn("Write the carry-forward context summary", captured["prompt"])

    def test_source_override_is_visible_and_recovered_independently(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        set_source_retention(review, 0, "s2", "demote")
        draft = build_draft(trace, review)
        self.assertIn("Source-level overrides", draft)
        self.assertIn("s2 · DELETE", draft)
        ensure_draft(trace, review)
        approve_draft(review)

        with tempfile.TemporaryDirectory() as directory:
            paths = StatePaths(Path(directory), "session", "project", "/tmp")
            paths.ensure()
            result = finalize_cycle(paths, "cycle", trace, proposal, review)
            recovered = json.loads((paths.cycle("cycle") / "demoted.json").read_text())

        self.assertEqual(["s2"], [value["source_id"] for value in recovered])
        self.assertIn("BEGIN USER-APPROVED CARRY-FORWARD DRAFT", result["directive"])
        self.assertIn(review["approved_summary"].strip(), result["directive"])

    def test_chat_revision_updates_structured_contract_and_exact_draft(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        ensure_draft(trace, review)
        revision = {
            "reply": "Marked the work blocked and removed the obsolete diagnostic.",
            "draft": "# Reviewed compaction summary\n\nLatency work is blocked; keep the distinction.",
            "global_constraint": "Never conflate latency with throughput.",
            "replace_global_constraint": True,
            "cluster_changes": [
                {
                    "id": "cluster-1",
                    "retention": None,
                    "work_state": "blocked",
                    "title": None,
                    "summary": None,
                    "next_step": "",
                    "clarification": "The benchmark is blocked on a late anchor.",
                }
            ],
            "source_changes": [
                {
                    "id": "s2",
                    "retention": "demote",
                    "work_state": "done",
                    "clarification": "Superseded diagnostic only.",
                }
            ],
        }
        reply = apply_revision(trace, review, "The benchmark is blocked; delete the old output.", revision)

        self.assertIn("blocked", reply)
        self.assertEqual("blocked", review["items"][0]["work_state"])
        self.assertEqual("", review["items"][0]["next_step"])
        self.assertEqual("demote", review["source_reviews"]["s2"]["retention"])
        self.assertEqual("Never conflate latency with throughput.", review["precommit"])
        self.assertIn("Latency work is blocked", review["draft_review"]["draft"])
        self.assertEqual(2, len(review["draft_review"]["messages"]))

    def test_claude_revision_worker_is_bounded_and_schema_driven(self):
        trace, proposal = fixture()
        review = new_review(proposal)
        captured = {}
        payload = {
            "reply": "No semantic changes; tightened wording.",
            "draft": "# Revised draft",
            "global_constraint": "",
            "replace_global_constraint": False,
            "cluster_changes": [],
            "source_changes": [],
        }

        def runner(command, **kwargs):
            captured["command"] = command
            captured["prompt"] = kwargs["input"]
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"structured_output": payload}),
                "",
            )

        with patch("compact_focus.draft.shutil.which", return_value="/usr/local/bin/claude"):
            result = run_revision_worker(trace, review, "Make it shorter.", runner=runner)

        self.assertEqual(payload, result)
        self.assertIn("--safe-mode", captured["command"])
        self.assertIn("--no-session-persistence", captured["command"])
        self.assertIn("--json-schema", captured["command"])
        self.assertIn("Make it shorter.", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
