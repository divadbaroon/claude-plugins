from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import SCHEMA_VERSION
from .review import (
    effective_source_retention,
    ensure_review_shape,
    review_delta,
    review_errors,
    review_hash,
)
from .state import StatePaths, append_jsonl, atomic_write_json, atomic_write_text, load_json, utc_now


class FinalizeError(RuntimeError):
    pass


def _source_map(trace: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        source["id"]: source
        for episode in trace.get("episodes", [])
        for source in episode.get("sources", [])
        if source.get("id")
    }


def recovery_id(session_id: str, cycle_id: str, source_id: str) -> str:
    digest = hashlib.sha1(f"{session_id}:{cycle_id}:{source_id}".encode("utf-8")).hexdigest()[:12]
    return "d-" + digest


def build_recovery_records(
    paths: StatePaths,
    cycle_id: str,
    trace: Dict[str, Any],
    review: Dict[str, Any],
) -> List[Dict[str, Any]]:
    sources = _source_map(trace)
    records: List[Dict[str, Any]] = []
    for item in review.get("items", []):
        for source_id in item.get("source_ids", []):
            if effective_source_retention(review, item, source_id) != "demote":
                continue
            source = sources[source_id]
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": recovery_id(paths.session_id, cycle_id, source_id),
                    "created_at": utc_now(),
                    "session_id": paths.session_id,
                    "project_id": paths.project_id,
                    "cycle_id": cycle_id,
                    "source_id": source_id,
                    "item_id": item.get("id"),
                    "title": item.get("title"),
                    "kind": source.get("kind"),
                    "class": source.get("class"),
                    "text": source.get("text", ""),
                    "byte_range": source.get("byte_range"),
                    "transcript_path": trace.get("transcript_path"),
                    "artifacts": source.get("artifacts", {}),
                }
            )
    return records


def compile_directive(
    paths: StatePaths,
    cycle_id: str,
    trace: Dict[str, Any],
    review: Dict[str, Any],
    recovery_records: List[Dict[str, Any]],
) -> str:
    sources = _source_map(trace)
    recovery_by_source = {record["source_id"]: record["id"] for record in recovery_records}
    approved_summary = str(review.get("approved_summary") or "").strip()
    lines = [
        "HUMAN-REVIEWED COMPACTION CONTRACT",
        "ADOPTION RULE: the user reviewed the draft between the markers below. Reproduce it verbatim as the carried summary core; do not paraphrase, expand, omit, or silently reinterpret its claims, work states, or retention boundaries. If the host requires additional scaffolding, the marked draft remains authoritative and must appear intact.",
        "",
        "BEGIN USER-APPROVED CARRY-FORWARD DRAFT",
        approved_summary,
        "END USER-APPROVED CARRY-FORWARD DRAFT",
        "",
        "STRUCTURED EVIDENCE BOUNDARIES",
    ]
    lines.extend(["", "CLUSTER DECISIONS · machine-checkable index; the approved draft above contains their meaning"])
    for item in review.get("items", []):
        retention = str(item.get("retention") or "preserve")
        recoveries = [
            recovery_by_source[source_id]
            for source_id in item.get("source_ids", [])
            if source_id in recovery_by_source
        ]
        recovery = f" · recover {', '.join(recoveries)}" if recoveries else ""
        lines.append(
            f"- {item.get('id')} · {retention} · {item.get('work_state', 'todo')} · "
            f"{item.get('title')}{recovery}"
        )
    overrides: List[str] = []
    for item in review.get("items", []):
        cluster_retention = str(item.get("retention") or "preserve")
        cluster_work_state = str(item.get("work_state") or "todo")
        for source_id in item.get("source_ids", []):
            source_review = review.get("source_reviews", {}).get(source_id) or {}
            retention = effective_source_retention(review, item, source_id)
            work_state = str(source_review.get("work_state") or cluster_work_state)
            clarifications = [
                str(value).strip()
                for value in source_review.get("clarifications", [])
                if str(value).strip()
            ]
            if retention == cluster_retention and work_state == cluster_work_state and not clarifications:
                continue
            source = sources.get(source_id) or {}
            excerpt = " ".join(str(source.get("text") or "").split())
            if retention == "preserve":
                evidence = excerpt[:1600]
            elif retention == "summarize":
                evidence = excerpt[:500]
            else:
                recovery = recovery_by_source.get(source_id)
                evidence = f"recover: {recovery}" if recovery else "omit from active context"
            overrides.append(
                f"- {source_id}: {retention} / {work_state} — {evidence}"
            )
            overrides.extend(f"  User clarification: {value}" for value in clarifications)
    if overrides:
        lines.extend(["", "SOURCE-LEVEL OVERRIDES", *overrides])
    lines.extend(
        [
            "",
            "RECOVERY",
            f"Demoted evidence remains local and searchable. Use `compact-focus recall <id>` or `compact-focus search <terms>`; session={paths.session_id}, cycle={cycle_id}.",
        ]
    )
    directive = "\n".join(lines).strip() + "\n"
    maximum = int(os.environ.get("COMPACT_FOCUS_DIRECTIVE_MAX_CHARS", "30000"))
    if len(directive) > maximum:
        raise FinalizeError(
            f"approved directive is {len(directive):,} characters; edit summaries below the {maximum:,}-character limit"
        )
    return directive


