from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import SCHEMA_VERSION
from .trace import _artifacts, _sanitize_value, _source


Record = Tuple[Dict[str, Any], int, int, int]
_COMPACT_COMMAND = re.compile(r"^/compact(?:\s|$)", re.I)
_TEXT_TYPES = {"input_text", "output_text", "text"}
_MEDIA_TYPES = {"input_image", "image", "document"}


def _read_records(transcript: Path) -> Tuple[List[Record], int]:
    records: List[Record] = []
    position = 0
    with transcript.open("rb") as handle:
        for line_number, raw in enumerate(handle):
            end = position + len(raw)
            try:
                row = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                position = end
                continue
            if isinstance(row, dict):
                records.append((row, position, end, line_number))
            position = end
    return records, position


def _identity(row: Dict[str, Any]) -> str:
    payload = row.get("payload") or {}
    return str(payload.get("id") or row.get("ordinal") or row.get("timestamp") or "record")


def _entry(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"uuid": _identity(row)}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict)
        and str(block.get("type") or "") in _TEXT_TYPES
        and block.get("text")
    ).strip()


def _human_message(payload: Dict[str, Any]) -> bool:
    if payload.get("type") != "message" or payload.get("role") != "user":
        return False
    text = _content_text(payload.get("content"))
    if text and not _COMPACT_COMMAND.match(text):
        return True
    content = payload.get("content")
    return bool(
        isinstance(content, list)
        and any(
            isinstance(block, dict) and str(block.get("type") or "") in _MEDIA_TYPES
            for block in content
        )
    )


