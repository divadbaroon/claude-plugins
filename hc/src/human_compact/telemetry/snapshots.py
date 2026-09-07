"""Payload snapshots: the actual prompt a model was sent, the raw reply,
the parsed reply, a request's body, an answer. They are the Bart Snapshot
entity and live beside the operation, never inside its attributes. Every
snapshot is redacted before it gets here and bounded here, by the size
rules of the contract: a huge string is cut, and a record that is still too
large is kept as a preview.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

MAX_SNAPSHOT_BYTES = 256 * 1024
SHRUNK_STRING = 4096


def now_iso() -> str:
    """ISO 8601 with milliseconds and a Z, as JavaScript writes it."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def dumps(value: Any) -> str:
    """JSON as ``JSON.stringify`` writes it: no spaces, text kept as text."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def size_of(value: Any) -> int:
    """The UTF-8 size of a value as JSON: what ``bytes`` means everywhere here."""
    return len(dumps(value).encode("utf-8"))


def shrink_strings(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return ("%s [… %d more chars truncated]" % (value[:cap], len(value) - cap)
                if len(value) > cap else value)
    if isinstance(value, list):
        return [shrink_strings(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: shrink_strings(v, cap) for k, v in value.items()}
    return value


def bound(content: Any, max_bytes: Optional[int] = None) -> Dict[str, Any]:
    """The content within its byte budget, and whether it had to be cut.

    Three rules, in order: (1) within the cap, kept whole; (2) over it,
    every string shortened to SHRUNK_STRING characters, kept if that fits;
    (3) still over, replaced by a preview wrapper holding the head of the
    shrunk JSON, with the original and shortened serialized sizes on record.
    ``bytes`` is always the UTF-8 size of the stored content as JSON and
    never exceeds the cap.
    """
    limit = int(max_bytes or MAX_SNAPSHOT_BYTES)
    original = size_of(content)
    if original <= limit:
        return {"content": content, "bytes": original, "truncated": False}
    shrunk = shrink_strings(content, SHRUNK_STRING)
    shrunk_bytes = size_of(shrunk)
    if shrunk_bytes <= limit:
        return {"content": shrunk, "bytes": shrunk_bytes, "truncated": True}
    text = dumps(shrunk)

    def wrap(preview):
        return {"[truncated]": True, "original_bytes": original,
                "shrunk_bytes": shrunk_bytes, "preview": preview}
    chars = min(len(text), limit // 2)
    wrapper = wrap(text[:chars])
    size = size_of(wrapper)
    while size > limit and chars > 0:
        chars = min(chars - 1, int(chars * (limit / size) * 0.98))
        wrapper = wrap(text[:max(chars, 0)])
        size = size_of(wrapper)
    return {"content": wrapper, "bytes": size, "truncated": True}


def kind_of(kind: Any) -> str:
    """A snapshot kind is one of the contract's, or any short name (the
    site keeps 40 characters of it)."""
    from .contract import SNAPSHOT_KINDS
    return kind if kind in SNAPSHOT_KINDS else str(kind)[:40]


def build(operation: Any, kind: str, content: Any,
          max_bytes: Optional[int] = None) -> Dict[str, Any]:
    """``content`` must already be redacted."""
    fitted = bound(content, max_bytes)
    run = getattr(operation, "run", None) or {}
    from .contract import SNAPSHOT_KINDS
    return {
        "snapshot_id": str(uuid.uuid4()),
        "operation_id": operation.operation_id,
        "trace_id": operation.trace_id,
        "span_id": operation.span_id,
        "run_id": run.get("run_id"),
        "onboarding_id": run.get("onboarding_id"),
        "test_run_id": run.get("test_run_id"),
        "kind": kind_of(kind),
        "content": fitted["content"],
        "bytes": fitted["bytes"],
        "truncated": fitted["truncated"],
        "redacted": True,
        "created_at": now_iso(),
    }
