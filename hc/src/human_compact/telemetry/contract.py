"""The Bart data contract, as this plugin writes it.

One vocabulary, shared with the site's telemetry layer
(berkeley-research ``api/_lib/telemetry`` and
``docs/observability/data-contract.md``): the operation types and levels,
the lifecycle event types, the snapshot kinds, the attribute caps, the
lineage attributes, the derived Run and the envelope's ordering. Every rule
here has the same name and the same effect as its JavaScript original, so a
run recorded by this process and read by the site's debugger is one thing
in two languages. What differs is only what is added for the plugin: the
names of its own detail operations, and the ``chat:`` run id.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

CONTRACT_VERSION = "1"

TYPES = ("workflow", "model", "database", "storage", "http", "processing")
LEVELS = ("workflow", "stage", "detail")
STATUS = ("running", "waiting", "completed", "failed")
MODES = ("live", "test", "simulation", "replay", "fixture")
EVENT_TYPES = ("operation.started", "operation.progress",
               "operation.completed", "operation.failed")
SNAPSHOT_KINDS = ("model_request", "model_raw_response",
                  "model_parsed_response", "normalized_result",
                  "processing_input", "processing_output",
                  "database_request", "database_response",
                  "page_text", "error_detail")

MAX_ATTRIBUTE_STRING = 2000
MAX_ATTRIBUTE_ARRAY = 50
LINEAGE_ATTRIBUTES = {"reads": "engelbart.lineage.reads",
                      "writes": "engelbart.lineage.writes"}

# A run this plugin records: one per chat workspace, named by the chat's
# session id. The site's own runs are onboarding row ids (uuids) or
# ``test:<name>``; this prefix keeps the three apart in one store.
RUN_ID_PREFIX = "chat:"
RUN_ID = re.compile(r"^chat:[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

# How much an operation means to a reader of the graph. A workflow is the
# root; a stage is a step worth showing on its own; a detail is what a stage
# is made of. The first six patterns are the site's; the last is this
# plugin's: the files every action reads and writes.
DETAIL_NAMES = (
    re.compile(r"^db\."),
    re.compile(r"^(?:row|calibrations|turns)\.load$"),
    re.compile(r"^(?:[a-z-]+-)?page\.fetch$"),
    re.compile(r"^page\.extract-text$"),
    re.compile(r"^link\.check$"),
    re.compile(r"^(?:storage|auth)\.request$"),
    re.compile(r"^(?:goals|bart-chat|prompts|manifest)\.(?:load|save)$"),
)


def level_of(name: str, type_: str, given: Optional[str] = None) -> str:
    if given in LEVELS:
        return given
    if type_ == "workflow":
        return "workflow"
    text = str(name)
    return ("detail" if any(p.search(text) for p in DETAIL_NAMES)
            else "stage")


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:      # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return str(value)
        return value
    return str(value)


def attribute_value(value: Any) -> Any:
    """Span attributes are scalars or arrays of scalars. Anything else is
    described briefly rather than dropped silently; None is dropped."""
    if value is None:
        return None
    if isinstance(value, str):
        return (value[:MAX_ATTRIBUTE_STRING] + "…"
                if len(value) > MAX_ATTRIBUTE_STRING else value)
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return _scalar(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scalar(v) for v in list(value)[:MAX_ATTRIBUTE_ARRAY]]
    return str(value)[:MAX_ATTRIBUTE_STRING]


def _flatten(names: Any) -> Iterable[Any]:
    if isinstance(names, (list, tuple, set, frozenset)):
        for item in names:
            for inner in _flatten(item):
                yield inner
    else:
        yield names


def lineage_names(names: Any, have: Optional[List[str]] = None) -> List[str]:
    """Strings only, trimmed, deduplicated, in the order given."""
    out = list(have or [])
    for name in _flatten(names):
        if not isinstance(name, str):
            continue
        clean = name.strip()
        if clean and clean not in out:
            out.append(clean)
    return out


# --- the derived Run and the envelope ----------------------------------------

def _pick(ops: List[Dict[str, Any]], key: str) -> Any:
    for op in ops:
        value = op.get(key)
        if value is not None:
            return value
    return None


def _first(values: Iterable[Any]) -> Optional[str]:
    kept = [str(v) for v in values if v]
    return min(kept) if kept else None


def _last(values: Iterable[Any]) -> Optional[str]:
    kept = [str(v) for v in values if v]
    return max(kept) if kept else None


def mode_of(ops: List[Dict[str, Any]], mode: Optional[str] = None) -> str:
    if mode in MODES:
        return mode
    for op in ops:
        found = (op.get("attributes") or {}).get("engelbart.mode")
        if found in MODES:
            return found
    return "test" if _pick(ops, "test_run_id") else "live"


def roots(operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted((op for op in operations if not op.get("parent_span_id")),
                  key=lambda op: str(op.get("started_at") or ""))


def derive_run(operations: Iterable[Dict[str, Any]],
               run_id: Optional[str] = None,
               mode: Optional[str] = None) -> Dict[str, Any]:
    ops = [op for op in operations if op]
    workflows = roots(ops)
    running = any(op.get("status") in ("running", "waiting") for op in ops)
    failed = any(op.get("status") == "failed" for op in workflows)
    hashes = [{"user_hash": (op.get("attributes") or {}).get("engelbart.user_hash")}
              for op in ops]
    return {
        "run_id": run_id or _pick(ops, "run_id"),
        "onboarding_id": _pick(ops, "onboarding_id"),
        "test_run_id": _pick(ops, "test_run_id"),
        "user_hash": _pick(hashes, "user_hash"),
        "mode": mode_of(ops, mode),
        "environment": _pick(ops, "environment"),
        "code_version": _pick(ops, "code_version"),
        "deployment": _pick(ops, "deployment"),
        "status": "running" if running else "failed" if failed else "completed",
        "started_at": _first(op.get("started_at") for op in ops),
        "ended_at": None if running else _last(op.get("ended_at") for op in ops),
        "trace_ids": list(dict.fromkeys(op.get("trace_id") for op in workflows)),
        "actions": [op.get("action")
                    or (op.get("attributes") or {}).get("engelbart.action")
                    or op.get("name") for op in workflows],
        "counts": {
            "traces": len({op.get("trace_id") for op in ops}),
            "operations": len(ops),
            "workflows": len([op for op in ops if op.get("type") == "workflow"]),
            "failed": len([op for op in ops if op.get("status") == "failed"]),
        },
    }


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def event_sort_key(event: Dict[str, Any]):
    """Wall clock first, then the trace, then the process-local sequence,
    then the id: the contract's order, with a unique final key."""
    try:
        sequence = int(event.get("sequence") or 0)
    except (TypeError, ValueError):
        sequence = 0
    return (_text(event.get("at")), _text(event.get("trace_id")), sequence,
            _text(event.get("event_id")))


