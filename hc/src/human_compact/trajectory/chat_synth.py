"""Bounded, session-scoped goal inference over Claude chat evidence.

The transcript/event store is authoritative evidence; goals are mutable,
user-supervised derived state. Initial refresh asks for a complete tree, while
later refreshes ask for conservative operations against the existing tree.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import chat_state as CS, goals as GM, providers as P


MAX_EVENT_CHARS = 36_000
MAX_CONTEXT_CHARS = 12_000
MAX_EVENT_TEXT = 2_000
MAX_REFRESH_PASSES = 3
_CONTEXT_NAMES = ("AGENTS.md", "CLAUDE.md", "README.md")
_TEXT_SUFFIXES = {
    ".md", ".txt", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".toml", ".yaml", ".yml", ".html", ".css", ".sh",
    ".rs", ".go", ".java", ".c", ".h", ".cpp", ".sql",
}
_DETACHED_PROCESSES = []


INITIAL_PROMPT = """Infer the current goal tree for ONE Claude Code chat.
This is mutable user-supervised state, not a transcript summary.

Evidence includes human prompts, visible assistant plans/progress, plan-tool
updates, tool results, task events, compact summaries, and project context.
Infer completion only from explicit completion evidence. Distinguish a goal
from its implementation tasks. Prefer 1-4 top-level goals, depth at most 3.
Never use private assistant thinking. Copy only supplied event ids.

Return ONLY minified JSON:
{"goals":[{"id":"g1","title":"","status":"active|in_progress|completed|abandoned","parent_goal_id":null,"description":"","priority":"normal|high|urgent","evidence_ids":[],"todos":[{"text":"","done":false,"evidence_ids":[]}]}],"important":{"items":[]}}

PROJECT CONTEXT:
<<CONTEXT>>

CHAT EVIDENCE (oldest first):
<<EVENTS>>"""


INCREMENTAL_PROMPT = """Update the current goal state for ONE Claude Code chat
using ONLY the new event evidence. Human UI edits and prompt relationships are
authoritative and are not yours to remove or rewrite.

Return ONLY minified JSON {"operations":[...]} using these operations:
{"op":"attach_evidence","goal_id":"","evidence_ids":[]}
{"op":"add_todo","goal_id":"","text":"","evidence_ids":[]}
{"op":"complete_todo","goal_id":"","text_match":""}
{"op":"set_status","goal_id":"","status":"active|in_progress|completed|abandoned"}
{"op":"new_goal","parent_goal_id":"<id or null>","title":"","description":"","evidence_ids":[],"todos":[],"distinct_because":""}

Rules: infer completion only from explicit evidence. A top-level new_goal needs
an explicitly distinct objective in distinct_because. Prefer attaching evidence
or creating a todo/subgoal. Do not rename, move, merge, delete, or edit notes,
priority, prompt_ids, important links, or manually authored content.

CURRENT STATE:
<<TREE>>

PROJECT CONTEXT:
<<CONTEXT>>

