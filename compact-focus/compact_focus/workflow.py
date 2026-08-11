from __future__ import annotations

import curses
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import SCHEMA_VERSION, VERSION
from .audit import audit_summary
from .finalize import FinalizeError, finalize_cycle, search
from .host import HOST_CODEX, detect_host
from .proposal import load_policy, normalize_proposal, prepare_proposal, rebase_proposal
from .review import new_review
from .state import (
    StatePaths,
    append_jsonl,
    atomic_write_json,
    cycle_id,
    file_lock,
    load_json,
    safe_component,
    utc_now,
)
from .terminal import TerminalError, terminal_lease
from .trace import build_trace
from .codex_trace import add_review_contract
from .tui import run_review


LOSS_RE = re.compile(
    r"\b(?:the\s+)?compaction\s+(?P<verb>lost|forgot|misread|misinterpreted)\s+(?P<query>.+)",
    re.I | re.S,
)


class WorkflowError(RuntimeError):
    pass


class PreparationSuperseded(RuntimeError):
    pass


def _read_recent_jsonl(path: Path, limit: int = 12) -> list[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    values: list[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _feedback_conditioning(paths: StatePaths) -> Tuple[str, str]:
    inclusion: list[str] = []
    encoding: list[str] = []
    for value in _read_recent_jsonl(paths.project / "feedback.jsonl"):
        query = _one_line(value.get("query"))[:700]
        if not query:
            continue
        if value.get("kind") == "misconstrual":
            encoding.append(f"- Prior compaction misconstrued: {query}")
        else:
            inclusion.append(f"- Prior compaction omitted/forgot: {query}")
    for value in _read_recent_jsonl(paths.project / "reviews.jsonl", limit=8):
        for change in value.get("changed_items", [])[:8]:
            if not isinstance(change, dict):
                continue
            fields = change.get("fields") or {}
            if fields:
                inclusion.append(
                    "- Human corrected proposal item "
                    + str(change.get("item_id") or "unknown")
                    + ": "
                    + _one_line(fields)[:900]
                )
    inclusion_text = ""
    encoding_text = ""
    if inclusion:
        inclusion_text = (
            "\n\nRECENT HUMAN FEEDBACK EXAMPLES (evidence, not universal rules; do not overgeneralize):\n"
            + "\n".join(inclusion[-20:])
        )
    if encoding:
        encoding_text = (
            "\n\nRECENT MISCONSTRUAL REPORTS (preserve the distinction the user says was encoded wrongly):\n"
            + "\n".join(encoding[-12:])
        )
    return inclusion_text, encoding_text


def read_hook_payload() -> Dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise WorkflowError(f"hook input is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError("hook input must be a JSON object")
    return value


def _transcript(payload: Dict[str, Any]) -> Path:
    raw = str(payload.get("transcript_path") or "")
    path = Path(raw).expanduser()
    if not raw or not path.is_file():
        raise WorkflowError(f"transcript is unavailable: {raw or '(missing path)'}")
    return path.resolve()


def _latest_reviewed_cycle(
    paths: StatePaths,
    *,
    require_postcompact: bool,
) -> Tuple[Optional[str], Dict[str, Any]]:
    try:
        candidates = sorted(
            (value for value in paths.cycles.iterdir() if value.is_dir()),
            key=lambda value: value.name,
            reverse=True,
        )
    except OSError:
        return None, {}
    for cycle in candidates:
        review = load_json(cycle / "review.json", {})
        finalization = load_json(cycle / "finalization.json", {})
        if not isinstance(review, dict) or review.get("outcome") != "approved":
            continue
        if not isinstance(finalization, dict) or not finalization.get("finalized_at"):
            continue
        if require_postcompact:
            postcompact_result = load_json(cycle / "postcompact.json", {})
            if not isinstance(postcompact_result, dict) or not postcompact_result.get("recorded_at"):
                continue
        return cycle.name, review
    return None, {}


def _pending_contract_path(paths: StatePaths) -> Path:
    return paths.session / "pending-contract.json"


def _clear_pending_contract(paths: StatePaths) -> bool:
    try:
        _pending_contract_path(paths).unlink()
        return True
    except FileNotFoundError:
        return False


def _set_pending_contract(
    paths: StatePaths,
    identifier: str,
    *,
    trigger: str,
    platform: str,
    compact_summaries_before: int,
) -> None:
    atomic_write_json(
        _pending_contract_path(paths),
        {
            "schema_version": SCHEMA_VERSION,
            "cycle_id": identifier,
            "approved_at": utc_now(),
            "trigger": trigger,
            "platform": platform,
            "compact_summaries_before": compact_summaries_before,
        },
    )


def _pending_contract_cycle(paths: StatePaths) -> Optional[str]:
    pending = load_json(_pending_contract_path(paths), {})
    if not isinstance(pending, dict):
        return None
    raw = str(pending.get("cycle_id") or "")
    identifier = safe_component(raw, "")
    if not identifier or identifier != raw:
        return None
    cycle = paths.cycle(identifier)
    review = load_json(cycle / "review.json", {})
    finalization = load_json(cycle / "finalization.json", {})
    if not isinstance(review, dict) or review.get("outcome") != "approved":
        return None
    if not isinstance(finalization, dict) or not finalization.get("finalized_at"):
        return None
    return identifier


def _update_pending_contract(
    paths: StatePaths,
    identifier: str,
    **fields: Any,
) -> bool:
    with file_lock(paths.session / ".pending-contract.lock") as acquired:
        if not acquired:  # blocking locks always acquire; defensive only
            return False
        pending = load_json(_pending_contract_path(paths), {})
        if not isinstance(pending, dict) or pending.get("cycle_id") != identifier:
            return False
        pending.update(fields)
        atomic_write_json(_pending_contract_path(paths), pending)
    return True


def _pending_prompt_contract(paths: StatePaths) -> Tuple[Optional[str], str, int]:
    identifier = _pending_contract_cycle(paths)
    if not identifier:
        return None, "", 0
    pending = load_json(_pending_contract_path(paths), {})
    if not isinstance(pending, dict):
        return None, "", 0
    if not (pending.get("continuation_started_at") or pending.get("postcompact_at")):
        return None, "", 0
    try:
        count = max(
            0,
            int(
                pending.get("prompt_reinforcement_count")
                or (1 if pending.get("prompt_reinforced_at") else 0)
            ),
        )
        limit = max(0, int(os.environ.get("COMPACT_FOCUS_PROMPT_REINFORCEMENTS", "3")))
    except (TypeError, ValueError):
        count, limit = 0, 3
    if count >= limit:
        return None, "", count
    review = load_json(paths.cycle(identifier) / "review.json", {})
    precommit = " ".join(str(review.get("precommit") or "").split())
    if precommit:
        reinforcement = (
            "USER-APPROVED COMPACTION PRECOMMIT — CURRENT GROUND TRUTH\n"
            + precommit
            + "\nThe line above was entered directly by the user in the most recent "
            "compaction editor. If this prompt asks what the user entered or what "
            "must not be misconstrued, use that exact line. Ignore older assistant "
            "claims that the value is missing or unclear."
        )
        return identifier, reinforcement, count
    try:
        directive = (paths.cycle(identifier) / "instructions.txt").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return None, "", count
    return (identifier, directive[:9000], count) if directive else (None, "", count)


def _managed_trace(paths: StatePaths, payload: Dict[str, Any]) -> Dict[str, Any]:
    trace = build_trace(_transcript(payload), payload.get("status"))
    if trace.get("platform") == HOST_CODEX:
        prior_cycle, prior_review = _latest_reviewed_cycle(paths, require_postcompact=True)
        if prior_cycle:
            add_review_contract(trace, prior_review, prior_cycle)
    return trace


def _cycle_ready(cycle: Path, trace: Dict[str, Any], allow_fallback: bool) -> bool:
    saved_trace = load_json(cycle / "trace.json", {})
    proposal = load_json(cycle / "proposal.initial.json", {})
    return bool(
        isinstance(saved_trace, dict)
        and isinstance(proposal, dict)
        and saved_trace.get("source_hash") == trace.get("source_hash")
        and proposal.get("source_hash") == trace.get("source_hash")
        and proposal.get("schema_version") == SCHEMA_VERSION
        and (allow_fallback or proposal.get("generator") != "fallback")
    )


def ensure_cycle(
    paths: StatePaths,
    payload: Dict[str, Any],
    *,
    allow_fallback_reuse: bool = False,
    generate_worker: bool = True,
    publish_guard: Optional[Callable[[], bool]] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any], bool]:
    trace = _managed_trace(paths, payload)
    latest = paths.latest_cycle_id()
    if latest and _cycle_ready(paths.cycle(latest), trace, allow_fallback_reuse):
        proposal = load_json(paths.cycle(latest) / "proposal.initial.json", {})
        return latest, trace, proposal, True

    prior_proposal: Dict[str, Any] = {}
    if latest:
        loaded = load_json(paths.cycle(latest) / "proposal.initial.json", {})
        if isinstance(loaded, dict):
            prior_proposal = loaded

    identifier = cycle_id(str(trace.get("source_hash") or "trace"))
    cycle = paths.cycle(identifier)
    cycle.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cycle / "trace.json", trace)
    atomic_write_json(
        cycle / "cycle.json",
        {
            "schema_version": SCHEMA_VERSION,
            "plugin_version": VERSION,
            "cycle_id": identifier,
            "created_at": utc_now(),
            "session_id": paths.session_id,
            "project_id": paths.project_id,
            "source_hash": trace.get("source_hash"),
            "snapshot_bytes": trace.get("snapshot_bytes"),
        },
    )
    if generate_worker:
        feedback_guidelines, feedback_lens = _feedback_conditioning(paths)
        proposal = prepare_proposal(
            trace,
            guidelines=(
                load_policy(paths.project / "policy" / "guidelines.md")
                + feedback_guidelines
            ),
            lens=load_policy(paths.project / "policy" / "lens.md") + feedback_lens,
            timeout=int(os.environ.get("COMPACT_FOCUS_WORKER_TIMEOUT", "180")),
            cancelled=(lambda: not publish_guard()) if publish_guard else None,
        )
    elif prior_proposal and prior_proposal.get("generator") not in {"fallback", "deterministic"}:
        proposal = rebase_proposal(prior_proposal, trace)
    else:
        proposal = normalize_proposal(
            None,
            trace,
            worker_error="Background analysis is not ready; the instant conservative episode view is shown.",
        )
        proposal["generator"] = "deterministic"
    if publish_guard and not publish_guard():
        atomic_write_json(
            cycle / "superseded.json",
            {
                "schema_version": SCHEMA_VERSION,
                "superseded_at": utc_now(),
                "reason": "foreground review preempted background analysis",
                "worker": proposal.get("worker"),
            },
        )
        paths.record("background_prepare_superseded", cycle_id=identifier)
        raise PreparationSuperseded("foreground review preempted background analysis")
    atomic_write_json(cycle / "proposal.initial.json", proposal)
    paths.set_latest_cycle(identifier)
    paths.record(
        "proposal_prepared",
        cycle_id=identifier,
        source_hash=trace.get("source_hash"),
        generator=proposal.get("generator"),
        item_count=len(proposal.get("items", [])),
        warning_count=len(proposal.get("warnings", [])),
        worker=proposal.get("worker"),
    )
    return identifier, trace, proposal, False


def worker_refresh_needed(paths: StatePaths, trace: Dict[str, Any]) -> bool:
    latest = paths.latest_cycle_id()
    if not latest:
        return True
    cycle = paths.cycle(latest)
    proposal = load_json(cycle / "proposal.initial.json", {})
    saved_trace = load_json(cycle / "trace.json", {})
    if not isinstance(proposal, dict) or not isinstance(saved_trace, dict):
        return True
    if proposal.get("generator") in {"fallback", "deterministic"}:
        return True
    if proposal.get("source_hash") == trace.get("source_hash"):
        return False
    prior_context = saved_trace.get("context") or {}
    current_context = trace.get("context") or {}
    visible_delta = max(
        0,
        int(current_context.get("visible_tokens_estimate") or 0)
        - int(prior_context.get("visible_tokens_estimate") or 0),
    )
    used_delta = max(
        0,
        int(current_context.get("used_tokens_observed") or 0)
        - int(prior_context.get("used_tokens_observed") or 0),
    )
    prior_episodes = len(saved_trace.get("episodes", []))
    current_episodes = len(trace.get("episodes", []))
    episode_delta = max(0, current_episodes - prior_episodes)
    token_threshold = int(os.environ.get("COMPACT_FOCUS_PREP_REFRESH_TOKENS", "40000"))
    episode_threshold = int(os.environ.get("COMPACT_FOCUS_PREP_REFRESH_EPISODES", "12"))
    return max(visible_delta, used_delta) >= token_threshold or episode_delta >= episode_threshold


def should_prepare(trace: Dict[str, Any]) -> bool:
    if "COMPACT_FOCUS_BACKGROUND" in os.environ:
        background = os.environ["COMPACT_FOCUS_BACKGROUND"]
    elif trace.get("platform") == HOST_CODEX:
        background = os.environ.get("COMPACT_FOCUS_CODEX_BACKGROUND", "0")
    else:
        background = os.environ.get("CLAUDE_PLUGIN_OPTION_BACKGROUND_ANALYSIS", "1")
    if background.lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    if os.environ.get("COMPACT_FOCUS_PREP_ALWAYS") == "1":
        return True
    threshold = float(os.environ.get("COMPACT_FOCUS_PREP_THRESHOLD_PCT", "50"))
    observed = (trace.get("context") or {}).get("used_pct_observed")
    if isinstance(observed, (int, float)):
        return observed >= threshold
    used = int((trace.get("context") or {}).get("used_tokens_observed") or 0)
    used_minimum = int(os.environ.get("COMPACT_FOCUS_PREP_USED_TOKENS", "80000"))
    if used:
        return used >= used_minimum
    visible = int((trace.get("context") or {}).get("visible_tokens_estimate") or 0)
    minimum = int(os.environ.get("COMPACT_FOCUS_PREP_VISIBLE_TOKENS", "50000"))
    return visible >= minimum


def prepare_in_background(payload: Dict[str, Any]) -> int:
    paths = StatePaths.from_hook(payload)
    paths.ensure()
    with file_lock(paths.lock, blocking=False) as acquired:
        if not acquired:
            return 0
        try:
            worker_started_ns = time.time_ns()
            foreground_marker = paths.session / "foreground-requested.json"

            def may_publish() -> bool:
                try:
                    return foreground_marker.stat().st_mtime_ns < worker_started_ns
                except FileNotFoundError:
                    return True

            limit = max(1, int(os.environ.get("COMPACT_FOCUS_PREP_COALESCE_LIMIT", "4")))
            for generation in range(limit):
                trace = _managed_trace(paths, payload)
                if not should_prepare(trace):
                    return 0
                latest = paths.latest_cycle_id()
                if latest and _cycle_ready(paths.cycle(latest), trace, allow_fallback=False):
                    return 0
                if not worker_refresh_needed(paths, trace):
                    paths.record(
                        "background_prepare_deferred",
                        source_hash=trace.get("source_hash"),
                        reason="existing analysis remains within refresh budget",
                    )
                    return 0
                ensure_cycle(
                    paths,
                    payload,
                    allow_fallback_reuse=False,
                    generate_worker=True,
                    publish_guard=may_publish,
                )
                refreshed = _managed_trace(paths, payload)
                latest = paths.latest_cycle_id()
                if latest and _cycle_ready(paths.cycle(latest), refreshed, allow_fallback=False):
                    return 0
                if not worker_refresh_needed(paths, refreshed):
                    paths.record(
                        "background_prepare_stale_reusable",
                        source_hash=refreshed.get("source_hash"),
                    )
                    return 0
                paths.record(
                    "background_prepare_coalesced",
                    generation=generation + 1,
                    source_hash=refreshed.get("source_hash"),
                )
            paths.record("background_prepare_stale_after_limit", limit=limit)
        except PreparationSuperseded:
            return 0
        except Exception as exc:
            paths.record("background_prepare_failed", error=str(exc)[:1000])
    return 0


def precompact(payload: Dict[str, Any]) -> int:
    paths = StatePaths.from_hook(payload)
    paths.ensure()
    trigger = str(payload.get("trigger") or "unknown")
    custom = str(payload.get("custom_instructions") or "").strip()
    if _clear_pending_contract(paths):
        paths.record("pending_contract_cleared", reason="new_precompact")
    paths.record("precompact", trigger=trigger, focused=bool(custom))
    if custom:
        paths.record("focused_compaction_passthrough", trigger=trigger)
        return 0
    if trigger == "auto" and os.environ.get("COMPACT_FOCUS_AUTO", "review") == "allow":
        paths.record("auto_compaction_passthrough")
        return 0

    with file_lock(paths.lock, blocking=False) as acquired:
        if not acquired:
            atomic_write_json(
                paths.session / "foreground-requested.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "requested_at": utc_now(),
                    "source": "precompact",
                },
            )
            paths.record("foreground_review_preempting_background")
        identifier, trace, proposal, reused = ensure_cycle(
            paths,
            payload,
            allow_fallback_reuse=True,
            generate_worker=False,
        )
        cycle = paths.cycle(identifier)
        review = new_review(proposal)
        atomic_write_json(cycle / "review.draft.json", review)
        paths.record(
            "review_opening",
            cycle_id=identifier,
            trigger=trigger,
            proposal_reused=reused,
            generator=proposal.get("generator"),
        )

        def save_draft(value: Dict[str, Any]) -> None:
            atomic_write_json(cycle / "review.draft.json", value)

        try:
            with terminal_lease():
                approved = run_review(trace, proposal, review, save_draft)
        except (TerminalError, curses.error) as exc:
            paths.record("review_failed", cycle_id=identifier, error=str(exc)[:1000])
            raise WorkflowError(
                f"inline review could not open ({exc}). Run `compact-focus doctor`; compaction was not performed"
            ) from exc
        except Exception as exc:
            paths.record("review_failed", cycle_id=identifier, error=str(exc)[:1000])
            raise WorkflowError(f"inline review failed ({exc}); compaction was not performed") from exc
        if not approved:
            paths.record("review_cancelled", cycle_id=identifier, trigger=trigger)
            raise WorkflowError("compaction cancelled in the review editor")
        try:
            result = finalize_cycle(paths, identifier, trace, proposal, review)
        except FinalizeError as exc:
            paths.record("finalize_failed", cycle_id=identifier, error=str(exc)[:1000])
            raise WorkflowError(f"review could not be finalized: {exc}") from exc
        paths.record(
            "review_approved",
            cycle_id=identifier,
            trigger=trigger,
            action_count=len(review.get("actions", [])),
            demoted_count=result.get("demoted_count"),
            instruction_chars=result.get("instruction_chars"),
        )
        _set_pending_contract(
            paths,
            identifier,
            trigger=trigger,
            platform=str(trace.get("platform") or detect_host(payload)),
            compact_summaries_before=_compact_summary_count(_transcript(payload)),
        )
        if trace.get("platform") == HOST_CODEX:
            sys.stdout.write(json.dumps({"continue": True}, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(str(result["directive"]))
        return 0


def prepare_detached(payload: Dict[str, Any]) -> int:
    """Detach model preparation because Codex does not yet support async hooks."""
    paths = StatePaths.from_hook(payload)
    paths.ensure()
    if os.name != "posix":
        paths.record("background_prepare_skipped", reason="detached hooks require POSIX")
        return 0
    child = os.fork()
    if child:
        paths.record("background_prepare_dispatched", child_pid=child)
        return 0
    try:  # pragma: no cover - exercised by live hook sessions
        os.setsid()
        descriptor = os.open(os.devnull, os.O_RDWR)
        for destination in (0, 1, 2):
            os.dup2(descriptor, destination)
        if descriptor > 2:
            os.close(descriptor)
        code = prepare_in_background(payload)
    except BaseException:
        code = 0
    os._exit(code)


def _compact_summaries(transcript: Path) -> list[str]:
    found: list[str] = []
    try:
        with transcript.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or not row.get("isCompactSummary"):
                    continue
                content = (row.get("message") or {}).get("content", "")
                if isinstance(content, str):
                    found.append(content)
                elif isinstance(content, list):
                    found.append(
                        "\n".join(
                            str(block.get("text", ""))
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        )
                    )
    except OSError:
        pass
    return found


def _compact_summary_count(transcript: Path) -> int:
    return len(_compact_summaries(transcript))


def _latest_new_compact_summary(transcript: Path, summaries_before: int) -> str:
    summaries = _compact_summaries(transcript)
    if len(summaries) <= summaries_before:
        return ""
    return summaries[-1]


def reconcile_transcript_audit(
    paths: StatePaths,
    identifier: str,
    transcript: Path,
    summaries_before: int,
    *,
    wait_seconds: float = 5.0,
) -> bool:
    """Replace a provisional Claude audit once the carried summary is appended."""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    summary = ""
    while True:
        summary = _latest_new_compact_summary(transcript, summaries_before)
        if summary or time.monotonic() >= deadline:
            break
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    if not summary:
        paths.record(
            "transcript_audit_unavailable",
            cycle_id=identifier,
            waited_seconds=wait_seconds,
        )
        return False

    cycle = paths.cycle(identifier)
    with file_lock(cycle / ".postcompact.lock") as acquired:
        if not acquired:  # blocking locks always acquire; defensive only
            return False
        result = load_json(cycle / "postcompact.json", {})
        review = load_json(cycle / "review.json", {})
        if not isinstance(result, dict) or result.get("cycle_id") != identifier:
            return False
        provisional_source = str(result.get("summary_source") or "unavailable")
        result.update(
            {
                "compact_summary": summary,
                "summary_source": "transcript",
                "summary_available": True,
                "audit_final": True,
                "provisional_summary_source": provisional_source,
                "reconciled_at": utc_now(),
                "adherence_audit": audit_summary(review, summary),
            }
        )
        atomic_write_json(cycle / "postcompact.json", result)
    paths.record(
        "transcript_audit_reconciled",
        cycle_id=identifier,
        summary_chars=len(summary),
    )
    return True


def _dispatch_transcript_audit(
    paths: StatePaths,
    identifier: str,
    transcript: Path,
    summaries_before: int,
) -> None:
    if os.name != "posix" or os.environ.get("COMPACT_FOCUS_ASYNC_AUDIT", "1") == "0":
        return
    try:
        child = os.fork()
    except OSError as exc:
        paths.record(
            "transcript_audit_dispatch_failed",
            cycle_id=identifier,
            error=str(exc)[:1000],
        )
        return
    if child:
        paths.record(
            "transcript_audit_dispatched",
            cycle_id=identifier,
            child_pid=child,
        )
        return
    try:  # pragma: no cover - exercised by live Claude hook sessions
        os.setsid()
        descriptor = os.open(os.devnull, os.O_RDWR)
        for destination in (0, 1, 2):
            os.dup2(descriptor, destination)
        if descriptor > 2:
            os.close(descriptor)
        reconcile_transcript_audit(
            paths,
            identifier,
            transcript,
            summaries_before,
        )
    except BaseException:
        pass
    os._exit(0)


def postcompact(payload: Dict[str, Any]) -> int:
    paths = StatePaths.from_hook(payload)
    paths.ensure()
    identifier = _pending_contract_cycle(paths)
    if not identifier:
        paths.record("postcompact_unmanaged", trigger=payload.get("trigger"))
        return 0
    cycle = paths.cycle(identifier)
    finalization = load_json(cycle / "finalization.json", {})
    if not isinstance(finalization, dict) or not finalization.get("finalized_at"):
        paths.record("postcompact_unmanaged", trigger=payload.get("trigger"))
        return 0
    pending = load_json(_pending_contract_path(paths), {})
    try:
        summaries_before = max(0, int(pending.get("compact_summaries_before") or 0))
    except (AttributeError, TypeError, ValueError):
        summaries_before = 0
    transcript_path: Optional[Path] = None
    try:
        transcript_path = _transcript(payload)
        transcript_summary = _latest_new_compact_summary(
            transcript_path, summaries_before
        )
    except WorkflowError:
        transcript_summary = ""
    payload_summary = str(payload.get("compact_summary") or "")
    summary = transcript_summary or payload_summary
    summary_source = "transcript" if transcript_summary else ("hook_payload" if payload_summary else "unavailable")
    host = detect_host(payload)
    if summary:
        adherence = audit_summary(load_json(cycle / "review.json", {}), summary)
    else:
        adherence = {
            "status": "unavailable",
            "reason": (
                "Codex did not expose plaintext remote compaction output to PostCompact."
                if host == HOST_CODEX
                else "No compact summary was present in the hook payload or transcript."
            ),
            "checked_items": 0,
            "checked_precommit": False,
            "possible_omissions": 0,
            "precommit": None,
            "items": [],
        }
    result = {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": identifier,
        "recorded_at": utc_now(),
        "trigger": payload.get("trigger"),
        "compact_summary": summary,
        "summary_source": summary_source,
        "platform": host,
        "summary_available": bool(summary),
        "audit_final": summary_source == "transcript" or host == HOST_CODEX,
        "adherence_audit": adherence,
    }
    atomic_write_json(cycle / "postcompact.json", result)
    _update_pending_contract(paths, identifier, postcompact_at=utc_now())
    paths.record("postcompact", cycle_id=identifier, summary_chars=len(summary))
    review = load_json(cycle / "review.json", {})
    possible = int((result.get("adherence_audit") or {}).get("possible_omissions") or 0)
    audit_note = (
        f" Lexical audit flags {possible} reviewed anchor(s) for human inspection."
        if possible
        else ""
    )
    message = (
        f"compact focus: applied {len(review.get('items', []))} reviewed items; "
        "the approved contract is restored into the immediate continuation. "
        f"{finalization.get('demoted_count', 0)} evidence record(s) remain recoverable."
        + audit_note
    )
    if host != HOST_CODEX:
        message += " If something was lost or misconstrued, say `the compaction lost <what>`."
    sys.stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=False) + "\n")
    if host != HOST_CODEX and summary_source != "transcript" and transcript_path:
        _dispatch_transcript_audit(
            paths,
            identifier,
            transcript_path,
            summaries_before,
        )
    return 0


