"""Spans, so a turn can be followed end to end.

No OpenTelemetry here -- hc has no such dependency, and a page served
from one process does not need a collector to see itself. Each span is
one JSON line in ``agent_trace.jsonl`` beside the event log, in the
shape an exporter would want later: name, trace and span ids, the
parent's id, start and end, attributes, status. Spans nest through a
thread-local stack, so ``bart.message`` -> ``overseer.route`` ->
``chat.reply`` is three lines that point at each other.

The names used across the package::

    bart.message -> overseer.route -> chat.reply | brainstorm.reply | path.plan
    todo.build -> overseer.route -> build.agent (model.call, file.read,
                  file.write, command.exec, preview.start)
               -> verifier.agent -> overseer.route
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .. import chat_state as CS
from ..secure_io import open_private_append

_local = threading.local()


def _stack() -> List[Dict[str, Any]]:
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    return stack


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


class Tracer:
    """Where spans go: a list always, a file when the tracer has a chat.

    ``spans`` keeps everything this tracer saw, oldest first, so a test can
    read the shape of a turn without a file.
    """

    def __init__(self, session_id: str = "", root: Optional[Path] = None,
                 to_file: bool = True):
        self.session_id = session_id
        self.root = root
        self.to_file = bool(to_file and session_id)
        self.spans: List[Dict[str, Any]] = []
        self._guard = threading.Lock()
        self._seq = 0

    def path(self) -> Optional[Path]:
        if not self.to_file:
            return None
        return CS.paths(self.session_id, self.root).session_dir / "agent_trace.jsonl"

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Dict[str, Any]]:
        stack = _stack()
        parent = stack[-1] if stack else None
        with self._guard:
            self._seq += 1
            seq = self._seq
        span = {
            "seq": seq,
            "name": name,
            "trace_id": parent["trace_id"] if parent else uuid.uuid4().hex,
            "span_id": uuid.uuid4().hex[:16],
            "parent_id": parent["span_id"] if parent else "",
            "start": _stamp(time.time()),
            "attrs": dict(attrs),
            "status": "ok",
        }
        began = time.monotonic()
        stack.append(span)
        try:
            yield span
        except BaseException as exc:
            span["status"] = "error"
            span["error"] = " ".join(f"{type(exc).__name__}: {exc}".split())[:200]
            raise
        finally:
            stack.pop()
            span["end"] = _stamp(time.time())
            span["ms"] = round((time.monotonic() - began) * 1000, 1)
            self._keep(span)

    def _keep(self, span: Dict[str, Any]) -> None:
        with self._guard:
            self.spans.append(span)
        spot = self.path()
        if spot is None:
            return
        try:
            spot.parent.mkdir(parents=True, exist_ok=True)
            with open_private_append(spot, root=spot.parent, secure_parent=False) as fh:
                fh.write(json.dumps(span, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def names(self) -> List[str]:
        """The span names in the order they ended."""
        return [s["name"] for s in self.spans]

    def tree(self) -> List[str]:
        """Each span as ``parent > child``, in start order -- the flow."""
        by_id = {s["span_id"]: s for s in self.spans}
        lines = []
        for span in sorted(self.spans, key=lambda s: s.get("seq", 0)):
            chain = [span["name"]]
            at = span
            while at.get("parent_id") and at["parent_id"] in by_id:
                at = by_id[at["parent_id"]]
                chain.append(at["name"])
            lines.append(" > ".join(reversed(chain)))
        return lines


# The tracer in use when nobody handed one over: spans in memory, no file.
_DEFAULT = Tracer()


def use(tracer: Optional[Tracer]) -> Tracer:
    """Make ``tracer`` the one module-level ``span`` writes to, on this
    thread: two chats answering at once each keep their own file."""
    _local.tracer = tracer or _DEFAULT
    return _local.tracer


def current() -> Tracer:
    return getattr(_local, "tracer", None) or _DEFAULT


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Dict[str, Any]]:
    with current().span(name, **attrs) as s:
        yield s