NEW EVIDENCE:
<<EVENTS>>"""


def _project_key(cwd: Path) -> str:
    # Claude encodes an absolute project path by replacing all non-word path
    # punctuation (including spaces) with hyphens.
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(cwd))


def _read_bounded(path: Path, remaining: int) -> str:
    if remaining <= 0 or path.is_symlink() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:remaining]
    except OSError:
        return ""


def _query_terms(events: Iterable[Dict[str, Any]]) -> set:
    stop = {"the", "and", "for", "with", "this", "that", "from", "into", "have"}
    text = " ".join(str(e.get("text") or "") for e in events)
    return {
        word for word in re.findall(r"[a-z0-9_-]{4,}", text.lower())
        if word not in stop
    }


def _nested_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _nested_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _nested_strings(nested)


def _referenced_files(cwd: Path, events: Iterable[Dict[str, Any]]) -> List[Path]:
    """Resolve explicitly mentioned text files, confined to the project root."""
    candidates: List[str] = []
    suffixes = "|".join(re.escape(suffix.lstrip(".")) for suffix in _TEXT_SUFFIXES)
    pattern = re.compile(
        rf"(?:^|[\s\"'`(])([A-Za-z0-9_./@+ -]+\.(?:{suffixes}))(?=$|[\s\"'`),:])",
        re.IGNORECASE,
    )
    for event in events:
        text = str(event.get("text") or "")
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            decoded = None
        if decoded is not None:
            candidates.extend(_nested_strings(decoded))
        candidates.extend(match.group(1).strip() for match in pattern.finditer(text))

    out, seen = [], set()
    for raw in candidates:
        value = raw.strip().removeprefix("file://")
        if not value or Path(value).suffix.lower() not in _TEXT_SUFFIXES:
            continue
        unresolved = Path(value).expanduser()
        if not unresolved.is_absolute():
            unresolved = cwd / unresolved
        if unresolved.is_symlink():
            continue
        try:
            resolved = unresolved.resolve()
            resolved.relative_to(cwd)
        except (OSError, ValueError):
            continue
        if not resolved.is_file() or resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
        if len(out) >= 4:
            break
    return out


def project_context(cwd_value: Optional[str], events: Iterable[Dict[str, Any]]) -> str:
    """Read bounded project instructions plus relevant durable memory notes."""
    cwd = Path(cwd_value or os.getcwd()).expanduser().resolve()
    chunks: List[str] = []
    remaining = MAX_CONTEXT_CHARS
    for base in (cwd, *cwd.parents):
        for name in _CONTEXT_NAMES:
            path = base / name
            text = _read_bounded(path, min(4_000, remaining))
            if text:
                chunks.append(f"## {path}\n{text}")
                remaining -= len(text)
        if remaining <= 0:
            break
        if (base / ".git").exists():
            break

    for path in _referenced_files(cwd, events):
        text = _read_bounded(path, min(2_500, remaining))
        if text:
            chunks.append(f"## Referenced file: {path}\n{text}")
            remaining -= len(text)
        if remaining <= 0:
            break

    memory_dir = (
        Path.home() / ".claude" / "projects" / _project_key(cwd) / "memory"
    )
    index = memory_dir / "MEMORY.md"
    index_text = _read_bounded(index, min(5_000, remaining))
    if index_text:
        chunks.append(f"## {index}\n{index_text}")
        remaining -= len(index_text)
        terms = _query_terms(events)
        candidates = []
        for link in re.findall(r"\[[^\]]*\]\(([^)]+\.md)\)|\[\[([^\]]+\.md)\]\]", index_text):
            raw = next((part for part in link if part), "")
            unresolved = memory_dir / raw
            target = unresolved.resolve()
            if (unresolved.is_symlink() or target.parent != memory_dir.resolve()
                    or not target.is_file()):
                continue
            score = len(terms & set(re.findall(r"[a-z0-9_-]{4,}", raw.lower())))
            candidates.append((score, target))
        for _, path in sorted(candidates, reverse=True)[:3]:
            text = _read_bounded(path, min(3_000, remaining))
            if text:
                chunks.append(f"## {path}\n{text}")
                remaining -= len(text)
            if remaining <= 0:
                break
    return "\n\n".join(chunks)[:MAX_CONTEXT_CHARS] or "(none found)"


def _event_digest(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    used = 0
    for event in events:
        if not event.get("usable_for_goals") or event.get("redacted"):
            continue
        text = str(event.get("text") or "")[:MAX_EVENT_TEXT]
        row = {
            key: event.get(key)
            for key in ("id", "ordinal", "timestamp", "kind", "role", "tool_name",
                        "tool_use_id", "is_error")
            if event.get(key) not in (None, "")
        }
        row["text"] = text
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        if used + len(encoded) > MAX_EVENT_CHARS:
            break
        rows.append(row)
        used += len(encoded)
    return rows


def _provider(provider=None):
    if provider is not None:
        return provider
    kind = os.environ.get("HC_CHAT_PROVIDER", "claude")
    if kind not in P.DEFAULTS:
        raise P.ProviderError(f"unknown chat provider: {kind}")
    model = os.environ.get("HC_CHAT_MODEL")
    return P.make(kind, "synthesize", model)


def _normalize_initial(data: Dict[str, Any], valid_ids: set) -> Dict[str, Any]:
    raw = data.get("goals")
    if not isinstance(raw, list):
        raise ValueError("chat synthesis response is missing goals")
    out = {"version": 1, "goals": []}
    seen = set()
    for index, value in enumerate(raw[:60], 1):
        if not isinstance(value, dict):
            continue
        goal = deepcopy(value)
        gid = str(goal.get("id") or f"g{index}")[:80]
        if gid in seen:
            gid = f"g{index}"
        seen.add(gid)
        goal["id"] = gid
        goal["title"] = str(goal.get("title") or "Untitled goal")[:120]
        goal["origin"] = "inferred"
        goal["prompt_ids"] = []
        goal["important_item_ids"] = []
        goal["evidence_ids"] = [
            eid for eid in goal.get("evidence_ids", [])
            if isinstance(eid, str) and eid in valid_ids
        ][:40]
        todos = []
        for todo in goal.get("todos", [])[:30]:
            if not isinstance(todo, dict) or not str(todo.get("text") or "").strip():
                continue
            todos.append({
                "text": str(todo["text"])[:160],
                "done": bool(todo.get("done")),
                "evidence_ids": [
                    eid for eid in todo.get("evidence_ids", [])
                    if isinstance(eid, str) and eid in valid_ids
                ][:20],
            })
        goal["todos"] = todos
        out["goals"].append(goal)
    GM.sanitize(out)
    return out


def _filtered_ops(data: Dict[str, Any], valid_ids: set) -> List[Dict[str, Any]]:
    allowed = {"attach_evidence", "add_todo", "complete_todo", "set_status", "new_goal"}
    out = []
    for value in data.get("operations", []) if isinstance(data, dict) else []:
        if not isinstance(value, dict) or value.get("op") not in allowed:
            continue
        op = deepcopy(value)
        if "evidence_ids" in op:
            op["evidence_ids"] = [
                eid for eid in op.get("evidence_ids", [])
                if isinstance(eid, str) and eid in valid_ids
            ][:40]
        out.append(op)
    return out


def _merge_initial_with_manual(inferred: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve UI-authored goals and every manual field across initial races."""
    previous = {g.get("id"): g for g in current.get("goals", [])}
    out = []
    for goal in inferred.get("goals", []):
        old = previous.get(goal.get("id"))
        if old:
            for field in ("prompt_ids", "important_item_ids", "notes", "priority"):
                if field in old:
                    goal[field] = deepcopy(old[field])
            if old.get("origin") == "user":
                goal = deepcopy(old)
        out.append(goal)
    inferred_ids = {g.get("id") for g in out}
    out.extend(deepcopy(g) for g in current.get("goals", [])
               if g.get("id") not in inferred_ids)
    inferred["goals"] = out
    GM.sanitize(inferred)
    return inferred


