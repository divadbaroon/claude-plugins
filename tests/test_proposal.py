import copy
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.proposal import (  # noqa: E402
    ProposalError,
    _run_cancellable,
    first_fraction_source_ids,
    normalize_proposal,
    parse_worker_output,
    run_worker,
)


def source(source_id, kind="user_prompt", klass="other", text="evidence"):
    return {
        "id": source_id,
        "kind": kind,
        "class": klass,
        "text": text,
        "tokens_estimate": 10,
        "artifacts": {"paths": [], "commits": [], "mentions_tests": []},
    }


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.trace = {
            "source_hash": "abc",
            "episodes": [
                {"id": "e1", "title": "first", "sources": [source("s1")]},
                {
                    "id": "e2",
                    "title": "second",
                    "sources": [source("s2", "tool_call", "file_changes"), source("s3")],
                },
            ],
        }

    def test_normalizer_rejects_invented_and_repairs_coverage(self):
        raw = {
            "representations": [],
            "items": [
                {
                    "title": "decision",
                    "summary": "keep it",
                    "type": "decision",
                    "status": "active",
                    "retention": "demote",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": ["s1", "invented"],
                    "rationale": "because",
                    "next_step": "test",
                    "rival_interpretations": [],
                },
                {
                    "title": "duplicate",
                    "summary": "duplicate",
                    "type": "context",
                    "status": "resolved",
                    "retention": "demote",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": ["s1"],
                    "rationale": "",
                    "next_step": "",
                    "rival_interpretations": [],
                },
            ],
        }
        proposal = normalize_proposal(raw, self.trace)
        assigned = [source_id for item in proposal["items"] for source_id in item["source_ids"]]
        self.assertCountEqual(["s1", "s2", "s3"], assigned)
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual("preserve", proposal["items"][0]["retention"])
        self.assertTrue(any("invented" in warning for warning in proposal["warnings"]))
        recovered = [item for item in proposal["items"] if item["title"].startswith("Unclassified")]
        self.assertEqual(1, len(recovered))
        self.assertTrue(recovered[0]["needs_review"])

    def test_fallback_is_loss_averse_and_not_blocking(self):
        proposal = normalize_proposal(None, self.trace, worker_error="offline")
        self.assertEqual("fallback", proposal["generator"])
        self.assertTrue(all(item["retention"] == "preserve" for item in proposal["items"]))
        self.assertFalse(any(item["needs_review"] for item in proposal["items"]))
        self.assertIn("offline", proposal["warnings"])

    def test_parse_worker_envelope(self):
        parsed = parse_worker_output('{"structured_output":{"items":[],"representations":[]}}')
        self.assertEqual([], parsed["items"])

    def test_rivals_remain_inspectable_without_forcing_adjudication(self):
        raw = {
            "representations": [],
            "items": [
                {
                    "title": "stable decision",
                    "summary": "the decision is sufficiently grounded",
                    "type": "decision",
                    "status": "active",
                    "retention": "preserve",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": ["s1", "s2", "s3"],
                    "rationale": "",
                    "next_step": "",
                    "rival_interpretations": ["causal view", "coordination view"],
                }
            ],
        }
        proposal = normalize_proposal(raw, self.trace)
        self.assertFalse(proposal["items"][0]["needs_review"])
        self.assertEqual(2, len(proposal["items"][0]["rival_interpretations"]))

    def test_first_fraction_uses_token_weight_not_source_count(self):
        sources = {
            "large": source("large"),
            "small-1": source("small-1"),
            "small-2": source("small-2"),
        }
        sources["large"]["tokens_estimate"] = 80
        selected = first_fraction_source_ids(
            ["large", "small-1", "small-2"],
            sources,
            30,
        )
        self.assertEqual({"large"}, selected)

    def test_worker_uses_minimal_profile_and_records_metrics(self):
        raw = {
            "representations": [],
            "items": [
                {
                    "title": "all evidence",
                    "summary": "keep it",
                    "type": "context",
                    "status": "active",
                    "retention": "preserve",
                    "confidence": "high",
                    "needs_review": False,
                    "source_ids": ["s1", "s2", "s3"],
                    "rationale": "",
                    "next_step": "",
                    "rival_interpretations": [],
                }
            ],
        }
        envelope = {
            "structured_output": raw,
            "duration_ms": 1234,
            "duration_api_ms": 900,
            "num_turns": 1,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs["input"]
            class Result:
                returncode = 0
                stdout = __import__("json").dumps(envelope)
                stderr = ""
            return Result()

        with unittest.mock.patch("compact_focus.proposal.shutil.which", return_value="/bin/claude"):
            proposal = run_worker(self.trace, runner=runner)
        self.assertIn("--effort", captured["command"])
        self.assertIn("--disable-slash-commands", captured["command"])
        self.assertIn("--system-prompt", captured["command"])
        self.assertEqual(1234, proposal["worker"]["duration_ms"])
        self.assertEqual(0.01, proposal["worker"]["total_cost_usd"])

    def test_cancellable_runner_terminates_superseded_process(self):
        with self.assertRaisesRegex(ProposalError, "superseded"):
            _run_cancellable(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                "",
                10,
                lambda: True,
            )


if __name__ == "__main__":
    unittest.main()
