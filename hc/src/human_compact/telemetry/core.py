"""Bart telemetry: the one small API the rest of the plugin talks to.

    from human_compact import telemetry as TELEMETRY

    with TELEMETRY.operation("bart.answer", "processing") as op:
        op.set_attribute("engelbart.bart.card", card["card"])
        op.snapshot("processing_output", card)

Underneath, an operation is one record with span-shaped ids
(``operation``), the parent link is kept in a context variable so an
operation started anywhere inside another -- a provider call three
modules down -- lands under it, everything recorded is redacted
(``redaction``), payloads are snapshots (``snapshots``), lifecycle events
are emitted as they happen, and all of it goes to the sinks (``sinks``):
the file under the chat's directory, and the site.

Observability is subordinate to the product: nothing in here may raise into
a request. Every sink call runs through ``safely``; a start that fails
hands back a no-op operation and the work proceeds untraced.

The run is one chat workspace: ``run_id`` is ``chat:<session id>``; every
request the goal page makes is one trace, rooted in a workflow operation,
whose id the reply names in ``x-engelbart-trace-id``.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from . import redaction as Redaction
from . import snapshots as Snapshots
from .contract import EVENT_TYPES, MODES, TYPES
from .operation import NoopOperation, Operation

MAX_LOGGED_ERRORS = 20
TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off"}
DEFAULT_SITE = "https://berkeley.mathetic.com"

_INSTANCE: contextvars.ContextVar = contextvars.ContextVar("engelbart.telemetry.instance", default=None)
_RUN: contextvars.ContextVar = contextvars.ContextVar("engelbart.telemetry.run", default=None)
_OPERATION: contextvars.ContextVar = contextvars.ContextVar("engelbart.telemetry.operation", default=None)
_SUPPRESS: contextvars.ContextVar = contextvars.ContextVar("engelbart.telemetry.suppress", default=False)
_PURPOSE: contextvars.ContextVar = contextvars.ContextVar("engelbart.telemetry.purpose", default=None)

_SEQUENCE_LOCK = threading.Lock()
_SEQUENCE = 0


def next_sequence() -> int:
    global _SEQUENCE
    with _SEQUENCE_LOCK:
        _SEQUENCE += 1
        return _SEQUENCE


def flag(env: Dict[str, str], name: str, fallback: bool = False) -> bool:
    """An explicit true/false wins; anything else is the caller's default."""
    value = str(env.get(name) or "").strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    return fallback


def under_test_runner() -> bool:
    """Whether this process is a test run.

    The site persists nothing under ``node --test`` unless asked; the same
    rule here keeps a test suite that inherits a real account from sending
    itself to the member's telemetry. ``ENGELBART_TELEMETRY_FORWARD=true``
    still forces forwarding on.
    """
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = str(getattr(spec, "name", "") or "")
    if name in ("unittest.__main__", "unittest", "pytest", "pytest.__main__"):
        return True
    return "pytest" in sys.modules