def _write_recovery_database(database: Path, records: Iterable[Dict[str, Any]]) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recovery (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                item_id TEXT,
                title TEXT,
                kind TEXT,
                class TEXT,
                text TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS recovery_fts USING fts5(id UNINDEXED, title, text)"
            )
            fts = True
        except sqlite3.OperationalError:
            fts = False
        for record in records:
            metadata = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                """
                INSERT OR REPLACE INTO recovery
                (id, created_at, session_id, project_id, cycle_id, source_id, item_id, title, kind, class, text, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["created_at"],
                    record["session_id"],
                    record["project_id"],
                    record["cycle_id"],
                    record["source_id"],
                    record.get("item_id"),
                    record.get("title"),
                    record.get("kind"),
                    record.get("class"),
                    record.get("text", ""),
                    metadata,
                ),
            )
            if fts:
                connection.execute("DELETE FROM recovery_fts WHERE id = ?", (record["id"],))
                connection.execute(
                    "INSERT INTO recovery_fts (id, title, text) VALUES (?, ?, ?)",
                    (record["id"], record.get("title"), record.get("text", "")),
                )
        connection.commit()
    finally:
        connection.close()
    with contextlib.suppress(OSError):
        os.chmod(database, 0o600)


def finalize_cycle(
    paths: StatePaths,
    cycle_id: str,
    trace: Dict[str, Any],
    proposal: Dict[str, Any],
    review: Dict[str, Any],
) -> Dict[str, Any]:
    ensure_review_shape(review)
    errors = review_errors(trace, review)
    if errors:
        raise FinalizeError("; ".join(errors))
    if not str(review.get("approved_summary") or "").strip():
        raise FinalizeError("the compaction draft was not reviewed and confirmed")
    cycle = paths.cycle(cycle_id)
    current_hash = review_hash(review)
    prior = load_json(cycle / "finalization.json", {})
    if (
        isinstance(prior, dict)
        and prior.get("review_hash") == current_hash
        and prior.get("source_hash") == trace.get("source_hash")
        and (cycle / "instructions.txt").exists()
    ):
        directive = (cycle / "instructions.txt").read_text(encoding="utf-8")
        return {**prior, "directive": directive, "reused": True}

    records = build_recovery_records(paths, cycle_id, trace, review)
    directive = compile_directive(paths, cycle_id, trace, review, records)
    delta = review_delta(proposal, review)
    review["completed_at"] = utc_now()
    review["outcome"] = "approved"
    atomic_write_json(cycle / "review.json", review)
    atomic_write_json(cycle / "feedback-delta.json", delta)
    atomic_write_json(cycle / "demoted.json", records)
    atomic_write_text(cycle / "instructions.txt", directive)
    _write_recovery_database(paths.project / "recovery.sqlite3", records)
    for record in records:
        append_jsonl(paths.project / "demoted.jsonl", record)
    append_jsonl(
        paths.project / "reviews.jsonl",
        {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "session_id": paths.session_id,
            "project_id": paths.project_id,
            "cycle_id": cycle_id,
            "source_hash": trace.get("source_hash"),
            "precommit": delta.get("precommit"),
            "changed_items": delta.get("changed_items", []),
            "action_count": delta.get("action_count", 0),
        },
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "source_hash": trace.get("source_hash"),
        "review_hash": current_hash,
        "finalized_at": utc_now(),
        "demoted_count": len(records),
        "instruction_chars": len(directive),
        "reused": False,
    }
    atomic_write_json(cycle / "finalization.json", result)
    return {**result, "directive": directive}


def recall(database: Path, identifier: str) -> Optional[Dict[str, Any]]:
    if not database.exists():
        return None
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT metadata_json FROM recovery WHERE id = ?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        connection.close()


def search(database: Path, terms: str, limit: int = 10) -> List[Dict[str, Any]]:
    if not database.exists():
        return []
    connection = sqlite3.connect(database)
    try:
        try:
            rows = connection.execute(
                """
                SELECT r.metadata_json FROM recovery_fts f
                JOIN recovery r ON r.id = f.id
                WHERE recovery_fts MATCH ? ORDER BY rank LIMIT ?
                """,
                (terms, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pattern = f"%{terms}%"
            rows = connection.execute(
                "SELECT metadata_json FROM recovery WHERE title LIKE ? OR text LIKE ? ORDER BY created_at DESC LIMIT ?",
                (pattern, pattern, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        connection.close()