def refresh(session_id: str, root: Optional[Path] = None, provider=None) -> Dict[str, Any]:
    """Analyze through the latest requested ordinal, coalescing concurrent work."""
    CS.paths(session_id, root)
    try:
        with CS.session_lock(session_id, root, wait_s=0):
            state = CS.get_analyzer_state(session_id, root)
            if state.get("status") == "running":
                CS.request_analysis(session_id, root=root)
                return {"status": "coalesced", "session_id": session_id}
            manifest = CS.load_manifest(session_id, root)
            target = int(manifest.get("last_ordinal") or 0)
            if not target:
                CS.set_analyzer_state(session_id, last_analyzed_ordinal=0,
                                      status="idle", error=None, root=root)
                return {"status": "empty", "session_id": session_id,
                        "analyzed_through": 0, "changes": []}
            CS.set_analyzer_state(session_id, status="running",
                                  requested_ordinal=target, error=None, root=root)
    except TimeoutError:
        CS.request_analysis(session_id, root=root)
        return {"status": "coalesced", "session_id": session_id}

    changes: List[str] = []
    passes = 0
    try:
        while True:
            if passes >= MAX_REFRESH_PASSES:
                CS.set_analyzer_state(session_id, status="pending", error=None, root=root)
                return {"status": "updated" if changes else "coalesced",
                        "session_id": session_id,
                        "analyzed_through": int(CS.get_analyzer_state(session_id, root)
                                                .get("last_analyzed_ordinal") or 0),
                        "changes": changes}
            state = CS.get_analyzer_state(session_id, root)
            cursor = int(state.get("last_analyzed_ordinal") or 0)
            requested = int(state.get("requested_ordinal") or 0)
            events = [e for e in CS.new_events_since(session_id, cursor, root)
                      if int(e.get("ordinal") or 0) <= requested]
            if not events:
                CS.set_analyzer_state(session_id, status="idle", error=None, root=root)
                return {"status": "current" if not changes else "updated",
                        "session_id": session_id, "analyzed_through": cursor,
                        "changes": changes}
            digest = _event_digest(events)
            if not digest:
                cursor = max(int(e.get("ordinal") or 0) for e in events)
                CS.set_analyzer_state(session_id, last_analyzed_ordinal=cursor, root=root)
                continue
            goals, important = CS.load_goals(session_id, root)
            passes += 1
            revision = CS.goal_revision(session_id, root)
            context = project_context(CS.load_manifest(session_id, root).get("cwd"), events)
            valid_ids = {row["id"] for row in digest}
            if not goals.get("goals"):
                prompt = (INITIAL_PROMPT.replace("<<CONTEXT>>", context)
                          .replace("<<EVENTS>>", json.dumps(digest, ensure_ascii=False)))
                proposed = _normalize_initial(_provider(provider).generate_json(prompt), valid_ids)
                proposed = _merge_initial_with_manual(proposed, goals)
                new_important = important
                step_changes = [f"goal + {g['title']}" for g in proposed["goals"]]
            else:
                prompt = (INCREMENTAL_PROMPT
                          .replace("<<TREE>>", json.dumps(goals, ensure_ascii=False))
                          .replace("<<CONTEXT>>", context)
                          .replace("<<EVENTS>>", json.dumps(digest, ensure_ascii=False)))
                response = _provider(provider).generate_json(prompt)
                ops = _filtered_ops(response, valid_ids)
                proposed, new_important = deepcopy(goals), deepcopy(important)
                step_changes = GM.apply_ops(
                    proposed, new_important, ops, max_new_top_level=1
                )
            if not CS.save_goals(session_id, proposed, new_important, root,
                                 expected_revision=revision):
                # A browser edit won the race. Recompute against the new state;
                # do not advance the evidence cursor.
                continue
            # Advance only through evidence actually included in this bounded
            # prompt. Later events remain pending for the next pass/worker.
            through = max(int(row.get("ordinal") or 0) for row in digest)
            changes.extend(step_changes)
            CS.set_analyzer_state(session_id, last_analyzed_ordinal=through,
                                  error=None, root=root)
    except Exception as exc:  # noqa: BLE001 - persist failure for inspection
        CS.set_analyzer_state(session_id, status="error", error=exc, root=root)
        return {"status": "error", "session_id": session_id,
                "analyzed_through": int(CS.get_analyzer_state(session_id, root)
                                        .get("last_analyzed_ordinal") or 0),
                "changes": changes, "error": str(exc)}