def _message_sources(
    row: Dict[str, Any],
    start: int,
    end: int,
) -> List[Dict[str, Any]]:
    payload = row.get("payload") or {}
    if payload.get("type") != "message":
        return []
    role = str(payload.get("role") or "unknown")
    if role not in {"user", "assistant"}:
        return []
    human = _human_message(payload)
    if role == "user" and not human:
        return []
    content = payload.get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    sources: List[Dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in _TEXT_TYPES and block.get("text"):
            sources.append(
                _source(
                    _entry(row),
                    index,
                    "user_prompt" if role == "user" else "assistant_text",
                    role,
                    block.get("text"),
                    start,
                    end,
                )
            )
        elif block_type in _MEDIA_TYPES:
            sources.append(
                _source(
                    _entry(row),
                    index,
                    "image" if "image" in block_type else "document",
                    role,
                    block,
                    start,
                    end,
                )
            )
    return sources


def _semantic_tool_source(
    row: Dict[str, Any],
    start: int,
    end: int,
) -> Optional[Dict[str, Any]]:
    payload = row.get("payload") or {}
    if row.get("type") != "event_msg" or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type in {"", "AgentMessage", "ContextCompaction", "Reasoning", "UserMessage"}:
        return None
    if item_type == "CommandExecution":
        tool_name = "Bash"
        body = {
            "command": item.get("command"),
            "cwd": item.get("cwd"),
            "status": item.get("status"),
            "exit_code": item.get("exit_code"),
            "duration": item.get("duration"),
            "stdout": item.get("stdout"),
            "stderr": item.get("stderr"),
        }
        kind = "tool_result"
    elif item_type == "FileChange":
        tool_name = "apply_patch"
        body = {
            "status": item.get("status"),
            "changes": item.get("changes"),
            "stdout": item.get("stdout"),
            "stderr": item.get("stderr"),
        }
        kind = "file_change"
    elif item_type == "Extension":
        tool_name = str(item.get("kind") or "extension")
        body = {
            "kind": item.get("kind"),
            "action": item.get("action"),
            "query": item.get("query"),
            "results": item.get("results"),
        }
        kind = "tool_result"
    else:
        tool_name = item_type
        body = item
        kind = "tool_result"
    return _source(_entry(row), 0, kind, "tool", body, start, end, tool_name)


def _fallback_tool_source(
    row: Dict[str, Any],
    start: int,
    end: int,
    names: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    payload = row.get("payload") or {}
    if row.get("type") != "response_item":
        return None
    payload_type = str(payload.get("type") or "")
    if payload_type in {"custom_tool_call", "function_call"}:
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        name = str(payload.get("name") or "tool")
        if call_id:
            names[call_id] = name
        raw_input = payload.get("input", payload.get("arguments", {}))
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except json.JSONDecodeError:
                pass
        return _source(
            _entry(row),
            0,
            "tool_call",
            "assistant",
            {"name": name, "input": raw_input},
            start,
            end,
            name,
        )
    if payload_type in {"custom_tool_call_output", "function_call_output"}:
        call_id = str(payload.get("call_id") or "")
        name = names.get(call_id, "tool")
        return _source(
            _entry(row),
            0,
            "tool_result",
            "tool",
            payload.get("output"),
            start,
            end,
            name,
        )
    return None


def _window_and_usage(
    records: Sequence[Record],
    active: Sequence[Record],
    status: Optional[Dict[str, Any]],
) -> Tuple[Optional[int], str, Optional[int]]:
    raw_override = os.environ.get("COMPACT_FOCUS_WINDOW", "")
    if raw_override.isdigit() and int(raw_override) > 0:
        window: Optional[int] = int(raw_override)
        window_source = "env:COMPACT_FOCUS_WINDOW"
    else:
        window = None
        window_source = "unknown"
    used: Optional[int] = None
    for row, _start, _end, _line in records:
        payload = row.get("payload") or {}
        candidate = None
        if row.get("type") == "session_meta":
            candidate = payload.get("context_window")
        elif row.get("type") == "event_msg" and payload.get("type") == "token_count":
            candidate = (payload.get("info") or {}).get("model_context_window")
        if window is None and isinstance(candidate, (int, float)) and candidate > 0:
            window = int(candidate)
            window_source = "codex-transcript"
    if window is None and isinstance(status, dict):
        candidate = status.get("model_context_window") or status.get("context_window")
        if isinstance(candidate, (int, float)) and candidate > 0:
            window = int(candidate)
            window_source = "hook-status"
    for row, _start, _end, _line in active:
        payload = row.get("payload") or {}
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        last = (payload.get("info") or {}).get("last_token_usage")
        if not isinstance(last, dict):
            continue
        candidate = last.get("total_tokens")
        if not isinstance(candidate, (int, float)):
            inputs = last.get("input_tokens")
            outputs = last.get("output_tokens")
            if isinstance(inputs, (int, float)) and isinstance(outputs, (int, float)):
                candidate = inputs + outputs
        if isinstance(candidate, (int, float)) and candidate >= 0:
            used = int(candidate)
    return window, window_source, used


def _input_audit(active: Sequence[Record]) -> Dict[str, Any]:
    reasoning = 0
    media = 0
    unsupported: Dict[str, int] = {}
    known = {
        "compacted",
        "event_msg",
        "response_item",
        "session_meta",
        "turn_context",
        "world_state",
    }
    for row, _start, _end, _line in active:
        row_type = str(row.get("type") or "")
        payload = row.get("payload") or {}
        if row_type == "response_item" and payload.get("type") == "reasoning":
            reasoning += 1
        if row_type == "response_item" and payload.get("type") == "message":
            for block in payload.get("content") or []:
                if isinstance(block, dict) and str(block.get("type") or "") in _MEDIA_TYPES:
                    media += 1
        if row_type not in known:
            unsupported[row_type or "unknown"] = unsupported.get(row_type or "unknown", 0) + 1
    return {
        "private_reasoning_blocks_excluded": reasoning,
        "media_or_document_blocks_metadata_only": media,
        "attachment_counts": {},
        "attachment_types_itemized": [],
        "unsupported_record_counts": dict(sorted(unsupported.items())),
    }


def _rehash_and_remeasure(trace: Dict[str, Any]) -> None:
    digest = hashlib.sha256()
    visible = 0
    window = (trace.get("context") or {}).get("window_tokens")
    used = (trace.get("context") or {}).get("used_tokens_observed")
    for ordinal, episode in enumerate(trace.get("episodes", []), 1):
        episode["ordinal"] = ordinal
        episode_tokens = sum(max(1, int(source.get("tokens_estimate") or 1)) for source in episode.get("sources", []))
        episode["tokens_estimate"] = episode_tokens
        episode["window_pct_estimate"] = round(episode_tokens * 100.0 / window, 2) if window else None
        episode["used_context_pct_estimate"] = round(episode_tokens * 100.0 / used, 2) if used else None
        visible += episode_tokens
        digest.update(str(episode.get("kind") or "").encode("utf-8"))
        for source in episode.get("sources", []):
            digest.update(str(source.get("id") or "").encode("utf-8"))
            digest.update(str(source.get("kind") or "").encode("utf-8"))
    trace["source_hash"] = digest.hexdigest()
    context = trace.setdefault("context", {})
    context["visible_tokens_estimate"] = visible
    context["unattributed_tokens_estimate"] = max(0, int(used or 0) - visible)
    context["visible_share_of_used_pct_estimate"] = round(visible * 100.0 / used, 2) if used else None


def add_review_contract(
    trace: Dict[str, Any],
    review: Dict[str, Any],
    cycle_id: str,
) -> None:
    """Make the previous human contract explicit when Codex's summary is opaque."""
    episodes: List[Dict[str, Any]] = []
    precommit = str(review.get("precommit") or "").strip()
    if precommit:
        source_id = "s-" + hashlib.sha1(f"{cycle_id}:precommit:{precommit}".encode("utf-8")).hexdigest()[:12]
        episodes.append(
            {
                "id": "e-" + hashlib.sha1(source_id.encode("utf-8")).hexdigest()[:12],
                "kind": "reviewed_contract",
                "title": "Do not misinterpret",
                "sources": [
                    {
                        "id": source_id,
                        "kind": "reviewed_contract",
                        "role": "developer",
                        "tool_name": None,
                        "class": "other",
                        "text": precommit,
                        "truncated": False,
                        "tokens_estimate": max(1, len(precommit) // 4),
                        "entry_uuid": f"carry:{cycle_id}:precommit",
                        "byte_range": None,
                        "artifacts": _artifacts(precommit),
                    }
                ],
                "carry_forward": {
                    "type": "constraint",
                    "status": "active",
                    "retention": "preserve",
                    "confidence": "high",
                    "summary": precommit,
                    "next_step": "",
                },
            }
        )
    for item in review.get("items", []):
        if item.get("retention") not in {"preserve", "summarize"}:
            continue
        title = str(item.get("title") or "Prior reviewed item").strip()[:180]
        summary = str(item.get("summary") or "").strip()
        next_step = str(item.get("next_step") or "").strip()
        text = summary + (("\nNext: " + next_step) if next_step else "")
        identity = f"{cycle_id}:{item.get('id')}:{item.get('retention')}:{text}"
        source_id = "s-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
        episodes.append(
            {
                "id": "e-" + hashlib.sha1(source_id.encode("utf-8")).hexdigest()[:12],
                "kind": "reviewed_contract",
                "title": title,
                "sources": [
                    {
                        "id": source_id,
                        "kind": "reviewed_contract",
                        "role": "developer",
                        "tool_name": None,
                        "class": "other",
                        "text": text,
                        "truncated": False,
                        "tokens_estimate": max(1, len(text) // 4),
                        "entry_uuid": f"carry:{cycle_id}:{item.get('id')}",
                        "byte_range": None,
                        "artifacts": _artifacts(text),
                    }
                ],
                "carry_forward": {
                    "type": item.get("type"),
                    "status": item.get("status"),
                    "retention": item.get("retention"),
                    "confidence": item.get("confidence"),
                    "summary": summary,
                    "next_step": next_step,
                },
            }
        )
    if not episodes:
        return
    trace["episodes"] = episodes + list(trace.get("episodes", []))
    trace.setdefault("warnings", []).append(
        f"Carried {len(episodes)} item(s) from the previous human-reviewed contract because Codex does not expose remote summary text."
    )
    trace["carry_forward_cycle"] = cycle_id
    _rehash_and_remeasure(trace)


def build_codex_trace(
    transcript: Path,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    records, snapshot_bytes = _read_records(transcript)
    boundary_index: Optional[int] = None
    for index, (row, _start, _end, _line) in enumerate(records):
        if row.get("type") == "compacted":
            boundary_index = index
    active = records[boundary_index + 1 :] if boundary_index is not None else records
    semantic_tools = any(_semantic_tool_source(row, start, end) is not None for row, start, end, _line in active)
    has_human_anchor = any(
        row.get("type") == "response_item" and _human_message(row.get("payload") or {})
        for row, _start, _end, _line in active
    )

    episodes: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    fallback_names: Dict[str, str] = {}

    def start_episode(kind: str, row: Dict[str, Any], start: int) -> Dict[str, Any]:
        seed = f"{_identity(row)}:{start}:{kind}"
        return {
            "id": "e-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12],
            "kind": kind,
            "sources": [],
            "byte_range": [start, start],
        }

    for row, start, end, _line in active:
        payload = row.get("payload") or {}
        if row.get("type") == "response_item" and _human_message(payload):
            current = start_episode("turn", row, start)
            episodes.append(current)
        elif (
            not has_human_anchor
            and row.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and (current is None or current.get("sources"))
        ):
            current = start_episode("continuation", row, start)
            episodes.append(current)
        sources = _message_sources(row, start, end)
        tool_source = _semantic_tool_source(row, start, end)
        if tool_source is not None:
            sources.append(tool_source)
        elif not semantic_tools:
            fallback = _fallback_tool_source(row, start, end, fallback_names)
            if fallback is not None:
                sources.append(fallback)
        if sources and current is None:
            current = start_episode("preamble", row, start)
            episodes.append(current)
        if sources and current is not None:
            current["sources"].extend(sources)
            current["byte_range"][1] = end

    episodes = [episode for episode in episodes if episode.get("sources")]
    for index, episode in enumerate(episodes, 1):
        source_identity = ":".join(str(source.get("id")) for source in episode["sources"])
        episode["id"] = "e-" + hashlib.sha1(source_identity.encode("utf-8")).hexdigest()[:12]
        episode["ordinal"] = index
        prompt = next(
            (str(source.get("text") or "") for source in episode["sources"] if source.get("kind") == "user_prompt"),
            "",
        )
        if not prompt:
            prompt = next(
                (str(source.get("text") or "") for source in episode["sources"] if source.get("kind") == "assistant_text"),
                "",
            )
        episode["title"] = " ".join(prompt.split())[:180] or f"Conversation evidence {index}"
        classes: Dict[str, int] = {}
        for source in episode["sources"]:
            name = str(source.get("class") or "other")
            classes[name] = classes.get(name, 0) + int(source.get("tokens_estimate") or 0)
        episode["classes"] = classes

    session_id = ""
    cwd = ""
    for row, _start, _end, _line in records:
        if row.get("type") == "session_meta":
            payload = row.get("payload") or {}
            session_id = str(payload.get("session_id") or payload.get("id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
    for row, _start, _end, _line in reversed(records):
        if row.get("type") == "turn_context":
            cwd = str((row.get("payload") or {}).get("cwd") or cwd)
            break

    window, window_source, used = _window_and_usage(records, active, status)
    warnings = [
        "Per-episode token shares are estimates reconstructed from Codex rollout evidence.",
        "System prompts, tool schemas, developer context, and encrypted compaction state are not attributable to episodes.",
        "Codex transcript JSONL is an explicitly unstable hook interface; unsupported records are counted rather than silently treated as user evidence.",
    ]
    if boundary_index is None:
        warnings.append("No prior compaction boundary was found; the trace begins at session start.")
    else:
        warnings.append(
            "The previous remote compaction summary is encrypted and cannot be inspected; only post-boundary evidence and Compact Focus's own prior reviewed contract are reviewable."
        )
    if window is None:
        warnings.append("Context-window size is unknown; percentage estimates are unavailable.")
    audit = _input_audit(active)
    if audit["private_reasoning_blocks_excluded"]:
        warnings.append("Reasoning records are intentionally excluded from reusable transcript evidence.")
    if audit["media_or_document_blocks_metadata_only"]:
        warnings.append("Binary media payloads are represented by bounded metadata rather than copied into state.")

    result: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "platform": "codex",
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": str(transcript),
        "source_hash": "",
        "snapshot_bytes": snapshot_bytes,
        "boundary": {
            "found": boundary_index is not None,
            "line": records[boundary_index][3] if boundary_index is not None else None,
            "byte": records[boundary_index][1] if boundary_index is not None else 0,
            "summary_inspectable": False if boundary_index is not None else None,
        },
        "context": {
            "window_tokens": window,
            "window_source": window_source,
            "used_tokens_observed": used,
            "used_pct_observed": round(used * 100.0 / window, 1) if used and window else None,
            "visible_tokens_estimate": 0,
            "unattributed_tokens_estimate": 0,
            "visible_share_of_used_pct_estimate": None,
        },
        "episodes": episodes,
        "input_audit": audit,
        "warnings": warnings,
    }
    _rehash_and_remeasure(result)
    return result
