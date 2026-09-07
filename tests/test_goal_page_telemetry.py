"""The goal page's telemetry, as the page's own routes produce it.

Every route the page acts through is a workflow root; what the server did
for it -- read the goals, ran the claude subprocess, saved the chat --
hangs beneath, in the site's own shapes. These tests drive the routes and
read what landed: under the chat (telemetry/records.jsonl), on a stand-in
for the site (the POST), and in the outbox when the site is out of reach.
"""
import http.client
import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hc" / "src"))

import test_goal_page as GP  # noqa: E402
from human_compact import telemetry as T  # noqa: E402
from human_compact.trajectory import autosync as AUTOSYNC  # noqa: E402
from human_compact.trajectory import supabase_client as SB  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

server_for, fetch, get_json, post_json = GP.server_for, GP.fetch, GP.get_json, GP.post_json
seed_design, bind_project, read_event = GP.seed_design, GP.bind_project, GP.read_event
TODOS_CARD = GP.TODOS_CARD
TOKEN = "session-token-0123456789abcdefghijklmnopqrstuvwxyz"
HEX32 = r"^[0-9a-f]{32}$"


def records_of(chat):
    return T.FileSink.read(Path(chat) / "telemetry")


def by_name(records):
    out = {}
    for op in records["operations"]:
        out.setdefault(op["name"], []).append(op)
    return out


def one(records, name):
    ops = by_name(records).get(name) or []
    assert len(ops) == 1, "expected one %s, found %d" % (name, len(ops))
    return ops[0]


def snapshot(records, snapshot_id):
    return next(s for s in records["snapshots"] if s["snapshot_id"] == snapshot_id)


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.05)


def completed(stdout):
    return subprocess.CompletedProcess(["claude"], 0, stdout=stdout, stderr="")


