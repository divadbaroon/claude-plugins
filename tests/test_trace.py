import json
import os
import tempfile
import unittest
import base64
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "compact-focus"))

from compact_focus.trace import build_trace  # noqa: E402
from compact_focus.codex_trace import add_review_contract  # noqa: E402


class TraceTests(unittest.TestCase):
    def write_transcript(self, rows):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "session.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        self.addCleanup(directory.cleanup)
        return path

    def test_trace_starts_at_latest_compaction_and_keeps_assistant_tools(self):
        rows = [
            {
                "type": "user",
                "uuid": "old-user",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "old context"},
            },
            {
                "type": "user",
                "uuid": "summary",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "previous compact summary"},
            },
            {
                "type": "user",
                "uuid": "new-user",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "fix timestamp drift"},
            },
            {
                "type": "assistant",
                "uuid": "assistant",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": [
                        {"type": "text", "text": "The late anchor disproves constant offset."},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Edit",
                            "input": {"file_path": "/tmp/project/clock.py", "old": "offset", "new": "drift"},
                        },
                    ],
                    "usage": {
                        "input_tokens": 5,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 450000,
                    },
                },
            },
            {
                "type": "user",
                "uuid": "result",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "edited clock.py"}],
                },
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        self.assertTrue(trace["boundary"]["found"])
        self.assertEqual(2, len(trace["episodes"]))
        rendered = json.dumps(trace)
        self.assertNotIn("old context", rendered)
        self.assertIn("late anchor", rendered)
        self.assertIn("clock.py", rendered)
        self.assertIn("tool_result", rendered)
        result_source = next(
            source
            for episode in trace["episodes"]
            for source in episode["sources"]
            if source["kind"] == "tool_result"
        )
        self.assertEqual("file_changes", result_source["class"])
        self.assertEqual(1_000_000, trace["context"]["window_tokens"])
        self.assertEqual(45.0, trace["context"]["used_pct_observed"])

    def test_usage_before_latest_compaction_is_not_reported_as_current(self):
        rows = [
            {
                "type": "assistant",
                "uuid": "old-answer",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "assistant",
                    "model": "claude-fable-5",
                    "content": [{"type": "text", "text": "old"}],
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 700000,
                    },
                },
            },
            {
                "type": "user",
                "uuid": "summary",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "isCompactSummary": True,
                "message": {"role": "user", "content": "fresh compact summary"},
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        self.assertIsNone(trace["context"]["used_tokens_observed"])
        self.assertIsNone(trace["context"]["used_pct_observed"])

    def test_source_ids_are_stable(self):
        rows = [
            {
                "type": "user",
                "uuid": "same",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "a prompt"},
            }
        ]
        path = self.write_transcript(rows)
        first = build_trace(path)
        second = build_trace(path)
        self.assertEqual(first["source_hash"], second["source_hash"])
        self.assertEqual(
            first["episodes"][0]["sources"][0]["id"],
            second["episodes"][0]["sources"][0]["id"],
        )

        rows[0]["message"]["content"] = "a changed prompt"
        changed = build_trace(self.write_transcript(rows))
        self.assertNotEqual(first["source_hash"], changed["source_hash"])
        self.assertNotEqual(
            first["episodes"][0]["sources"][0]["id"],
            changed["episodes"][0]["sources"][0]["id"],
        )

    def test_compact_command_wrapper_does_not_invalidate_semantic_trace(self):
        rows = [
            {
                "type": "user",
                "uuid": "prompt",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "preserve this decision"},
            }
        ]
        first = build_trace(self.write_transcript(rows))
        rows.append(
            {
                "type": "user",
                "uuid": "command",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "/compact"},
            }
        )
        second = build_trace(self.write_transcript(rows))
        self.assertEqual(first["source_hash"], second["source_hash"])
        self.assertNotIn("/compact", json.dumps(second))

    def test_slash_commands_are_not_reported_as_file_artifacts(self):
        rows = [
            {
                "type": "user",
                "uuid": "prompt",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "user",
                    "content": "Run one bare /compact, then inspect /tmp/project/result.json.",
                },
            }
        ]
        trace = build_trace(self.write_transcript(rows))
        artifacts = trace["episodes"][0]["sources"][0]["artifacts"]["paths"]
        self.assertNotIn("/compact", artifacts)
        self.assertIn("/tmp/project/result.json", artifacts)

    def test_unknown_model_does_not_invent_a_context_window(self):
        rows = [
            {
                "type": "user",
                "uuid": "prompt",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "one prompt"},
            },
            {
                "type": "assistant",
                "uuid": "answer",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "assistant",
                    "model": "future-model-with-unknown-window",
                    "content": [{"type": "text", "text": "answer"}],
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 900,
                    },
                },
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        self.assertIsNone(trace["context"]["window_tokens"])
        self.assertIsNone(trace["context"]["used_pct_observed"])
        self.assertIsNotNone(trace["episodes"][0]["used_context_pct_estimate"])

    def test_binary_media_is_metadata_only_and_private_thinking_is_excluded(self):
        def png(width, height):
            signature = b"\x89PNG\r\n\x1a\n"
            ihdr_body = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
            ihdr = b"IHDR" + ihdr_body
            chunk = struct.pack(">I", len(ihdr_body)) + ihdr + struct.pack(">I", zlib.crc32(ihdr))
            return signature + chunk

        encoded = base64.b64encode(png(280, 140)).decode("ascii")
        rows = [
            {
                "type": "user",
                "uuid": "media-only-turn",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": encoded,
                            },
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "uuid": "private",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private chain of thought"},
                        {"type": "text", "text": "The screenshot shows a failed review."},
                    ],
                },
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        rendered = json.dumps(trace)
        self.assertNotIn(encoded, rendered)
        self.assertNotIn("private chain of thought", rendered)
        self.assertIn("payload_omitted", rendered)
        self.assertIn("width", rendered)
        self.assertIn("280", rendered)
        self.assertIn("height", rendered)
        self.assertIn("140", rendered)
        self.assertEqual(50, next(
            source["tokens_estimate"]
            for source in trace["episodes"][0]["sources"]
            if source["kind"] == "image"
        ))
        self.assertEqual(1, trace["input_audit"]["private_reasoning_blocks_excluded"])
        self.assertEqual(1, trace["input_audit"]["media_or_document_blocks_metadata_only"])

    def test_file_attachments_and_snapshot_metadata_are_provenance_sources(self):
        rows = [
            {
                "type": "user",
                "uuid": "prompt",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "review the active file"},
            },
            {
                "type": "attachment",
                "uuid": "file-attachment",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "attachment": {
                    "type": "file",
                    "filename": "/tmp/project/active.py",
                    "content": {
                        "type": "text",
                        "file": {"filePath": "/tmp/project/active.py", "content": "VALUE = 7"},
                    },
                },
            },
            {
                "type": "file-history-snapshot",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "snapshot": {
                    "trackedFileBackups": {
                        "active.py": {
                            "backupFileName": "backup-1.txt",
                            "backupTime": "2026-08-11T00:00:00Z",
                            "version": 3,
                            "realParentDir": "/tmp/project",
                        }
                    }
                },
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        sources = [source for episode in trace["episodes"] for source in episode["sources"]]
        attachment = next(source for source in sources if source["kind"] == "attachment_file")
        snapshot = next(source for source in sources if source["kind"] == "file_snapshot")
        self.assertIn("VALUE = 7", attachment["text"])
        self.assertIn("backup-1.txt", snapshot["text"])
        self.assertIn('"version": 3', snapshot["text"])
        self.assertEqual("file_changes", snapshot["class"])

    def test_unchanged_cumulative_file_snapshots_are_not_repeated(self):
        backup = {
            "active.py": {
                "backupFileName": "backup-1.txt",
                "backupTime": "2026-08-11T00:00:00Z",
                "version": 1,
            }
        }
        rows = [
            {
                "type": "user",
                "uuid": "prompt",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "edit active.py"},
            },
            {
                "type": "file-history-snapshot",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "snapshot": {"trackedFileBackups": backup},
            },
            {
                "type": "file-history-snapshot",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "snapshot": {"trackedFileBackups": backup},
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        snapshots = [
            source
            for episode in trace["episodes"]
            for source in episode["sources"]
            if source["kind"] == "file_snapshot"
        ]
        self.assertEqual(1, len(snapshots))

    def test_data_urls_inside_tool_results_are_redacted(self):
        encoded = base64.b64encode(b"not really an image" * 200).decode("ascii")
        rows = [
            {
                "type": "user",
                "uuid": "prompt",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {"role": "user", "content": "inspect output"},
            },
            {
                "type": "user",
                "uuid": "result",
                "sessionId": "s1",
                "cwd": "/tmp/project",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "unknown",
                            "content": {"image_url": "data:image/png;base64," + encoded},
                        }
                    ],
                },
            },
        ]
        rendered = json.dumps(build_trace(self.write_transcript(rows)))
        self.assertNotIn(encoded, rendered)
        self.assertIn("payload_omitted", rendered)

    def test_codex_trace_uses_latest_boundary_and_semantic_tool_items(self):
        rows = [
            {
                "type": "session_meta",
                "ordinal": 0,
                "payload": {
                    "id": "codex-session",
                    "session_id": "codex-session",
                    "cwd": "/tmp/project",
                    "context_window": 258400,
                },
            },
            {
                "type": "response_item",
                "ordinal": 1,
                "payload": {
                    "id": "old-user",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "obsolete context"}],
                },
            },
            {
                "type": "compacted",
                "ordinal": 2,
                "payload": {"replacement_history": [{"type": "compaction", "encrypted_content": "opaque"}]},
            },
            {
                "type": "response_item",
                "ordinal": 3,
                "payload": {
                    "id": "new-user",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "preserve nonlinear drift"}],
                },
            },
            {
                "type": "response_item",
                "ordinal": 4,
                "payload": {
                    "id": "assistant",
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "The constant-offset model is falsified."}],
                },
            },
            {
                "type": "event_msg",
                "ordinal": 5,
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "id": "edit",
                        "type": "FileChange",
                        "status": "completed",
                        "changes": {"clock.py": {"kind": "update", "diff": "+ drift"}},
                    },
                },
            },
            {
                "type": "response_item",
                "ordinal": 6,
                "payload": {"id": "private", "type": "reasoning", "encrypted_content": "secret"},
            },
            {
                "type": "event_msg",
                "ordinal": 7,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "model_context_window": 258400,
                        "last_token_usage": {"total_tokens": 129200},
                    },
                },
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        rendered = json.dumps(trace)
        self.assertEqual("codex", trace["platform"])
        self.assertTrue(trace["boundary"]["found"])
        self.assertNotIn("obsolete context", rendered)
        self.assertIn("nonlinear drift", rendered)
        self.assertIn("constant-offset", rendered)
        self.assertIn("clock.py", rendered)
        self.assertNotIn("secret", rendered)
        self.assertEqual(258400, trace["context"]["window_tokens"])
        self.assertEqual(50.0, trace["context"]["used_pct_observed"])
        self.assertEqual(1, trace["input_audit"]["private_reasoning_blocks_excluded"])
        self.assertIn(
            "file_change",
            {source["kind"] for episode in trace["episodes"] for source in episode["sources"]},
        )

    def test_codex_midturn_continuation_splits_at_assistant_progress(self):
        rows = [
            {"type": "session_meta", "ordinal": 0, "payload": {"id": "s", "cwd": "/tmp"}},
            {"type": "compacted", "ordinal": 1, "payload": {"replacement_history": []}},
            {
                "type": "response_item",
                "ordinal": 2,
                "payload": {
                    "id": "a1",
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Inspect the parser."}],
                },
            },
            {
                "type": "event_msg",
                "ordinal": 3,
                "payload": {
                    "type": "item_completed",
                    "item": {"id": "c1", "type": "CommandExecution", "status": "completed", "command": ["pytest"], "stdout": "ok"},
                },
            },
            {
                "type": "response_item",
                "ordinal": 4,
                "payload": {
                    "id": "a2",
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Now package the plugin."}],
                },
            },
        ]
        trace = build_trace(self.write_transcript(rows))
        self.assertEqual(2, len(trace["episodes"]))
        self.assertEqual("Inspect the parser.", trace["episodes"][0]["title"])
        self.assertEqual("Now package the plugin.", trace["episodes"][1]["title"])

    def test_prior_codex_review_contract_carries_structured_user_decisions(self):
        trace = {
            "platform": "codex",
            "source_hash": "",
            "episodes": [],
            "context": {"window_tokens": 1000, "used_tokens_observed": 500},
            "warnings": [],
        }
        review = {
            "precommit": "Do not confuse throughput with latency.",
            "items": [
                {
                    "id": "i1",
                    "title": "Active benchmark",
                    "summary": "Latency is the binding outcome.",
                    "type": "decision",
                    "status": "active",
                    "retention": "summarize",
                    "confidence": "high",
                    "next_step": "Run the cold benchmark.",
                },
                {
                    "id": "i2",
                    "title": "Discarded logs",
                    "summary": "Old output",
                    "type": "mechanical",
                    "status": "resolved",
                    "retention": "demote",
                    "confidence": "high",
                },
            ],
        }
        add_review_contract(trace, review, "cycle-1")
        self.assertEqual(2, len(trace["episodes"]))
        carried = trace["episodes"][1]["carry_forward"]
        self.assertEqual("summarize", carried["retention"])
        self.assertEqual("decision", carried["type"])
        self.assertNotIn("Discarded logs", json.dumps(trace))
        self.assertTrue(trace["source_hash"])


if __name__ == "__main__":
    unittest.main()
