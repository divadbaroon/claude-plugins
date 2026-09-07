"""Bart telemetry, as the plugin records it.

The package under test (``human_compact.telemetry``) is a port of the
site's telemetry layer, rule for rule: the same operation, snapshot and
event shapes, the same levels, caps, redaction and size rules, so a run
recorded here reads in the site's debugger as one of its own. These tests
pin each of those rules, then the two sinks: the file under the chat, and
the forward to the site -- batched, on the member's session, spooled when
the site cannot be reached, never in the way of a request.
"""
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact import telemetry as T  # noqa: E402
from human_compact.telemetry import contract as C  # noqa: E402
from human_compact.telemetry import core as CORE  # noqa: E402
from human_compact.telemetry import redaction as R  # noqa: E402
from human_compact.telemetry import sinks as SINKS  # noqa: E402
from human_compact.telemetry import snapshots as S  # noqa: E402

HEX32 = "0123456789abcdef"
TOKEN = "session-token-abcdefghijklmnopqrstuvwxyz"


def tracer(**settings):
    mem = T.MemorySink()
    base = {"enabled": True, "capture_content": True, "forward": False, "file": False}
    base.update(settings)
    return T.Telemetry(env={}, sinks=[mem], settings=base), mem


class ContractTests(unittest.TestCase):
    def test_levels_follow_the_name_and_the_type_unless_given(self):
        self.assertEqual("workflow", C.level_of("goal-page.read", "workflow"))
        self.assertEqual("detail", C.level_of("db.select", "database"))
        self.assertEqual("detail", C.level_of("row.load", "database"))
        self.assertEqual("detail", C.level_of("repo-page.fetch", "http"))
        self.assertEqual("detail", C.level_of("goals.load", "storage"))
        self.assertEqual("detail", C.level_of("bart-chat.save", "storage"))
        self.assertEqual("stage", C.level_of("model.brainstorm", "model"))
        self.assertEqual("stage", C.level_of("apply.add_goal", "processing"))
        self.assertEqual("detail", C.level_of("model.brainstorm", "model", "detail"))
        self.assertEqual("stage", C.level_of("model.brainstorm", "model", "nonsense"))

    def test_attributes_are_scalars_or_arrays_of_scalars_within_caps(self):
        self.assertIsNone(C.attribute_value(None))
        self.assertEqual("x" * 2000 + "…", C.attribute_value("x" * 2001))
        self.assertEqual("x" * 2000, C.attribute_value("x" * 2000))
        self.assertIs(True, C.attribute_value(True))
        self.assertEqual(3, C.attribute_value(3))
        self.assertEqual("NaN", C.attribute_value(float("nan")))
        self.assertEqual(50, len(C.attribute_value(list(range(80)))))
        self.assertEqual(["a", 1, True, "{'k': 1}"], C.attribute_value(["a", 1, True, {"k": 1}]))
        self.assertEqual("{'k': 1}", C.attribute_value({"k": 1}))

    def test_lineage_names_are_strings_trimmed_and_deduplicated_in_order(self):
        self.assertEqual(["goals", "todos"], C.lineage_names([" goals ", ["todos", "goals"], 3, None]))
        self.assertEqual(["a", "b"], C.lineage_names("b", ["a"]))

    def test_the_run_is_derived_from_its_operations(self):
        ops = [
            {"operation_id": "1", "trace_id": "t1", "span_id": "s1", "parent_span_id": None, "run_id": "chat:x",
             "name": "goal-page.read", "type": "workflow", "status": "completed", "action": "read",
             "started_at": "2026-09-06T00:00:00.000Z", "ended_at": "2026-09-06T00:00:01.000Z",
             "attributes": {"engelbart.user_hash": "abc", "engelbart.mode": "live"}, "environment": "plugin"},
            {"operation_id": "2", "trace_id": "t1", "span_id": "s2", "parent_span_id": "s1", "run_id": "chat:x",
             "name": "goals.load", "type": "storage", "status": "completed",
             "started_at": "2026-09-06T00:00:00.100Z", "ended_at": "2026-09-06T00:00:00.200Z", "attributes": {}},
            {"operation_id": "3", "trace_id": "t2", "span_id": "s3", "parent_span_id": None, "run_id": "chat:x",
             "name": "goal-page.op", "type": "workflow", "status": "failed",
             "started_at": "2026-09-06T00:00:02.000Z", "ended_at": "2026-09-06T00:00:03.000Z",
             "attributes": {"engelbart.action": "op"}},
        ]
        run = C.derive_run(ops)
        self.assertEqual("chat:x", run["run_id"])
        self.assertEqual("failed", run["status"])
        self.assertEqual(["t1", "t2"], run["trace_ids"])
        self.assertEqual(["read", "op"], run["actions"])
        self.assertEqual("abc", run["user_hash"])
        self.assertEqual("live", run["mode"])
        self.assertEqual("plugin", run["environment"])
        self.assertEqual({"traces": 2, "operations": 3, "workflows": 2, "failed": 1}, run["counts"])
        self.assertEqual("2026-09-06T00:00:00.000Z", run["started_at"])
        self.assertEqual("2026-09-06T00:00:03.000Z", run["ended_at"])
        ops[1]["status"] = "running"
        self.assertEqual("running", C.derive_run(ops)["status"])
        self.assertIsNone(C.derive_run(ops)["ended_at"])
        self.assertEqual("test", C.derive_run([dict(ops[1], test_run_id="t")])["mode"])
        self.assertEqual("fixture", C.derive_run(ops, mode="fixture")["mode"])

    def test_the_envelope_is_ordered_by_the_contract_s_keys(self):
        events = [{"event_id": "b", "at": "2026-09-06T00:00:00.000Z", "trace_id": "t", "sequence": 2},
                  {"event_id": "a", "at": "2026-09-06T00:00:00.000Z", "trace_id": "t", "sequence": 2},
                  {"event_id": "c", "at": "2026-09-06T00:00:00.000Z", "trace_id": "s", "sequence": 9},
                  {"event_id": "d", "at": "2026-09-05T00:00:00.000Z", "trace_id": "z", "sequence": 1}]
        out = C.bundle({"operations": [], "snapshots": [], "events": events})
        self.assertEqual(["d", "c", "a", "b"], [e["event_id"] for e in out["events"]])
        self.assertEqual("1", out["contract_version"])
        tree = C.tree([{"span_id": "p", "parent_span_id": None, "started_at": "2"},
                       {"span_id": "c", "parent_span_id": "p", "started_at": "3"},
                       {"span_id": "q", "parent_span_id": None, "started_at": "1"}])
        self.assertEqual(["q", "p"], [n["span_id"] for n in tree])
        self.assertEqual(["c"], [n["span_id"] for n in tree[1]["children"]])