class FakeSite:
    """The site's endpoint, stood in: every POST kept, answered as told."""
    def __init__(self, status=200):
        self.posts = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                site.posts.append({"path": self.path, "headers": dict(self.headers), "body": body})
                data = json.dumps({"ok": True, "accepted": {
                    "operations": len(body.get("operations") or [])}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def closed_port_url():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return "http://127.0.0.1:%d" % port


class TelemetryCase(GP.ChatCase):
    def env(self, **values):
        patch = mock.patch.dict(os.environ, {k: str(v) for k, v in values.items()})
        patch.start()
        self.addCleanup(patch.stop)

    def add_goal(self, url, title="Export the dataset to parquet"):
        request = GP.urllib.request.Request(
            url + "/api/goal-page/op", data=json.dumps({"op": "add_goal", "title": title}).encode(),
            headers={"Content-Type": "application/json", "Origin": url}, method="POST")
        with GP.NO_PROXY_OPENER.open(request, timeout=15) as response:
            return dict(response.headers), json.loads(response.read())


class RouteTraceTests(TelemetryCase):
    def test_every_route_the_page_acts_through_is_a_trace_the_reply_names(self):
        seed_design(self.chat)
        with server_for(self.chat) as url:
            status, headers, body = fetch(url + "/api/goal-page")
            self.assertEqual(200, status)
            read_trace = headers["x-engelbart-trace-id"]
            op_headers, answer = self.add_goal(url)
            op_trace = op_headers["x-engelbart-trace-id"]
        self.assertRegex(read_trace, HEX32)
        self.assertRegex(op_trace, HEX32)
        self.assertNotEqual(read_trace, op_trace)
        self.assertTrue(answer["ok"], answer)
        records = records_of(self.chat)
        path = self.chat / "telemetry" / "records.jsonl"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)

        read = one(records, "goal-page.read")
        self.assertEqual(("workflow", "workflow", "completed", read_trace, None, "chat:chat", "read", "plugin"),
                         (read["type"], read["level"], read["status"], read["trace_id"], read["parent_span_id"],
                          read["run_id"], read["action"], read["environment"]))
        self.assertEqual({"http.request.method": "GET", "url.path": "/api/goal-page",
                          "http.response.status_code": 200, "engelbart.run_id": "chat:chat",
                          "engelbart.action": "read", "engelbart.mode": "live"},
                         {k: v for k, v in read["attributes"].items()
                          if k.startswith(("http.", "url.", "engelbart.run", "engelbart.action", "engelbart.mode"))})
        self.assertEqual(read["operation_id"], read["attributes"]["bart.operation_id"])
        self.assertIsNone(read["onboarding_id"])
        [load] = [op for op in by_name(records)["goals.load"] if op["trace_id"] == read_trace]
        self.assertEqual(("storage", "detail", read["span_id"]), (load["type"], load["level"], load["parent_span_id"]))
        self.assertIn("goals", load["attributes"]["engelbart.lineage.reads"])
        self.assertEqual("goals.json", load["attributes"]["engelbart.storage.object"])
        self.assertIsInstance(load["attributes"]["engelbart.goals.count"], int)
        output = snapshot(records, read["snapshots"]["processing_output"])
        self.assertEqual(("processing_output", read["operation_id"], "chat:chat", True),
                         (output["kind"], output["operation_id"], output["run_id"], output["redacted"]))
        self.assertEqual(GP.GOAL_TITLE, output["content"]["goals"][0]["title"])

        op = one(records, "goal-page.op")
        self.assertEqual(("completed", op_trace, "op", "add_goal"),
                         (op["status"], op["trace_id"], op["action"], op["attributes"]["engelbart.op"]))
        self.assertEqual({"op": "add_goal", "title": "Export the dataset to parquet"},
                         snapshot(records, op["snapshots"]["processing_input"])["content"])
        apply = one(records, "apply.add_goal")
        self.assertEqual(("processing", "stage", op["span_id"], op_trace, True, answer["id"]),
                         (apply["type"], apply["level"], apply["parent_span_id"], apply["trace_id"],
                          apply["attributes"]["engelbart.apply.ok"], apply["attributes"]["engelbart.apply.id"]))
        under = [o for o in records["operations"] if o["parent_span_id"] == apply["span_id"]]
        self.assertEqual(["goals.load", "goals.save"], [o["name"] for o in sorted(under, key=lambda o: o["started_at"])])
        save = one(records, "goals.save")
        self.assertIn("goals", save["attributes"]["engelbart.lineage.writes"])
        self.assertEqual(answer["id"], snapshot(records, op["snapshots"]["processing_output"])["content"]["id"])
        # Every record's lifecycle: a start and an end for each operation.
        kinds = {}
        for e in records["events"]:
            kinds.setdefault(e["operation_id"], []).append(e["type"])
        for record in records["operations"]:
            self.assertEqual("operation.started", kinds[record["operation_id"]][0], record["name"])
            self.assertEqual("operation.completed", kinds[record["operation_id"]][-1], record["name"])

    def test_a_refused_op_fails_the_root_with_the_refusal(self):
        seed_design(self.chat)
        with server_for(self.chat) as url:
            answer = post_json(url + "/api/goal-page/op", {"op": "no_such_op"}, {"Origin": url})
        self.assertFalse(answer["ok"])
        records = records_of(self.chat)
        root = one(records, "goal-page.op")
        self.assertEqual("failed", root["status"])
        self.assertEqual(("RequestRefused", 200), (root["error"]["name"], root["error"]["status_code"]))
        self.assertEqual(answer["error"], root["error"]["message"])
        self.assertEqual("RequestRefused", root["attributes"]["error.type"])
        self.assertIn("error_detail", root["snapshots"])
        self.assertEqual("operation.failed", [e for e in records["events"] if e["operation_id"] == root["operation_id"]][-1]["type"])
        # Refused at the door: no stage was reached, so none is recorded.
        self.assertEqual({"goal-page.op"}, set(by_name(records)))

    def test_static_files_and_the_page_s_polls_are_untraced_unless_asked(self):
        seed_design(self.chat)
        with server_for(self.chat) as url:
            for path in ("/", "/api/goal-page/panes?goal=g1"):
                status, headers, body = fetch(url + path)
                self.assertEqual(200, status, path)
                self.assertNotIn("x-engelbart-trace-id", {k.lower() for k in headers}, path)
        self.assertFalse((self.chat / "telemetry" / "records.jsonl").exists(),
                         "nothing the page polls for is a trace")
        self.env(ENGELBART_TRACE_POLLS="true")
        with server_for(self.chat) as url:
            status, headers, body = fetch(url + "/api/goal-page/panes?goal=g1")
            self.assertRegex(headers["x-engelbart-trace-id"], HEX32)
        poll = one(records_of(self.chat), "goal-page.panes.poll")
        self.assertEqual(("panes", True, "/api/goal-page/panes"),
                         (poll["action"], poll["attributes"]["engelbart.poll"], poll["attributes"]["url.path"]))

    def test_the_change_feed_reports_each_revision_while_it_waits(self):
        goal, subgoals = seed_design(self.chat)
        with mock.patch.object(ui, "SSE_PING_SECONDS", 0.3), server_for(self.chat) as url:
            connection = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=10)
            try:
                connection.request("GET", "/api/goal-page/events")
                response = connection.getresponse()
                self.assertRegex(response.getheader("x-engelbart-trace-id"), HEX32)
                self.assertEqual(["retry: 2000"], read_event(response))
                opened_on = GP.revision_of(read_event(response))
                answer = post_json(url + "/api/goal-page/op",
                                   {"op": "set_notes", "goal_id": subgoals[0], "notes": "hello"}, {"Origin": url})
                self.assertEqual(answer["revision"], GP.revision_of(read_event(response)))
            finally:
                response.close()
                connection.close()
        records = records_of(self.chat)
        stream = one(records, "goal-page.events")
        self.assertEqual(("events", "revision", 200),
                         (stream["action"], stream["attributes"]["bart.waiting_reason"],
                          stream["attributes"]["http.response.status_code"]))
        progress = [e["progress"] for e in records["events"]
                    if e["operation_id"] == stream["operation_id"] and e["type"] == "operation.progress"]
        self.assertEqual([{"message": "revision", "engelbart.goals.revision": opened_on},
                          {"message": "revision", "engelbart.goals.revision": answer["revision"]}], progress)


