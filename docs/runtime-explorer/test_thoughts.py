"""Boundary tests for persisted questions, delivery, and answer attribution."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from serve import Bridge, Handler, ThreadingHTTPServer
from thought_store import AnswerReader, Store, queue_prompt


def question(n=1, parent=None, text="Does resuming update Notes?"):
    return {"id": "q_" + f"{n:032x}", "question": text, "parent_id": parent,
            "anchor": {"quote": "New Notes are not attached.", "prefix": "", "suffix": ""},
            "source": {"id": "same-subgoal-notes", "title": "Same subgoal", "text": "New Notes are not attached.", "url": "#question=same-subgoal-notes"}}


class ThoughtsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.log = self.base / "rollout-thread-test.jsonl"
        self.log.write_text("")
        self.store = Store(self.base / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, payload, kind="event_msg"):
        with self.log.open("a") as stream:
            stream.write(json.dumps({"type": kind, "payload": payload}) + "\n")

    def marked_user(self, key):
        self.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"[THOUGHT_QUESTION:{key}]\nQuestion"}]}, "response_item")

    def test_idempotency_and_child_persist_across_reopen(self):
        parent = self.store.create(question())
        self.store.create(question())
        child = self.store.create(question(2, parent["id"]))
        reopened = Store(self.base / "state")
        self.assertEqual(len(reopened.all()), 2)
        self.assertEqual(reopened.get(child["id"])["parent_id"], parent["id"])
        with self.assertRaises(ValueError):
            self.store.create(question(text="Another question with reused id"))
        with self.assertRaises(ValueError):
            self.store.create(question(3, question(99)["id"]))

    def test_passage_whitespace_survives_storage(self):
        raw = question()
        raw["anchor"] = {"quote": " a phrase ", "prefix": "before\n", "suffix": " after "}
        self.assertEqual(self.store.create(raw)["anchor"], raw["anchor"])

    def test_queue_serializes_and_uncertain_requires_manual_retry(self):
        a, b = self.store.create(question()), self.store.create(question(2))
        self.assertEqual(self.store.claim_next()["id"], a["id"])
        self.assertIsNone(self.store.claim_next())
        self.store.status(a["id"], "uncertain")
        self.assertIsNone(self.store.claim_next())
        self.store.retry(a["id"])
        self.assertEqual(self.store.claim_next()["id"], a["id"])
        self.store.complete(a["id"], "The transcript is reused, not refreshed.")
        self.assertEqual(self.store.claim_next()["id"], b["id"])

    def test_only_correlated_final_is_saved_and_not_overwritten(self):
        a = self.store.create(question())
        reader = AnswerReader(self.store, self.log)
        self.append({"type": "task_complete", "last_agent_message": "Unrelated answer", "turn_id": "old"})
        reader.poll()
        self.assertEqual(self.store.get(a["id"])["answer"], "")
        self.append({"type": "task_started", "turn_id": "new"})
        self.marked_user(a["id"])
        self.append({"type": "agent_message", "message": "Intermediate commentary"})
        self.append({"type": "task_complete", "last_agent_message": f"[THOUGHT_ANSWER:{a['id']}]\nNo. Notes are not refreshed.", "turn_id": "new"})
        reader.poll()
        self.assertEqual(self.store.get(a["id"])["answer"], "No. Notes are not refreshed.")
        self.assertEqual(self.store.get(a["id"])["turn_id"], "new")
        self.store.complete(a["id"], "Duplicate event")
        self.assertEqual(self.store.get(a["id"])["answer"], "No. Notes are not refreshed.")

    def test_restart_retains_pending_correlation_and_partial_line(self):
        a = self.store.create(question())
        reader = AnswerReader(self.store, self.log)
        self.marked_user(a["id"])
        reader.poll()
        line = json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Answer after restart", "turn_id": "turn"}})
        with self.log.open("a") as stream:
            stream.write(line[:20])
        reader.poll()
        with self.log.open("a") as stream:
            stream.write(line[20:] + "\n")
        AnswerReader(self.store, self.log).poll()
        self.assertEqual(self.store.get(a["id"])["answer"], "Answer after restart")

    def test_unrelated_user_input_breaks_implicit_attribution(self):
        a = self.store.create(question())
        reader = AnswerReader(self.store, self.log)
        self.marked_user(a["id"])
        self.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Actually, do something unrelated"}]}, "response_item")
        self.append({"type": "task_complete", "last_agent_message": "Answer to unrelated request"})
        reader.poll()
        self.assertEqual(self.store.get(a["id"])["answer"], "")
        self.assertEqual(self.store.get(a["id"])["status"], "uncertain")

    def test_aborted_and_empty_turns_are_recoverable(self):
        a, b = self.store.create(question()), self.store.create(question(2))
        reader = AnswerReader(self.store, self.log)
        self.store.claim_next()
        self.marked_user(a["id"])
        self.append({"type": "turn_aborted", "reason": "interrupted"})
        reader.poll()
        self.assertEqual(self.store.get(a["id"])["status"], "failed")
        self.assertEqual(self.store.claim_next()["id"], b["id"])
        self.marked_user(b["id"])
        self.append({"type": "task_complete", "last_agent_message": ""})
        reader.poll()
        self.assertEqual(self.store.get(b["id"])["status"], "failed")
        self.assertEqual(self.store.retry(a["id"])["status"], "waiting")
        self.marked_user(a["id"])
        self.append({"type": "task_complete", "last_agent_message": f"[THOUGHT_ANSWER:{a['id']}]\n"})
        reader.poll()
        self.assertEqual(self.store.get(a["id"])["status"], "failed")

    def test_lost_turn_correlation_can_be_retried_or_completed_by_marker(self):
        a = self.store.create(question())
        reader = AnswerReader(self.store, self.log)
        self.marked_user(a["id"])
        self.append({"type": "task_started", "turn_id": "replacement"})
        reader.poll()
        self.assertEqual(self.store.get(a["id"])["status"], "uncertain")
        self.append({"type": "task_complete", "last_agent_message": f"[THOUGHT_ANSWER:{a['id']}]\nRecovered answer"})
        reader.poll()
        self.assertEqual(self.store.get(a["id"])["answer"], "Recovered answer")

    def test_delivery_arguments_and_failure_states(self):
        bridge = Bridge(self.base, self.base / "delivery", "thread-test", self.log, "codex", self.base)
        a = bridge.store.create(question(text="What does `$(touch /tmp/never)` mean?"))
        node = bridge.store.claim_next()
        with patch("serve.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            bridge.deliver(node)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["codex", "queue", "--thread", "thread-test"])
        self.assertIn("$(touch /tmp/never)", args[5])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(bridge.store.get(a["id"])["status"], "queued")
        with patch("serve.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 30)):
            bridge.deliver(node)
        self.assertEqual(bridge.store.get(a["id"])["status"], "uncertain")
        with patch("serve.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "Daemon unavailable")):
            bridge.deliver(node)
        self.assertEqual(bridge.store.get(a["id"])["status"], "failed")
        self.assertIn("Daemon unavailable", bridge.store.get(a["id"])["error"])

    def test_server_restart_does_not_resend_inflight_delivery(self):
        bridge = Bridge(self.base, self.base / "delivery", "thread-test", self.log, "codex", self.base)
        node = bridge.store.create(question())
        bridge.store.claim_next()
        again = Bridge(self.base, self.base / "delivery", "thread-test", self.log, "codex", self.base)
        self.assertEqual(again.store.get(node["id"])["status"], "uncertain")
        self.assertIsNone(again.store.claim_next())

    def test_local_api_requires_origin_token_and_never_serves_database(self):
        (self.base / "index.html").write_text("<h1>Reading</h1>")
        bridge = Bridge(self.base, self.base / ".thoughts", "thread-test", self.log, "codex", self.base)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.bridge = bridge
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(origin + "/api/thoughts") as response:
                config = json.load(response)
            body = json.dumps(question()).encode()
            for headers in ({"Content-Type": "application/json"}, {"Content-Type": "application/json", "Origin": "https://elsewhere.example", "X-Thought-Token": config["token"]}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(origin + "/api/questions", body, headers))
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            headers = {"Content-Type": "application/json", "Origin": origin, "X-Thought-Token": config["token"]}
            with urlopen(Request(origin + "/api/questions", body, headers)) as response:
                self.assertEqual(json.load(response)["question"]["status"], "waiting")
            with self.assertRaises(HTTPError) as error:
                urlopen(origin + "/.thoughts/questions.sqlite3")
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
