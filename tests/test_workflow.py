import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.cli import main  # noqa: E402
from compact_focus.state import StatePaths, append_jsonl  # noqa: E402
from compact_focus.workflow import (  # noqa: E402
    WorkflowError,
    ensure_cycle,
    precompact,
    prepare_in_background,
    prompt_feedback,
    postcompact,
    should_prepare,
)


def raw_proposal(trace):
    source_ids = [
        source["id"]
        for episode in trace["episodes"]
        for source in episode["sources"]
    ]
    return {
        "schema_version": 3,
        "source_hash": trace["source_hash"],
        "created_at": "now",
        "generator": "test",
        "representations": [],
        "class_rules": [],
        "warnings": [],
        "items": [
            {
                "id": "i-test",
                "title": "Keep the decision",
                "summary": "The implementation decision remains active.",
                "type": "decision",
                "status": "active",
                "retention": "preserve",
                "model_retention": "preserve",
                "confidence": "high",
                "needs_review": False,
                "reviewed": True,
                "source_ids": source_ids,
                "rationale": "",
                "next_step": "Run the integration test.",
                "rival_interpretations": [],
                "rule_floor": None,
                "user_touched": False,
                "origin": "proposal",
            }
        ],
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.transcript = self.root / "session.jsonl"
        self.rows = [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "session",
                "cwd": str(self.root),
                "message": {"role": "user", "content": "preserve the active decision"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": "session",
                "cwd": str(self.root),
                "message": {
                    "role": "assistant",
                    "model": "claude-haiku-4-5",
                    "content": [{"type": "text", "text": "The decision is implemented."}],
                    "usage": {
                        "input_tokens": 100000,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
        ]
        self.write_rows()
        self.payload = {
            "session_id": "session",
            "cwd": str(self.root),
            "transcript_path": str(self.transcript),
            "trigger": "manual",
            "custom_instructions": "",
        }
        self.environment = patch.dict(
            os.environ,
            {"COMPACT_FOCUS_STATE_DIR": str(self.root / "state")},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_rows(self):
        with self.transcript.open("w", encoding="utf-8") as handle:
            for row in self.rows:
                handle.write(json.dumps(row) + "\n")

    def test_cycle_reuses_exact_snapshot_and_invalidates_drift(self):
        paths = StatePaths.from_hook(self.payload)
        paths.ensure()
        with patch("compact_focus.workflow.prepare_proposal", side_effect=lambda trace, **_kwargs: raw_proposal(trace)) as prepare:
            first, _trace, _proposal, reused = ensure_cycle(paths, self.payload)
            second, _trace, _proposal, reused_second = ensure_cycle(paths, self.payload)
            self.assertFalse(reused)
            self.assertTrue(reused_second)
            self.assertEqual(first, second)
            self.rows.append(
                {
                    "type": "user",
                    "uuid": "u2",
                    "sessionId": "session",
                    "cwd": str(self.root),
                    "message": {"role": "user", "content": "new evidence"},
                }
            )
            self.write_rows()
            third, _trace, _proposal, reused_third = ensure_cycle(paths, self.payload)
            self.assertFalse(reused_third)
            self.assertNotEqual(second, third)
            self.assertEqual(2, prepare.call_count)

    def test_background_worker_does_not_rerun_for_one_late_turn(self):
        calls = 0

        def prepare(trace, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.rows.append(
                    {
                        "type": "user",
                        "uuid": "arrived-during-worker",
                        "sessionId": "session",
                        "cwd": str(self.root),
                        "message": {"role": "user", "content": "late turn"},
                    }
                )
                self.write_rows()
            return raw_proposal(trace)

        with (
            patch("compact_focus.workflow.prepare_proposal", side_effect=prepare),
            patch.dict(os.environ, {"COMPACT_FOCUS_PREP_ALWAYS": "1"}),
        ):
            self.assertEqual(0, prepare_in_background(self.payload))
        self.assertEqual(1, calls)
        paths = StatePaths.from_hook(self.payload)
        _identifier, current_trace, current_proposal, _reused = ensure_cycle(
            paths,
            self.payload,
            allow_fallback_reuse=True,
            generate_worker=False,
        )
        self.assertEqual("hybrid", current_proposal["generator"])
        assigned = {
            source_id
            for item in current_proposal["items"]
            for source_id in item["source_ids"]
        }
        expected = {
            source["id"]
            for episode in current_trace["episodes"]
            for source in episode["sources"]
        }
        self.assertEqual(expected, assigned)

    def test_unknown_window_prepares_from_observed_usage_without_fake_window(self):
        trace = {
            "context": {
                "used_pct_observed": None,
                "used_tokens_observed": 90000,
                "visible_tokens_estimate": 100,
            }
        }
        with patch.dict(
            os.environ,
            {
                "COMPACT_FOCUS_PREP_USED_TOKENS": "80000",
                "COMPACT_FOCUS_PREP_VISIBLE_TOKENS": "50000",
            },
        ):
            self.assertTrue(should_prepare(trace))
            trace["context"]["used_tokens_observed"] = 79000
            self.assertFalse(should_prepare(trace))
        with patch.dict(os.environ, {"COMPACT_FOCUS_BACKGROUND": "0"}):
            trace["context"]["used_tokens_observed"] = 90000
            self.assertFalse(should_prepare(trace))

    def test_foreground_review_does_not_wait_for_busy_background_lock(self):
        @contextlib.contextmanager
        def busy_lock(*_args, **_kwargs):
            yield False

        with (
            patch("compact_focus.workflow.file_lock", side_effect=busy_lock),
            patch("compact_focus.workflow.prepare_proposal") as prepare,
            patch("compact_focus.workflow.terminal_lease", return_value=contextlib.nullcontext()),
            patch("compact_focus.workflow.run_review", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, precompact(self.payload))
        prepare.assert_not_called()
        paths = StatePaths.from_hook(self.payload)
        self.assertTrue((paths.session / "foreground-requested.json").exists())

    def test_foreground_marker_supersedes_background_publication(self):
        paths = StatePaths.from_hook(self.payload)

        def prepare(trace, **_kwargs):
            paths.ensure()
            from compact_focus.state import atomic_write_json
            atomic_write_json(paths.session / "foreground-requested.json", {"requested": True})
            return raw_proposal(trace)

        with (
            patch("compact_focus.workflow.prepare_proposal", side_effect=prepare),
            patch.dict(os.environ, {"COMPACT_FOCUS_PREP_ALWAYS": "1"}),
        ):
            self.assertEqual(0, prepare_in_background(self.payload))
        self.assertIsNone(paths.latest_cycle_id())
        superseded = list(paths.cycles.glob("*/superseded.json"))
        self.assertEqual(1, len(superseded))

    def test_misconstrual_feedback_recovers_preserved_trace_evidence(self):
        paths = StatePaths.from_hook(self.payload)
        paths.ensure()
        cycle_id = "feedback-cycle"
        cycle = paths.cycle(cycle_id)
        cycle.mkdir(parents=True)
        trace = {
            "episodes": [
                {
                    "title": "timestamp model decision",
                    "sources": [
                        {
                            "id": "s-drift",
                            "kind": "assistant_text",
                            "text": "The late anchor disproves constant offset; nonlinear drift remains open.",
                        }
                    ],
                }
            ]
        }
        from compact_focus.state import atomic_write_json
        atomic_write_json(cycle / "trace.json", trace)
        paths.set_latest_cycle(cycle_id)
        output = io.StringIO()
        payload = {
            **self.payload,
            "prompt": "the compaction misread nonlinear drift as a resolved constant offset",
        }
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, prompt_feedback(payload))
        rendered = output.getvalue()
        self.assertIn("s-drift", rendered)
        self.assertIn("misconstrual", rendered)
        feedback = [json.loads(line) for line in (paths.project / "feedback.jsonl").read_text().splitlines()]
        self.assertEqual("misconstrual", feedback[-1]["kind"])

    def test_explicit_feedback_conditions_the_correct_worker_axis(self):
        paths = StatePaths.from_hook(self.payload)
        paths.ensure()
        append_jsonl(
            paths.project / "feedback.jsonl",
            {"kind": "misconstrual", "query": "failed test was encoded as resolved"},
        )
        captured = {}

        def prepare(trace, **kwargs):
            captured.update(kwargs)
            return raw_proposal(trace)

        with patch("compact_focus.workflow.prepare_proposal", side_effect=prepare):
            ensure_cycle(paths, self.payload)
        self.assertIn("MISCONSTRUAL", captured["lens"])
        self.assertIn("failed test was encoded as resolved", captured["lens"])
        self.assertNotIn("failed test was encoded as resolved", captured["guidelines"])

    def test_postcompact_records_lexical_adherence_audit(self):
        with (
            patch("compact_focus.workflow.terminal_lease", return_value=contextlib.nullcontext()),
            patch("compact_focus.workflow.run_review", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, precompact(self.payload))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                postcompact(
                    {
                        **self.payload,
                        "compact_summary": "A summary with unrelated vocabulary.",
                    }
                ),
            )
        paths = StatePaths.from_hook(self.payload)
        post = json.loads(
            (paths.cycle(paths.latest_cycle_id()) / "postcompact.json").read_text()
        )
        self.assertEqual(1, post["adherence_audit"]["checked_items"])
        self.assertEqual(1, post["adherence_audit"]["possible_omissions"])

    def test_focused_compaction_passes_through_without_directive(self):
        payload = {**self.payload, "custom_instructions": "keep the benchmark"}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, precompact(payload))
        self.assertEqual("", output.getvalue())

    def test_one_invocation_approves_and_emits_directive(self):
        output = io.StringIO()

        def approve(_trace, _proposal, review, save):
            review["precommit"] = "Do not confuse throughput with latency."
            save(review)
            return True

        with (
            patch("compact_focus.workflow.prepare_proposal") as prepare,
            patch("compact_focus.workflow.terminal_lease", return_value=contextlib.nullcontext()),
            patch("compact_focus.workflow.run_review", side_effect=approve),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, precompact(self.payload))
        directive = output.getvalue()
        self.assertIn("HUMAN-REVIEWED COMPACTION CONTRACT", directive)
        self.assertIn("Do not confuse throughput with latency", directive)
        prepare.assert_not_called()
        paths = StatePaths.from_hook(self.payload)
        final = paths.cycle(paths.latest_cycle_id()) / "finalization.json"
        self.assertTrue(final.exists())

    def test_cancel_blocks_and_emits_no_directive(self):
        output = io.StringIO()
        with (
            patch("compact_focus.workflow.prepare_proposal", side_effect=lambda trace, **_kwargs: raw_proposal(trace)),
            patch("compact_focus.workflow.terminal_lease", return_value=contextlib.nullcontext()),
            patch("compact_focus.workflow.run_review", return_value=False),
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaisesRegex(WorkflowError, "cancelled"):
                precompact(self.payload)
        self.assertEqual("", output.getvalue())

    def test_cli_returns_blocking_code_on_bad_precompact_payload(self):
        stderr = io.StringIO()
        with patch("sys.stdin", io.StringIO("{}")), contextlib.redirect_stderr(stderr):
            code = main(["hook", "precompact"])
        self.assertEqual(2, code)
        self.assertIn("transcript is unavailable", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