class BartTraceTests(TelemetryCase):
    def setUp(self):
        super().setUp()
        self.env(HC_CHAT_PROVIDER="claude", ANTHROPIC_API_KEY="sk-ant-api03-test-key-value-000111222333")

    def ask(self, url, subgoal, text):
        return post_json(url + "/api/goal-page/bart",
                         {"goal_id": "", "subgoal_id": subgoal, "transcript": [{"role": "you", "text": text}]},
                         {"Origin": url})

    def test_bart_s_turn_records_the_model_call_with_its_prompt_and_reply(self):
        goal, subgoals = seed_design(self.chat)
        run = mock.Mock(return_value=completed("Here you go:\n" + json.dumps(TODOS_CARD)))
        with mock.patch("human_compact.trajectory.providers.subprocess.run", run), server_for(self.chat) as url:
            answer = self.ask(url, subgoals[1], "save it as parquet; my key is sk-ant-api03-test-key-value-000111222333")
        self.assertTrue(answer["ok"], answer)
        records = records_of(self.chat)
        root = one(records, "goal-page.bart")
        context, turn, call = (one(records, n) for n in ("bart.context", "bart.message", "model.chat"))
        self.assertEqual("completed", root["status"])
        self.assertEqual([root["span_id"]] * 2, [context["parent_span_id"], turn["parent_span_id"]])
        self.assertEqual((one(records, "chat.reply")["span_id"], root["trace_id"], "model", "stage"),
                         (call["parent_span_id"], call["trace_id"], call["type"], call["level"]))
        # Chat returns prose and the two flat TODOs; nested brainstorm subgoals are not Chat rows.
        self.assertEqual(("none", 3, True, GP.SUBGOAL_TITLES[1]),
                         (turn["attributes"]["engelbart.bart.card"], turn["attributes"]["engelbart.bart.replies"],
                          turn["attributes"]["engelbart.bart.ok"], turn["attributes"]["engelbart.bart.subgoal"]))
        attrs = call["attributes"]
        self.assertEqual(("chat", "anthropic", "claude-cli", "chat", 0, True, "subscription"),
                         (attrs["gen_ai.operation.name"], attrs["gen_ai.provider.name"], attrs["engelbart.model.gateway"],
                          attrs["engelbart.model.purpose"], attrs["engelbart.model.exit_code"],
                          attrs["engelbart.model.structured"], attrs["engelbart.model.credentials"]))
        self.assertEqual(run.call_args.args[0][4], attrs["gen_ai.request.model"])
        self.assertGreater(attrs["engelbart.model.prompt_chars"], 100)
        request = snapshot(records, call["snapshots"]["model_request"])["content"]
        self.assertEqual("cli://claude", request["url"])
        self.assertEqual(["claude", "-p"], request["command"][:2])
        [message] = request["body"]["messages"]
        self.assertEqual("user", message["role"])
        self.assertIn(GP.SUBGOAL_TITLES[1], message["content"])
        self.assertIn("save it as parquet", message["content"])
        raw = snapshot(records, call["snapshots"]["model_raw_response"])["content"]
        self.assertEqual((0, ""), (raw["returncode"], raw["stderr"]))
        self.assertIn("Two rows, then.", raw["stdout"])
        self.assertEqual(TODOS_CARD, snapshot(records, call["snapshots"]["model_parsed_response"])["content"])
        self.assertEqual("none", snapshot(records, turn["snapshots"]["processing_output"])["content"]["card"])
        # The key the reader pasted is in no record: not in the request body
        # the page sent, not in the prompt the model was sent.
        text = (self.chat / "telemetry" / "records.jsonl").read_text()
        self.assertNotIn("sk-ant-api03-test-key-value-000111222333", text)
        self.assertIn("[redacted]", text)
        self.assertNotIn("ANTHROPIC_API_KEY", json.dumps(request))

    def test_a_model_that_times_out_fails_the_turn_from_the_call_up(self):
        goal, subgoals = seed_design(self.chat)
        run = mock.Mock(side_effect=subprocess.TimeoutExpired(["claude"], 7))
        with mock.patch("human_compact.trajectory.providers.subprocess.run", run), server_for(self.chat) as url:
            answer = self.ask(url, subgoals[1], "save it")
        self.assertFalse(answer["ok"])
        self.assertIn("timed out", answer["error"])
        records = records_of(self.chat)
        root, turn, call = (one(records, n) for n in ("goal-page.bart", "bart.message", "model.chat"))
        self.assertEqual(["failed"] * 3, [o["status"] for o in (root, turn, call)])
        self.assertEqual("ProviderError", call["error"]["name"])
        self.assertIn("timed out", call["error"]["message"])
        self.assertIn("timed out", turn["error"]["message"])
        self.assertEqual("RequestRefused", root["error"]["name"])
        self.assertNotIn("model_raw_response", call["snapshots"])
        self.assertIn("error_detail", call["snapshots"])


