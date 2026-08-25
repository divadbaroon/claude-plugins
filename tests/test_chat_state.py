import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


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
        # A stand-in for Claude's own per-project transcript, so mirrored
        # goal documents never land in the developer's real home.
        self.chat_jsonl = str(
            Path(self.temp.name) / "claude-project" / f"{SID}.jsonl")

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_session_state_is_owner_only(self):
        parent = Path(self.temp.name)
        parent.chmod(0o755)
        self.transcript.write_text(
            json.dumps(user_record("private conversation")) + "\n",
            encoding="utf-8",
        )
        CS.ingest_transcript(SID, self.transcript, root=self.base)
        p = CS.paths(SID, self.base)
        self.assertEqual(0o755, parent.stat().st_mode & 0o777)
        self.assertEqual(0o700, p.base.stat().st_mode & 0o777)
        self.assertEqual(0o700, p.session_dir.stat().st_mode & 0o777)
        for artifact in (p.manifest, p.events, p.prompts):
            self.assertEqual(0o600, artifact.stat().st_mode & 0o777, artifact)

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_fresh_default_chat_hook_secures_the_full_state_path(self):
        vault = Path(self.temp.name) / ".claude-vault"
        payload = {
            "session_id": SID,
            "hook_event_name": "SessionStart",
            "cwd": "/repo",
        }
        with mock.patch.dict(os.environ, {
            "CLAUDE_VAULT_DIR": str(vault),
            "HC_CHAT_STATE_DIR": "",
        }):
            CS.ingest_hook(payload)
            p = CS.paths(SID)

        for directory in (vault, vault / "chat-sessions", p.session_dir):
            self.assertEqual(0o700, directory.stat().st_mode & 0o777, directory)
        for artifact in (p.manifest, p.events, p.prompts):
            self.assertEqual(0o600, artifact.stat().st_mode & 0o777, artifact)

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_default_chat_hook_refuses_symlinked_vault_without_writing_target(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(mode=0o755)
        vault = Path(self.temp.name) / ".claude-vault"
        vault.symlink_to(outside, target_is_directory=True)
        payload = {
            "session_id": SID,
            "hook_event_name": "SessionStart",
            "cwd": "/repo",
        }
        with mock.patch.dict(os.environ, {
            "CLAUDE_VAULT_DIR": str(vault),
            "HC_CHAT_STATE_DIR": "",
        }), self.assertRaisesRegex(RuntimeError, "private state"):
            CS.ingest_hook(payload)

        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual(0o755, outside.stat().st_mode & 0o777)

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
                user_record("/bart", uuid="launcher", prompt_id="launcher"),
                user_record(
                    "<command-name>/bart</command-name>\n"
                    "<command-message>goals-ui</command-message>",
                    uuid="wrapped-launcher",
                    prompt_id="wrapped-launcher",
                ),
                user_record(
                    "<command-name>/compact</command-name>\n"
                    "<command-message>compact</command-message>\n"
                    "<command-args></command-args>",
                    uuid="wrapped-compact",
                    prompt_id="wrapped-compact",
                ),
                user_record(
                    "/compact", uuid="plain-compact", prompt_id="plain-compact"
                ),
                tool_result_record(text="not a human prompt"),
                notification,
                user_record(
                    "Explain <command-name> as markup",
                    uuid="markup-discussion",
                    prompt_id="markup-discussion",
                ),
                user_record("Assignable", uuid="human", prompt_id="human"),
            ],
        )
        CS.ingest_transcript(SID, self.transcript, root=self.base)
        prompts = CS.load_prompts(SID, self.base)

        self.assertEqual(2, len(prompts))
        self.assertEqual(
            {"id", "role", "text", "created_at", "ordinal"}, set(prompts[0])
        )
        self.assertEqual(
            ["Explain <command-name> as markup", "Assignable"],
            [prompt["text"] for prompt in prompts],
        )
        self.assertTrue(all(prompt["role"] == "user" for prompt in prompts))
        kinds = {event["kind"] for event in CS.load_events(SID, self.base)}
        self.assertIn("tool_result", kinds)
        self.assertIn("task_notification", kinds)
        launchers = [
            event for event in CS.load_events(SID, self.base)
            if "bart" in event.get("text", "")
        ]
        self.assertEqual(2, len(launchers))
        self.assertTrue(all(not event["usable_for_goals"] for event in launchers))
        compact_commands = [
            event for event in CS.load_events(SID, self.base)
            if "compact" in event.get("text", "")
        ]
        self.assertEqual(2, len(compact_commands))
        self.assertTrue(
            all(not event["usable_for_goals"] for event in compact_commands)
        )

    def test_prompt_load_filters_command_envelopes_from_legacy_projection(self):
        prompt_file = CS.paths(SID, self.base).prompts
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text(json.dumps({"prompts": [
            {"id": "old-command", "role": "user", "text":
             "<command-name>/compact</command-name>\n"
             "<command-message>compact</command-message>\n"
             "<command-args></command-args>"},
            {"id": "real", "role": "user", "text": "Keep this message"},
        ]}))

        self.assertEqual(
            ["Keep this message"],
            [prompt["text"] for prompt in CS.load_prompts(SID, self.base)],
        )

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
        self.assertIn(
            "  - USER NOTES:\n    Preserve browser-authored relationships", context)
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

    def _chat_with_prompts(self):
        write_jsonl(self.transcript, [
            user_record("wire the goal to the task list", uuid="u1", prompt_id="p1"),
            user_record("unrelated aside", uuid="u2", prompt_id="p2"),
        ])
        CS.ingest_transcript(SID, self.transcript, root=self.base)
        return [p["id"] for p in CS.load_prompts(SID, self.base)]

    def test_todo_rows_persist_in_their_own_json_file_apart_from_the_notes(self):
        # The rail's rows go to todos.json; goals.json (where the notes
        # live) carries none of them. A TODO is never stored in, derived
        # from, or parsed out of the notes document.
        rows = [{"id": "t0000000a", "text": "ship it", "depth": 0,
                 "status": "building", "question": ""},
                {"id": "t0000000b", "text": "and test", "depth": 1,
                 "status": "", "question": ""}]
        CS.save_goals(SID, {"version": 1, "goals": [
            {"id": "g1", "title": "Ship the router", "todos": [],
             "notes": "# Decisions\n- keep sqlite\n",
             "todo_items": [dict(r) for r in rows]}]},
            {"items": []}, self.base)
        p = CS.paths(SID, self.base)
        stored = json.loads(p.goals.read_text(encoding="utf-8"))
        self.assertEqual([], stored["goals"][0]["todo_items"])
        held = json.loads(p.todos.read_text(encoding="utf-8"))
        self.assertEqual({"g1": rows}, held["todos"])
        # And the load joins them back, rows over the same goal.
        goal = CS.load_goals(SID, self.base)[0]["goals"][0]
        self.assertEqual(rows, goal["todo_items"])
        self.assertEqual("# Decisions\n- keep sqlite\n", goal["notes"])

    def test_a_store_from_before_the_split_still_loads_its_rows(self):
        # goals.json written by an older build carries the rows inline and
        # no todos.json exists; the load takes them as they are.
        rows = [{"id": "t0000000a", "text": "ship it", "depth": 0,
                 "status": "", "question": ""}]
        p = CS.paths(SID, self.base)
        p.session_dir.mkdir(parents=True, exist_ok=True)
        p.goals.write_text(json.dumps({"version": 1, "goals": [
            {"id": "g1", "title": "Ship the router",
             "todo_items": [dict(r) for r in rows]}]}), encoding="utf-8")
        self.assertFalse(p.todos.exists())
        goal = CS.load_goals(SID, self.base)[0]["goals"][0]
        self.assertEqual(rows, goal["todo_items"])

    def test_prompts_cited_as_evidence_attach_to_their_goal(self):
        first, second = self._chat_with_prompts()
        CS.save_goals(SID, {"version": 1, "goals": [
            {"id": "g1", "title": "Connect the goal", "evidence_ids": [first],
             "todos": []}]}, {"items": []}, self.base)
        goal = CS.load_goals(SID, self.base)[0]["goals"][0]
        self.assertEqual([first], goal["prompt_ids"])
        self.assertEqual([first], goal["auto_prompt_ids"])
        self.assertNotIn(second, goal["prompt_ids"])

    def test_evidence_that_is_not_a_prompt_never_becomes_a_link(self):
        self._chat_with_prompts()
        CS.save_goals(SID, {"version": 1, "goals": [
            {"id": "g1", "title": "Connect the goal", "todos": [],
             "evidence_ids": ["tool:abc", "result:def", "prompt:not-in-chat"]}]},
            {"items": []}, self.base)
        goal = CS.load_goals(SID, self.base)[0]["goals"][0]
        self.assertEqual([], goal["prompt_ids"])

    def test_a_detached_prompt_is_not_re_linked_by_later_analysis(self):
        first, _ = self._chat_with_prompts()
        base = {"id": "g1", "title": "Connect the goal", "todos": [],
                "evidence_ids": [first]}
        CS.save_goals(SID, {"version": 1, "goals": [dict(base)]},
                      {"items": []}, self.base)

        goals, important = CS.load_goals(SID, self.base)
        goal = goals["goals"][0]
        goal["prompt_ids"] = []
        goal["auto_prompt_ids"] = []
        goal["detached_prompt_ids"] = [first]     # what the UI records on detach
        CS.save_goals(SID, goals, important, self.base)

        # The analyzer keeps citing the same evidence on every later pass.
        goals, important = CS.load_goals(SID, self.base)
        goals["goals"][0]["evidence_ids"] = [first, "tool:xyz"]
        CS.save_goals(SID, goals, important, self.base)
        self.assertEqual([], CS.load_goals(SID, self.base)[0]["goals"][0]["prompt_ids"])

    def test_a_user_link_survives_and_is_not_labelled_automatic(self):
        first, second = self._chat_with_prompts()
        CS.save_goals(SID, {"version": 1, "goals": [
            {"id": "g1", "title": "Connect the goal", "todos": [],
             "evidence_ids": [first], "prompt_ids": [second]}]},
            {"items": []}, self.base)
        goal = CS.load_goals(SID, self.base)[0]["goals"][0]
        self.assertEqual([second, first], goal["prompt_ids"])
        self.assertEqual([first], goal["auto_prompt_ids"])

    def test_ui_launcher_detection_spans_current_and_legacy_spellings(self):
        for text in ("/bart", "/bart now", "\\goals-ui", "bart",
                     "<command-name>/bart</command-name>",
                     "/hc-ui", "<command-name>/hc-ui</command-name>"):
            with self.subTest(launcher=text):
                self.assertTrue(CS._is_goals_ui_launcher(text))
        for text in ("goal", "open the goal ui please", "/bart-ish", ""):
            with self.subTest(other=text):
                self.assertFalse(CS._is_goals_ui_launcher(text))

    def test_session_id_rejects_traversal(self):
        for bad in ("", "../escape", "a/b", ".", " space", "x" * 201):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CS.paths(bad, self.base)
        self.assertFalse((Path(self.temp.name) / "escape").exists())

    def test_goal_context_carries_the_whole_notes_document(self):
        self.transcript.touch()
        CS.ingest_hook(
            {
                "session_id": SID,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Write the doc model",
                "transcript_path": str(self.transcript),
            },
            root=self.base,
        )
        body = "\n".join(f"- decision {n}" for n in range(120))
        CS.save_goals(
            SID,
            {"version": 1, "goals": [{
                "id": "g1", "title": "Write the doc model", "status": "active",
                "parent_goal_id": None, "todos": [], "important_item_ids": [],
                "prompt_ids": [],
                "notes": f"# Objective\n\n# Decisions\n{body}\n\n# Blockers\n",
            }]},
            {"items": []},
            root=self.base,
        )

        context = CS.paths(SID, self.base).goal_context.read_text(encoding="utf-8")
        self.assertIn("# Decisions", context)
        self.assertIn("- decision 0", context)
        self.assertIn("- decision 119", context)
        self.assertGreater(len(body), 280)
        self.assertNotIn("# Objective", context)
        self.assertNotIn("# Blockers", context)

    def test_goal_context_names_every_attached_source_by_kind(self):
        self.transcript.touch()
        CS.save_goals(
            SID,
            {"version": 1, "goals": [
                {"id": "g1", "title": "With sources", "status": "active",
                 "parent_goal_id": None, "todos": [], "important_item_ids": [],
                 "prompt_ids": [], "priority": "high",
                 "sources": [
                     {"id": "s1", "type": "local", "label": "~/proj"},
                     {"id": "s2", "type": "github", "label": "octo/repo"},
                     {"id": "s3", "type": "doc",
                      "label": "https://example.com/spec"},
                 ]},
                {"id": "g2", "title": "Without sources", "status": "active",
                 "parent_goal_id": None, "todos": [], "important_item_ids": [],
                 "prompt_ids": []},
            ]},
            {"items": []},
            root=self.base,
        )

        context = CS.paths(SID, self.base).goal_context.read_text(encoding="utf-8")
        self.assertIn("  - SOURCE (local): ~/proj", context)
        self.assertIn("  - SOURCE (github): octo/repo", context)
        self.assertIn("  - SOURCE (doc): https://example.com/spec", context)
        self.assertLess(context.index("PRIORITY: high"),
                        context.index("SOURCE (local)"))
        # The goal that has none contributes no SOURCE line at all.
        self.assertEqual(3, context.count("- SOURCE ("))

    def test_goal_context_caps_a_long_source_list_at_six(self):
        self.transcript.touch()
        CS.save_goals(
            SID,
            {"version": 1, "goals": [{
                "id": "g1", "title": "Many sources", "status": "active",
                "parent_goal_id": None, "todos": [], "important_item_ids": [],
                "prompt_ids": [],
                "sources": [{"id": f"s{n}", "type": "local",
                             "label": f"~/p{n}"} for n in range(10)],
            }]},
            {"items": []},
            root=self.base,
        )

        context = CS.paths(SID, self.base).goal_context.read_text(encoding="utf-8")
        self.assertEqual(6, context.count("- SOURCE ("))
        self.assertIn("- SOURCE (local): ~/p5", context)
        self.assertNotIn("~/p6", context)

    def test_goal_context_is_never_truncated_by_a_character_budget(self):
        # Every cap here was ours, not the host's. A goal the user wrote 4,000
        # characters into is not served by handing the model the first 280.
        description = "why this matters " * 240
        notes = "\n".join(f"- decision {n}" for n in range(400))
        prompt_text = "rebuild the pipeline " * 200
        goals = {"version": 1, "goals": [{
            "id": f"g{n}", "title": f"Goal {n}", "status": "active",
            "parent_goal_id": None, "todos": [],
            "description": description,
            "notes": f"# Decisions\n{notes}\n",
            "prompt_ids": [f"p{i}" for i in range(6)],
            "important_item_ids": [f"i{i}" for i in range(5)],
        } for n in range(4)]}
        important = {"items": [{"id": f"i{i}", "text": f"important {i} " * 60}
                               for i in range(5)]}
        prompts = [{"id": f"p{i}", "text": f"{i} {prompt_text}"}
                   for i in range(6)]

        text = CS._goal_context_text(SID, goals, important, prompts)

        self.assertGreater(len(text), 8_000)
        self.assertIn(description.strip(), text)
        self.assertIn("- decision 0", text)
        self.assertIn("- decision 399", text)
        for i in range(6):
            self.assertIn(f"USER PROMPT: {i} {prompt_text}".strip(), text)
        for i in range(5):
            self.assertIn(f"IMPORTANT: {important['items'][i]['text']}", text)
        self.assertIn("Goal 3", text)

    def test_goals_ui_stays_on_until_it_is_disabled_and_can_be_re_enabled(self):
        self.assertFalse(CS.goals_ui_active(SID, self.base))
        CS.mark_goals_ui_invoked(SID, self.base)
        self.assertTrue(CS.goals_ui_active(SID, self.base))
        opened_at = CS.load_manifest(SID, self.base)["goals_ui_invoked_at"]

        CS.disable_goals_ui(SID, self.base)
        self.assertFalse(CS.goals_ui_active(SID, self.base))
        self.assertTrue(CS.goals_ui_invoked(SID, self.base))

        CS.mark_goals_ui_invoked(SID, self.base)
        self.assertTrue(CS.goals_ui_active(SID, self.base))
        # Re-opening clears the disable without rewriting the first opt-in.
        self.assertEqual(opened_at,
                         CS.load_manifest(SID, self.base)["goals_ui_invoked_at"])
        self.assertIsNone(
            CS.load_manifest(SID, self.base).get("goals_ui_disabled_at"))

    def test_disabling_forgets_the_snapshot_so_re_opening_resends_everything(self):
        CS.save_goals(SID, {"version": 1, "goals": []}, {"items": []},
                      root=self.base)
        CS.mark_goals_ui_invoked(SID, self.base)
        CS.render_context_injection(SID, "delta", transcript_path=self.chat_jsonl,
                                    root=self.base)
        self.assertTrue(CS.load_context_snapshot(SID, self.base))

        CS.disable_goals_ui(SID, self.base)

        # Claude was told nothing while the feature was off, so a diff against
        # what it "last saw" would be against text it no longer has.
        self.assertEqual({}, CS.load_context_snapshot(SID, self.base))

    def test_context_snapshot_is_owner_only_and_survives_corruption(self):
        CS.save_goals(SID, {"version": 1, "goals": []}, {"items": []},
                      root=self.base)
        CS.save_context_snapshot(SID, "hello", self.base)
        snapshot = CS.paths(SID, self.base).context_snapshot

        self.assertEqual(0o600, snapshot.stat().st_mode & 0o777)
        self.assertEqual("hello", CS.load_context_snapshot(SID, self.base)["text"])
        snapshot.write_text("[]", encoding="utf-8")
        self.assertEqual({}, CS.load_context_snapshot(SID, self.base))
        CS.clear_context_snapshot(SID, self.base)
        self.assertFalse(snapshot.exists())
        CS.clear_context_snapshot(SID, self.base)

    def test_mirror_lands_beside_the_transcript_and_only_rewrites_on_change(self):
        CS.save_goals(SID, {"version": 1, "goals": []}, {"items": []},
                      root=self.base)
        transcript = Path(self.temp.name) / "claude-project" / f"{SID}.jsonl"

        target = CS.mirror_goal_context(SID, str(transcript), "/repo",
                                        root=self.base)

        stored = CS.paths(SID, self.base).goal_context.read_text(encoding="utf-8")
        self.assertEqual(transcript.parent / "goals-ui" / f"{SID}.md", target)
        self.assertEqual(stored, target.read_text(encoding="utf-8"))
        self.assertEqual(0o600, target.stat().st_mode & 0o777)
        self.assertEqual(0o700, target.parent.stat().st_mode & 0o777)

        # Unchanged goals must not rewrite the file on every prompt.
        before = target.stat().st_mtime_ns
        self.assertEqual(target, CS.mirror_goal_context(
            SID, str(transcript), "/repo", root=self.base))
        self.assertEqual(before, target.stat().st_mtime_ns)

    def test_mirror_falls_back_to_a_project_directory_claude_already_made(self):
        CS.save_goals(SID, {"version": 1, "goals": []}, {"items": []},
                      root=self.base)
        home = Path(self.temp.name) / "home"
        cwd = Path(self.temp.name) / "work space"
        # Claude encodes a project by hyphenating its absolute path.
        project = (home / ".claude" / "projects"
                   / re.sub(r"[^A-Za-z0-9_-]", "-", str(cwd)))

        with mock.patch("pathlib.Path.home", return_value=home):
            # Nothing of Claude's is there yet, so there is nothing to sit
            # beside: inventing the directory would be litter, not a mirror.
            self.assertIsNone(
                CS.mirror_goal_context(SID, None, str(cwd), root=self.base))
            project.mkdir(parents=True)
            target = CS.mirror_goal_context(SID, None, str(cwd), root=self.base)

        self.assertEqual(project / "goals-ui" / f"{SID}.md", target)
        self.assertTrue(target.is_file())

    def test_mirror_never_raises_when_it_cannot_write(self):
        CS.save_goals(SID, {"version": 1, "goals": []}, {"items": []},
                      root=self.base)
        not_a_directory = Path(self.temp.name) / "occupied"
        not_a_directory.write_text("in the way", encoding="utf-8")

        self.assertIsNone(CS.mirror_goal_context(
            SID, str(not_a_directory / "chat.jsonl"), None, root=self.base))
        # No location to write to at all is a silent no-op, not a crash.
        self.assertIsNone(CS.mirror_goal_context(SID, None, None, root=self.base))

    def test_delta_falls_back_to_the_whole_file_when_the_diff_is_not_smaller(self):
        CS.mark_goals_ui_invoked(SID, self.base)
        CS.save_goals(
            SID,
            {"version": 1, "goals": [{
                "id": f"g{n}", "title": f"Goal {n}", "status": "active",
                "parent_goal_id": None, "todos": [], "prompt_ids": [],
            } for n in range(30)]},
            {"items": []},
            root=self.base,
        )
        first = CS.render_context_injection(
            SID, "full", transcript_path=self.chat_jsonl, root=self.base)
        self.assertIn("# Goals for this Claude chat (full file:", first)

        # Every goal renamed: the diff restates the file twice over, so the
        # file itself is the cheaper thing to send.
        CS.save_goals(
            SID,
            {"version": 1, "goals": [{
                "id": f"g{n}", "title": f"Renamed goal {n}", "status": "active",
                "parent_goal_id": None, "todos": [], "prompt_ids": [],
            } for n in range(30)]},
            {"items": []},
            root=self.base,
        )
        second = CS.render_context_injection(
            SID, "delta", transcript_path=self.chat_jsonl, root=self.base)

        self.assertIn("# Goals for this Claude chat (full file:", second)
        self.assertNotIn("changed since your last message", second)
        self.assertEqual(
            CS.paths(SID, self.base).goal_context.read_text(encoding="utf-8"),
            CS.load_context_snapshot(SID, self.base)["text"],
        )

    def test_a_remembered_render_is_the_only_one_that_moves_the_snapshot(self):
        CS.save_goals(SID, {"version": 1, "goals": []}, {"items": []},
                      root=self.base)
        CS.render_context_injection(SID, "full", transcript_path=self.chat_jsonl,
                                    root=self.base)
        before = CS.load_context_snapshot(SID, self.base)["sha256"]
        CS.save_goals(
            SID,
            {"version": 1, "goals": [{
                "id": "g1", "title": "New", "status": "active",
                "parent_goal_id": None, "todos": [], "prompt_ids": [],
            }]},
            {"items": []},
            root=self.base,
        )

        text = CS.render_context_injection(
            SID, "full", transcript_path=self.chat_jsonl, root=self.base,
            remember=False)

        self.assertIn("New", text)
        self.assertEqual(before, CS.load_context_snapshot(SID, self.base)["sha256"])

    # --- notices: what the workspace is allowed to tell the reader ---------

    def test_a_notice_store_keeps_only_the_newest_twenty_newest_last(self):
        # A workspace opened after a long session should not replay every
        # turn that ever finished, and the ones worth showing are the last.
        for n in range(25):
            CS.add_notice(SID, "session_stopped", f"turn {n}", self.base)

        rows = CS.load_notices(SID, self.base)

        self.assertEqual(20, len(rows))
        self.assertEqual([f"turn {n}" for n in range(5, 25)],
                         [row["detail"] for row in rows])
        self.assertEqual(20, len({row["id"] for row in rows}))
        for row in rows:
            self.assertEqual("session_stopped", row["kind"])
            self.assertRegex(row["id"], r"^[0-9a-f]+$")
            self.assertRegex(row["at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_a_notice_is_timestamped_finely_enough_to_beat_a_page_load(self):
        # The browser shows a notice only when it is newer than the moment
        # the page opened. Second-resolution timestamps round a notice
        # written just after that moment back before it, and the banner then
        # never appears at all.
        row = CS.add_notice(SID, "session_stopped", "", self.base)
        self.assertRegex(row["at"], r"T\d{2}:\d{2}:\d{2}\.\d{3}")

    def test_a_notice_detail_is_one_scannable_line(self):
        long = "x" * 400
        row = CS.add_notice(
            SID, "session_stopped", f"  wrapped\n  over\tlines {long}", self.base)

        self.assertEqual(160, len(row["detail"]))
        self.assertTrue(row["detail"].startswith("wrapped over lines xxx"))
        self.assertNotIn("\n", row["detail"])
        self.assertEqual(row["detail"], CS.load_notices(SID, self.base)[0]["detail"])

    def test_a_kind_with_no_copy_is_not_recorded(self):
        # The banner says one of three sentences. A kind nobody wrote a
        # sentence for would reach the reader as a blank or a raw enum.
        self.assertIsNone(CS.add_notice(SID, "compacted", "whatever", self.base))
        self.assertEqual([], CS.load_notices(SID, self.base))

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_notices_are_owner_only_and_survive_corruption(self):
        CS.add_notice(SID, "session_ended", "clear", self.base)
        p = CS.paths(SID, self.base)
        self.assertEqual(0o600, p.notices.stat().st_mode & 0o777)

        p.notices.write_text("{not json", encoding="utf-8")
        self.assertEqual([], CS.load_notices(SID, self.base))
        # And the store repairs itself rather than staying unreadable.
        CS.add_notice(SID, "session_ended", "again", self.base)
        self.assertEqual(["again"],
                         [row["detail"] for row in CS.load_notices(SID, self.base)])

    def test_a_missing_store_reads_as_nothing_to_say(self):
        self.assertEqual([], CS.load_notices(SID, self.base))

    def test_stopping_a_turn_records_what_claude_last_said(self):
        CS.ingest_hook({
            "session_id": SID,
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "last_assistant_message": "  Done.\nTests   pass.  ",
        }, root=self.base)

        rows = CS.load_notices(SID, self.base)

        self.assertEqual(1, len(rows))
        self.assertEqual("session_stopped", rows[0]["kind"])
        self.assertEqual("Done. Tests pass.", rows[0]["detail"])

    def test_a_returning_subagent_names_itself_without_ingesting_its_words(self):
        # SubagentStop.last_assistant_message is the *subagent's* final
        # response, not the conversation's. It may name the agent in a
        # notice; it may not enter this session's event stream as something
        # Claude said to the user.
        CS.ingest_hook({
            "session_id": SID,
            "hook_event_name": "SubagentStop",
            "cwd": "/repo",
            "agent_id": "def456",
            "agent_type": "Explore",
            "last_assistant_message": "Analysis complete. Found 3 potential issues",
        }, root=self.base)

        rows = CS.load_notices(SID, self.base)

        self.assertEqual(1, len(rows))
        self.assertEqual("subagent_returned", rows[0]["kind"])
        self.assertEqual("Explore: Analysis complete. Found 3 potential issues",
                         rows[0]["detail"])
        self.assertEqual([], [event for event in CS.load_events(SID, self.base)
                              if "Analysis complete" in event["text"]])

    def test_a_nameless_subagent_still_returns(self):
        CS.ingest_hook({
            "session_id": SID,
            "hook_event_name": "SubagentStop",
            "cwd": "/repo",
            "last_assistant_message": "done",
        }, root=self.base)
        CS.ingest_hook({
            "session_id": SID,
            "hook_event_name": "SubagentStop",
            "cwd": "/repo",
            "agent_id": "agent-77",
        }, root=self.base)

        self.assertEqual([("subagent_returned", "done"),
                          ("subagent_returned", "agent-77")],
                         [(row["kind"], row["detail"])
                          for row in CS.load_notices(SID, self.base)])

    def test_a_session_ending_records_the_reason_it_gave(self):
        CS.ingest_hook({
            "session_id": SID,
            "hook_event_name": "SessionEnd",
            "cwd": "/repo",
            "reason": "clear",
        }, root=self.base)

        self.assertEqual([("session_ended", "clear")],
                         [(row["kind"], row["detail"])
                          for row in CS.load_notices(SID, self.base)])

    def test_an_ordinary_hook_says_nothing(self):
        CS.ingest_hook({
            "session_id": SID,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/repo",
            "prompt": "keep going",
        }, root=self.base)

        self.assertEqual([], CS.load_notices(SID, self.base))

    def test_a_notice_that_cannot_be_written_does_not_cost_the_ingest(self):
        # A hook may never fail over a banner.
        CS.paths(SID, self.base).session_dir.mkdir(parents=True, exist_ok=True)
        CS.paths(SID, self.base).notices.mkdir()

        result = CS.ingest_hook({
            "session_id": SID,
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "last_assistant_message": "still ingested",
        }, root=self.base)

        self.assertEqual(1, result.appended)
        self.assertEqual([], CS.load_notices(SID, self.base))
        self.assertIn("still ingested",
                      [event["text"] for event in CS.load_events(SID, self.base)])



if __name__ == "__main__":
    unittest.main()
