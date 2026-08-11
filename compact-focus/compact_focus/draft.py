from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .proposal import RETENTIONS
from .review import (
    WORK_STATES,
    add_clarification,
    effective_source_retention,
    ensure_review_shape,
    record_action,
    set_item_field,
    set_precommit,
    set_source_retention,
    set_work_state,
)
from .state import utc_now


RETENTION_HEADING = {
    "preserve": "Preserve faithfully",
    "summarize": "Compact to outcomes",
    "demote": "Remove from active context",
}
RETENTION_LABEL = {
    "preserve": "PRESERVE",
    "summarize": "COMPACT",
    "demote": "DELETE",
}
WORK_STATE_LABEL = {
    "todo": "TODO",
    "in_progress": "IN PROGRESS",
    "done": "DONE",
    "blocked": "BLOCKED",
}


REVISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reply",
        "draft",
        "cluster_changes",
        "source_changes",
        "global_constraint",
        "replace_global_constraint",
    ],
    "properties": {
        "reply": {"type": "string", "maxLength": 1000},
        "draft": {"type": "string", "minLength": 1, "maxLength": 24000},
        "global_constraint": {"type": "string"},
        "replace_global_constraint": {"type": "boolean"},
        "cluster_changes": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "retention",
                    "work_state",
                    "title",
                    "summary",
                    "next_step",
                    "clarification",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "retention": {
                        "anyOf": [
                            {"type": "string", "enum": list(RETENTIONS)},
                            {"type": "null"},
                        ]
                    },
                    "work_state": {
                        "anyOf": [
                            {"type": "string", "enum": list(WORK_STATES)},
                            {"type": "null"},
                        ]
                    },
                    "title": {"type": ["string", "null"]},
                    "summary": {"type": ["string", "null"]},
                    "next_step": {"type": ["string", "null"]},
                    "clarification": {"type": ["string", "null"]},
                },
            },
        },
        "source_changes": {
            "type": "array",
            "maxItems": 300,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "retention", "work_state", "clarification"],
                "properties": {
                    "id": {"type": "string"},
                    "retention": {
                        "anyOf": [
                            {"type": "string", "enum": list(RETENTIONS)},
                            {"type": "null"},
                        ]
                    },
                    "work_state": {
                        "anyOf": [
                            {"type": "string", "enum": list(WORK_STATES)},
                            {"type": "null"},
                        ]
                    },
                    "clarification": {"type": ["string", "null"]},
                },
            },
        },
    },
}

SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["draft"],
    "properties": {
        "draft": {"type": "string", "minLength": 1, "maxLength": 12000},
    },
}


class DraftError(RuntimeError):
    pass


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _source_map(trace: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(source["id"]): source
        for episode in trace.get("episodes", [])
        for source in episode.get("sources", [])
        if source.get("id")
    }


def _source_title(source: Dict[str, Any]) -> str:
    text = _one_line(source.get("text"))
    label = str(source.get("tool_name") or source.get("kind") or "source").replace("_", " ")
    return f"{label}: {text[:120]}" if text else label


