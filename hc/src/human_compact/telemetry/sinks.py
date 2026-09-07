"""Where records go.

A sink is anything with some of ``on_operation_start(record)``,
``on_operation_end(record)``, ``on_snapshot(snapshot)``, ``on_event(event)``,
``flush(timeout)`` and ``close()``. The tracer calls each through
``safely``: a sink that raises is logged once and never reaches a request.

* ``MemorySink`` keeps everything in lists; tests and the envelope builder
  read it.
* ``FileSink`` appends one JSON line per record to ``telemetry/records.jsonl``
  under the chat's own directory: the local truth, written whether or not
  the machine is signed in, readable back as an envelope.
* ``ForwardSink`` batches records and POSTs them as contract envelopes to
  the site's ``/api/engelbart-telemetry`` on the member's own session, from
  a background thread that never holds a request; what cannot be sent now
  waits in ``telemetry/outbox`` for the next chance.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import contract as Contract
from .snapshots import dumps, now_iso

RECORDS_NAME = "records.jsonl"
OUTBOX_NAME = "outbox"
REJECTED_NAME = "rejected"
ROTATE_BYTES = 50 * 1024 * 1024
ENDPOINT = "/api/engelbart-telemetry"
DEFAULT_SITE = "https://berkeley.mathetic.com"
USER_AGENT = "engelbart-goal-page-telemetry/1"

# One POST at most this big, and this many records; the site refuses more.
MAX_BATCH_BYTES = 3_000_000
MAX_BATCH_OPERATIONS = 400
MAX_BATCH_EVENTS = 1500
MAX_BATCH_SNAPSHOTS = 400
POST_TIMEOUT_S = 20.0
RETRY_BASE_S = 30.0
RETRY_MAX_S = 900.0
OUTBOX_LIMIT = 200
REPLAY_PER_CYCLE = 20


class MemorySink:
    def __init__(self, limit: int = 20000):
        self.limit = limit
        self.operations: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.starts: List[Dict[str, Any]] = []
        self.snapshots: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.flushed = 0
        self._lock = threading.Lock()

    def on_operation_start(self, record):
        with self._lock:
            self.starts.append(dict(record))
            self.operations[record["operation_id"]] = record

    def on_operation_end(self, record):
        with self._lock:
            self.operations[record["operation_id"]] = record

    def on_snapshot(self, snapshot):
        with self._lock:
            self.snapshots.append(snapshot)
            del self.snapshots[:-self.limit]

    def on_event(self, event):
        with self._lock:
            self.events.append(event)
            del self.events[:-self.limit]

    def flush(self, _timeout=None):
        self.flushed += 1
        return True

    def find(self, name: str) -> List[Dict[str, Any]]:
        return [dict(op) for op in self.operations.values() if op.get("name") == name]

    def one(self, name: str) -> Dict[str, Any]:
        found = self.find(name)
        if len(found) != 1:
            raise LookupError("%d operations named %s" % (len(found), name))
        return found[0]

    def snapshot(self, snapshot_id: Optional[str]) -> Optional[Dict[str, Any]]:
        for s in self.snapshots:
            if s.get("snapshot_id") == snapshot_id:
                return s
        return None

    def envelope(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            return Contract.bundle({"operations": [dict(op) for op in self.operations.values()],
                                    "snapshots": list(self.snapshots),
                                    "events": list(self.events)}, run_id=run_id)

    def clear(self):
        with self._lock:
            self.operations.clear()
            self.starts.clear()
            self.snapshots.clear()
            self.events.clear()


class ConsoleSink:
    """One JSON line per finished operation, on stderr."""
    def on_operation_end(self, record):
        line = {k: record.get(k) for k in ("name", "type", "level", "status", "duration_ms",
                                          "trace_id", "span_id", "parent_span_id", "run_id")}
        print("engelbart-telemetry " + dumps(line), file=sys.stderr, flush=True)


def _private_append(path: Path, line: str, root: Optional[Path]) -> None:
    from ..trajectory.secure_io import open_private_append
    with open_private_append(path, root=root or path.parent) as stream:
        stream.write(line)
        stream.write("\n")


class FileSink:
    """The chat's own record: ``telemetry/records.jsonl`` under its directory.

    Each line is ``{"kind": "operation" | "snapshot" | "event", "at": ..., "record": {...}}``.
    An operation is written when it starts and again when it ends; the
    later line supersedes by ``operation_id``. Past ROTATE_BYTES the file
    is renamed with a timestamp and a new one begun.
    """
    def __init__(self, directory, root: Optional[Path] = None):
        self.directory = Path(directory)
        self.root = Path(root) if root is not None else None
        self._lock = threading.Lock()
        self.written = 0

    @property
    def path(self) -> Path:
        return self.directory / RECORDS_NAME

    def _write(self, kind: str, record: Dict[str, Any]) -> None:
        line = dumps({"kind": kind, "at": now_iso(), "record": record})
        with self._lock:
            path = self.path
            try:
                if path.stat().st_size + len(line) > ROTATE_BYTES:
                    stamp = now_iso().replace(":", "").replace("-", "").replace(".", "")
                    os.replace(path, self.directory / ("records.%s.jsonl" % stamp))
            except OSError:
                pass
            _private_append(path, line, self.root)
            self.written += 1

    def on_operation_start(self, record):
        self._write("operation", record)

    def on_operation_end(self, record):
        self._write("operation", record)

    def on_snapshot(self, snapshot):
        self._write("snapshot", snapshot)

    def on_event(self, event):
        self._write("event", event)

    def flush(self, _timeout=None):
        return True

    @staticmethod
    def read(directory) -> Dict[str, Any]:
        """Every record the file holds: operations by id (last line wins),
        snapshots and events in file order."""
        operations: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        snapshots: List[Dict[str, Any]] = []
        events: List[Dict[str, Any]] = []
        path = Path(directory) / RECORDS_NAME
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            record = item.get("record") if isinstance(item, dict) else None
            if not isinstance(record, dict):
                continue
            kind = item.get("kind")
            if kind == "operation" and record.get("operation_id"):
                operations[record["operation_id"]] = record
            elif kind == "snapshot":
                snapshots.append(record)
            elif kind == "event":
                events.append(record)
        return {"operations": list(operations.values()), "snapshots": snapshots,
                "events": events}

    @staticmethod
    def envelope(directory, run_id: Optional[str] = None) -> Dict[str, Any]:
        return Contract.bundle(FileSink.read(directory), run_id=run_id)


class Rejected(Exception):
    """The site refused the envelope for what it is (400, 404, 413): sending
    it again would only be refused again."""


class Unauthorized(Exception):
    """The session the envelope was sent on is not good: get another."""


class Unavailable(Exception):
    """The site could not be reached or answered 5xx: send later."""


def default_session(root: Optional[Path], site: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The member's session on this machine, for the POST: the access token
    the site's ``verifyUser`` accepts, the user id it names, and where the
    site is. None when the machine is not signed in. The machine token
    itself is exchanged, never sent.
    """
    from ..trajectory import supabase_client as SB
    try:
        credentials = SB.engelbart_credentials()
        base = site or str(credentials.get("apiBase") or "").rstrip("/") or DEFAULT_SITE
        session = SB.current_session(root)
    except Exception:                                        # noqa: BLE001
        return None
    token = str(session.get("access_token") or "")
    if not token:
        return None
    return {"access_token": token, "user_id": str(session.get("user_id") or ""),
            "site": base}


