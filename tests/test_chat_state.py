import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402


SID = "11111111-2222-4333-8444-555555555555"


def user_record(text, *, uuid="u1", prompt_id="p1", **extra):
    value = {
        "type": "user",
        "uuid": uuid,
        "promptId": prompt_id,
        "timestamp": "2026-08-12T08:00:00Z",
        "cwd": "/repo",
        "isSidechain": False,
        "origin": {"kind": "human"},
        "promptSource": {"kind": "typed"},
        "message": {"role": "user", "content": text},
    }
    value.update(extra)
    return value


def assistant_record(blocks, *, uuid="a1"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-08-12T08:01:00Z",
        "cwd": "/repo",
        "isSidechain": False,
        "message": {"role": "assistant", "content": blocks},
    }


def tool_result_record(tool_id="tool-1", text="tests passed", *, uuid="r1"):
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-08-12T08:02:00Z",
        "cwd": "/repo",
        "isSidechain": False,
        "sourceToolAssistantUUID": "a1",
        "toolUseResult": {"stdout": text, "stderr": ""},
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": text,
                }
            ],
        },
    }


def write_jsonl(path, records, trailing_newline=True):
    data = "\n".join(json.dumps(record) for record in records)
    if trailing_newline:
        data += "\n"
    path.write_text(data, encoding="utf-8")


class ChatStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / "state"
        self.transcript = Path(self.temp.name) / "chat.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_session_state_is_owner_only(self):
        self.transcript.write_text(
            json.dumps(user_record("private conversation")) + "\n",
            encoding="utf-8",
        )
        CS.ingest_transcript(SID, self.transcript, root=self.base)
        p = CS.paths(SID, self.base)
        self.assertEqual(0o700, p.session_dir.stat().st_mode & 0o777)
        for artifact in (p.manifest, p.events, p.prompts):
            self.assertEqual(0o600, artifact.stat().st_mode & 0o777, artifact)

    def test_partial_line_waits_for_completion_and_reopen_persists(self):
        first = user_record("First prompt")
        second = user_record("Second prompt", uuid="u2", prompt_id="p2")
        raw_first = json.dumps(first) + "\n"
        raw_second = json.dumps(second)
        self.transcript.write_text(raw_first + raw_second[:20], encoding="utf-8")

        one = CS.ingest_transcript(SID, self.transcript, root=self.base)
        self.assertEqual(1, one.appended)
        self.assertEqual(["First prompt"], [p["text"] for p in CS.load_prompts(SID, self.base)])

        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(raw_second[20:] + "\n")
        two = CS.ingest_transcript(SID, self.transcript, root=self.base)
        self.assertEqual(1, two.appended)
        self.assertEqual([1, 2], [e["ordinal"] for e in CS.load_events(SID, self.base)])

        # A new loader invocation sees durable files and ingestion is idempotent.
        three = CS.ingest_transcript(SID, self.transcript, root=self.base)
        self.assertEqual(0, three.appended)
        self.assertEqual(2, CS.load_manifest(SID, self.base)["event_count"])
        self.assertEqual("/repo", CS.load_manifest(SID, self.base)["cwd"])

    def test_truncate_rewrite_replays_but_deduplicates_stable_records(self):
        first = user_record("Keep me")
        second = user_record("Replace me", uuid="u2", prompt_id="p2")
        write_jsonl(self.transcript, [first, second])
        CS.ingest_transcript(SID, self.transcript, root=self.base)

        replacement = user_record("New tail", uuid="u3", prompt_id="p3")
        write_jsonl(self.transcript, [first, replacement])
        result = CS.ingest_transcript(SID, self.transcript, root=self.base)

        self.assertTrue(result.rewound)
        self.assertEqual(1, result.appended)
        self.assertEqual(
            ["Keep me", "Replace me", "New tail"],
            [p["text"] for p in CS.load_prompts(SID, self.base)],
        )
        ids = [event["id"] for event in CS.load_events(SID, self.base)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_hook_lag_reconciles_prompt_and_stop_with_canonical_jsonl(self):
        self.transcript.touch()
        hook_base = {
            "session_id": SID,
            "transcript_path": str(self.transcript),
            "cwd": "/repo",
        }
        CS.ingest_hook(
            {**hook_base, "hook_event_name": "UserPromptSubmit", "prompt": "Build it"},
            root=self.base,
        )
        CS.ingest_hook(
            {
                **hook_base,
                "hook_event_name": "Stop",
                "last_assistant_message": "Implemented and tested.",
            },
            root=self.base,
        )
        before = CS.load_events(SID, self.base)
        self.assertEqual(2, len(before))
        durable_prompt_id = CS.load_prompts(SID, self.base)[0]["id"]

        write_jsonl(
            self.transcript,
            [
                user_record("Build it"),
                assistant_record([{"type": "text", "text": "Implemented and tested."}]),
            ],
        )
        result = CS.ingest_transcript(SID, self.transcript, root=self.base)
        after = CS.load_events(SID, self.base)

        self.assertEqual(0, result.appended)
        self.assertEqual(2, len(after))
        self.assertEqual(durable_prompt_id, CS.load_prompts(SID, self.base)[0]["id"])
        self.assertEqual("prompt:p1", after[0]["canonical_id"])
        self.assertTrue(all(e["source"]["type"] == "claude_jsonl" for e in after))

    def test_hook_dedupes_newly_flushed_turn_but_preserves_repeated_prompt(self):
        write_jsonl(self.transcript, [user_record("continue")])
        hook = {
            "session_id": SID,
            "transcript_path": str(self.transcript),
            "cwd": "/repo",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "continue",
        }
        first = CS.ingest_hook(hook, root=self.base)
        self.assertEqual(1, first.total_events)

        # The next identical submission is a distinct human act even though
        # its canonical JSONL row has not flushed yet.
        second = CS.ingest_hook(hook, root=self.base)
        self.assertEqual(2, second.total_events)
        prompts = CS.load_prompts(SID, self.base)
        self.assertEqual(["continue", "continue"], [p["text"] for p in prompts])
        self.assertEqual(2, len({p["id"] for p in prompts}))

    def test_visible_assistant_plan_tool_and_result_are_goal_evidence(self):
        write_jsonl(
            self.transcript,
            [
                user_record("Ship the session UI"),
                assistant_record(
                    [
                        {"type": "text", "text": "I am mapping the backend now."},
                        {
                            "type": "tool_use",
                            "id": "plan-1",
                            "name": "update_plan",
                            "input": {
                                "plan": [
                                    {"step": "Debug rebuild", "status": "completed"},
                                    {"step": "Connect chat UI", "status": "in_progress"},
                                ]
                            },
                        },
                        {"type": "thinking", "thinking": "private scratch work"},
                    ]
                ),
                tool_result_record("plan-1", "plan stored"),
            ],
        )
        CS.ingest_transcript(SID, self.transcript, root=self.base)
        events = CS.load_events(SID, self.base)

        self.assertEqual(
            [
                "human_prompt",
                "assistant_message",
                "plan_update",
                "assistant_thinking",
                "tool_result",
            ],
            [event["kind"] for event in events],
        )
        plan = next(event for event in events if event["kind"] == "plan_update")
        self.assertIn("Debug rebuild", plan["text"])
        self.assertTrue(plan["usable_for_goals"])
        result = next(event for event in events if event["kind"] == "tool_result")
        self.assertEqual("plan stored", result["text"])
        self.assertEqual("result:plan-1", result["id"])
        thought = next(event for event in events if event["kind"] == "assistant_thinking")
        self.assertEqual("", thought["text"])
        self.assertFalse(thought["usable_for_goals"])

    def test_prompt_projection_excludes_tool_results_synthetic_users_and_launcher(self):
        notification = user_record(
            "<task-notification>worker finished</task-notification>",
            uuid="n1",
            prompt_id="n1",
            origin={"kind": "task-notification"},
            promptSource={"kind": "system"},
        )
        write_jsonl(
            self.transcript,
            [
                user_record("/hc-ui", uuid="launcher", prompt_id="launcher"),
                user_record(
                    "<command-name>/hc-ui</command-name>\n"
                    "<command-message>hc-ui</command-message>",
                    uuid="wrapped-launcher",
                    prompt_id="wrapped-launcher",
                ),
                tool_result_record(text="not a human prompt"),
                notification,
                user_record("Assignable", uuid="human", prompt_id="human"),
            ],
        )
        CS.ingest_transcript(SID, self.transcript, root=self.base)
        prompts = CS.load_prompts(SID, self.base)

        self.assertEqual(1, len(prompts))
        self.assertEqual(
            {"id", "role", "text", "created_at", "ordinal"}, set(prompts[0])
        )
        self.assertEqual("Assignable", prompts[0]["text"])
        self.assertEqual("user", prompts[0]["role"])
        kinds = {event["kind"] for event in CS.load_events(SID, self.base)}
        self.assertIn("tool_result", kinds)
        self.assertIn("task_notification", kinds)

    def test_post_tool_batch_captures_plan_and_result_before_transcript_flush(self):
        payload = {
            "session_id": SID,
            "hook_event_name": "PostToolBatch",
            "cwd": "/repo",
            "tool_calls": [
                {
                    "tool_name": "update_plan",
                    "tool_use_id": "plan-1",
                    "tool_input": {
                        "plan": [{"step": "Connect current chat", "status": "in_progress"}]
                    },
                    "tool_response": [{"type": "text", "text": "plan stored"}],
                }
            ],
        }
        result = CS.ingest_hook(payload, root=self.base)
        self.assertEqual(2, result.appended)
        events = CS.load_events(SID, self.base)
        self.assertEqual(["plan_update", "tool_result"], [e["kind"] for e in events])
        self.assertIn("Connect current chat", events[0]["text"])
        self.assertEqual("plan stored", events[1]["text"])

        # Canonical transcript records upgrade the same ids rather than adding
        # duplicate evidence for the analyzer.
        write_jsonl(
            self.transcript,
            [
                assistant_record(
                    [
                        {
                            "type": "tool_use",
                            "id": "plan-1",
                            "name": "update_plan",
                            "input": payload["tool_calls"][0]["tool_input"],
                        }
                    ]
                ),
                tool_result_record("plan-1", "plan stored"),
            ],
        )
        replay = CS.ingest_transcript(SID, self.transcript, root=self.base)
        self.assertEqual(0, replay.appended)
        canonical = CS.load_events(SID, self.base)
        self.assertEqual(2, len(canonical))
        self.assertTrue(
            all(e["source"]["type"] == "claude_jsonl" for e in canonical)
        )

    def test_task_hooks_are_persisted_for_goal_completion_evidence(self):
        created = {
            "session_id": SID,
            "hook_event_name": "TaskCreated",
            "task_subject": "Wire prompt assignments",
            "task_description": "Many-to-many goal relation",
            "cwd": "/repo",
        }
        completed = {
            **created,
            "hook_event_name": "TaskCompleted",
        }
        CS.ingest_hook(created, root=self.base)
        CS.ingest_hook(completed, root=self.base)
        events = CS.load_events(SID, self.base)
        self.assertEqual(["task_created", "task_completed"], [e["kind"] for e in events])
        self.assertTrue(all(e["usable_for_goals"] for e in events))

    def test_concurrent_hook_ingestion_has_no_lost_updates_or_ordinal_gaps(self):
        barrier = threading.Barrier(9)
        failures = []

        def ingest(index):
            try:
                barrier.wait()
                CS.ingest_hook(
                    {
                        "session_id": SID,
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": f"prompt {index}",
                        "cwd": "/repo",
                    },
                    root=self.base,
                )
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=ingest, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual([], failures)
        events = CS.load_events(SID, self.base)
        self.assertEqual(8, len(events))
        self.assertEqual(list(range(1, 9)), [event["ordinal"] for event in events])
        self.assertEqual(8, len(CS.load_prompts(SID, self.base)))

    def test_analyzer_cursor_goal_context_and_compare_and_swap(self):
        self.transcript.touch()
        CS.ingest_hook(
            {
                "session_id": SID,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Persist this requirement",
                "transcript_path": str(self.transcript),
            },
            root=self.base,
        )
        pending = CS.get_analyzer_state(SID, self.base)
        self.assertEqual("pending", pending["status"])
        self.assertEqual(1, pending["requested_ordinal"])
        self.assertEqual(1, len(CS.new_events_since(SID, 0, self.base)))
        self.assertEqual([], CS.new_events_since(SID, 1, self.base))

        goals = {
            "version": 1,
            "goals": [
                {
                    "id": "g1",
                    "title": "Connect this chat",
                    "status": "active",
                    "parent_goal_id": None,
                    "todos": [],
                    "important_item_ids": [],
                    "prompt_ids": [CS.load_prompts(SID, self.base)[0]["id"]],
                    "description": "Keep the scoped UI synchronized",
                    "notes": "Preserve browser-authored relationships",
                    "priority": "high",
                },
                {
                    "id": "g2",
                    "title": "Debug the rebuild timeout",
                    "status": "completed",
                    "parent_goal_id": None,
                    "todos": [],
                    "important_item_ids": [],
                    "prompt_ids": [],
                }
            ],
        }
        initial_revision = CS.goal_revision(SID, self.base)
        self.assertTrue(
            CS.save_goals(
                SID,
                goals,
                {"items": []},
                root=self.base,
                expected_revision=initial_revision,
            )
        )
        self.assertFalse(
            CS.save_goals(
                SID,
                {"version": 1, "goals": []},
                {"items": []},
                root=self.base,
                expected_revision=initial_revision,
            )
        )
        context = CS.paths(SID, self.base).goal_context.read_text(encoding="utf-8")
        self.assertIn("Connect this chat", context)
        self.assertIn("USER PROMPT: Persist this requirement", context)
        self.assertIn("DESCRIPTION: Keep the scoped UI synchronized", context)
        self.assertIn("USER NOTES: Preserve browser-authored relationships", context)
        self.assertIn("PRIORITY: high", context)
        self.assertIn("Recent inactive goals:", context)
        self.assertIn("Debug the rebuild timeout [completed]", context)

        state = CS.set_analyzer_state(
            SID,
            last_analyzed_ordinal=1,
            status="idle",
            error=None,
            root=self.base,
        )
        self.assertEqual(1, state["last_analyzed_ordinal"])
        self.assertIsNone(state["error"])

    def test_session_id_rejects_traversal(self):
        for bad in ("", "../escape", "a/b", ".", " space", "x" * 201):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CS.paths(bad, self.base)
        self.assertFalse((Path(self.temp.name) / "escape").exists())


if __name__ == "__main__":
    unittest.main()
