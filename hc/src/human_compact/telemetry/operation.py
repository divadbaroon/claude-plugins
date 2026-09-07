"""One operation: an execution with a lifetime (running -> completed or
failed). Events happen INSIDE an operation and are lifecycle events, never
operations of their own. The record an operation renders (``to_json``) is
the Bart Operation entity of the contract.

There is no OpenTelemetry SDK here -- the plugin has no dependencies and its
wheel travels inside a compiled binary -- so the ids are made the way a
span's are (a 16-byte trace id, an 8-byte span id, both hex) and the parent
link is kept by the tracer's context (``core``). A collector that wants
these as spans has every field it needs.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, Optional

from .contract import (LINEAGE_ATTRIBUTES, TYPES, attribute_value, level_of,
                       lineage_names)
from .redaction import sanitize_error
from .snapshots import kind_of, now_iso


def new_trace_id() -> str:
    return os.urandom(16).hex()


def new_span_id() -> str:
    return os.urandom(8).hex()


class Operation:
    def __init__(self, telemetry, *, name: str, type_: str, run: Optional[Dict[str, Any]] = None,
                 level: Optional[str] = None, trace_id: Optional[str] = None,
                 parent_span_id: Optional[str] = None, attributes: Optional[Dict[str, Any]] = None,
                 reads: Any = None, writes: Any = None):
        self.telemetry = telemetry
        self.enabled = True
        self.operation_id = str(uuid.uuid4())
        self.trace_id = trace_id or new_trace_id()
        self.span_id = new_span_id()
        self.parent_span_id = parent_span_id or None
        self.name = str(name)
        self.type = type_ if type_ in TYPES else "processing"
        self.level = level_of(self.name, self.type, level)
        self.status = "running"
        self.started_at = now_iso()
        self._started_ns = time.perf_counter_ns()
        self.ended_at: Optional[str] = None
        self.duration_ms: Optional[float] = None
        self.attributes: Dict[str, Any] = {}
        self.snapshots: Dict[str, str] = {}
        self.error: Optional[Dict[str, Any]] = None
        # The run is a shared, mutable dict for the request: what it learns
        # after this operation started (the member's hash, once a session
        # is read) reaches every operation started after that.
        self.run = run
        # One record dict per operation, updated in place by to_json(): a
        # sink that keeps it sees later fields arrive.
        self.record: Optional[Dict[str, Any]] = None
        self.set_attributes({"bart.operation_id": self.operation_id,
                             "bart.type": self.type, **(attributes or {})})
        if reads:
            self.reads(reads)
        if writes:
            self.writes(writes)

    @property
    def ended(self) -> bool:
        return self.status not in ("running", "waiting")

    # Lineage: which stored values this operation read and wrote, by name.
    def reads(self, *names) -> "Operation":
        return self.lineage("reads", names)

    def writes(self, *names) -> "Operation":
        return self.lineage("writes", names)

    def lineage(self, kind: str, names) -> "Operation":
        key = LINEAGE_ATTRIBUTES[kind]
        have = self.attributes.get(key)
        listed = lineage_names(names, have if isinstance(have, list) else [])
        if listed:
            self.set_attribute(key, listed)
        return self

    def set_attribute(self, key: str, value: Any) -> "Operation":
        safe = attribute_value(self.telemetry.redact_scalar(value))
        if safe is None:
            return self
        self.attributes[str(key)] = safe
        return self

    def set_attributes(self, values: Optional[Dict[str, Any]]) -> "Operation":
        for key, value in (values or {}).items():
            self.set_attribute(key, value)
        return self

    def _safe_attributes(self, attributes: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        safe = {}
        for key, value in (attributes or {}).items():
            v = attribute_value(self.telemetry.redact_scalar(value))
            if v is not None:
                safe[str(key)] = v
        return safe

    # Something that happened inside the operation: a retry, a revision
    # noticed, a stage reached. An ``operation.progress`` live event.
    def event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> "Operation":
        if self.ended:
            return self
        safe = self._safe_attributes(attributes)
        progress = {"message": str(name)}
        progress.update(safe)
        self.telemetry.emit("operation.progress", self, progress=progress)
        return self

    # Waiting on something outside it: a stream idle until the goals change.
    def waiting(self, reason: Optional[str] = None) -> "Operation":
        if self.ended:
            return self
        self.status = "waiting"
        if reason:
            self.set_attribute("bart.waiting_reason", reason)
        return self

    # A payload snapshot attached to this operation, by kind. Returns the
    # snapshot id, or None when detailed capture is off or the value is empty.
    def snapshot(self, kind: str, value: Any, force: bool = False,
                 max_bytes: Optional[int] = None) -> Optional[str]:
        kind = kind_of(kind)
        snapshot_id = self.telemetry.capture_snapshot(self, kind, value, force=force,
                                                      max_bytes=max_bytes)
        if snapshot_id:
            self.snapshots[kind] = snapshot_id
            self.set_attribute("bart.snapshot.%s" % kind, snapshot_id)
        return snapshot_id

    def complete(self, attributes: Optional[Dict[str, Any]] = None) -> "Operation":
        if self.ended:
            return self
        if attributes:
            self.set_attributes(attributes)
        self.status = "completed"
        self._finish()
        # The event first, the record last: ended() is what a server
        # draining the tracer waits on, so everything of the operation is
        # in the sinks by the time it is no longer live.
        self.telemetry.emit("operation.completed", self)
        self.telemetry.ended(self)
        return self

    def fail(self, error: Any, attributes: Optional[Dict[str, Any]] = None) -> "Operation":
        if self.ended:
            return self
        if attributes:
            self.set_attributes(attributes)
        self.status = "failed"
        self.error = (sanitize_error(error, secrets=self.telemetry.secrets())
                      or {"name": "Error", "message": "failed"})
        self._finish()
        self.set_attributes({"error.type": self.error["name"],
                             "bart.error.status_code": self.error.get("status_code")})
        self.snapshot("error_detail", sanitize_error(error, secrets=self.telemetry.secrets(),
                                                     stack=True))
        self.telemetry.emit("operation.failed", self, error=self.error)
        self.telemetry.ended(self)
        return self

    def _finish(self) -> None:
        self.ended_at = now_iso()
        self.duration_ms = (time.perf_counter_ns() - self._started_ns) / 1e6

    def to_json(self) -> Dict[str, Any]:
        """The Bart Operation entity. The same dict every time, refreshed."""
        run = self.run or {}
        if self.record is None:
            self.record = {}
        error = None
        if self.error:
            error = {"name": self.error.get("name"), "message": self.error.get("message"),
                     "status_code": self.error.get("status_code"),
                     "code": self.error.get("code"), "detail": self.error.get("detail")}
        self.record.update({
            "operation_id": self.operation_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "run_id": run.get("run_id"),
            "onboarding_id": run.get("onboarding_id"),
            "test_run_id": run.get("test_run_id"),
            "action": run.get("action"),
            "name": self.name,
            "type": self.type,
            "level": self.level,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": None if self.duration_ms is None else round(self.duration_ms, 3),
            "attributes": dict(self.attributes),
            "snapshots": dict(self.snapshots),
            "error": error,
            "environment": run.get("environment"),
            "code_version": run.get("code_version"),
            "deployment": run.get("deployment"),
        })
        return self.record

    def __enter__(self) -> "Operation":
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc is not None:
            self.fail(exc)
        else:
            self.complete()
        return False


class NoopOperation:
    """What an operation is when tracing is off, suppressed, or rootless:
    every method is safe to call and does nothing."""
    enabled = False
    operation_id = None
    trace_id = ""
    span_id = ""
    parent_span_id = None
    name = ""
    type = "processing"
    level = "detail"
    status = "running"
    run = None

    def __init__(self):
        self.attributes: Dict[str, Any] = {}
        self.snapshots: Dict[str, str] = {}
        self.error = None

    ended = False

    def set_attribute(self, *_a, **_k): return self
    def set_attributes(self, *_a, **_k): return self
    def reads(self, *_a): return self
    def writes(self, *_a): return self
    def event(self, *_a, **_k): return self
    def waiting(self, *_a): return self
    def snapshot(self, *_a, **_k): return None
    def complete(self, *_a, **_k): return self
    def fail(self, *_a, **_k): return self
    def to_json(self): return None
    def __enter__(self): return self
    def __exit__(self, *_a): return False