def http_post(url: str, body: bytes, headers: Dict[str, str], timeout: float):
    """(status, text) of one POST; a connection failure is Unavailable."""
    request = urllib.request.Request(url, data=body, method="POST")
    for name, value in headers.items():
        request.add_header(name, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:                                # noqa: BLE001
                text = ""
        return exc.code, text
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Unavailable(str(exc)[:200]) from None


class ForwardSink:
    """Envelopes to the site, batched, on a thread of their own.

    Records queue up as they happen; every ``flush_ms`` (or sooner when a
    batch is full) the thread builds one envelope of them and POSTs it on
    the member's session. A failure keeps the envelope in the outbox and
    backs off; an envelope the site refuses outright is kept aside in
    ``outbox/rejected`` so a bug is visible rather than retried forever.
    Nothing here waits on a request, and nothing raises out.
    """
    def __init__(self, directory, run: Dict[str, Any], *, root: Optional[Path] = None,
                 session: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 site: Optional[str] = None, post: Optional[Callable] = None,
                 flush_ms: int = 2000, protect: Optional[Callable[[Any], Any]] = None,
                 log: Optional[Callable[[str], Any]] = None, now: Callable[[], float] = time.monotonic):
        self.directory = Path(directory)
        self.root = Path(root) if root is not None else None
        self.run = run
        self.site = site
        self._session = session or (lambda: default_session(self.root, self.site))
        self._post = post or http_post
        self.flush_ms = max(50, int(flush_ms))
        self._protect = protect
        self._log = log or (lambda message: None)
        self._now = now
        self._lock = threading.Lock()
        self._operations: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._snapshots: List[Dict[str, Any]] = []
        self._events: List[Dict[str, Any]] = []
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._cached_session: Optional[Dict[str, Any]] = None
        self._retry_at = 0.0
        self._failures = 0
        self.stats = {"posted": 0, "accepted": 0, "spooled": 0, "replayed": 0,
                      "rejected": 0, "failures": 0, "last_error": ""}

    # --- what comes in ---------------------------------------------------

    def _enqueue(self, kind: str, record: Dict[str, Any]) -> None:
        if self._closed:
            return
        with self._lock:
            if kind == "operation":
                self._operations[record["operation_id"]] = record
            elif kind == "snapshot":
                self._snapshots.append(record)
            else:
                self._events.append(record)
            self._idle.clear()
            full = (len(self._operations) >= MAX_BATCH_OPERATIONS
                    or len(self._events) >= MAX_BATCH_EVENTS
                    or len(self._snapshots) >= MAX_BATCH_SNAPSHOTS)
            self._ensure_thread()
        if full:
            self._wake.set()

    def on_operation_start(self, record):
        self._enqueue("operation", record)

    def on_operation_end(self, record):
        self._enqueue("operation", record)

    def on_snapshot(self, snapshot):
        self._enqueue("snapshot", snapshot)

    def on_event(self, event):
        self._enqueue("event", event)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name="hc-telemetry-forward")
            self._thread.start()

    # --- the thread ------------------------------------------------------

    def _worker(self) -> None:
        while True:
            self._wake.wait(self.flush_ms / 1000.0)
            self._wake.clear()
            try:
                self._cycle()
            except Exception as exc:                         # noqa: BLE001
                self._log("forward cycle failed: %s" % str(exc)[:200])
            with self._lock:
                empty = not (self._operations or self._snapshots or self._events)
                if empty:
                    self._idle.set()
                if self._closed and empty:
                    return

    def _cycle(self) -> None:
        while True:
            envelope = self._take()
            if envelope is None:
                break
            self._deliver(envelope)
        if self._now() >= self._retry_at:
            self._replay_outbox()

    def _take(self) -> Optional[Dict[str, Any]]:
        """One envelope's worth of records off the queue, within the batch
        bounds; what does not fit stays for the next."""
        with self._lock:
            if not (self._operations or self._snapshots or self._events):
                return None
            operations: List[Dict[str, Any]] = []
            snapshots: List[Dict[str, Any]] = []
            events: List[Dict[str, Any]] = []
            size = 0
            while self._operations and len(operations) < MAX_BATCH_OPERATIONS:
                _key, record = next(iter(self._operations.items()))
                grown = size + len(dumps(record)) + 1
                if operations and grown > MAX_BATCH_BYTES:
                    break
                self._operations.popitem(last=False)
                operations.append(dict(record))
                size = grown
            while self._snapshots and len(snapshots) < MAX_BATCH_SNAPSHOTS:
                grown = size + len(dumps(self._snapshots[0])) + 1
                if (operations or snapshots) and grown > MAX_BATCH_BYTES:
                    break
                snapshots.append(self._snapshots.pop(0))
                size = grown
            while self._events and len(events) < MAX_BATCH_EVENTS:
                grown = size + len(dumps(self._events[0])) + 1
                if (operations or snapshots or events) and grown > MAX_BATCH_BYTES:
                    break
                events.append(self._events.pop(0))
                size = grown
        return self.envelope(operations, snapshots, events)

    def envelope(self, operations, snapshots, events) -> Dict[str, Any]:
        run = self.run or {}
        return {
            "contract_version": Contract.CONTRACT_VERSION,
            "run": {"run_id": run.get("run_id"), "mode": run.get("mode") or "live",
                    "environment": run.get("environment"), "code_version": run.get("code_version"),
                    "deployment": run.get("deployment"), "origin": "goal-page",
                    "label": run.get("label")},
            "operations": sorted(operations, key=Contract.operation_sort_key),
            "snapshots": sorted(snapshots, key=Contract.snapshot_sort_key),
            "events": sorted(events, key=Contract.event_sort_key),
        }

    # --- the wire --------------------------------------------------------

    def _session_now(self) -> Optional[Dict[str, Any]]:
        if self._cached_session is None:
            found = self._session()
            if found and found.get("access_token"):
                self._cached_session = dict(found)
                if self._protect:
                    self._protect(found["access_token"])
                if found.get("user_id") and self.run is not None and not self.run.get("user_hash"):
                    from .core import user_hash
                    self.run["user_hash"] = user_hash(found["user_id"])
        return self._cached_session

    def send(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """One envelope to the site, now. Raises Unauthorized, Rejected or
        Unavailable; returns the site's answer."""
        session = self._session_now()
        if not session:
            raise Unauthorized("not signed in")
        site = (self.site or session.get("site") or DEFAULT_SITE).rstrip("/")
        body = dumps(envelope).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Authorization": "Bearer " + session["access_token"],
                   "User-Agent": USER_AGENT}
        status, text = self._post(site + ENDPOINT, body, headers, POST_TIMEOUT_S)
        self.stats["posted"] += 1
        if 200 <= status < 300:
            try:
                answer = json.loads(text) if text.strip() else {}
            except ValueError:
                answer = {}
            return answer if isinstance(answer, dict) else {}
        detail = ""
        try:
            detail = str((json.loads(text) or {}).get("error") or "")[:200]
        except (ValueError, AttributeError):
            detail = text[:200]
        if status in (401, 403):
            self._cached_session = None
            raise Unauthorized("%d %s" % (status, detail))
        if 400 <= status < 500 and status != 429:
            raise Rejected("%d %s" % (status, detail))
        raise Unavailable("%d %s" % (status, detail))

    def _deliver(self, envelope: Dict[str, Any]) -> bool:
        if self._now() < self._retry_at:
            self._spool(envelope)
            return False
        try:
            answer = self.send(envelope)
        except Rejected as exc:
            self._reject(envelope, str(exc))
            return False
        except (Unauthorized, Unavailable) as exc:
            self._back_off(str(exc))
            self._spool(envelope)
            return False
        except Exception as exc:                             # noqa: BLE001
            self._back_off(str(exc))
            self._spool(envelope)
            return False
        self._failures = 0
        self._retry_at = 0.0
        accepted = answer.get("accepted") if isinstance(answer, dict) else None
        if isinstance(accepted, dict):
            self.stats["accepted"] += int(accepted.get("operations") or 0)
        return True

    def _back_off(self, reason: str) -> None:
        self._failures += 1
        self.stats["failures"] += 1
        self.stats["last_error"] = reason[:200]
        wait = min(RETRY_MAX_S, RETRY_BASE_S * (2 ** min(self._failures - 1, 6)))
        self._retry_at = self._now() + wait
        if self._failures in (1, 5, 10):
            self._log("could not forward telemetry (%s); retrying in %ds" % (reason[:120], wait))

    # --- the outbox ------------------------------------------------------

    @property
    def outbox(self) -> Path:
        return self.directory / OUTBOX_NAME

    def _spool(self, envelope: Dict[str, Any]) -> None:
        from ..trajectory.secure_io import atomic_write_text, secure_dir
        try:
            secure_dir(self.outbox, self.root or self.directory)
            held = sorted(p for p in self.outbox.glob("*.json"))
            while len(held) >= OUTBOX_LIMIT:
                oldest = held.pop(0)
                oldest.unlink(missing_ok=True)
            name = "%d-%06d.json" % (time.time_ns(), len(held))
            atomic_write_text(self.outbox / name, dumps(envelope), root=self.root or self.outbox)
            self.stats["spooled"] += 1
        except OSError as exc:
            self._log("could not spool telemetry: %s" % str(exc)[:200])

    def _reject(self, envelope: Dict[str, Any], reason: str) -> None:
        from ..trajectory.secure_io import atomic_write_text, secure_dir
        self.stats["rejected"] += 1
        self.stats["last_error"] = reason[:200]
        self._log("the site refused a telemetry envelope: %s" % reason[:160])
        try:
            where = self.outbox / REJECTED_NAME
            secure_dir(where, self.root or self.directory)
            atomic_write_text(where / ("%d.json" % time.time_ns()),
                              dumps({"reason": reason, "envelope": envelope}),
                              root=self.root or where)
        except OSError:
            pass

    def _replay_outbox(self, limit: int = REPLAY_PER_CYCLE) -> int:
        try:
            held = sorted(p for p in self.outbox.glob("*.json"))
        except OSError:
            return 0
        sent = 0
        for path in held[:limit]:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            try:
                self.send(envelope)
            except Rejected as exc:
                path.unlink(missing_ok=True)
                self._reject(envelope, str(exc))
                continue
            except (Unauthorized, Unavailable) as exc:
                self._back_off(str(exc))
                break
            except Exception as exc:                         # noqa: BLE001
                self._back_off(str(exc))
                break
            self._failures = 0
            self._retry_at = 0.0
            path.unlink(missing_ok=True)
            sent += 1
            self.stats["replayed"] += 1
        return sent

    # --- settling --------------------------------------------------------

    def pending(self) -> int:
        with self._lock:
            return len(self._operations) + len(self._snapshots) + len(self._events)

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Push what is queued now and wait, up to ``timeout`` seconds, for
        the thread to have dealt with it. True when the queue drained."""
        if self.pending() == 0:
            return True
        self._ensure_thread()
        self._wake.set()
        return self._idle.wait(2.0 if timeout is None else max(0.0, float(timeout)))

    def close(self, timeout: float = 2.0) -> None:
        self._closed = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
