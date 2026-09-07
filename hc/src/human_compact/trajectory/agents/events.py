"""The local event log: every interaction, one line each, per chat.

``agent_events.jsonl`` sits in the chat's session directory beside
goals.json and bart.json (see chat_state.paths), and is separate from the
``events.jsonl`` there, which is the chat-ingest feed the hooks write. Each
line is one LocalEvent::

    {"id", "projectId", "timestamp", "type", "source", "payload",
     "subgoalId", "todoId", "runId"}

``source`` is who caused it: ``user`` for a click or a message, ``system``
for something the machinery did (a build ended, a verdict landed, the
Overseer routed), ``agent`` for what an agent said or did.

Appending is the only write. The file is rotated when it grows past
KEEP_BYTES so the log stays a log and not an archive; ``read`` tails it.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .. import chat_state as CS
from ..secure_io import open_private_append, atomic_write_text

USER, SYSTEM, AGENT = "user", "system", "agent"
SOURCES = (USER, SYSTEM, AGENT)

# What the file may grow to before it is cut back to its last KEEP_LINES.
KEEP_BYTES = 2_000_000
KEEP_LINES = 2000
MAX_PAYLOAD_CHARS = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def path(session_id: str, root: Optional[Path]) -> Path:
    return CS.paths(session_id, root).session_dir / "agent_events.jsonl"


def new_event(type: str, source: str, payload: Optional[Dict[str, Any]] = None, *,
              project_id: str = "", subgoal_id: str = "", todo_id: str = "",
              run_id: str = "") -> Dict[str, Any]:
    """One event, shaped; not yet written."""
    if source not in SOURCES:
        raise ValueError("not an event source: %r" % (source,))
    if not isinstance(type, str) or not type.strip():
        raise ValueError("an event needs a type")
    return {
        "id": "e" + uuid.uuid4().hex[:12],
        "projectId": str(project_id or ""),
        "timestamp": _now(),
        "type": type.strip(),
        "source": source,
        "payload": _bounded(payload if isinstance(payload, dict) else {}),
        "subgoalId": str(subgoal_id or ""),
        "todoId": str(todo_id or ""),
        "runId": str(run_id or ""),
    }


def _bounded(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A payload is context, not storage: long strings are cut."""
    out: Dict[str, Any] = {}
    for key, value in bounded(payload, chars=MAX_PAYLOAD_CHARS).items():
        if isinstance(value, str) and len(value) > MAX_PAYLOAD_CHARS:
            value = value[:MAX_PAYLOAD_CHARS]
        out[str(key)] = value
    return out


def record(session_id: str, root: Optional[Path], event: Dict[str, Any]) -> Dict[str, Any]:
    """Append one event. Returns it, as written."""
    with CS.session_lock(session_id, root, wait_s=5):
        spot = path(session_id, root)
        spot.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with open_private_append(spot, root=spot.parent, secure_parent=False) as fh:
            fh.write(line + "\n")
        _rotate(spot)
        return event


def _rotate(spot: Path) -> None:
    try:
        if spot.stat().st_size <= KEEP_BYTES:
            return
        lines = spot.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    kept = lines[-KEEP_LINES:]
    atomic_write_text(spot, "\n".join(kept) + "\n", root=spot.parent)


def read(session_id: str, root: Optional[Path], *, limit: Optional[int] = None,
         types: Optional[Iterable[str]] = None,
         subgoal_id: str = "") -> List[Dict[str, Any]]:
    """The log, oldest first; the last ``limit`` when one is given."""
    try:
        text = path(session_id, root).read_text(encoding="utf-8")
    except OSError:
        return []
    wanted = set(types) if types else None
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        if wanted and event["type"] not in wanted:
            continue
        if subgoal_id and event.get("subgoalId") != subgoal_id:
            continue
        out.append(event)
    return out[-limit:] if limit else out


def clear(session_id: str, root: Optional[Path]) -> None:
    try:
        os.remove(path(session_id, root))
    except OSError:
        pass


INTERACTIONS = frozenset({"project.opened", "goal.opened", "subgoal.selected",
    "tab.changed", "preview.opened", "preview.closed", "preview.interacted",
    "artifact.opened", "plan.suggestion_accepted", "plan.suggestion_rejected",
    "todo.add_started", "build.cancelled", "chat.saved"})


def bounded(value, chars=600, items=20, depth=0):
    if depth > 7:
        return str(value)[:chars]
    if isinstance(value, dict):
        return {str(k)[:80]: bounded(v, chars, items, depth+1)
                for k, v in list(value.items())[:items]}
    if isinstance(value, (list, tuple)):
        return [bounded(v, chars, items, depth+1) for v in value[:items]]
    return value[:chars] if isinstance(value, str) else value


def interaction(session_id, root, body, project_id=""):
    if not isinstance(body, dict) or body.get("type") not in INTERACTIONS:
        raise ValueError("unknown interaction")
    return record(session_id, root, new_event(body["type"], USER,
        bounded(body.get("payload") or {}), project_id=project_id,
        subgoal_id=str(body.get("subgoalId") or "")[:80]))


def summary(session_id, root):
    recent = read(session_id, root, limit=40)
    counts = {}
    for e in recent:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    return {"counts": counts, "recent": bounded(recent[-12:])}