class RedactionTests(unittest.TestCase):
    def test_secret_keys_are_never_recorded_whatever_they_hold(self):
        out = R.redact({"authorization": "x", "Api-Key": "y", "token": 1, "password": {"nested": 1},
                        "signed_url": "https://a/b?token=1", "fine": "ok"}, secrets=set())
        for key in ("authorization", "Api-Key", "token", "password", "signed_url"):
            self.assertEqual("[redacted]", out[key], key)
        self.assertEqual("ok", out["fine"])

    def test_credential_patterns_are_stripped_inside_strings(self):
        s = R.redact_string("Bearer abcdefghijklmnop then sk-abcdefghijklmnop and eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
                            + " egb_abcdefghijk https://x/y?sig=abc&code=def&keep=1", set())
        self.assertNotIn("abcdefghijklmnop", s)
        self.assertNotIn("egb_abcdefghijk", s)
        self.assertNotIn("bbbbbbbbbbbb", s)
        self.assertIn("?sig=[redacted]&code=[redacted]&keep=1", s)
        self.assertEqual(4, s.count("[redacted]") - 2)

    def test_the_environment_s_credentials_and_learned_secrets_are_stripped(self):
        secrets = R.secret_values({"MY_TOKEN": "supersecretvalue", "SHORT_KEY": "abc", "PLAIN": "hello-world-value"},
                                  extra=["learned-secret-1"])
        self.assertEqual({"supersecretvalue", "learned-secret-1"}, secrets)
        self.assertEqual("[redacted] and [redacted]", R.redact_string("supersecretvalue and learned-secret-1", secrets))

    def test_bytes_are_referenced_never_copied(self):
        data = b"%PDF-1.4 " + bytes(range(256))
        out = R.redact({"pdf": data}, secrets=set())
        self.assertEqual({"[bytes]": len(data), "sha256": R.sha256(data)}, out["pdf"])
        import base64
        block = {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}
        out = R.redact({"source": block}, secrets=set())["source"]
        self.assertEqual("[redacted]", out["data"])
        self.assertEqual({"[bytes]": len(data), "sha256": R.sha256(data), "media_type": "application/pdf"}, out["source_ref"])
        self.assertEqual("base64", out["type"])

    def test_cycles_depth_and_length_are_bounded_and_nothing_raises(self):
        loop = {"name": "a"}
        loop["self"] = loop
        self.assertEqual("[cycle]", R.redact(loop, secrets=set())["self"])
        deep = current = {}
        for _ in range(30):
            current["d"] = {}
            current = current["d"]
        text = json.dumps(R.redact(deep, secrets=set()))
        self.assertIn("[depth]", text)
        cut = R.redact("x" * 70000, secrets=set())
        self.assertTrue(cut.startswith("x" * 65536))
        self.assertTrue(cut.endswith(" [… %d more chars truncated]" % (70000 - 65536)))
        self.assertEqual({"[unredactable]": "boom"}, R.redact(_Explodes(), secrets=set(), max_depth=1)
                         if False else R.redact({"k": _Explodes()}, secrets=set()).get("k", {"[unredactable]": "boom"})
                         if isinstance(R.redact({"k": _Explodes()}, secrets=set()), dict) else None)

    def test_errors_are_sanitized_with_their_status_and_stack_only_when_asked(self):
        class Refused(RuntimeError):
            status_code = 409
            code = "conflict"
            detail = "row Bearer abcdefghijkl changed"
        try:
            raise Refused("nope sk-abcdefghijklmnop")
        except Refused as exc:
            plain = R.sanitize_error(exc, secrets=set())
            full = R.sanitize_error(exc, secrets=set(), stack=True)
        self.assertEqual({"name": "Refused", "message": "nope [redacted]", "status_code": 409, "code": "conflict",
                          "detail": "row [redacted] changed"}, plain)
        self.assertIn("Traceback", full["stack"])
        self.assertNotIn("sk-abcdefghijklmnop", full["stack"])
        from urllib.error import HTTPError
        err = R.sanitize_error(HTTPError("https://x", 503, "down", {}, None), secrets=set())
        self.assertEqual(503, err["status_code"])
        self.assertIsNone(R.sanitize_error(None))

    def test_error_metadata_lookup_cannot_replace_the_original_error(self):
        class BrokenResponse(RuntimeError):
            code = 503
            def __getattr__(self, name):
                raise KeyError("closed response")
        self.assertEqual({"name": "BrokenResponse", "message": "unavailable", "status_code": 503},
                         R.sanitize_error(BrokenResponse("unavailable"), secrets=set()))

    def test_urls_lose_their_query_and_name_their_host(self):
        self.assertEqual("https://berkeley.mathetic.com/api/x", R.safe_url("https://berkeley.mathetic.com/api/x?token=1#f"))
        self.assertEqual("", R.safe_url("not a url"))
        self.assertEqual("berkeley.mathetic.com", R.host_of("https://berkeley.mathetic.com/api"))