class ChatSaveTraceTests(TelemetryCase):
    def test_saving_the_conversation_is_a_storage_detail_under_its_route(self):
        goal, subgoals = seed_design(self.chat)
        said = [{"id": "m-1", "who": "you", "kind": "text", "text": "first"}]
        with server_for(self.chat) as url:
            answer = post_json(url + "/api/goal-page/chat",
                               {"subgoal_id": subgoals[1], "messages": said}, {"Origin": url})
            self.assertTrue(answer["ok"], answer)
        records = records_of(self.chat)
        root = one(records, "goal-page.chat")
        save = one(records, "bart-chat.save")
        self.assertEqual(("storage", "detail", root["span_id"], "completed"),
                         (save["type"], save["level"], save["parent_span_id"], save["status"]))
        self.assertIn("bart-chat", save["attributes"]["engelbart.lineage.writes"])
        self.assertEqual(said, snapshot(records, root["snapshots"]["processing_input"])["content"]["messages"])


class SwitchTests(TelemetryCase):
    def test_switched_off_nothing_is_recorded_and_no_header_is_sent(self):
        seed_design(self.chat)
        self.env(ENGELBART_TELEMETRY="off")
        with server_for(self.chat) as url:
            status, headers, body = fetch(url + "/api/goal-page")
            self.assertEqual(200, status)
            self.assertNotIn("x-engelbart-trace-id", {k.lower() for k in headers})
            op_headers, answer = self.add_goal(url)
            self.assertTrue(answer["ok"])
            self.assertNotIn("x-engelbart-trace-id", {k.lower() for k in op_headers})
        self.assertFalse((self.chat / "telemetry").exists())

    def test_without_content_the_operations_stay_and_the_payloads_go(self):
        seed_design(self.chat)
        self.env(ENGELBART_TRACE_CONTENT="false")
        with server_for(self.chat) as url:
            fetch(url + "/api/goal-page")
            self.add_goal(url)
        records = records_of(self.chat)
        self.assertEqual({"goal-page.read", "goal-page.op", "apply.add_goal", "goals.load", "goals.save"},
                         set(by_name(records)))
        self.assertEqual([], records["snapshots"])
        self.assertEqual({}, one(records, "goal-page.op")["snapshots"])

    def test_without_the_file_sink_nothing_is_written_under_the_chat(self):
        seed_design(self.chat)
        self.env(ENGELBART_TELEMETRY_FILE="false")
        with server_for(self.chat) as url:
            status, headers, body = fetch(url + "/api/goal-page")
            self.assertRegex(headers["x-engelbart-trace-id"], HEX32)
        self.assertFalse((self.chat / "telemetry").exists())

    def test_forwarding_is_off_under_a_test_runner_unless_asked(self):
        tele, run = ui._telemetry_for(self.chat, True)
        self.addCleanup(tele.close)
        self.assertTrue(tele.enabled)
        self.assertEqual("chat:chat", run["run_id"])
        self.assertEqual([T.FileSink], [type(s) for s in tele.sinks])
        self.assertFalse(tele.settings["forward"])
        off, run = ui._telemetry_for(self.chat, False)
        self.assertFalse(off.enabled)
        self.assertIsNone(run["run_id"])


