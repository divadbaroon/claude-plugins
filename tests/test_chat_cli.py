import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact import cli  # noqa: E402
from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
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
                # A first /bart asks the account for a web setup; the
                # account lives under this root, so an empty one here keeps
                # the developer's real machine token out of the tests.
                "HUMAN_COMPACT_HOME": str(Path(self.temp.name) / "managed"),
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

    def _hold_session_lock(self):
        """Hold the session lock the way another live process holds it."""
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import time; time.sleep(30)"])
        self.addCleanup(self._release_session_lock, holder)
        lock_dir = CS.paths(SID).lock_dir
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "owner.json").write_text(
            json.dumps({"pid": holder.pid, "created_at": "2026-08-17T00:00:00+00:00"}),
            encoding="utf-8")
        return holder

    def _release_session_lock(self, holder):
        shutil.rmtree(CS.paths(SID).lock_dir, ignore_errors=True)
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)

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
        # History must still accumulate, or /bart would open onto nothing.
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
        # /bart is a one-time opt-in, not a window that has to stay open:
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

        with mock.patch.object(CS, "ingest_hook") as ingest:
            raw = self._hook("SubagentStart", argv=["--inject-only"],
                             agent_type="Explore")
        response = json.loads(raw)

        # Injecting into a subagent needs neither the ingest nor the
        # agent-run observation, so it does not pay for them.
        ingest.assert_not_called()
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

    def test_a_locked_session_costs_the_injection_not_the_turn(self):
        CS.save_goals(SID, one_goal_tree(), {"items": []})
        CS.mark_goals_ui_invoked(SID)
        self._hook("UserPromptSubmit", prompt="First")
        CS.save_goals(SID, two_goal_tree(), {"items": []})
        holder = self._hold_session_lock()

        started = time.monotonic()
        raw = self._hook("PostToolBatch", argv=["--inject-only"])
        elapsed = time.monotonic() - started

        # The async entry ingesting this same batch holds the session lock.
        # Blocking on it would spend the model's whole 5s hook budget.
        self.assertLess(elapsed, 1.0, f"hook stalled {elapsed:.2f}s on the lock")
        self.assertEqual("", raw)

        self._release_session_lock(holder)
        # Nothing was recorded as seen, so the change is still pending and
        # the next injection restates it rather than losing it.
        self.assertIn("+- Ship the diff cache", self._injected(
            self._hook("PostToolBatch", argv=["--inject-only"])))

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
                             command_name="bart")

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
        # to the user; that line is the whole of what /bart says.
        self.assertEqual(
            {"decision": "block", "reason": "bart: http://127.0.0.1:9012/"},
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

    def test_a_server_is_out_of_date_only_once_its_code_moves_past_it(self):
        stamp = self.cli._package_code_stamp()
        self.assertTrue(stamp)
        self.assertTrue(
            self.cli._server_outran_its_code({"started_at": stamp - 60}))
        self.assertFalse(
            self.cli._server_outran_its_code({"started_at": stamp + 60}))
        # A record with no start time is not evidence of anything.
        self.assertFalse(self.cli._server_outran_its_code({"started_at": 0}))
        self.assertFalse(self.cli._server_outran_its_code(None))

    def test_reopening_replaces_a_server_older_than_the_code_it_serves(self):
        pids = []
        try:
            for _ in range(2):
                with (mock.patch.object(self.cli, "_request_chat_refresh"),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertEqual(0, self.cli.chat_ui_main(
                        ["--session", SID, "--cwd", "/repo", "--no-open"]))
                record = self.cli._read_server_registry(CS.paths(SID).session_dir)
                pids.append(record["pid"])
                # Back-date the running server to before this package was last
                # edited: that is what an open workspace becomes the moment a
                # build touches the code behind it.
                aged = dict(record)
                aged["started_at"] = self.cli._package_code_stamp() - 60
                self.cli._write_server_registry(CS.paths(SID).session_dir, aged)

            self.assertNotEqual(pids[0], pids[1])
            # The replaced one is stopped, not merely left behind: a second
            # server on the old port would keep answering the old tab.
            self.assertFalse(self.cli._pid_alive(pids[0]))
            fresh = self.cli._read_server_registry(CS.paths(SID).session_dir)
            self.assertEqual(pids[1], fresh["pid"])
            self.assertTrue(self.cli._healthy_chat_server(fresh, SID))
        finally:
            for pid in pids:
                if not self.cli._pid_alive(pid):
                    continue
                os.kill(pid, signal.SIGTERM)
            for process in list(self.cli._DETACHED_PROCESSES):
                if process.pid in pids:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()

    def test_a_running_build_keeps_the_workspace_it_is_reading(self):
        self._register_server()
        aged = self.cli._read_server_registry(CS.paths(SID).session_dir)
        aged["started_at"] = self.cli._package_code_stamp() - 60
        self.cli._write_server_registry(CS.paths(SID).session_dir, aged)
        builds = CS.paths(SID).session_dir / "builds"
        builds.mkdir(parents=True, exist_ok=True)
        (builds / "g1.json").write_text(json.dumps({
            "goal_id": "g1", "status": "running", "pid": os.getpid(),
        }), encoding="utf-8")
        self.assertTrue(self.cli._chat_server_is_building(CS.paths(SID).session_dir))

        output = io.StringIO()
        with (mock.patch.object(self.cli, "_healthy_chat_server", return_value=True),
              mock.patch.object(self.cli, "_stop_chat_server") as stopped,
              mock.patch.object(self.cli, "_request_chat_refresh"),
              mock.patch("webbrowser.open")):
            self.assertEqual(0, self.cli.chat_hook_main(
                [], stdin=io.StringIO(json.dumps({
                    "session_id": SID,
                    "hook_event_name": "UserPromptExpansion",
                    "cwd": "/repo",
                })), stdout=output))

        stopped.assert_not_called()
        reason = json.loads(output.getvalue())["reason"]
        self.assertTrue(reason.startswith("bart: http://127.0.0.1:9012/"))
        self.assertIn("a build is in flight", reason)

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


class OneServerPerProjectTests(unittest.TestCase):
    """A project has many chats and one workspace between them.

    The registry used to live in the chat's own directory, so every chat
    started a server of its own -- three chats in one project meant three
    ports, three windows, and three different trees.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.environ["HC_CHAT_STATE_DIR"] = str(self.root)
        self.addCleanup(os.environ.pop, "HC_CHAT_STATE_DIR", None)
        self.home = str(self.root / "acme")
        self.og = "aaaaaaaa-1111-4ccc-8ddd-eeeeeeeeeeee"
        self.other = "bbbbbbbb-2222-4ccc-8ddd-eeeeeeeeeeee"
        for sid in (self.og, self.other):
            CS.ingest_hook({"session_id": sid, "hook_event_name": "SessionStart",
                            "cwd": self.home}, root=self.root)
        CS.save_goals(self.og, {"version": 1, "goals": [
            {"id": "g1", "title": "the project's work", "status": "active",
             "parent_goal_id": None}]}, {"items": []}, root=self.root)

    def test_chats_in_one_project_resolve_to_one_store(self):
        for sid in (self.og, self.other):
            CS.bind_project(sid, self.home, root=self.root)
        self.assertEqual(CS.tree_session(self.og, self.root),
                         CS.tree_session(self.other, self.root))

    def test_the_registry_a_second_chat_reads_is_the_first_chat_s(self):
        # The launcher looks for a running server where the project's store
        # is, so a second chat finds the first one's registration.
        for sid in (self.og, self.other):
            CS.bind_project(sid, self.home, root=self.root)
        shared = CS.tree_session(self.other, self.root)
        cli._write_server_registry(CS.paths(shared, self.root).session_dir, {
            "schema_version": 1, "session_id": shared, "pid": os.getpid(),
            "url": "http://127.0.0.1:9/", "started_at": 0})
        seen = cli._read_server_registry(
            CS.paths(CS.tree_session(self.og, self.root), self.root).session_dir)
        self.assertEqual("http://127.0.0.1:9/", (seen or {}).get("url"),
                         "the second chat must find the first chat's server")

    def test_an_unbound_chat_still_gets_a_workspace_of_its_own(self):
        # It has no project yet -- it is about to be asked which -- so it
        # cannot join one.
        lone = "cccccccc-3333-4ccc-8ddd-eeeeeeeeeeee"
        CS.ingest_hook({"session_id": lone, "hook_event_name": "SessionStart",
                        "cwd": str(self.root / "elsewhere")}, root=self.root)
        self.assertEqual(lone, CS.tree_session(lone, self.root))


class ProjectOwnsItsServerTests(unittest.TestCase):
    """Opening a workspace for a chat that has one open joins it.

    The registry used to live in the chat's own directory, so the question
    asked at launch was "is a server running for ME?" -- which every chat in
    a project answered no to, and three chats meant three ports showing
    three trees. The question is now asked of the project.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.environ["HC_CHAT_STATE_DIR"] = str(self.root)
        self.addCleanup(os.environ.pop, "HC_CHAT_STATE_DIR", None)
        self.home = str(self.root / "acme")
        Path(self.home).mkdir(parents=True, exist_ok=True)
        self.og = "aaaaaaaa-4444-4ccc-8ddd-eeeeeeeeeeee"
        self.other = "bbbbbbbb-5555-4ccc-8ddd-eeeeeeeeeeee"
        for sid in (self.og, self.other):
            CS.ingest_hook({"session_id": sid, "hook_event_name": "SessionStart",
                            "cwd": self.home}, root=self.root)
        CS.save_goals(self.og, {"version": 1, "goals": [
            {"id": "g1", "title": "the work", "status": "active",
             "parent_goal_id": None}]}, {"items": []}, root=self.root)
        for sid in (self.og, self.other):
            CS.bind_project(sid, self.home, root=self.root)

    def _open(self, sid):
        buf = io.StringIO()
        with (mock.patch.object(cli, "_request_chat_refresh"),
              contextlib.redirect_stdout(buf)):
            cli.chat_ui_main(["--session", sid, "--cwd", self.home, "--no-open"])
        return buf.getvalue().strip().splitlines()[-1]

    def test_a_second_chat_opens_the_workspace_the_first_one_started(self):
        running = {"schema_version": 1, "session_id": CS.tree_session(self.og, self.root),
                   "pid": os.getpid(), "url": "http://127.0.0.1:9/", "started_at": 0}
        PS.set_server_record(self.root, self.home, running)
        with (mock.patch.object(cli, "_healthy_chat_server", return_value=True),
              mock.patch.object(cli, "subprocess") as spawned):
            self.assertEqual("http://127.0.0.1:9/", self._open(self.other))
        spawned.Popen.assert_not_called()

    def test_a_project_with_nothing_running_starts_one_and_records_it(self):
        def stood_up(*a, **k):
            PS.set_server_record(self.root, self.home, {
                "schema_version": 1,
                "session_id": CS.tree_session(self.og, self.root),
                "pid": os.getpid(), "url": "http://127.0.0.1:11/",
                "started_at": 0})
            return mock.DEFAULT
        healthy = iter([False, True, True, True, True])
        with (mock.patch.object(cli, "_healthy_chat_server",
                                side_effect=lambda *a, **k: next(healthy, True)),
              mock.patch.object(cli, "_request_chat_refresh"),
              mock.patch.object(cli.subprocess, "Popen",
                                side_effect=stood_up) as spawn):
            spawn.return_value.poll.return_value = None
            url = self._open(self.og)
        self.assertEqual("http://127.0.0.1:11/", url)
        self.assertEqual("http://127.0.0.1:11/",
                         PS.server_record(self.root, self.home)["url"])

    def test_the_workspace_a_project_starts_serves_the_project_s_store(self):
        # Not the chat that happened to ask for it: a chat bound into a
        # project must be handed that project's goals, not its own blank tree.
        seen = {}

        def stood_up(command, *a, **k):
            seen["command"] = list(command)
            PS.set_server_record(self.root, self.home, {
                "schema_version": 1,
                "session_id": CS.tree_session(self.other, self.root),
                "pid": os.getpid(), "url": "http://127.0.0.1:12/",
                "started_at": 0})
            return mock.DEFAULT
        healthy = iter([False])
        with (mock.patch.object(cli, "_healthy_chat_server",
                                side_effect=lambda *a, **k: next(healthy, True)),
              mock.patch.object(cli, "_request_chat_refresh"),
              mock.patch.object(cli.subprocess, "Popen",
                                side_effect=stood_up) as spawn):
            spawn.return_value.poll.return_value = None
            self._open(self.other)
        self.assertIn(self.og, seen["command"],
                      "the server must be told to serve the project's store")

    def test_a_stale_record_is_replaced_rather_than_opened(self):
        PS.set_server_record(self.root, self.home, {
            "schema_version": 1, "session_id": self.og, "pid": 999999,
            "url": "http://127.0.0.1:13/", "started_at": 0})

        def stood_up(*a, **k):
            PS.set_server_record(self.root, self.home, {
                "schema_version": 1,
                "session_id": CS.tree_session(self.og, self.root),
                "pid": os.getpid(), "url": "http://127.0.0.1:14/",
                "started_at": 0})
            return mock.DEFAULT
        healthy = iter([False])
        with (mock.patch.object(cli, "_healthy_chat_server",
                                side_effect=lambda *a, **k: next(healthy, True)),
              mock.patch.object(cli, "_request_chat_refresh"),
              mock.patch.object(cli.subprocess, "Popen",
                                side_effect=stood_up) as spawn):
            spawn.return_value.poll.return_value = None
            self.assertEqual("http://127.0.0.1:14/", self._open(self.og))
