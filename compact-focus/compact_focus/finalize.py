from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import SCHEMA_VERSION
from .review import review_delta, review_errors, review_hash
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


def _artifacts(item: Dict[str, Any], sources: Dict[str, Dict[str, Any]]) -> List[str]:
    values = set()
    for source_id in item.get("source_ids", []):
        source = sources.get(source_id) or {}
        for key in ("paths", "commits"):
            values.update(source.get("artifacts", {}).get(key, []))
    return sorted(values)


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
        if item.get("retention") != "demote":
            continue
        for source_id in item.get("source_ids", []):
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
    lines = [
        "HUMAN-REVIEWED COMPACTION CONTRACT",
        "Treat the following interpretation and retention decisions as binding. Preserve distinctions; do not silently reinterpret item status.",
    ]
    precommit = str(review.get("precommit") or "").strip()
    if precommit:
        lines.extend(["", "DO NOT MISINTERPRET", precommit])
    for retention, heading in (
        ("preserve", "PRESERVE FAITHFULLY"),
        ("summarize", "SUMMARIZE TO OUTCOMES"),
        ("demote", "DEMOTED — DO NOT CARRY FORWARD"),
    ):
        matching = [item for item in review.get("items", []) if item.get("retention") == retention]
        if not matching:
            continue
        lines.extend(["", heading])
        for item in matching:
            label = f"[{item.get('type')}/{item.get('status')}] {item.get('title')}"
            summary = str(item.get("summary") or "").strip()
            if retention == "demote":
                ids = [recovery_by_source[source_id] for source_id in item.get("source_ids", []) if source_id in recovery_by_source]
                suffix = f" (recover: {', '.join(ids)})" if ids else ""
                lines.append(f"- {label}{suffix}")
                continue
            lines.append(f"- {label}: {summary}")
            next_step = str(item.get("next_step") or "").strip()
            if next_step:
                lines.append(f"  Next: {next_step}")
            artifacts = _artifacts(item, sources)
            if artifacts:
                lines.append("  Artifacts: " + ", ".join(artifacts[:20]))
    lines.extend(
        [
            "",
            "RECOVERY",
            f"Demoted evidence remains local and searchable. Use `cf recall <id>` or `cf search <terms>`; session={paths.session_id}, cycle={cycle_id}.",
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
    errors = review_errors(trace, review)
    if errors:
        raise FinalizeError("; ".join(errors))
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