class ForwardTests(TelemetryCase):
    def sign_in(self):
        SB.session_path(self.root).write_text(json.dumps({
            "access_token": TOKEN, "refresh_token": "", "user_id": "user-1",
            "email": "m@example.com", "expires_at": int(time.time()) + 86400}))

    def test_the_run_reaches_the_site_on_the_member_s_session(self):
        seed_design(self.chat)
        bind_project(self)
        self.sign_in()
        site = FakeSite()
        self.addCleanup(site.close)
        self.env(ENGELBART_TELEMETRY_FORWARD="true", ENGELBART_TELEMETRY_URL=site.url,
                 ENGELBART_TELEMETRY_FLUSH_MS="50")
        def delivered():
            return {op["operation_id"]: op for post in site.posts
                    for op in post["body"]["operations"]}
        apply = ui._apply
        def after_started_envelope(*args, **kwargs):
            # Cross a real flush boundary while the request is in flight.
            # Running records are intentional live telemetry, followed by
            # the terminal update for that same operation ID.
            wait_for(lambda: any(op["name"] == "goal-page.op" and op["status"] == "running"
                                 for op in delivered().values()), 5)
            return apply(*args, **kwargs)
        with mock.patch.object(ui, "_apply", side_effect=after_started_envelope), server_for(self.chat) as url:
            fetch(url + "/api/goal-page")
            op_headers, answer = self.add_goal(url)
        self.assertTrue(answer["ok"], answer)
        expected = {"goal-page.read", "goal-page.op", "apply.add_goal", "goals.save"}
        self.assertTrue(wait_for(lambda: expected <= {op["name"] for op in delivered().values()
                                                       if op["status"] == "completed"}, 5),
                        "terminal operation updates were not sent")
        self.assertTrue(any(op["name"] == "goal-page.op" and op["status"] == "running"
                            for post in site.posts for op in post["body"]["operations"]))
        operations = list(delivered().values())
        [first] = site.posts[:1]
        self.assertEqual("/api/engelbart-telemetry", first["path"])
        self.assertEqual("Bearer " + TOKEN, first["headers"]["Authorization"])
        self.assertEqual("engelbart-goal-page-telemetry/1", first["headers"]["User-Agent"])
        self.assertEqual("1", first["body"]["contract_version"])
        self.assertEqual({"run_id": "chat:chat", "mode": "live", "environment": "plugin", "deployment": None,
                          "origin": "goal-page", "label": "project"},
                         {k: v for k, v in first["body"]["run"].items() if k != "code_version"})
        names = {op["name"] for op in operations}
        self.assertLessEqual({"goal-page.read", "goal-page.op", "apply.add_goal", "goals.save"}, names)
        for op in operations:
            self.assertEqual(T.user_hash("user-1"), op["attributes"]["engelbart.user_hash"], op["name"])
            self.assertEqual("completed", op["status"], "every operation must eventually receive its terminal update")
        self.assertEqual(op_headers["x-engelbart-trace-id"],
                         next(op for op in operations if op["name"] == "goal-page.op")["trace_id"])
        kinds = {s["kind"] for post in site.posts for s in post["body"]["snapshots"]}
        self.assertIn("processing_output", kinds)
        self.assertTrue(any(e["type"] == "operation.completed" for post in site.posts for e in post["body"]["events"]))
        # The session token travels as the header and nowhere else.
        self.assertNotIn(TOKEN, json.dumps([p["body"] for p in site.posts]))
        self.assertNotIn(TOKEN, (self.chat / "telemetry" / "records.jsonl").read_text())
        self.assertFalse((self.chat / "telemetry" / "outbox").exists())

    def test_a_site_out_of_reach_leaves_the_envelope_in_the_outbox(self):
        seed_design(self.chat)
        self.sign_in()
        self.env(ENGELBART_TELEMETRY_FORWARD="true", ENGELBART_TELEMETRY_URL=closed_port_url(),
                 ENGELBART_TELEMETRY_FLUSH_MS="50")
        with mock.patch("sys.stderr"):
            with server_for(self.chat) as url:
                status, headers, body = fetch(url + "/api/goal-page")
                self.assertEqual(200, status)
        outbox = self.chat / "telemetry" / "outbox"
        [spooled] = list(outbox.glob("*.json"))
        self.assertEqual(0o600, spooled.stat().st_mode & 0o777)
        held = json.loads(spooled.read_text())
        self.assertEqual("chat:chat", held["run"]["run_id"])
        self.assertEqual("1", held["contract_version"])
        self.assertEqual(["goal-page.read", "goals.load"],
                         sorted(op["name"] for op in held["operations"]))
        self.assertNotIn(TOKEN, spooled.read_text())

    def test_not_signed_in_nothing_leaves_the_machine(self):
        seed_design(self.chat)
        site = FakeSite()
        self.addCleanup(site.close)
        self.env(ENGELBART_TELEMETRY_FORWARD="true", ENGELBART_TELEMETRY_URL=site.url,
                 ENGELBART_TELEMETRY_FLUSH_MS="50")
        with mock.patch("sys.stderr"):
            with server_for(self.chat) as url:
                fetch(url + "/api/goal-page")
        self.assertEqual([], site.posts)
        self.assertEqual(1, len(list((self.chat / "telemetry" / "outbox").glob("*.json"))),
                         "kept for a later session")


