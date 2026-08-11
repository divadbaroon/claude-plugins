from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import SCHEMA_VERSION
from .state import utc_now
from .trace import evidence_text


ITEM_TYPES = (
    "decision",
    "constraint",
    "hypothesis",
    "test",
    "result",
    "dead_end",
    "open_question",
    "artifact",
    "mechanical",
    "context",
)
STATUSES = ("active", "resolved", "unclear")
RETENTIONS = ("preserve", "summarize", "demote")
CONFIDENCES = ("high", "medium", "low")
RETENTION_WEIGHT = {"demote": 0, "summarize": 1, "preserve": 2}


PROPOSAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["representations", "items"],
    "properties": {
        "representations": {
            "type": "array",
            "minItems": 2,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "title", "thesis", "chunks"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "thesis": {"type": "string"},
                    "chunks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "source_ids"],
                            "properties": {
                                "label": {"type": "string"},
                                "source_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "summary",
                    "type",
                    "status",
                    "retention",
                    "confidence",
                    "needs_review",
                    "source_ids",
                    "rationale",
                    "next_step",
                    "rival_interpretations",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "type": {"type": "string", "enum": list(ITEM_TYPES)},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "retention": {"type": "string", "enum": list(RETENTIONS)},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                    "needs_review": {"type": "boolean"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                    "next_step": {"type": "string"},
                    "rival_interpretations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}


DEFAULT_CLASS_RULES = [
    {
        "id": "first_n",
        "label": "First part of this context",
        "enabled": True,
        "retention": "preserve",
        "percent": 30,
    },
    {
        "id": "file_changes",
        "label": "File changes and their results",
        "enabled": True,
        "retention": "preserve",
    },
    {
        "id": "subagents",
        "label": "Subagent transcripts",
        "enabled": True,
        "retention": "summarize",
    },
    {
        "id": "todos",
        "label": "Task state and todos",
        "enabled": True,
        "retention": "preserve",
    },
]


class ProposalError(RuntimeError):
    pass


def _clip(value: Any, limit: int, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    head = text[: max(1, limit - 1)]
    if " " in head:
        candidate = head.rsplit(" ", 1)[0].rstrip(".,:;")
        if len(candidate) >= max(12, limit // 2):
            return candidate + "…"
    return head + "…"


def _source_index(trace: Dict[str, Any]) -> Tuple[List[str], Dict[str, Dict[str, Any]], Dict[str, str]]:
    ordered: List[str] = []
    sources: Dict[str, Dict[str, Any]] = {}
    episodes: Dict[str, str] = {}
    for episode in trace.get("episodes", []):
        episode_id = str(episode.get("id") or "")
        for source in episode.get("sources", []):
            source_id = str(source.get("id") or "")
            if not source_id or source_id in sources:
                continue
            ordered.append(source_id)
            sources[source_id] = source
            episodes[source_id] = episode_id
    return ordered, sources, episodes


def _item_id(source_ids: Sequence[str], title: str) -> str:
    identity = "\x1f".join(source_ids) + "\x1e" + title
    return "i-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]


def first_fraction_source_ids(
    source_order: Sequence[str],
    sources: Dict[str, Dict[str, Any]],
    percent: float,
) -> Set[str]:
    """Select sources starting in the first N% of attributable transcript tokens."""
    bounded = max(0.0, min(100.0, percent))
    if bounded <= 0 or not source_order:
        return set()
    weights = [max(1, int((sources.get(source_id) or {}).get("tokens_estimate") or 1)) for source_id in source_order]
    total = sum(weights)
    cutoff = total * bounded / 100.0
    selected: Set[str] = set()
    consumed = 0
    for source_id, weight in zip(source_order, weights):
        if consumed >= cutoff:
            break
        selected.add(source_id)
        consumed += weight
    return selected


def rule_floor(
    source_ids: Sequence[str],
    source_order: Sequence[str],
    sources: Dict[str, Dict[str, Any]],
    rules: Sequence[Dict[str, Any]],
) -> Optional[str]:
    floors: List[str] = []
    for rule in rules:
        if not rule.get("enabled"):
            continue
        rule_id = rule.get("id")
        applies = False
        if rule_id == "first_n":
            first_ids = first_fraction_source_ids(
                source_order,
                sources,
                float(rule.get("percent", 30)),
            )
            applies = any(source_id in first_ids for source_id in source_ids)
        else:
            applies = any((sources.get(source_id) or {}).get("class") == rule_id for source_id in source_ids)
        if applies and rule.get("retention") in RETENTIONS:
            floors.append(str(rule["retention"]))
    if not floors:
        return None
    return max(floors, key=RETENTION_WEIGHT.__getitem__)


def _apply_floor(retention: str, floor: Optional[str]) -> str:
    if not floor:
        return retention
    return max((retention, floor), key=RETENTION_WEIGHT.__getitem__)


def _fallback_items(trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for episode in trace.get("episodes", []):
        source_ids = [str(source["id"]) for source in episode.get("sources", []) if source.get("id")]
        if not source_ids:
            continue
        title = _clip(episode.get("title"), 120, "Conversation evidence")
        prompt = next(
            (
                _clip(source.get("text"), 600)
                for source in episode.get("sources", [])
                if source.get("kind") in {"user_prompt", "compact_summary"}
            ),
            title,
        )
        conclusion = next(
            (
                _clip(source.get("text"), 700)
                for source in reversed(episode.get("sources", []))
                if source.get("kind") == "assistant_text"
            ),
            "",
        )
        artifacts = sorted(
            {
                value
                for source in episode.get("sources", [])
                for key in ("paths", "commits")
                for value in source.get("artifacts", {}).get(key, [])
            }
        )
        summary_parts = [prompt]
        if conclusion and conclusion != prompt:
            summary_parts.append("Latest conclusion: " + conclusion)
        if artifacts:
            summary_parts.append("Artifacts: " + ", ".join(artifacts[:12]))
        carried = episode.get("carry_forward") if isinstance(episode.get("carry_forward"), dict) else {}
        carried_retention = str(carried.get("retention") or "preserve")
        if carried_retention not in RETENTIONS:
            carried_retention = "preserve"
        items.append(
            {
                "id": _item_id(source_ids, title),
                "title": title,
                "summary": str(carried.get("summary") or "\n\n".join(summary_parts)),
                "type": str(carried.get("type") or "context"),
                "status": str(carried.get("status") or "unclear"),
                "retention": carried_retention,
                "model_retention": carried_retention,
                "confidence": str(carried.get("confidence") or "low"),
                "needs_review": False,
                "source_ids": source_ids,
                "rationale": "Conservative fallback: the proposal worker was unavailable or invalid.",
                "next_step": str(carried.get("next_step") or ""),
                "rival_interpretations": [],
                "rule_floor": "preserve",
                "user_touched": False,
            }
        )
    return items


def _analysis_metrics(trace: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "analysis_source_hash": trace.get("source_hash"),
        "analysis_source_count": sum(
            len(episode.get("sources", [])) for episode in trace.get("episodes", [])
        ),
        "analysis_visible_tokens": int(
            (trace.get("context") or {}).get("visible_tokens_estimate") or 0
        ),
        "analysis_used_tokens": (
            (trace.get("context") or {}).get("used_tokens_observed")
        ),
    }


def rebase_proposal(prior: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
    """Carry grounded analysis forward and conservatively add sources that arrived later."""
    rebased = normalize_proposal(prior, trace)
    rebased["generator"] = "hybrid"
    rebased["worker"] = prior.get("worker")
    rebased["analysis_source_hash"] = prior.get("analysis_source_hash") or prior.get("source_hash")
    rebased["analysis_source_count"] = prior.get("analysis_source_count")
    rebased["analysis_visible_tokens"] = prior.get("analysis_visible_tokens")
    rebased["analysis_used_tokens"] = prior.get("analysis_used_tokens")
    added = sum(
        1
        for item in rebased.get("items", [])
        if str(item.get("title") or "").startswith("Unclassified:")
    )
    rebased.setdefault("warnings", []).insert(
        0,
        f"Background analysis was rebased onto the current transcript; {added} recent episode group(s) remain conservatively unclassified.",
    )
    return rebased


def normalize_proposal(
    raw: Any,
    trace: Dict[str, Any],
    *,
    worker_error: Optional[str] = None,
) -> Dict[str, Any]:
    ordered, sources, _episode_for_source = _source_index(trace)
    rules = [dict(rule) for rule in DEFAULT_CLASS_RULES]
    warnings: List[str] = []
    if worker_error:
        warnings.append(worker_error)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return {
            "schema_version": SCHEMA_VERSION,
            "source_hash": trace.get("source_hash"),
            "created_at": utc_now(),
            "generator": "fallback",
            "representations": [],
            "items": _fallback_items(trace),
            "class_rules": rules,
            "warnings": warnings or ["Proposal worker returned no usable structure."],
        }

    claimed: Set[str] = set()
    items: List[Dict[str, Any]] = []
    invalid_refs: Set[str] = set()
    for candidate in raw.get("items", []):
        if not isinstance(candidate, dict):
            continue
        candidate_ids = candidate.get("source_ids")
        if not isinstance(candidate_ids, list):
            continue
        valid_ids: List[str] = []
        for value in candidate_ids:
            source_id = str(value)
            if source_id not in sources:
                invalid_refs.add(source_id)
            elif source_id not in claimed:
                claimed.add(source_id)
                valid_ids.append(source_id)
        if not valid_ids:
            continue
        title = _clip(candidate.get("title"), 120, "Conversation evidence")
        retention = str(candidate.get("retention") or "preserve")
        if retention not in RETENTIONS:
            retention = "preserve"
        model_retention = retention
        floor = rule_floor(valid_ids, ordered, sources, rules)
        retention = _apply_floor(model_retention, floor)
        confidence = str(candidate.get("confidence") or "low")
        if confidence not in CONFIDENCES:
            confidence = "low"
        rivals = candidate.get("rival_interpretations")
        if not isinstance(rivals, list):
            rivals = []
        rivals = [_clip(value, 240) for value in rivals if _clip(value, 240)][:3]
        item_type = str(candidate.get("type") or "context")
        status = str(candidate.get("status") or "unclear")
        needs_review = bool(candidate.get("needs_review")) or confidence == "low"
        items.append(
            {
                "id": _item_id(valid_ids, title),
                "title": title,
                "summary": _clip(candidate.get("summary"), 1200, title),
                "type": item_type if item_type in ITEM_TYPES else "context",
                "status": status if status in STATUSES else "unclear",
                "retention": retention,
                "model_retention": model_retention,
                "confidence": confidence,
                "needs_review": needs_review,
                "source_ids": valid_ids,
                "rationale": _clip(candidate.get("rationale"), 600),
                "next_step": _clip(candidate.get("next_step"), 400),
                "rival_interpretations": rivals,
                "rule_floor": floor,
                "user_touched": False,
            }
        )

    missing = [source_id for source_id in ordered if source_id not in claimed]
    if missing:
        by_episode: Dict[str, List[str]] = {}
        episode_order: List[str] = []
        for episode in trace.get("episodes", []):
            values = [source["id"] for source in episode.get("sources", []) if source.get("id") in missing]
            if values:
                episode_id = str(episode.get("id"))
                episode_order.append(episode_id)
                by_episode[episode_id] = values
        titles = {str(episode.get("id")): str(episode.get("title") or "") for episode in trace.get("episodes", [])}
        for episode_id in episode_order:
            source_ids = by_episode[episode_id]
            title = _clip(titles.get(episode_id), 100, "Unclassified evidence")
            items.append(
                {
                    "id": _item_id(source_ids, "Unclassified: " + title),
                    "title": "Unclassified: " + title,
                    "summary": "The proposal worker did not assign this transcript evidence.",
                    "type": "context",
                    "status": "unclear",
                    "retention": "preserve",
                    "model_retention": "preserve",
                    "confidence": "low",
                    "needs_review": True,
                    "source_ids": source_ids,
                    "rationale": "Coverage repair added by the deterministic validator.",
                    "next_step": "Classify or explicitly preserve this evidence.",
                    "rival_interpretations": [],
                    "rule_floor": "preserve",
                    "user_touched": False,
                }
            )
        warnings.append(f"Validator conservatively recovered {len(missing)} unassigned source(s).")
    if invalid_refs:
        warnings.append(f"Validator rejected {len(invalid_refs)} invented or stale source reference(s).")

    representations: List[Dict[str, Any]] = []
    raw_representations = raw.get("representations")
    if isinstance(raw_representations, list):
        for index, representation in enumerate(raw_representations[:3]):
            if not isinstance(representation, dict):
                continue
            chunks: List[Dict[str, Any]] = []
            for chunk in representation.get("chunks", []):
                if not isinstance(chunk, dict) or not isinstance(chunk.get("source_ids"), list):
                    continue
                refs = list(dict.fromkeys(str(value) for value in chunk["source_ids"] if str(value) in sources))
                if refs:
                    chunks.append({"label": _clip(chunk.get("label"), 120, "Evidence"), "source_ids": refs})
            representations.append(
                {
                    "id": _clip(representation.get("id"), 40, f"r{index + 1}"),
                    "title": _clip(representation.get("title"), 80, f"Representation {index + 1}"),
                    "thesis": _clip(representation.get("thesis"), 500),
                    "chunks": chunks,
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "source_hash": trace.get("source_hash"),
        "created_at": utc_now(),
        "generator": f"{trace.get('platform') or 'claude'}-model",
        "representations": representations,
        "items": items,
        "class_rules": rules,
        "warnings": warnings,
    }


def build_prompt(trace: Dict[str, Any], guidelines: str = "", lens: str = "") -> str:
    policy = ""
    if guidelines.strip():
        policy += "\nPROJECT RETENTION POLICY:\n" + guidelines.strip()[:12000]
    if lens.strip():
        policy += "\nPROJECT INTERPRETIVE LENS:\n" + lens.strip()[:12000]
    host = "Codex" if trace.get("platform") == "codex" else "Claude Code"
    return f"""You are proposing a human-reviewed compaction ledger for one {host} session.

Your output is a proposal, never a decision. Partition EVERY SOURCE id exactly once across items. Copy source ids exactly; invent none. Preserve the user's language where it carries distinctions. Include assistant conclusions, tool outcomes, tests, diffs, artifacts, failed approaches, and constraints—not merely user prompts.

First construe the same evidence through 2-3 genuinely rival problem representations. Then produce one practical item partition. Mark needs_review when rival representations disagree about meaning, status, or retention. Keep these axes separate:
- type: what kind of knowledge it is
- status: whether it is active, resolved, or unclear
- retention: preserve verbatim-level detail, summarize to an outcome, or demote to recoverable storage
- confidence: confidence in your construal

Default retention policy: preserve ongoing work, decisions, constraints, active files, test evidence, and unresolved contradictions; summarize completed/resolved/mechanical work and subagent detail; demote only redundant or superseded evidence. Low confidence never licenses demotion.
{policy}

TRANSCRIPT EVIDENCE (stable ids, reconstructed estimates):
{evidence_text(trace)}
"""


def parse_worker_output(stdout: str) -> Any:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProposalError(f"worker output was not JSON: {exc}") from exc
    if isinstance(envelope, dict):
        for key in ("structured_output", "structuredOutput"):
            if isinstance(envelope.get(key), dict):
                return envelope[key]
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
        if "items" in envelope:
            return envelope
    raise ProposalError("worker JSON contained no structured proposal")


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _terminate_worker(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows CI does not launch a real worker
            process.terminate()
        process.communicate(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover
                process.kill()
        except OSError:
            pass
        process.communicate()


def _run_cancellable(
    command: Sequence[str],
    prompt: str,
    timeout: int,
    cancelled: Callable[[], bool],
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    deadline = time.monotonic() + timeout
    pending_input: Optional[str] = prompt
    while True:
        if cancelled():
            _terminate_worker(process)
            raise ProposalError("proposal worker was superseded by foreground review")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_worker(process)
            raise ProposalError(f"proposal worker timed out after {timeout}s")
        try:
            stdout, stderr = process.communicate(
                input=pending_input,
                timeout=min(0.25, remaining),
            )
            return subprocess.CompletedProcess(
                list(command),
                process.returncode,
                stdout,
                stderr,
            )
        except subprocess.TimeoutExpired:
            pending_input = None


def run_worker(
    trace: Dict[str, Any],
    *,
    guidelines: str = "",
    lens: str = "",
    runner: Runner = subprocess.run,
    timeout: int = 180,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    host = str(trace.get("platform") or "claude")
    effort = os.environ.get("COMPACT_FOCUS_EFFORT", "low")
    temporary: Optional[tempfile.TemporaryDirectory[str]] = None
    output_path: Optional[Path] = None
    if host == "codex":
        executable = shutil.which("codex")
        if not executable:
            raise ProposalError("Codex CLI is unavailable")
        model = os.environ.get(
            "COMPACT_FOCUS_CODEX_MODEL",
            os.environ.get("COMPACT_FOCUS_MODEL", "gpt-5.6-luna"),
        )
        temporary = tempfile.TemporaryDirectory(prefix="compact-focus-worker-")
        temporary_path = Path(temporary.name)
        schema_path = temporary_path / "proposal.schema.json"
        output_path = temporary_path / "proposal.json"
        schema_path.write_text(
            json.dumps(PROPOSAL_SCHEMA, separators=(",", ":")),
            encoding="utf-8",
        )
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-C",
            str(temporary_path),
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
            'developer_instructions="Return only the requested JSON. Do not call tools or use external context."',
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
            raise ProposalError("Claude CLI is unavailable")
        model = os.environ.get("COMPACT_FOCUS_MODEL", "haiku")
        budget = os.environ.get("COMPACT_FOCUS_MAX_BUDGET_USD", "0.10")
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
            "You are a bounded JSON transformation worker. Follow the user prompt and its JSON schema exactly. Do not use tools, skills, or external context.",
            "--no-session-persistence",
            "--max-budget-usd",
            budget,
            "--json-schema",
            json.dumps(PROPOSAL_SCHEMA, separators=(",", ":")),
            "--output-format",
            "json",
        ]
    started = time.monotonic()
    try:
        prompt = build_prompt(trace, guidelines, lens)
        if runner is subprocess.run and cancelled is not None:
            result = _run_cancellable(command, prompt, timeout, cancelled)
        else:
            result = runner(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if temporary is not None:
            temporary.cleanup()
        raise ProposalError(f"proposal worker failed: {exc}") from exc
    worker_output = result.stdout
    try:
        if result.returncode != 0:
            detail = _clip(result.stderr or result.stdout, 500, f"exit {result.returncode}")
            raise ProposalError(f"proposal worker exited {result.returncode}: {detail}")
        if output_path is not None and output_path.is_file():
            worker_output = output_path.read_text(encoding="utf-8")
        parsed = parse_worker_output(worker_output)
    finally:
        if temporary is not None:
            temporary.cleanup()
    normalized = normalize_proposal(parsed, trace)
    if not normalized["items"] and trace.get("episodes"):
        raise ProposalError("proposal worker produced no grounded items")
    elapsed_ms = round((time.monotonic() - started) * 1000)
    try:
        envelope = json.loads(worker_output)
    except json.JSONDecodeError:
        envelope = {}
    metadata: Dict[str, Any] = {
        "status": "success",
        "host": host,
        "model": model,
        "effort": effort,
        "elapsed_ms": elapsed_ms,
    }
    if isinstance(envelope, dict):
        for key in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd"):
            if isinstance(envelope.get(key), (int, float)):
                metadata[key] = envelope[key]
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            metadata["usage"] = {
                key: value
                for key, value in usage.items()
                if isinstance(value, (int, float, bool))
            }
    normalized["worker"] = metadata
    normalized.update(_analysis_metrics(trace))
    return normalized


def prepare_proposal(
    trace: Dict[str, Any],
    *,
    guidelines: str = "",
    lens: str = "",
    runner: Runner = subprocess.run,
    timeout: int = 180,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        return run_worker(
            trace,
            guidelines=guidelines,
            lens=lens,
            runner=runner,
            timeout=timeout,
            cancelled=cancelled,
        )
    except ProposalError as exc:
        fallback = normalize_proposal(None, trace, worker_error=str(exc))
        fallback["worker"] = {
            "status": "fallback",
            "model": os.environ.get("COMPACT_FOCUS_MODEL", "haiku"),
            "effort": os.environ.get("COMPACT_FOCUS_EFFORT", "low"),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": _clip(exc, 500),
        }
        fallback.update(_analysis_metrics(trace))
        return fallback


def load_policy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