def build_draft(trace: Dict[str, Any], review: Dict[str, Any]) -> str:
    """Create the exact human-readable summary shown before native compaction."""
    ensure_review_shape(review)
    sources = _source_map(trace)
    lines: List[str] = ["# Reviewed compaction summary"]
    precommit = str(review.get("precommit") or "").strip()
    if precommit:
        lines.extend(["", "## Non-negotiable interpretation", precommit])

    for retention in RETENTIONS:
        matching = [item for item in review.get("items", []) if item.get("retention") == retention]
        if not matching:
            continue
        lines.extend(["", f"## {RETENTION_HEADING[retention]}"])
        for item in matching:
            work_state = WORK_STATE_LABEL.get(str(item.get("work_state")), "TODO")
            confidence = str(item.get("confidence") or "low").upper()
            title = _one_line(item.get("title"))
            summary = str(item.get("summary") or "").strip()
            if retention == "demote":
                lines.append(f"- [{work_state} · {confidence}] {title} — omit from active context; locally recoverable.")
            else:
                lines.append(f"- [{work_state} · {confidence}] {title}: {summary}")
                next_step = str(item.get("next_step") or "").strip()
                if next_step:
                    lines.append(f"  Next: {next_step}")
                artifacts = sorted(
                    {
                        value
                        for source_id in item.get("source_ids", [])
                        if effective_source_retention(review, item, source_id) != "demote"
                        for key in ("paths", "commits")
                        for value in (sources.get(source_id) or {}).get("artifacts", {}).get(key, [])
                    }
                )
                if artifacts and "artifacts:" not in summary.lower():
                    lines.append("  Artifacts: " + ", ".join(artifacts[:20]))
            for clarification in item.get("clarifications", []):
                if str(clarification).strip():
                    lines.append(f"  User clarification: {str(clarification).strip()}")

    overrides: List[str] = []
    for item in review.get("items", []):
        cluster_retention = str(item.get("retention") or "preserve")
        cluster_work_state = str(item.get("work_state") or "todo")
        for source_id in item.get("source_ids", []):
            source_review = review.get("source_reviews", {}).get(source_id) or {}
            retention = effective_source_retention(review, item, source_id)
            work_state = str(source_review.get("work_state") or cluster_work_state)
            clarifications = [str(value).strip() for value in source_review.get("clarifications", []) if str(value).strip()]
            if retention == cluster_retention and work_state == cluster_work_state and not clarifications:
                continue
            source = sources.get(source_id, {"id": source_id})
            detail = _source_title(source)
            line = (
                f"- {source_id} · {RETENTION_LABEL[retention]}"
                f" · {WORK_STATE_LABEL.get(work_state, 'TODO')} · "
                f"{detail}"
            )
            if retention == "demote":
                line += " — omit source evidence; locally recoverable."
            overrides.append(line)
            overrides.extend(f"  User clarification: {value}" for value in clarifications)
    if overrides:
        lines.extend(["", "## Source-level overrides", *overrides])

    lines.extend(
        [
            "",
            "## Recovery boundary",
            "Items marked Remove and source units marked DELETE leave active context but remain in Compact Focus recovery.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def ensure_draft(trace: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    ensure_review_shape(review)
    state = review.setdefault("draft_review", {})
    if not isinstance(state, dict):
        state = {}
        review["draft_review"] = state
    if not state.get("draft") or state.get("stale"):
        state["draft"] = build_draft(trace, review)
        state["stale"] = False
        state["approved"] = False
        state["messages"] = []
        state["revision_count"] = 0
        state["generated_by"] = "deterministic"
        state["generated_at"] = utc_now()
    return state


def approve_draft(review: Dict[str, Any]) -> str:
    state = review.get("draft_review") or {}
    draft = str(state.get("draft") or "").strip()
    if not draft:
        raise DraftError("the compaction draft is empty")
    maximum = int(os.environ.get("COMPACT_FOCUS_DRAFT_MAX_CHARS", "24000"))
    if len(draft) > maximum:
        raise DraftError(
            f"the compaction draft is {len(draft):,} characters; chat or edit it below {maximum:,} before confirming"
        )
    review["approved_summary"] = draft + "\n"
    state["approved"] = True
    state["approved_at"] = utc_now()
    record_action(review, "approve_draft", revision_count=int(state.get("revision_count") or 0))
    return review["approved_summary"]


def edit_draft(review: Dict[str, Any], value: str) -> None:
    draft = str(value).strip()
    if not draft:
        raise DraftError("the compaction draft cannot be empty")
    state = review.setdefault("draft_review", {})
    state["draft"] = draft + "\n"
    state["stale"] = False
    state["approved"] = False
    state["revision_count"] = int(state.get("revision_count") or 0) + 1
    state["generated_by"] = "human"
    state.setdefault("messages", []).append(
        {"ts": utc_now(), "role": "assistant", "text": "Draft edited directly by the user."}
    )
    review["approved_summary"] = ""
    record_action(review, "edit_draft", chars=len(draft))


def _revision_context(trace: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    sources = _source_map(trace)
    clusters: List[Dict[str, Any]] = []
    for item in review.get("items", []):
        units = []
        for source_id in item.get("source_ids", []):
            source = sources.get(source_id, {})
            source_review = review.get("source_reviews", {}).get(source_id) or {}
            units.append(
                {
                    "id": source_id,
                    "kind": source.get("kind"),
                    "retention": effective_source_retention(review, item, source_id),
                    "work_state": source_review.get("work_state"),
                    "clarifications": source_review.get("clarifications", []),
                    "text": str(source.get("text") or "")[:800],
                }
            )
        clusters.append(
            {
                key: item.get(key)
                for key in (
                    "id",
                    "title",
                    "summary",
                    "retention",
                    "work_state",
                    "confidence",
                    "rationale",
                    "next_step",
                    "clarifications",
                )
            }
            | {"sources": units}
        )
    return {
        "global_constraint": review.get("precommit", ""),
        "clusters": clusters,
    }


def _summary_context(trace: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    sources = _source_map(trace)
    clusters: List[Dict[str, Any]] = []
    for item in review.get("items", []):
        evidence: List[Dict[str, Any]] = []
        for source_id in item.get("source_ids", []):
            source = sources.get(source_id, {})
            if source.get("kind") != "user_prompt" and source.get("class") != "subagents":
                continue
            source_review = review.get("source_reviews", {}).get(source_id) or {}
            evidence.append(
                {
                    "kind": source.get("kind"),
                    "class": source.get("class"),
                    "retention": effective_source_retention(review, item, source_id),
                    "work_state": source_review.get("work_state"),
                    "clarifications": source_review.get("clarifications", []),
                    "text": str(source.get("text") or "")[:1600],
                }
            )
        clusters.append(
            {
                key: item.get(key)
                for key in (
                    "title",
                    "summary",
                    "retention",
                    "work_state",
                    "confidence",
                    "next_step",
                    "clarifications",
                )
            }
            | {"reviewable_evidence": evidence}
        )
    return {
        "global_constraint": review.get("precommit", ""),
        "clusters": clusters,
    }


def build_summary_prompt(trace: Dict[str, Any], review: Dict[str, Any]) -> str:
    maximum = int(os.environ.get("COMPACT_FOCUS_GENERATED_SUMMARY_MAX_CHARS", "9000"))
    return f"""Write the carry-forward context summary that a coding agent should receive after compaction.

The reviewed contract below is ground truth. Preserve every non-negotiable constraint and the semantics of every PRESERVE cluster. Compress COMPACT clusters to decisions, outcomes, and consequences. Do not carry DELETE clusters into active context except when one short warning is required to prevent a live task from becoming incoherent. Preserve active work state, unresolved blockers, user corrections, exact next actions, and important file or commit references already present in cluster summaries. Never invent facts.

Produce concise, self-contained Markdown, normally 300-900 words and no more than {maximum:,} characters. Prefer dense sections such as Current objective, Binding decisions, Current state, and Next actions. Do not reproduce the ledger, confidence scores, source IDs, raw tool logs, file diffs, recovery mechanics, or a catalog of deleted material. Write the summary itself, without a preamble about what you did.

REVIEWED CONTRACT:
{json.dumps(_summary_context(trace, review), ensure_ascii=False)}
"""


def build_revision_prompt(trace: Dict[str, Any], review: Dict[str, Any], feedback: str) -> str:
    state = ensure_draft(trace, review)
    history = list(state.get("messages", []))[-8:]
    return f"""Revise a proposed context-compaction summary from the user's feedback.

The current structured contract is ground truth. Do not invent evidence or identifiers. The user may explicitly change a cluster/source label, work state, title, summary, next step, clarification, or global constraint; encode each such semantic change in the matching structured change array. Use null for every unchanged cluster/source field. An empty next_step or summary explicitly clears that field. Set replace_global_constraint true only when the user asks to add, replace, or clear that constraint; an empty global_constraint with true clears it. If wording alone changes, leave the change arrays empty. DELETE means remove from active context, not erase local recovery. Keep the revised draft self-contained and concise. Your reply should say what changed in one sentence.

STRUCTURED CONTRACT:
{json.dumps(_revision_context(trace, review), ensure_ascii=False)}

CURRENT DRAFT:
{state.get('draft', '')}

RECENT REVIEW CHAT:
{json.dumps(history, ensure_ascii=False)}

USER FEEDBACK:
{feedback.strip()[:8000]}
"""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _parse_worker_output(stdout: str) -> Dict[str, Any]:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DraftError(f"draft worker output was not JSON: {exc}") from exc
    if isinstance(envelope, dict):
        for key in ("structured_output", "structuredOutput"):
            value = envelope.get(key)
            if isinstance(value, dict):
                return value
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                value = json.loads(result)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                return value
        if "draft" in envelope:
            return envelope
    raise DraftError("draft worker returned no structured output")


def _run_schema_worker(
    trace: Dict[str, Any],
    prompt: str,
    schema: Dict[str, Any],
    worker_role: str,
    *,
    runner: Runner = subprocess.run,
    timeout: int = 180,
) -> Dict[str, Any]:
    host = str(trace.get("platform") or "claude")
    effort = os.environ.get("COMPACT_FOCUS_EFFORT", "low")
    temporary: Optional[tempfile.TemporaryDirectory[str]] = None
    output_path: Optional[Path] = None
    if host == "codex":
        executable = shutil.which("codex")
        if not executable:
            raise DraftError("Codex CLI is unavailable; edit the draft directly with e")
        model = os.environ.get(
            "COMPACT_FOCUS_CODEX_MODEL",
            os.environ.get("COMPACT_FOCUS_MODEL", "gpt-5.6-luna"),
        )
        temporary = tempfile.TemporaryDirectory(prefix="compact-focus-draft-")
        workdir = Path(temporary.name)
        schema_path = workdir / "draft.schema.json"
        output_path = workdir / "draft.json"
        schema_path.write_text(json.dumps(schema, separators=(",", ":")), encoding="utf-8")
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-C",
            str(workdir),
            "-s",
            "read-only",
            "-a",
            "never",
            "-c",
            "features.hooks=false",
            "-c",
            "features.plugins=false",
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-c",
            f'developer_instructions="You are a bounded {worker_role}. Return only the requested JSON. Do not call tools or use external context."',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--model",
            model,
            "-",
        ]
    else:
        executable = shutil.which("claude")
        if not executable:
            raise DraftError("Claude CLI is unavailable; edit the draft directly with e")
        model = os.environ.get("COMPACT_FOCUS_DRAFT_MODEL", os.environ.get("COMPACT_FOCUS_MODEL", "haiku"))
        budget = os.environ.get("COMPACT_FOCUS_DRAFT_MAX_BUDGET_USD", "0.08")
        command = [
            executable,
            "-p",
            "--safe-mode",
            "--model",
            model,
            "--effort",
            effort,
            "--tools",
            "",
            "--disable-slash-commands",
            "--system-prompt",
            f"You are a bounded {worker_role}. Return only schema-valid JSON. Do not use tools, skills, or external context.",
            "--no-session-persistence",
            "--max-budget-usd",
            budget,
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            "--output-format",
            "json",
        ]
    try:
        result = runner(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = _one_line(result.stderr or result.stdout)[:500]
            raise DraftError(f"draft worker exited {result.returncode}: {detail}")
        output = result.stdout
        if output_path is not None and output_path.is_file():
            output = output_path.read_text(encoding="utf-8")
        return _parse_worker_output(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DraftError(f"draft worker failed: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.cleanup()


def run_summary_worker(
    trace: Dict[str, Any],
    review: Dict[str, Any],
    *,
    runner: Runner = subprocess.run,
    timeout: int = 180,
) -> Dict[str, Any]:
    result = _run_schema_worker(
        trace,
        build_summary_prompt(trace, review),
        SUMMARY_SCHEMA,
        "context-compaction summary writer",
        runner=runner,
        timeout=timeout,
    )
    draft = str(result.get("draft") or "").strip()
    if not draft:
        raise DraftError("summary worker returned an empty draft")
    maximum = int(os.environ.get("COMPACT_FOCUS_GENERATED_SUMMARY_MAX_CHARS", "9000"))
    if len(draft) > maximum:
        raise DraftError(f"generated summary exceeded {maximum:,} characters")
    return {"draft": draft}


def run_revision_worker(
    trace: Dict[str, Any],
    review: Dict[str, Any],
    feedback: str,
    *,
    runner: Runner = subprocess.run,
    timeout: int = 180,
) -> Dict[str, Any]:
    return _run_schema_worker(
        trace,
        build_revision_prompt(trace, review, feedback),
        REVISION_SCHEMA,
        "compaction-draft revision worker",
        runner=runner,
        timeout=timeout,
    )


def apply_generated_summary(review: Dict[str, Any], result: Dict[str, Any]) -> str:
    draft = str(result.get("draft") or "").strip()
    if not draft:
        raise DraftError("summary worker returned an empty draft")
    state = review.setdefault("draft_review", {})
    state["draft"] = draft + "\n"
    state["stale"] = False
    state["approved"] = False
    state["messages"] = []
    state["revision_count"] = 0
    state["generated_by"] = "model"
    state["generated_at"] = utc_now()
    review["approved_summary"] = ""
    record_action(review, "generate_summary", chars=len(draft))
    return state["draft"]


def apply_revision(
    trace: Dict[str, Any],
    review: Dict[str, Any],
    feedback: str,
    revision: Dict[str, Any],
) -> str:
    ensure_review_shape(review)
    item_index = {str(item.get("id")): index for index, item in enumerate(review.get("items", []))}
    source_index = {
        str(source_id): index
        for index, item in enumerate(review.get("items", []))
        for source_id in item.get("source_ids", [])
    }
    for change in revision.get("cluster_changes", []):
        if not isinstance(change, dict) or str(change.get("id")) not in item_index:
            raise DraftError(f"draft worker referenced unknown cluster {change.get('id')}")
        index = item_index[str(change["id"])]
        if change.get("retention") is not None:
            set_item_field(review, index, "retention", str(change["retention"]))
        if change.get("work_state") is not None:
            set_work_state(review, index, str(change["work_state"]))
        if change.get("title") is not None and str(change.get("title") or "").strip():
            set_item_field(review, index, "title", str(change["title"]))
        if change.get("summary") is not None:
            set_item_field(review, index, "summary", str(change["summary"]))
        if change.get("next_step") is not None:
            set_item_field(review, index, "next_step", str(change["next_step"]))
        if change.get("clarification") is not None and str(change.get("clarification") or "").strip():
            add_clarification(review, index, str(change["clarification"]))
    for change in revision.get("source_changes", []):
        if not isinstance(change, dict) or str(change.get("id")) not in source_index:
            raise DraftError(f"draft worker referenced unknown source {change.get('id')}")
        source_id = str(change["id"])
        index = source_index[source_id]
        if change.get("retention") is not None:
            set_source_retention(review, index, source_id, str(change["retention"]))
        if change.get("work_state") is not None:
            set_work_state(review, index, str(change["work_state"]), source_id=source_id)
        if change.get("clarification") is not None and str(change.get("clarification") or "").strip():
            add_clarification(review, index, str(change["clarification"]), source_id=source_id)
    constraint = str(revision.get("global_constraint") or "").strip()
    if revision.get("replace_global_constraint"):
        set_precommit(review, constraint)

    draft = str(revision.get("draft") or "").strip()
    if not draft:
        raise DraftError("draft worker returned an empty draft")
    state = review.setdefault("draft_review", {})
    state["draft"] = draft + "\n"
    state["stale"] = False
    state["approved"] = False
    state["revision_count"] = int(state.get("revision_count") or 0) + 1
    state["generated_by"] = "model"
    messages = state.setdefault("messages", [])
    messages.append({"ts": utc_now(), "role": "user", "text": feedback.strip()[:8000]})
    reply = _one_line(revision.get("reply"))[:1000] or "Revised the draft."
    messages.append({"ts": utc_now(), "role": "assistant", "text": reply})
    review["approved_summary"] = ""
    record_action(review, "chat_revision", chars=len(draft), reply=reply)
    return reply


def revise_draft(
    trace: Dict[str, Any],
    review: Dict[str, Any],
    feedback: str,
    *,
    runner: Runner = subprocess.run,
    timeout: int = 180,
) -> str:
    revision = run_revision_worker(trace, review, feedback, runner=runner, timeout=timeout)
    return apply_revision(trace, review, feedback, revision)