def read_settings(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Everything the environment decides. On by default, everywhere it can
    work; ``ENGELBART_TELEMETRY=off`` records nothing at all."""
    source = os.environ if env is None else env
    forward_default = not under_test_runner()
    try:
        flush_ms = int(float(source.get("ENGELBART_TELEMETRY_FLUSH_MS") or 0)) or 2000
    except (TypeError, ValueError):
        flush_ms = 2000
    return {
        "enabled": flag(source, "ENGELBART_TELEMETRY", True),
        "capture_content": flag(source, "ENGELBART_TRACE_CONTENT", True),
        "trace_polls": flag(source, "ENGELBART_TRACE_POLLS", False),
        "file": flag(source, "ENGELBART_TELEMETRY_FILE", True),
        "forward": flag(source, "ENGELBART_TELEMETRY_FORWARD", forward_default),
        "log": flag(source, "ENGELBART_TELEMETRY_LOG", False),
        "flush_ms": flush_ms,
        "test_run_id": str(source.get("ENGELBART_TEST_RUN") or "").strip()[:80] or None,
        "site": str(source.get("ENGELBART_TELEMETRY_URL") or "").strip().rstrip("/") or None,
    }


def version() -> Optional[str]:
    try:
        from importlib.metadata import version as _version
        return _version("human-compact")
    except Exception:                                        # noqa: BLE001
        return None


def run_defaults(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    settings = read_settings(env)
    return {
        "run_id": None,
        "onboarding_id": None,
        "test_run_id": settings["test_run_id"],
        "user_hash": None,
        "action": None,
        "mode": None,
        "environment": "plugin",
        "code_version": version(),
        "deployment": None,
    }


def run_attributes(run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = {}
    for key in ("run_id", "onboarding_id", "test_run_id", "action", "user_hash", "mode"):
        if run and run.get(key) is not None:
            out["engelbart.%s" % key] = run[key]
    return out


def user_hash(user_id: Any) -> Optional[str]:
    """A member is named in telemetry by a short hash of their id, never
    by email: the first 16 hex characters of sha256(id)."""
    if not user_id:
        return None
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]


def lifecycle_event(type_: str, operation: Operation, progress: Optional[Dict[str, Any]] = None,
                    error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The Bart Event entity: the same trace, span and parent ids as the
    operation, so an event and its operation are one thing."""
    run = operation.run or {}
    event: Dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "sequence": next_sequence(),
        "type": type_ if type_ in EVENT_TYPES else "operation.progress",
        "at": Snapshots.now_iso(),
        "operation_id": operation.operation_id,
        "trace_id": operation.trace_id,
        "span_id": operation.span_id,
        "parent_span_id": operation.parent_span_id,
        "run_id": run.get("run_id"),
        "onboarding_id": run.get("onboarding_id"),
        "test_run_id": run.get("test_run_id"),
        "action": run.get("action"),
        "name": operation.name,
        "operation_type": operation.type,
        "level": operation.level,
        "status": operation.status,
    }
    if type_ in ("operation.started", "operation.completed", "operation.failed"):
        event["attributes"] = dict(operation.attributes)
    if type_ in ("operation.completed", "operation.failed"):
        event["duration_ms"] = (None if operation.duration_ms is None
                                else round(operation.duration_ms, 3))
        event["snapshots"] = dict(operation.snapshots)
    if progress:
        event["progress"] = progress
    if error:
        event["error"] = {"name": error.get("name"), "message": error.get("message"),
                          "status_code": error.get("status_code")}
    return event