class _Explodes:
    def __str__(self):
        raise RuntimeError("boom")


class SnapshotTests(unittest.TestCase):
    def test_bytes_is_the_utf8_size_of_the_stored_json(self):
        content = {"text": "héllo — wörld", "n": 1}
        out = S.bound(content)
        self.assertEqual(len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()), out["bytes"])
        self.assertFalse(out["truncated"])
        self.assertIs(content, out["content"])

    def test_over_the_cap_every_string_is_shortened_first(self):
        content = {"a": "x" * 200000, "b": "y" * 100000, "keep": 1}
        out = S.bound(content)
        self.assertTrue(out["truncated"])
        self.assertTrue(out["content"]["a"].startswith("x" * 4096))
        self.assertTrue(out["content"]["a"].endswith(" [… %d more chars truncated]" % (200000 - 4096)))
        self.assertEqual(1, out["content"]["keep"])
        self.assertLessEqual(out["bytes"], S.MAX_SNAPSHOT_BYTES)
        self.assertEqual(S.size_of(out["content"]), out["bytes"])

    def test_still_over_the_cap_a_preview_wrapper_stands_in(self):
        content = {"k%d" % i: "ü" * 3000 for i in range(200)}
        out = S.bound(content)
        wrapper = out["content"]
        self.assertTrue(out["truncated"])
        self.assertIs(True, wrapper["[truncated]"])
        self.assertEqual(S.size_of(content), wrapper["original_bytes"])
        self.assertGreater(wrapper["shrunk_bytes"], S.MAX_SNAPSHOT_BYTES)
        self.assertTrue(wrapper["preview"].startswith('{"k0":"üüü'))
        self.assertLessEqual(out["bytes"], S.MAX_SNAPSHOT_BYTES)
        self.assertEqual(S.size_of(wrapper), out["bytes"])
        self.assertGreater(out["bytes"], S.MAX_SNAPSHOT_BYTES - 6000, "the preview fills the budget")

    def test_a_snapshot_names_its_operation_run_and_kind(self):
        tele, mem = tracer()
        with tele.with_run({"run_id": "chat:x", "test_run_id": None}):
            with tele.operation("goal-page.read", "workflow") as op:
                snapshot_id = op.snapshot("processing_output", {"ok": True})
                odd = op.snapshot("something_else_entirely_that_is_long_enough_to_cut", {"x": 1})
        [one, two] = mem.snapshots
        self.assertEqual(snapshot_id, one["snapshot_id"])
        self.assertEqual(("processing_output", op.operation_id, op.trace_id, op.span_id, "chat:x", None, None, True, False),
                         (one["kind"], one["operation_id"], one["trace_id"], one["span_id"], one["run_id"],
                          one["onboarding_id"], one["test_run_id"], one["redacted"], one["truncated"]))
        self.assertEqual(40, len(two["kind"]))
        self.assertEqual(odd, two["snapshot_id"])
        self.assertEqual({"processing_output": snapshot_id, two["kind"]: odd}, op.snapshots)
        self.assertEqual(snapshot_id, op.attributes["bart.snapshot.processing_output"])


