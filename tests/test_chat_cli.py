import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory.ui import ThreadingHTTPServer  # noqa: E402


SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def one_goal_tree():
    return {
        "version": 1,
        "goals": [{
            "id": "g1",
            "title": "Connect this chat to goals",
            "status": "in_progress",
            "parent_goal_id": None,
            "todos": [],
            "prompt_ids": [],
        }],
    }


def two_goal_tree():
    tree = one_goal_tree()
    tree["goals"].append({
        "id": "g2",
        "title": "Ship the diff cache",
        "status": "active",
        "parent_goal_id": None,
        "todos": [],
        "prompt_ids": [],
    })
    return tree


class ChatCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "chat-state"
        self.env = mock.patch.dict(
            os.environ,
            {
                "HC_CHAT_STATE_DIR": str(self.root),
                "PYTHONPATH": str(HC_SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                # Local health probes must bypass even a hostile proxy setup.
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "",
                "no_proxy": "",
            },
        )
        self.env.start()
        # Claude keeps its own per-project transcript directory; the mirrored
        # goal document has to land beside it, not inside the plugin's state.
        self.project = Path(self.temp.name) / "claude-project"
        self.transcript = self.project / f"{SID}.jsonl"
        self.mirror = self.project / "goals-ui" / f"{SID}.md"
        import human_compact.cli as cli
        self.cli = cli
        for process in list(self.cli._DETACHED_PROCESSES):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        self.cli._DETACHED_PROCESSES.clear()

    def tearDown(self):
        for process in list(self.cli._DETACHED_PROCESSES):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:  # pragma: no cover - defensive cleanup
                    process.kill()
                    process.wait(timeout=5)
        self.cli._DETACHED_PROCESSES.clear()
        self.env.stop()
        self.temp.cleanup()

    def _register_server(self, url="http://127.0.0.1:9012/", pid=None):
        self.cli._write_server_registry(CS.paths(SID).session_dir, {
            "schema_version": 1,
            "session_id": SID,
            "pid": os.getpid() if pid is None else pid,
            "url": url,
            "started_at": 0,
        })

    def _payload(self, event, **extra):
        payload = {
            "session_id": SID,
            "hook_event_name": event,
            "cwd": "/repo",
            "transcript_path": str(self.transcript),
        }
        payload.update(extra)
        return payload

    def _hook(self, event, argv=None, **extra):
        """Run one hook and return its raw stdout."""
        output = io.StringIO()
        code = self.cli.chat_hook_main(
            list(argv or []),
            stdin=io.StringIO(json.dumps(self._payload(event, **extra))),
            stdout=output,
        )
        self.assertEqual(0, code)
        return output.getvalue()

    def _injected(self, raw):
        """The additionalContext of a hook response, or "" when it said nothing."""
        if not raw.strip():
            return ""
        return json.loads(raw)["hookSpecificOutput"]["additionalContext"]

    def _snapshot_sha(self):
        return CS.load_context_snapshot(SID).get("sha256")

    def test_prompt_hook_ingests_and_injects_cached_goal_context(self):
        goals = {
            "version": 1,
            "goals": [{
                "id": "g1",
                "title": "Connect this chat to goals",
                "status": "in_progress",
                "parent_goal_id": None,
                "todos": [{"text": "Stream plans", "done": False}],
                "prompt_ids": [],
            }],
        }
        CS.save_goals(SID, goals, {"items": []})
        CS.mark_goals_ui_invoked(SID)
        payload = {
            "session_id": SID,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Continue from the plan",
            "cwd": "/repo",
        }
        output = io.StringIO()

        code = self.cli.chat_hook_main(
            [], stdin=io.StringIO(json.dumps(payload)), stdout=output
        )

        self.assertEqual(0, code)
        response = json.loads(output.getvalue())
        self.assertEqual(
            "UserPromptSubmit",
            response["hookSpecificOutput"]["hookEventName"],
        )
        self.assertIn(
            "Connect this chat to goals",
            response["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(
            ["Continue from the plan"],
            [p["text"] for p in CS.load_prompts(SID)],
        )

    def test_stop_hook_requests_background_refresh_but_never_emits_context(self):
        CS.mark_goals_ui_invoked(SID)
        payload = {
            "session_id": SID,
            "hook_event_name": "Stop",
            "last_assistant_message": "The rebuild fix is tested.",
            "cwd": "/repo",
        }
        output = io.StringIO()
        with mock.patch.object(self.cli, "_request_chat_refresh") as refresh:
            code = self.cli.chat_hook_main(
                [], stdin=io.StringIO(json.dumps(payload)), stdout=output
            )
        self.assertEqual(0, code)
        self.assertEqual("", output.getvalue())
        refresh.assert_called_once_with(SID)
        self.assertEqual(
            "The rebuild fix is tested.", CS.load_events(SID)[0]["text"]
        )

    def test_stop_hook_ingests_but_never_analyzes_before_goals_ui(self):
        from human_compact.trajectory import chat_synth
        payload = {
            "session_id": SID,
            "hook_event_name": "Stop",
            "last_assistant_message": "The rebuild fix is tested.",
            "cwd": "/repo",
        }
        output = io.StringIO()
        with mock.patch.object(chat_synth, "spawn_refresh") as spawn:
            code = self.cli.chat_hook_main(
                [], stdin=io.StringIO(json.dumps(payload)), stdout=output
            )
        self.assertEqual(0, code)
        self.assertEqual("", output.getvalue())
        spawn.assert_not_called()
        # History must still accumulate, or /goals-ui would open onto nothing.
        self.assertEqual(
            "The rebuild fix is tested.", CS.load_events(SID)[0]["text"]
        )

    def test_stop_hook_analyzes_the_chat_once_goals_ui_is_invoked(self):
        from human_compact.trajectory import chat_synth
        CS.mark_goals_ui_invoked(SID)
        payload = {
            "session_id": SID,
            "hook_event_name": "Stop",
            "last_assistant_message": "The rebuild fix is tested.",
            "cwd": "/repo",
        }
        with mock.patch.object(chat_synth, "spawn_refresh") as spawn:
            code = self.cli.chat_hook_main(
                [], stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO()
            )
        self.assertEqual(0, code)
        spawn.assert_called_once_with(SID)

    def test_prompt_hook_injects_even_though_the_workspace_is_closed(self):
        # /goals-ui is a one-time opt-in, not a window that has to stay open:
        # the user who closed the tab still owns the goals they wrote in it.
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self.assertTrue(CS.paths(SID).goal_context.exists())
        self.assertIsNone(
            self.cli._read_server_registry(CS.paths(SID).session_dir))

        context = self._injected(
            self._hook("UserPromptSubmit", prompt="Continue from the plan"))

        self.assertIn("Connect this chat to goals", context)

    def test_prompt_hook_survives_a_corrupt_context_snapshot(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        CS.paths(SID).context_snapshot.write_text("{not json", encoding="utf-8")

        # Unreadable cache state may cost the diff, never the prompt: the
        # hook falls back to the whole file instead of raising.
        context = self._injected(
            self._hook("UserPromptSubmit", prompt="Continue from the plan"))

        self.assertIn("# Goals for this Claude chat (full file:", context)
        self.assertIn("Connect this chat to goals", context)

    def test_chat_context_active_follows_the_goals_ui_opt_in_only(self):
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        self.assertFalse(self.cli._chat_context_active(SID))
        CS.mark_goals_ui_invoked(SID)
        # No registry at all, then a registry whose process is gone: neither
        # is a reason to stop honouring the opt-in.
        self.assertTrue(self.cli._chat_context_active(SID))
        self._register_server(pid=dead.pid)
        self.assertTrue(self.cli._chat_context_active(SID))
        CS.disable_goals_ui(SID)
        self.assertFalse(self.cli._chat_context_active(SID))
        CS.mark_goals_ui_invoked(SID)
        self.assertTrue(self.cli._chat_context_active(SID))

    def test_first_prompt_sends_the_whole_file_and_mirrors_it_for_the_user(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)

        context = self._injected(self._hook("UserPromptSubmit", prompt="Go"))

        stored = CS.paths(SID).goal_context.read_text(encoding="utf-8")
        self.assertEqual(
            f"# Goals for this Claude chat (full file: {self.mirror})",
            context.splitlines()[0],
        )
        self.assertIn(stored, context)
        # The user gets a real file to open, beside Claude's own transcript.
        self.assertTrue(self.mirror.is_file())
        self.assertEqual(stored, self.mirror.read_text(encoding="utf-8"))
        self.assertEqual(0o600, self.mirror.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.mirror.parent.stat().st_mode & 0o777)
        self.assertEqual(
            CS.load_context_snapshot(SID).get("text"), stored)

    def test_an_unchanged_goal_document_is_never_re_sent(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self.assertTrue(self._hook("UserPromptSubmit", prompt="First").strip())

        # Nothing changed, so nothing is worth a single token of context.
        self.assertEqual("", self._hook("UserPromptSubmit", prompt="Second"))

    def test_a_changed_goal_document_is_sent_as_a_diff(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self._hook("UserPromptSubmit", prompt="First")
        CS.save_goals(SID, two_goal_tree(), {"items": []})

        context = self._injected(self._hook("UserPromptSubmit", prompt="Second"))

        self.assertEqual(
            f"# Goals for this chat changed since your last message "
            f"(full file: {self.mirror})",
            context.splitlines()[0],
        )
        self.assertIn("--- goals (as you last saw them)", context)
        self.assertIn("+++ goals (now)", context)
        self.assertIn("+- Ship the diff cache", context)
        # The unchanged goal is not restated inside the diff body.
        self.assertNotIn("+- Connect this chat to goals", context)
        self.assertEqual(
            CS.paths(SID).goal_context.read_text(encoding="utf-8"),
            CS.load_context_snapshot(SID).get("text"),
        )

    def test_session_start_resends_the_whole_file_after_a_compaction(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self._hook("UserPromptSubmit", prompt="First")
        CS.save_goals(SID, two_goal_tree(), {"items": []})

        # SessionStart fires on compaction, when the earlier injection is gone
        # from the model's context and a diff against it would be a lie.
        context = self._injected(self._hook("SessionStart", source="compact"))

        stored = CS.paths(SID).goal_context.read_text(encoding="utf-8")
        self.assertIn("# Goals for this Claude chat (full file:", context)
        self.assertIn(stored, context)
        self.assertNotIn("changed since your last message", context)
        self.assertEqual(stored, CS.load_context_snapshot(SID).get("text"))

    def test_a_subagent_gets_the_whole_file_without_eating_the_next_diff(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self._hook("UserPromptSubmit", prompt="First")
        CS.save_goals(SID, two_goal_tree(), {"items": []})
        before = self._snapshot_sha()

        raw = self._hook("SubagentStart", agent_type="Explore")
        response = json.loads(raw)

        self.assertEqual(
            "SubagentStart", response["hookSpecificOutput"]["hookEventName"])
        # A subagent starts with an empty context, so it needs everything.
        self.assertIn("# Goals for this Claude chat (full file:",
                      self._injected(raw))
        self.assertIn("Ship the diff cache", self._injected(raw))
        # ...and its private read must not spend the main conversation's diff.
        self.assertEqual(before, self._snapshot_sha())
        self.assertIn("+- Ship the diff cache",
                      self._injected(self._hook("UserPromptSubmit", prompt="Next")))

    def test_post_tool_batch_injects_the_delta_without_ingesting_again(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self._hook("UserPromptSubmit", prompt="First")
        CS.save_goals(SID, two_goal_tree(), {"items": []})

        with mock.patch.object(CS, "ingest_hook") as ingest:
            raw = self._hook("PostToolBatch", argv=["--inject-only"])

        # The async entry already ingested this batch; the synchronous one is
        # on the 5s prompt budget and may only speak.
        ingest.assert_not_called()
        response = json.loads(raw)
        self.assertEqual(
            "PostToolBatch", response["hookSpecificOutput"]["hookEventName"])
        self.assertIn("+- Ship the diff cache", self._injected(raw))

    def test_the_ingesting_post_tool_batch_entry_stays_silent(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self._hook("UserPromptSubmit", prompt="First")
        CS.save_goals(SID, two_goal_tree(), {"items": []})

        # This entry runs async and its stdout is discarded; consuming the
        # delta here would silently starve the synchronous entry.
        self.assertEqual("", self._hook("PostToolBatch"))
        self.assertIn("+- Ship the diff cache",
                      self._injected(self._hook("PostToolBatch",
                                                argv=["--inject-only"])))

    def test_goals_ui_disable_turns_the_chat_off_until_it_is_opened_again(self):
        from human_compact.trajectory import chat_synth
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self._hook("UserPromptSubmit", prompt="First")

        with mock.patch.object(self.cli, "chat_ui_main") as launch:
            raw = self._hook("UserPromptExpansion", command_args="disable",
                             command_name="goals-ui")

        launch.assert_not_called()
        response = json.loads(raw)
        self.assertEqual("block", response["decision"])
        self.assertIn("disabled", response["reason"])
        self.assertNotIn("hookSpecificOutput", response)
        self.assertFalse(CS.goals_ui_active(SID))

        CS.save_goals(SID, two_goal_tree(), {"items": []})
        self.assertEqual("", self._hook("UserPromptSubmit", prompt="Second"))
        with mock.patch.object(chat_synth, "spawn_refresh") as spawn:
            self._hook("Stop", last_assistant_message="done")
        spawn.assert_not_called()

        self._register_server()
        with (mock.patch.object(self.cli, "_healthy_chat_server",
                                return_value=True),
              mock.patch.object(self.cli, "_request_chat_refresh"),
              mock.patch("webbrowser.open"),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(0, self.cli.chat_ui_main(
                ["--session", SID, "--cwd", "/repo"]))

        self.assertTrue(CS.goals_ui_active(SID))
        # Re-opening starts over: the model was told nothing while it was off.
        self.assertIn("# Goals for this Claude chat (full file:",
                      self._injected(self._hook("UserPromptSubmit", prompt="Third")))

    def test_chat_ui_marks_the_session_as_goals_ui_invoked(self):
        self._register_server()
        with (mock.patch.object(self.cli, "_healthy_chat_server", return_value=True),
              mock.patch.object(self.cli, "_request_chat_refresh"),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(
                0,
                self.cli.chat_ui_main(
                    ["--session", SID, "--cwd", "/repo", "--no-open"]
                ),
            )
        first = CS.load_manifest(SID).get("goals_ui_invoked_at")
        self.assertTrue(first)
        self.assertTrue(CS.goals_ui_invoked(SID))

        # _now() has one-second granularity, so back-date the stamp: only a
        # real first-write-wins guard leaves an older timestamp standing.
        earlier = "2020-01-01T00:00:00+00:00"
        seeded = CS.load_manifest(SID)
        seeded["goals_ui_invoked_at"] = earlier
        CS._atomic_json(CS.paths(SID).manifest, seeded)

        CS.mark_goals_ui_invoked(SID)
        self.assertEqual(earlier, CS.load_manifest(SID).get("goals_ui_invoked_at"))

    def test_ui_expansion_hook_launches_without_skill_shell_execution(self):
        payload = {
            "session_id": SID,
            "hook_event_name": "UserPromptExpansion",
            "cwd": "/stable/project",
        }
        output = io.StringIO()
        def launched(_args):
            print("http://127.0.0.1:9012/")
            return 0
        with mock.patch.object(self.cli, "chat_ui_main", side_effect=launched) as launch:
            code = self.cli.chat_hook_main(
                [], stdin=io.StringIO(json.dumps(payload)), stdout=output
            )
        self.assertEqual(0, code)
        launch.assert_called_once_with(
            ["--session", SID, "--cwd", "/stable/project"]
        )
        # Nothing is handed to the model on the way past, so the launch cannot
        # cost a turn even when it succeeds.
        self.assertNotIn("hookSpecificOutput", json.loads(output.getvalue()))

    def test_ui_expansion_ends_the_turn_with_the_url_and_never_calls_claude(self):
        payload = {
            "session_id": SID,
            "hook_event_name": "UserPromptExpansion",
            "cwd": "/stable/project",
        }
        self._register_server()
        output = io.StringIO()
        # The real launcher runs here: opening the workspace must still opt
        # this chat in, and must still be the only thing that speaks.
        with (mock.patch.object(self.cli, "_healthy_chat_server",
                                return_value=True),
              mock.patch.object(self.cli, "_request_chat_refresh"),
              mock.patch("webbrowser.open") as opened):
            code = self.cli.chat_hook_main(
                [], stdin=io.StringIO(json.dumps(payload)), stdout=output
            )
        self.assertEqual(0, code)
        # `decision: block` ends the turn with no model call and shows `reason`
        # to the user; that line is the whole of what /goals-ui says.
        self.assertEqual(
            {"decision": "block", "reason": "goals-ui: http://127.0.0.1:9012/"},
            json.loads(output.getvalue()),
        )
        opened.assert_called_once_with("http://127.0.0.1:9012/")
        self.assertTrue(CS.goals_ui_invoked(SID))

    def test_ui_expansion_blocks_instead_of_claiming_success_on_launch_failure(self):
        payload = {
            "session_id": SID,
            "hook_event_name": "UserPromptExpansion",
            "cwd": "/stable/project",
        }
        output = io.StringIO()
        with mock.patch.object(
            self.cli, "chat_ui_main", side_effect=SystemExit("server failed")
        ):
            self.cli.chat_hook_main(
                [], stdin=io.StringIO(json.dumps(payload)), stdout=output
            )
        response = json.loads(output.getvalue())
        self.assertEqual("block", response["decision"])
        self.assertIn("server failed", response["reason"])

    def test_ui_expansion_blocks_when_session_state_cannot_initialize(self):
        payload = {
            "session_id": "../invalid",
            "hook_event_name": "UserPromptExpansion",
            "cwd": "/stable/project",
        }
        output = io.StringIO()
        self.cli.chat_hook_main(
            [], stdin=io.StringIO(json.dumps(payload)), stdout=output
        )
        response = json.loads(output.getvalue())
        self.assertEqual("block", response["decision"])
        self.assertIn("initialize chat state", response["reason"])

    def test_inference_guard_makes_hook_inert(self):
        payload = {
            "session_id": SID,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "must not recurse",
        }
        with mock.patch.dict(os.environ, {"HC_CHAT_INFERENCE": "1"}):
            self.assertEqual(
                0,
                self.cli.chat_hook_main(
                    [], stdin=io.StringIO(json.dumps(payload)), stdout=io.StringIO()
                ),
            )
        self.assertEqual([], CS.load_events(SID))

    def test_health_rejects_non_object_json(self):
        class ListHealth(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = b"[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ListHealth)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            record = {"pid": os.getpid(),
                      "url": f"http://127.0.0.1:{server.server_port}/"}
            self.assertFalse(self.cli._healthy_chat_server(record, SID))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_chat_ui_daemon_returns_then_reuses_exact_session_server(self):
        first = io.StringIO()
        second = io.StringIO()
        pid = None
        try:
            with (mock.patch.object(self.cli, "_request_chat_refresh"),
                  contextlib.redirect_stdout(first)):
                self.assertEqual(
                    0,
                    self.cli.chat_ui_main(
                        ["--session", SID, "--cwd", "/repo", "--no-open"]
                    ),
                )
            record = self.cli._read_server_registry(CS.paths(SID).session_dir)
            self.assertTrue(self.cli._healthy_chat_server(record, SID))
            pid = record["pid"]

            with (mock.patch.object(self.cli, "_request_chat_refresh"),
                  contextlib.redirect_stdout(second)):
                self.assertEqual(
                    0,
                    self.cli.chat_ui_main(
                        ["--session", SID, "--cwd", "/repo", "--no-open"]
                    ),
                )
            reused = self.cli._read_server_registry(CS.paths(SID).session_dir)
            self.assertEqual(pid, reused["pid"])
            self.assertEqual(first.getvalue(), second.getvalue())
            self.assertTrue(first.getvalue().startswith("http://127.0.0.1:"))
        finally:
            if pid and self.cli._pid_alive(pid):
                os.kill(pid, signal.SIGTERM)
                process = next(
                    (p for p in self.cli._DETACHED_PROCESSES if p.pid == pid), None
                )
                if process is not None:
                    process.wait(timeout=5)
                else:
                    try:
                        os.waitpid(pid, 0)
                    except ChildProcessError:
                        pass

    def test_refresh_worker_hands_off_remaining_bounded_evidence(self):
        from human_compact.trajectory import chat_synth

        order = []
        states = [
            {"last_analyzed_ordinal": 3, "status": "running"},
            {"last_analyzed_ordinal": 9, "status": "pending"},
        ]
        with (
            mock.patch.object(
                chat_synth.CS, "get_analyzer_state", side_effect=states
            ),
            mock.patch.object(
                chat_synth, "refresh",
                side_effect=lambda _sid: order.append("refresh") or {
                    "status": "updated", "changes": ["goal + bounded"]
                },
            ),
            mock.patch.object(
                chat_synth, "clear_worker_record",
                side_effect=lambda _sid: order.append("clear"),
            ),
            mock.patch.object(
                chat_synth, "spawn_refresh",
                side_effect=lambda _sid: order.append("spawn") or {"status": "spawned"},
            ) as spawn,
        ):
            code = self.cli.chat_refresh_main(["--session", SID])

        self.assertEqual(0, code)
        self.assertEqual(["refresh", "clear", "spawn"], order)
        spawn.assert_called_once_with(SID)

    def test_refresh_worker_does_not_loop_without_cursor_progress(self):
        from human_compact.trajectory import chat_synth

        with (
            mock.patch.object(
                chat_synth.CS, "get_analyzer_state",
                side_effect=[
                    {"last_analyzed_ordinal": 3, "status": "running"},
                    {"last_analyzed_ordinal": 3, "status": "pending"},
                ],
            ),
            mock.patch.object(
                chat_synth, "refresh", return_value={"status": "coalesced"}
            ),
            mock.patch.object(chat_synth, "clear_worker_record"),
            mock.patch.object(chat_synth, "spawn_refresh") as spawn,
        ):
            code = self.cli.chat_refresh_main(["--session", SID])

        self.assertEqual(0, code)
        spawn.assert_not_called()

    def test_refresh_worker_hands_off_exit_race_without_cursor_progress(self):
        from human_compact.trajectory import chat_synth

        with (
            mock.patch.object(
                chat_synth.CS, "get_analyzer_state",
                side_effect=[
                    {"last_analyzed_ordinal": 3, "status": "running"},
                    {"last_analyzed_ordinal": 3, "status": "pending"},
                ],
            ),
            mock.patch.object(
                chat_synth, "refresh", return_value={
                    "status": "updated", "needs_handoff": True,
                },
            ),
            mock.patch.object(chat_synth, "clear_worker_record"),
            mock.patch.object(
                chat_synth, "spawn_refresh", return_value={"status": "spawned"}
            ) as spawn,
        ):
            code = self.cli.chat_refresh_main(["--session", SID])

        self.assertEqual(0, code)
        spawn.assert_called_once_with(SID)


if __name__ == "__main__":
    unittest.main()