class Telemetry:
    """One tracer. The server keeps one per workspace; the module-level
    functions below reach the one active in the current context, so code
    that knows nothing about servers (a provider) still records under the
    request that called it."""

    def __init__(self, env: Optional[Dict[str, str]] = None, sinks: Optional[List[Any]] = None,
                 settings: Optional[Dict[str, Any]] = None):
        self.env = os.environ if env is None else env
        self.settings = dict(read_settings(self.env))
        self.settings.update(settings or {})
        self.sinks: List[Any] = []
        self._live_lock = threading.Lock()
        self._live = 0
        self.logged = 0
        self._extra_secrets: set = set()
        self._secret_cache: Optional[set] = None
        self._lock = threading.Lock()
        for sink in sinks or []:
            self.add_sink(sink)
        if self.settings.get("log") and not sinks:
            from .sinks import ConsoleSink
            self.add_sink(ConsoleSink())

    # --- configuration ---------------------------------------------------

    def configure(self, **settings) -> "Telemetry":
        self.settings.update(settings)
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def add_sink(self, sink: Any):
        with self._lock:
            self.sinks.append(sink)

        def remove():
            with self._lock:
                self.sinks = [s for s in self.sinks if s is not sink]
        return remove

    def protect(self, value: Any) -> "Telemetry":
        """A secret learned at runtime (a session token) joins the redaction set."""
        text = str(value or "")
        if len(text) >= 8:
            with self._lock:
                self._extra_secrets.add(text)
                self._secret_cache = None
        return self

    def secrets(self) -> set:
        cache = self._secret_cache
        if cache is None:
            cache = Redaction.secret_values(self.env, list(self._extra_secrets))
            self._secret_cache = cache
        return cache

    def redact(self, value: Any, **options) -> Any:
        return Redaction.redact(value, secrets=self.secrets(), **options)

    def redact_scalar(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return Redaction.redact_string(value, self.secrets(), 4000)

    # --- never into the product ------------------------------------------

    def log(self, message: str) -> None:
        if self.logged >= MAX_LOGGED_ERRORS:
            return
        self.logged += 1
        try:
            print("engelbart-telemetry: %s" % message, file=sys.stderr, flush=True)
        except Exception:                                    # noqa: BLE001
            pass

    def safely(self, what: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:                             # noqa: BLE001
            self.log("%s failed: %s" % (what, str(exc)[:200]))
            return None

    def fanout(self, method: str, record: Any) -> None:
        for sink in list(self.sinks):
            handler = getattr(sink, method, None)
            if callable(handler):
                self.safely("sink %s" % method, handler, record)

    # --- context ---------------------------------------------------------

    def current(self) -> Optional[Operation]:
        op = _OPERATION.get()
        return op if op is not None and op.telemetry is self else None

    def run(self) -> Optional[Dict[str, Any]]:
        return _RUN.get()

    @staticmethod
    def suppressed() -> bool:
        return _SUPPRESS.get() is True

    @contextmanager
    def untraced(self) -> Iterator[None]:
        """Run the block with tracing off: no operations, no events."""
        token = _SUPPRESS.set(True)
        try:
            yield
        finally:
            _SUPPRESS.reset(token)

    @contextmanager
    def with_run(self, seed: Optional[Dict[str, Any]] = None, **more) -> Iterator[Dict[str, Any]]:
        """Run the block inside a fresh run context (the Bart Run).
        Operations started within inherit its ids as attributes and in
        their records. The dict handed back is the block's own copy."""
        run = dict(run_defaults(self.env))
        run.update(seed or {})
        run.update(more)
        if run.get("mode") not in MODES:
            run["mode"] = "test" if run.get("test_run_id") else "live"
        if not run.get("run_id"):
            run["run_id"] = (run.get("onboarding_id")
                             or ("test:%s" % run["test_run_id"] if run.get("test_run_id") else None))
        tokens = (_RUN.set(run), _INSTANCE.set(self))
        try:
            yield run
        finally:
            _INSTANCE.reset(tokens[1])
            _RUN.reset(tokens[0])

    def set_run(self, patch: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Learn something about the current run after it started."""
        run = self.run()
        if run is None:
            return None
        values = dict(patch or {})
        if values.get("user_id"):
            values["user_hash"] = user_hash(values.pop("user_id"))
        else:
            values.pop("user_id", None)
        run.update(values)
        op = self.current()
        if op is not None:
            op.set_attributes(run_attributes(run))
        return run

    # --- operations ------------------------------------------------------

    def start_operation(self, name: str, type_: str, *, level: Optional[str] = None,
                        attributes: Optional[Dict[str, Any]] = None, reads: Any = None,
                        writes: Any = None, parent: Any = None):
        """Every trace has a workflow root. A model, storage, http or
        processing operation started with no operation above it is
        untraced: a CLI command that calls a provider opens no workflow and
        must not leave rootless records behind."""
        if not self.enabled or self.suppressed():
            return NoopOperation()
        above = parent if parent is not None and getattr(parent, "enabled", False) else None
        if above is None:
            above = self.current()
        if type_ != "workflow" and above is None:
            return NoopOperation()
        try:
            run = above.run if above is not None else self.run()
            if run is None:
                run = dict(run_defaults(self.env))
            merged = dict(run_attributes(run))
            merged.update(attributes or {})
            op = Operation(self, name=name, type_=type_, run=run, level=level,
                           trace_id=above.trace_id if above is not None else None,
                           parent_span_id=above.span_id if above is not None else None,
                           attributes=merged, reads=reads, writes=writes)
            with self._live_lock:
                self._live += 1
            self.fanout("on_operation_start", op.to_json())
            self.emit("operation.started", op)
            return op
        except Exception as exc:                             # noqa: BLE001
            self.log("could not start operation %s: %s" % (name, str(exc)[:200]))
            return NoopOperation()

    @contextmanager
    def operation(self, name: str, type_: str, **spec) -> Iterator[Any]:
        """The preferred form: the block runs with the operation as the
        active parent, is completed when it returns and failed when it
        raises; the original exception is re-raised untouched."""
        op = self.start_operation(name, type_, **spec)
        if not op.enabled:
            yield op
            return
        tokens = (_OPERATION.set(op), _RUN.set(op.run), _INSTANCE.set(self))
        try:
            yield op
        except BaseException as exc:
            op.fail(exc)
            raise
        else:
            op.complete()
        finally:
            _INSTANCE.reset(tokens[2])
            _RUN.reset(tokens[1])
            _OPERATION.reset(tokens[0])

    # --- records ---------------------------------------------------------

    def capture_snapshot(self, operation: Any, kind: str, value: Any, force: bool = False,
                         max_bytes: Optional[int] = None) -> Optional[str]:
        if not getattr(operation, "enabled", False):
            return None
        if not self.settings.get("capture_content", True) and not force:
            return None

        def build():
            snapshot = Snapshots.build(operation, kind, self.redact(value), max_bytes=max_bytes)
            self.fanout("on_snapshot", snapshot)
            return snapshot["snapshot_id"]
        return self.safely("snapshot", build)

    def emit(self, type_: str, operation: Any, progress: Optional[Dict[str, Any]] = None,
             error: Optional[Dict[str, Any]] = None) -> None:
        if not getattr(operation, "enabled", False):
            return
        self.safely("event", lambda: self.fanout(
            "on_event", lifecycle_event(type_, operation, progress=progress, error=error)))

    def ended(self, operation: Operation) -> None:
        # Written before it is counted as over: a server draining on
        # live() then finds the closing record in its sinks.
        self.fanout("on_operation_end", operation.to_json())
        with self._live_lock:
            self._live -= 1

    def live(self) -> int:
        """Operations started and not yet ended: what a server waits on
        before its sinks are closed, so a stream told to close writes its
        closing record first."""
        with self._live_lock:
            return self._live

    # --- settling --------------------------------------------------------

    def flush(self, timeout: Optional[float] = None) -> str:
        """Settle every sink within a bound (seconds; the flush setting by
        default). Never raises."""
        budget = (self.settings.get("flush_ms", 2000) / 1000.0) if timeout is None else float(timeout)
        outcome = "flushed"
        for sink in list(self.sinks):
            flush = getattr(sink, "flush", None)
            if not callable(flush):
                continue
            result = self.safely("sink flush", flush, budget)
            if result is False:
                outcome = "timeout"
        return outcome

    def close(self) -> None:
        for sink in list(self.sinks):
            close = getattr(sink, "close", None)
            if callable(close):
                self.safely("sink close", close)


# --- the module-level API: whichever tracer is active here -------------------

DEFAULT = Telemetry()


def instance() -> Telemetry:
    """The tracer of the current context: a server's, inside its requests;
    the process default -- which has no sinks and no run -- elsewhere."""
    found = _INSTANCE.get()
    return found if found is not None else DEFAULT


def current() -> Optional[Operation]:
    return instance().current()


def run() -> Optional[Dict[str, Any]]:
    return _RUN.get()


def operation(name: str, type_: str, **spec):
    return instance().operation(name, type_, **spec)


def start_operation(name: str, type_: str, **spec):
    return instance().start_operation(name, type_, **spec)


def untraced():
    return instance().untraced()


def with_run(seed: Optional[Dict[str, Any]] = None, **more):
    return instance().with_run(seed, **more)


@contextmanager
def purpose(name: str) -> Iterator[None]:
    """What a model call is for, named by the caller that knows: the
    provider records ``model.<purpose>``."""
    token = _PURPOSE.set(str(name))
    try:
        yield
    finally:
        _PURPOSE.reset(token)


def current_purpose() -> Optional[str]:
    return _PURPOSE.get()


def context() -> contextvars.Context:
    """The context to carry into a thread that continues this work."""
    return contextvars.copy_context()


@contextmanager
def activate(op):
    """Carry an explicitly owned asynchronous lifecycle through a local scope."""
    if op is None or not op.enabled:
        yield op
        return
    tokens = (_OPERATION.set(op), _RUN.set(op.run), _INSTANCE.set(op.telemetry))
    try:
        yield op
    finally:
        _INSTANCE.reset(tokens[2])
        _RUN.reset(tokens[1])
        _OPERATION.reset(tokens[0])