class AutosyncTraceTests(TelemetryCase):
    def setUp(self):
        super().setUp()
        with AUTOSYNC._GUARD:
            for timer in AUTOSYNC._TIMERS.values():
                timer.cancel()
            AUTOSYNC._TIMERS.clear()
            AUTOSYNC._SENDING.clear()
            AUTOSYNC._AGAIN.clear()
            AUTOSYNC._LAST.clear()

    def test_the_send_after_an_edit_is_its_own_trace_under_the_workspace(self):
        seed_design(self.chat)
        project = bind_project(self)
        self.env(HC_AUTOSYNC_SECONDS="0.05")
        sent = []

        def fake_sync(root, cwd):
            sent.append(cwd)
            return {"ok": True, "project_id": "p", "sent": {"goals": 4, "todos": 2}}
        patch = mock.patch.multiple("human_compact.trajectory.supabase_client", sync_project=fake_sync,
                                    status=lambda root=None: {"configured": True, "signed_in": True})
        patch.start()
        self.addCleanup(patch.stop)
        with server_for(self.chat) as url:
            op_headers, answer = self.add_goal(url)
            self.assertTrue(answer["ok"])
            self.assertTrue(wait_for(lambda: "autosync.push" in by_name(records_of(self.chat))
                                     and records_of(self.chat) and
                                     by_name(records_of(self.chat))["autosync.push"][0]["status"] != "running", 5))
        self.assertEqual([str(project.resolve())], sent)
        records = records_of(self.chat)
        push = one(records, "autosync.push")
        rpc = one(records, "supabase.sync-project")
        self.assertEqual(("workflow", None, "completed", "chat:chat"),
                         (push["type"], push["parent_span_id"], push["status"], push["run_id"]))
        self.assertNotEqual(op_headers["x-engelbart-trace-id"], push["trace_id"], "a trace of its own")
        self.assertEqual((True, False, ["goals", "todos"]), (push["attributes"]["engelbart.autosync.ok"],
                                                             push["attributes"]["engelbart.autosync.waiting"],
                                                             push["attributes"]["engelbart.autosync.sent"]))
        self.assertEqual(("http", push["span_id"], push["trace_id"], "POST", "/rest/v1/rpc/hc_sync_project", 4),
                         (rpc["type"], rpc["parent_span_id"], rpc["trace_id"], rpc["attributes"]["http.request.method"],
                          rpc["attributes"]["url.path"], rpc["attributes"]["engelbart.autosync.goals"]))
        self.assertEqual(["project"], rpc["attributes"]["engelbart.lineage.writes"])


if __name__ == "__main__":
    unittest.main()