def session_start(payload: Dict[str, Any]) -> int:
    if str(payload.get("source") or "") != "compact":
        return 0
    paths = StatePaths.from_hook(payload)
    paths.ensure()
    identifier = _pending_contract_cycle(paths)
    if not identifier:
        paths.record("compact_contract_missing")
        return 0
    instruction_path = paths.cycle(identifier) / "instructions.txt"
    try:
        directive = instruction_path.read_text(encoding="utf-8").strip()
    except OSError:
        paths.record("compact_contract_missing", cycle_id=identifier)
        return 0
    if not directive:
        return 0
    _update_pending_contract(paths, identifier, continuation_started_at=utc_now())
    paths.record(
        "compact_contract_injected",
        cycle_id=identifier,
        chars=len(directive),
        platform=detect_host(payload),
    )
    output = {
        "continue": True,
        "systemMessage": "compact focus: restored the approved compaction contract",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": directive,
        },
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    return 0


def prompt_feedback(payload: Dict[str, Any]) -> int:
    prompt = str(payload.get("prompt") or "")
    paths = StatePaths.from_hook(payload)
    paths.ensure()
    reinforcement_cycle, reinforcement, reinforcement_count = _pending_prompt_contract(paths)
    contexts: list[str] = []
    if reinforcement:
        contexts.append(reinforcement)
    match = LOSS_RE.search(prompt)
    if not match:
        if contexts:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n".join(contexts),
                }
            }
            sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            if reinforcement_cycle:
                _update_pending_contract(
                    paths,
                    reinforcement_cycle,
                    prompt_reinforced_at=utc_now(),
                    prompt_reinforcement_count=reinforcement_count + 1,
                )
                paths.record(
                    "compact_contract_prompt_reinforced",
                    cycle_id=reinforcement_cycle,
                    chars=len(reinforcement),
                )
        return 0
    query = _one_line(match.group("query"))[:500]
    verb = match.group("verb").lower()
    kind = "misconstrual" if verb in {"misread", "misinterpreted"} else "omission"
    matches = search(paths.project / "recovery.sqlite3", query, limit=6)
    if not matches:
        identifier = paths.latest_cycle_id()
        if identifier:
            trace = load_json(paths.cycle(identifier) / "trace.json", {})
            matches = _search_trace_sources(trace, query, limit=6)
    feedback = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "session_id": paths.session_id,
        "project_id": paths.project_id,
        "cycle_id": paths.latest_cycle_id(),
        "kind": kind,
        "query": query,
        "match_ids": [value.get("id") for value in matches],
    }
    append_jsonl(paths.project / "feedback.jsonl", feedback)
    paths.record(
        "revealed_loss",
        query=query,
        match_ids=[value.get("id") for value in matches],
        kind=kind,
    )
    if matches:
        excerpts = []
        for value in matches:
            text = _one_line(value.get("text"))[:1200]
            excerpts.append(f"[{value.get('id')}] {value.get('title')}: {text}")
        contexts.append(
            (
                f"Recovered evidence relevant to the user's explicit compaction {kind} report. "
                "Treat it as evidence, not automatically as current truth; correct the omission or construal explicitly.\n"
                + "\n".join(excerpts)
            )[:9000]
        )
    if not contexts:
        return 0
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(contexts),
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    if reinforcement_cycle:
        _update_pending_contract(
            paths,
            reinforcement_cycle,
            prompt_reinforced_at=utc_now(),
            prompt_reinforcement_count=reinforcement_count + 1,
        )
        paths.record(
            "compact_contract_prompt_reinforced",
            cycle_id=reinforcement_cycle,
            chars=len(reinforcement),
        )
    return 0


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _search_trace_sources(
    trace: Dict[str, Any],
    query: str,
    limit: int = 6,
) -> list[Dict[str, Any]]:
    terms = {
        value.lower()
        for value in re.findall(r"[A-Za-z0-9_.-]{3,}", query)
    }
    candidates: list[Tuple[int, int, Dict[str, Any]]] = []
    ordinal = 0
    for episode in trace.get("episodes", []):
        for source in episode.get("sources", []):
            ordinal += 1
            text = str(source.get("text") or "")
            lowered = text.lower()
            score = sum(lowered.count(term) for term in terms)
            if score:
                candidates.append(
                    (
                        score,
                        ordinal,
                        {
                            "id": source.get("id"),
                            "title": episode.get("title"),
                            "kind": source.get("kind"),
                            "text": text,
                        },
                    )
                )
    candidates.sort(key=lambda value: (-value[0], -value[1]))
    return [value for _score, _ordinal, value in candidates[:limit]]
