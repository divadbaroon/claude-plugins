import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402


SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class ChatCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "chat-state"
        self.env = mock.patch.dict(
            os.environ,
            {
                "HC_CHAT_STATE_DIR": str(self.root),
                "PYTHONPATH": str(HC_SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
        )
        self.env.start()
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
        response = json.loads(output.getvalue())
        self.assertEqual(
            "hc-ui opened for this chat at http://127.0.0.1:9012/",
            response["hookSpecificOutput"]["additionalContext"],
        )
        launch.assert_called_once_with(
            ["--session", SID, "--cwd", "/stable/project"]
        )

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


if __name__ == "__main__":
    unittest.main()