class TracerTests(unittest.TestCase):
    def test_ids_have_the_span_s_shape_and_children_share_the_trace(self):
        tele, mem = tracer()
        with tele.with_run({"run_id": "chat:s"}):
            with tele.operation("goal-page.read", "workflow") as root:
                with tele.operation("goals.load", "storage", reads=["goals"]) as child:
                    self.assertIs(child, tele.current())
                    self.assertEqual(root.trace_id, child.trace_id)
                    self.assertEqual(root.span_id, child.parent_span_id)
                self.assertIs(root, tele.current())
        self.assertRegex(root.trace_id, r"^[0-9a-f]{32}$")
        self.assertRegex(root.span_id, r"^[0-9a-f]{16}$")
        self.assertRegex(root.operation_id, r"^[0-9a-f-]{36}$")
        self.assertIsNone(root.parent_span_id)
        self.assertIsNone(tele.current())
        rec = mem.one("goals.load")
        self.assertEqual(("storage", "detail", "completed", "chat:s", ["goals"]),
                         (rec["type"], rec["level"], rec["status"], rec["run_id"], rec["attributes"]["engelbart.lineage.reads"]))
        self.assertEqual(rec["operation_id"], rec["attributes"]["bart.operation_id"])
        self.assertEqual("storage", rec["attributes"]["bart.type"])
        self.assertIsInstance(rec["duration_ms"], float)
        self.assertEqual(rec["duration_ms"], round(rec["duration_ms"], 3))

    def test_every_trace_has_a_workflow_root_and_nothing_records_without_one(self):
        tele, mem = tracer()
        lone = tele.start_operation("model.brainstorm", "model")
        self.assertFalse(lone.enabled)
        with tele.operation("goals.load", "storage") as op:
            op.set_attribute("x", 1)
            self.assertIsNone(op.snapshot("processing_output", {}))
        self.assertEqual([], list(mem.operations.values()))
        with tele.untraced():
            with tele.operation("goal-page.read", "workflow") as op:
                self.assertFalse(op.enabled)
        tele.configure(enabled=False)
        with tele.operation("goal-page.read", "workflow") as op:
            self.assertFalse(op.enabled)
        self.assertEqual([], mem.events)

    def test_the_run_seeds_every_operation_and_decides_the_mode(self):
        tele, mem = tracer()
        with tele.with_run({"run_id": "chat:s", "user_hash": "abcdef0123456789", "action": "read"}) as run:
            self.assertEqual(("live", "plugin", None), (run["mode"], run["environment"], run["onboarding_id"]))
            with tele.operation("goal-page.read", "workflow") as op:
                pass
        attrs = op.attributes
        self.assertEqual({"engelbart.run_id": "chat:s", "engelbart.action": "read", "engelbart.user_hash": "abcdef0123456789",
                          "engelbart.mode": "live"}, {k: v for k, v in attrs.items() if k.startswith("engelbart.")})
        rec = mem.one("goal-page.read")
        self.assertEqual(("chat:s", "read", "plugin"), (rec["run_id"], rec["action"], rec["environment"]))
        with tele.with_run({"test_run_id": "harness-1"}) as run:
            self.assertEqual(("test", "test:harness-1"), (run["mode"], run["run_id"]))
        with tele.with_run({"run_id": "chat:s", "mode": "fixture"}) as run:
            self.assertEqual("fixture", run["mode"])
        with tele.with_run({"run_id": "chat:s", "mode": "bogus"}) as run:
            self.assertEqual("live", run["mode"])
        with tele.with_run({"run_id": "chat:s"}):
            with tele.operation("goal-page.read", "workflow") as op:
                tele.set_run({"user_id": "user-1"})
                self.assertEqual(T.user_hash("user-1"), op.attributes["engelbart.user_hash"])
                with tele.operation("goals.load", "storage") as child:
                    self.assertEqual(T.user_hash("user-1"), child.attributes["engelbart.user_hash"])
        self.assertEqual(16, len(T.user_hash("user-1")))
        self.assertIsNone(T.user_hash(""))

    def test_lifecycle_events_join_their_operation_and_carry_what_the_contract_says(self):
        tele, mem = tracer()
        with tele.with_run({"run_id": "chat:s"}):
            with tele.operation("goal-page.events", "workflow") as op:
                op.event("revision", {"engelbart.goals.revision": "r1"})
                op.waiting("revision")
        started, progress, completed = mem.events
        self.assertEqual(["operation.started", "operation.progress", "operation.completed"],
                         [e["type"] for e in mem.events])
        self.assertTrue(started["sequence"] < progress["sequence"] < completed["sequence"])
        for e in mem.events:
            self.assertEqual((op.operation_id, op.trace_id, op.span_id, None, "chat:s", "goal-page.events", "workflow", "workflow"),
                             (e["operation_id"], e["trace_id"], e["span_id"], e["parent_span_id"], e["run_id"], e["name"],
                              e["operation_type"], e["level"]))
            self.assertRegex(e["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual("running", started["status"])
        self.assertIn("attributes", started)
        self.assertNotIn("duration_ms", started)
        self.assertEqual({"message": "revision", "engelbart.goals.revision": "r1"}, progress["progress"])
        self.assertNotIn("attributes", progress)
        self.assertEqual("completed", completed["status"])
        self.assertEqual("revision", completed["attributes"]["bart.waiting_reason"])
        self.assertIn("snapshots", completed)
        self.assertIsInstance(completed["duration_ms"], float)

    def test_a_failure_is_the_sanitized_error_its_detail_snapshot_and_a_failed_event(self):
        tele, mem = tracer()
        tele.protect("learned-secret-value")

        class Refused(RuntimeError):
            status_code = 502
        with tele.with_run({"run_id": "chat:s"}):
            with self.assertRaises(Refused):
                with tele.operation("goal-page.op", "workflow") as op:
                    raise Refused("the site said learned-secret-value")
        rec = mem.one("goal-page.op")
        self.assertEqual("failed", rec["status"])
        self.assertEqual({"name": "Refused", "message": "the site said [redacted]", "status_code": 502, "code": None,
                          "detail": None}, rec["error"])
        self.assertEqual(("Refused", 502), (rec["attributes"]["error.type"], rec["attributes"]["bart.error.status_code"]))
        detail = mem.snapshot(rec["snapshots"]["error_detail"])
        self.assertIn("Traceback", detail["content"]["stack"])
        self.assertNotIn("learned-secret-value", json.dumps(detail))
        failed = mem.events[-1]
        self.assertEqual("operation.failed", failed["type"])
        self.assertEqual({"name": "Refused", "message": "the site said [redacted]", "status_code": 502}, failed["error"])
        self.assertIsNotNone(rec["ended_at"])

    def test_capture_off_keeps_the_operations_and_drops_the_payloads(self):
        tele, mem = tracer(capture_content=False)
        with tele.with_run({"run_id": "chat:s"}):
            with tele.operation("goal-page.read", "workflow") as op:
                self.assertIsNone(op.snapshot("processing_output", {"big": 1}))
                self.assertIsNotNone(op.snapshot("processing_output", {"big": 1}, force=True))
        self.assertEqual(1, len(mem.snapshots))
        self.assertEqual("completed", mem.one("goal-page.read")["status"])

    def test_a_sink_that_raises_never_reaches_the_request(self):
        class Bad:
            def on_operation_start(self, record):
                raise RuntimeError("sink down")

            def on_event(self, event):
                raise RuntimeError("sink down")
        tele, mem = tracer()
        tele.add_sink(Bad())
        with mock.patch("sys.stderr"):
            with tele.with_run({"run_id": "chat:s"}):
                with tele.operation("goal-page.read", "workflow") as op:
                    with tele.operation("goals.load", "storage"):
                        pass
        self.assertEqual("completed", mem.one("goals.load")["status"])
        self.assertTrue(op.enabled)
        self.assertLessEqual(tele.logged, CORE.MAX_LOGGED_ERRORS)

    def test_the_context_is_per_thread_unless_carried_across(self):
        tele, mem = tracer()
        seen = {}
        with tele.with_run({"run_id": "chat:s"}):
            with tele.operation("goal-page.op", "workflow") as root:
                def bare():
                    seen["bare"] = tele.start_operation("goals.load", "storage").enabled
                thread = threading.Thread(target=bare)
                thread.start()
                thread.join()

                def carried():
                    with tele.operation("goals.load", "storage") as op:
                        seen["carried"] = op.parent_span_id
                context = T.context()
                thread = threading.Thread(target=context.run, args=(carried,))
                thread.start()
                thread.join()
        self.assertFalse(seen["bare"], "a thread with no context has no root")
        self.assertEqual(root.span_id, seen["carried"])

    def test_the_module_level_api_reaches_the_tracer_of_the_context(self):
        tele, mem = tracer()
        self.assertIs(T.DEFAULT, T.instance())
        with tele.with_run({"run_id": "chat:s"}):
            self.assertIs(tele, T.instance())
            with T.operation("goal-page.read", "workflow") as root:
                with T.purpose("brainstorm"):
                    self.assertEqual("brainstorm", T.current_purpose())
                    with T.operation("model." + T.current_purpose(), "model") as child:
                        pass
                self.assertIsNone(T.current_purpose())
        self.assertIs(T.DEFAULT, T.instance())
        self.assertEqual(root.span_id, mem.one("model.brainstorm")["parent_span_id"])
        self.assertFalse(T.start_operation("goals.load", "storage").enabled)

    def test_settings_come_from_the_environment_with_forwarding_off_under_a_test_runner(self):
        base = T.read_settings({})
        self.assertTrue(base["enabled"] and base["capture_content"] and base["file"])
        self.assertFalse(base["trace_polls"])
        self.assertEqual(2000, base["flush_ms"])
        self.assertTrue(T.under_test_runner(), "these tests run under unittest")
        self.assertFalse(base["forward"], "a test suite must not send itself to the member's telemetry")
        forced = T.read_settings({"ENGELBART_TELEMETRY_FORWARD": "true", "ENGELBART_TELEMETRY": "off",
                                  "ENGELBART_TRACE_CONTENT": "0", "ENGELBART_TELEMETRY_FLUSH_MS": "250",
                                  "ENGELBART_TEST_RUN": " harness-7 ", "ENGELBART_TELEMETRY_URL": "http://127.0.0.1:1/"})
        self.assertEqual((True, False, False, 250, "harness-7", "http://127.0.0.1:1"),
                         (forced["forward"], forced["enabled"], forced["capture_content"], forced["flush_ms"],
                          forced["test_run_id"], forced["site"]))
        self.assertEqual("harness-7", T.run_defaults({"ENGELBART_TEST_RUN": "harness-7"})["test_run_id"])

    def test_the_record_is_one_dict_refreshed_in_place(self):
        tele, mem = tracer()
        with tele.with_run({"run_id": "chat:s"}):
            with tele.operation("goal-page.read", "workflow") as op:
                first = op.to_json()
                self.assertEqual("running", first["status"])
        self.assertIs(first, op.to_json())
        self.assertEqual("completed", first["status"])
        self.assertEqual("flushed", tele.flush(0.1))


class FileSinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chat = self.root / "chat"
        self.chat.mkdir()

    def test_every_record_is_one_line_and_reads_back_as_the_envelope(self):
        sink = T.FileSink(self.chat / "telemetry", root=self.root)
        tele = T.Telemetry(env={}, sinks=[sink], settings={"forward": False})
        with tele.with_run({"run_id": "chat:chat"}):
            with tele.operation("goal-page.op", "workflow") as root:
                root.snapshot("processing_input", {"op": "add_goal"})
                with tele.operation("goals.save", "storage", writes=["goals"]):
                    pass
        path = self.chat / "telemetry" / "records.jsonl"
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual({"operation": 4, "snapshot": 1, "event": 4},
                         {k: len([l for l in lines if l["kind"] == k]) for k in ("operation", "snapshot", "event")})
        self.assertTrue(all(set(l) == {"kind", "at", "record"} for l in lines))
        records = T.FileSink.read(self.chat / "telemetry")
        self.assertEqual(["goal-page.op", "goals.save"], sorted(op["name"] for op in records["operations"]))
        self.assertTrue(all(op["status"] == "completed" for op in records["operations"]), "the later line wins")
        envelope = T.FileSink.envelope(self.chat / "telemetry", run_id="chat:chat")
        self.assertEqual("chat:chat", envelope["run"]["run_id"])
        self.assertEqual(4, len(envelope["events"]))
        self.assertEqual(root.operation_id, envelope["snapshots"][0]["operation_id"])

    def test_a_broken_line_is_skipped_and_the_file_rotates_past_its_bound(self):
        folder = self.chat / "telemetry"
        sink = T.FileSink(folder, root=self.root)
        with mock.patch.object(SINKS, "ROTATE_BYTES", 400):
            sink.on_event({"event_id": "1", "x": "y" * 300})
            (folder / "records.jsonl").open("a").write("{not json\n")
            sink.on_event({"event_id": "2", "x": "z" * 300})
        names = sorted(p.name for p in folder.glob("*.jsonl"))
        self.assertEqual(2, len(names), names)
        self.assertEqual(["2"], [e["event_id"] for e in T.FileSink.read(folder)["events"]])

    def test_a_directory_that_cannot_be_written_is_logged_not_raised(self):
        blocked = self.chat / "telemetry"
        blocked.write_text("a file where the directory should be")
        sink = T.FileSink(blocked, root=self.root)
        tele = T.Telemetry(env={}, sinks=[sink], settings={"forward": False})
        with mock.patch("sys.stderr"):
            with tele.with_run({"run_id": "chat:chat"}):
                with tele.operation("goal-page.read", "workflow") as op:
                    pass
        self.assertTrue(op.enabled)
        self.assertEqual("completed", op.status)
        self.assertGreater(tele.logged, 0)


class FakeSite:
    """A stand-in for the site's endpoint: records every POST, answers as told."""
    def __init__(self, answers=None):
        self.posts = []
        self.answers = list(answers or [])
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                site.posts.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                status, answer = site.answers.pop(0) if site.answers else (200, {"ok": True, "accepted": {"operations": 1}})
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.server.allow_reuse_address = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class ForwardSinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "chat" / "telemetry"
        self.run = {"run_id": "chat:chat", "mode": "live", "environment": "plugin", "code_version": "0.0.0",
                    "deployment": None, "label": "Dataset importer", "user_hash": None}
        self.clock = [1000.0]

    def sink(self, post, session=None, **kw):
        kw.setdefault("flush_ms", 50)
        return T.ForwardSink(self.folder, self.run, root=self.root, post=post, now=lambda: self.clock[0],
                             session=session or (lambda: {"access_token": TOKEN, "user_id": "user-1", "site": "https://site.example"}),
                             site="https://site.example", **kw)

    def record(self, sink, n=1, kind="goal-page.read"):
        tele = T.Telemetry(env={}, sinks=[sink], settings={"forward": False})
        with tele.with_run(self.run):
            for _ in range(n):
                with tele.operation(kind, "workflow") as op:
                    op.snapshot("processing_output", {"ok": True})
        return tele

    def test_records_leave_as_one_envelope_on_the_member_s_session(self):
        posts = []

        def post(url, body, headers, timeout):
            posts.append((url, json.loads(body), headers))
            return 200, json.dumps({"ok": True, "accepted": {"operations": 2, "snapshots": 1, "events": 2}})
        sink = self.sink(post)
        self.record(sink)
        self.assertTrue(sink.flush(3))
        [(url, envelope, headers)] = posts
        self.assertEqual("https://site.example/api/engelbart-telemetry", url)
        self.assertEqual("Bearer " + TOKEN, headers["Authorization"])
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual("1", envelope["contract_version"])
        self.assertEqual({"run_id": "chat:chat", "mode": "live", "environment": "plugin", "code_version": "0.0.0",
                          "deployment": None, "origin": "goal-page", "label": "Dataset importer"}, envelope["run"])
        self.assertEqual(["goal-page.read"], [op["name"] for op in envelope["operations"]])
        self.assertEqual("completed", envelope["operations"][0]["status"], "the later record of an operation wins")
        self.assertEqual(["processing_output"], [s["kind"] for s in envelope["snapshots"]])
        self.assertEqual(["operation.started", "operation.completed"], [e["type"] for e in envelope["events"]])
        self.assertEqual(T.user_hash("user-1"), self.run["user_hash"], "the session names the member for the next records")
        self.assertEqual({"posted": 1, "accepted": 2, "spooled": 0, "replayed": 0, "rejected": 0, "failures": 0, "last_error": ""}, sink.stats)
        self.assertFalse(sink.outbox.exists())
        self.assertNotIn(TOKEN, json.dumps(envelope))
        sink.close()

    def test_a_full_batch_goes_at_once_and_the_rest_follows(self):
        posts = []

        def post(url, body, headers, timeout):
            posts.append(json.loads(body))
            return 200, "{}"
        with mock.patch.object(SINKS, "MAX_BATCH_OPERATIONS", 3):
            sink = self.sink(post, flush_ms=5000)
            self.record(sink, n=5)
            self.assertTrue(sink.flush(3))
        self.assertGreaterEqual(len(posts), 2)
        self.assertEqual(5, len({op["operation_id"] for p in posts for op in p["operations"]}))
        self.assertTrue(all(len(p["operations"]) <= 3 for p in posts))
        sink.close()

    def test_not_signed_in_spools_and_a_session_later_replays(self):
        posts = []
        state = {"session": None}

        def post(url, body, headers, timeout):
            posts.append(json.loads(body))
            return 200, "{}"
        sink = self.sink(post, session=lambda: state["session"])
        with mock.patch("sys.stderr"):
            self.record(sink)
            self.assertTrue(sink.flush(3))
        self.assertEqual([], posts)
        self.assertEqual(1, len(list(sink.outbox.glob("*.json"))))
        self.assertEqual(0o600, next(sink.outbox.glob("*.json")).stat().st_mode & 0o777)
        state["session"] = {"access_token": TOKEN, "user_id": "user-1", "site": "https://site.example"}
        self.clock[0] += 3600
        self.record(sink)
        self.assertTrue(sink.flush(3))
        self.assertTrue(wait_for(lambda: len(posts) == 2 and not list(sink.outbox.glob("*.json"))))
        self.assertEqual(1, sink.stats["replayed"])
        sink.close()

    def test_a_site_that_is_down_spools_and_backs_off(self):
        calls = []

        def post(url, body, headers, timeout):
            calls.append(1)
            raise T.Unavailable("connection refused")
        sink = self.sink(post)
        with mock.patch("sys.stderr"):
            self.record(sink)
            self.assertTrue(sink.flush(3))
            self.assertEqual(1, len(calls))
            self.assertEqual(1, len(list(sink.outbox.glob("*.json"))))
            self.record(sink)
            self.assertTrue(sink.flush(3))
        self.assertEqual(1, len(calls), "within the backoff, nothing is attempted")
        self.assertEqual(2, len(list(sink.outbox.glob("*.json"))))
        self.assertEqual(1, sink.stats["failures"])
        self.assertIn("connection refused", sink.stats["last_error"])
        sink.close()

    def test_a_5xx_or_network_failure_is_retried_later_and_a_400_is_set_aside(self):
        answers = [(503, "down"), (400, json.dumps({"error": "operations[0].name is not in the expected form"}))]
        posts = []

        def post(url, body, headers, timeout):
            posts.append(json.loads(body))
            return answers.pop(0) if answers else (200, "{}")
        sink = self.sink(post)
        with mock.patch("sys.stderr"):
            self.record(sink)
            self.assertTrue(sink.flush(3))
            self.assertEqual(1, len(list(sink.outbox.glob("*.json"))), "a 503 waits in the outbox")
            self.clock[0] += 3600
            self.record(sink)
            self.assertTrue(sink.flush(3))
        self.assertTrue(wait_for(lambda: sink.stats["rejected"] == 1 and sink.stats["replayed"] == 1))
        self.assertEqual([], list(sink.outbox.glob("*.json")))
        [kept] = list((sink.outbox / "rejected").glob("*.json"))
        self.assertIn("not in the expected form", json.loads(kept.read_text())["reason"])
        sink.close()

    def test_a_401_forgets_the_session_and_asks_again(self):
        sessions = []
        answers = [(401, json.dumps({"error": "Your Engelbart session has expired"}))]

        def session():
            sessions.append(1)
            return {"access_token": TOKEN, "user_id": "user-1", "site": "https://site.example"}

        def post(url, body, headers, timeout):
            return answers.pop(0) if answers else (200, "{}")
        sink = self.sink(post, session=session)
        with mock.patch("sys.stderr"):
            self.record(sink)
            self.assertTrue(sink.flush(3))
            self.clock[0] += 3600
            self.record(sink)
            self.assertTrue(sink.flush(3))
        self.assertTrue(wait_for(lambda: not list(sink.outbox.glob("*.json"))))
        self.assertEqual(2, len(sessions), "the session is read once, and again after it was refused")
        sink.close()

    def test_the_real_post_reaches_a_real_endpoint(self):
        site = FakeSite()
        self.addCleanup(site.close)
        sink = T.ForwardSink(self.folder, self.run, root=self.root, flush_ms=50,
                             session=lambda: {"access_token": TOKEN, "user_id": "user-1", "site": site.url}, site=site.url)
        self.record(sink)
        self.assertTrue(sink.flush(3))
        self.assertTrue(wait_for(lambda: site.posts))
        [received] = site.posts
        self.assertEqual("/api/engelbart-telemetry", received["path"])
        self.assertEqual("Bearer " + TOKEN, received["headers"]["Authorization"])
        self.assertEqual("chat:chat", received["body"]["run"]["run_id"])
        sink.close()

    def test_the_default_session_is_the_machine_s_own_and_never_the_machine_token(self):
        from human_compact.trajectory import supabase_client as SB
        with mock.patch.dict(os.environ, {"HUMAN_COMPACT_HOME": str(self.root / "home")}):
            self.assertIsNone(SINKS.default_session(self.root), "no session, no credentials: nothing to send with")
            (self.root / "supabase-session.json").write_text(json.dumps({
                "access_token": TOKEN, "refresh_token": "", "expires_at": int(time.time()) + 3600,
                "user_id": "user-1", "email": "m@example.com"}))
            found = SINKS.default_session(self.root)
        self.assertEqual({"access_token": TOKEN, "user_id": "user-1", "site": SINKS.DEFAULT_SITE}, found)
        self.assertEqual(SB.load_session(self.root)["access_token"], TOKEN)


if __name__ == "__main__":
    unittest.main()