def spawn_refresh(session_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """Request analysis and start one detached, coalescing worker process."""
    p = CS.paths(session_id, root)
    with CS.session_lock(session_id, root, wait_s=5):
        requested = CS.request_analysis(session_id, root=root)
        worker = p.session_dir / "analyzer.json"
        try:
            prior = json.loads(worker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            prior = {}
        pid = prior.get("pid")
        try:
            if pid and CS.get_analyzer_state(session_id, root).get("status") == "running":
                os.kill(int(pid), 0)
                return {"status": "coalesced", "session_id": session_id,
                        "requested_ordinal": requested.get("requested_ordinal", 0)}
        except (OSError, TypeError, ValueError):
            pass
        command = [sys.executable, "-m", "human_compact.cli", "chat-refresh",
                   "--session", session_id]
        child_env = os.environ.copy()
        if root is not None:
            child_env["HC_CHAT_STATE_DIR"] = str(Path(root).expanduser().resolve())
        child_env["HC_CHAT_INFERENCE"] = "1"
        log = p.session_dir / "analyzer.log"
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
        with log.open("ab", buffering=0) as handle:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=handle,
                stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
                env=child_env,
            )
        _DETACHED_PROCESSES.append(process)
        worker.write_text(json.dumps({"pid": process.pid}) + "\n", encoding="utf-8")
        worker.chmod(0o600)
        return {"status": "spawned", "session_id": session_id,
                "pid": process.pid,
                "requested_ordinal": requested.get("requested_ordinal", 0)}


def clear_worker_record(session_id: str, root: Optional[Path] = None) -> None:
    worker = CS.paths(session_id, root).session_dir / "analyzer.json"
    try:
        value = json.loads(worker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    if value.get("pid") in (None, os.getpid()):
        worker.unlink(missing_ok=True)