def operation_sort_key(op: Dict[str, Any]):
    return (_text(op.get("started_at")), _text(op.get("span_id")),
            _text(op.get("operation_id")))


def snapshot_sort_key(snapshot: Dict[str, Any]):
    return (_text(snapshot.get("created_at")), _text(snapshot.get("snapshot_id")))


def bundle(records: Dict[str, Any], run_id: Optional[str] = None,
           mode: Optional[str] = None) -> Dict[str, Any]:
    """The envelope, ordered: operations by start, snapshots by creation,
    events by event_sort_key."""
    operations = sorted((op for op in records.get("operations") or [] if op),
                        key=operation_sort_key)
    snapshots = sorted((s for s in records.get("snapshots") or [] if s),
                       key=snapshot_sort_key)
    events = sorted((e for e in records.get("events") or [] if e),
                    key=event_sort_key)
    return {
        "contract_version": CONTRACT_VERSION,
        "run": derive_run(operations, run_id=run_id, mode=mode),
        "operations": operations,
        "snapshots": snapshots,
        "events": events,
    }


def tree(operations: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The tree a graph draws: each operation with its children, roots first."""
    nodes = [dict(op, children=[]) for op in operations if op]
    by_span = {node.get("span_id"): node for node in nodes}
    out: List[Dict[str, Any]] = []
    for node in nodes:
        parent = by_span.get(node.get("parent_span_id")) if node.get("parent_span_id") else None
        (parent["children"] if parent else out).append(node)

    def sort_deep(items):
        items.sort(key=lambda n: str(n.get("started_at") or ""))
        for n in items:
            sort_deep(n["children"])
    sort_deep(out)
    return out
