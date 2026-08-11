from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from . import VERSION
from .companion import CompanionError, terminal_launch_command
from .finalize import recall as recall_record
from .finalize import search as search_records
from .host import HOST_CODEX, detect_host
from .review import new_review
from .state import StatePaths, atomic_write_json, load_json, project_id, state_root
from .tui import run_review
from .workflow import (
    WorkflowError,
    postcompact,
    precompact,
    prepare_detached,
    prepare_in_background,
    prompt_feedback,
    read_hook_payload,
    session_start,
)


def _paths_for_cwd(session_id: Optional[str] = None) -> StatePaths:
    cwd = os.path.realpath(os.getcwd())
    base = state_root()
    pid = project_id(cwd)
    if not session_id:
        try:
            session_id = (base / "projects" / pid / "recent-session").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            session_id = "unknown"
    return StatePaths.explicit(session_id or "unknown", cwd)


def _hook(event: str) -> int:
    payload: Dict[str, Any] = {}
    try:
        payload = read_hook_payload()
        if event == "precompact":
            return precompact(payload)
        if event == "prepare":
            return prepare_in_background(payload)
        if event == "prepare-dispatch":
            code = prepare_detached(payload)
            if detect_host(payload) == HOST_CODEX:
                print("{}")
            return code
        if event == "postcompact":
            return postcompact(payload)
        if event == "feedback":
            return prompt_feedback(payload)
        if event == "session-start":
            return session_start(payload)
        raise WorkflowError(f"unknown hook event: {event}")
    except WorkflowError as exc:
        if event == "prepare":
            return 0
        if event == "precompact" and detect_host(payload) == HOST_CODEX:
            print(
                json.dumps(
                    {
                        "continue": False,
                        "stopReason": str(exc),
                        "systemMessage": f"compact focus: {exc}",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(f"compact focus: {exc}", file=sys.stderr)
        return 2 if event == "precompact" else 0
    except Exception as exc:
        if event == "prepare":
            return 0
        if event == "precompact" and detect_host(payload) == HOST_CODEX:
            detail = f"unexpected precompact failure: {exc}"
            print(
                json.dumps(
                    {
                        "continue": False,
                        "stopReason": detail,
                        "systemMessage": f"compact focus: {detail}",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(f"compact focus: unexpected {event} failure: {exc}", file=sys.stderr)
        return 2 if event == "precompact" else 0


def _status(args: argparse.Namespace) -> int:
    paths = _paths_for_cwd(args.session)
    latest = paths.latest_cycle_id()
    result: Dict[str, Any] = {
        "version": VERSION,
        "state_root": str(paths.base),
        "project_id": paths.project_id,
        "session_id": paths.session_id,
        "latest_cycle": latest,
        "recent_events": _recent_events(paths.events),
    }
    if latest:
        cycle = paths.cycle(latest)
        result["proposal"] = load_json(cycle / "proposal.initial.json", {})
        result["review"] = load_json(
            cycle / "review.json",
            load_json(cycle / "review.draft.json", {}),
        )
        result["finalization"] = load_json(cycle / "finalization.json", {})
        result["postcompact"] = load_json(cycle / "postcompact.json", {})
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        proposal = result.get("proposal") or {}
        review = result.get("review") or {}
        finalization = result.get("finalization") or {}
        postcompact = result.get("postcompact") or {}
        print(f"compact focus {VERSION}")
        print(f"state: {paths.base}")
        print(f"project: {paths.project_id}")
        print(f"session: {paths.session_id}")
        print(f"cycle: {latest or 'none'}")
        if latest:
            print(
                f"proposal: {proposal.get('generator', 'unknown')} · "
                f"{len(proposal.get('items', []))} items"
            )
            print(
                f"review: {review.get('outcome', 'not started')} · "
                f"{len(review.get('actions', []))} actions"
            )
            print(f"finalized: {'yes' if finalization.get('finalized_at') else 'no'}")
            audit = postcompact.get("adherence_audit") or {}
            if audit:
                source = str(postcompact.get("summary_source") or "unavailable")
                if postcompact.get("audit_final") is False:
                    source += " (provisional)"
                print(
                    f"summary audit: {audit.get('checked_items', 0)} checked · "
                    f"{audit.get('possible_omissions', 0)} possible lexical gaps · "
                    f"{source}"
                )
            events = result.get("recent_events") or []
            if events:
                print("events: " + " → ".join(str(value.get("event")) for value in events[-6:]))
    return 0


def _recent_events(path: Any, limit: int = 20) -> list[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    events: list[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _review(args: argparse.Namespace) -> int:
    paths = _paths_for_cwd(args.session)
    latest = args.cycle or paths.latest_cycle_id()
    result_file = Path(args.result_file).expanduser().resolve() if args.result_file else None

    def finish(approved: bool, error: str = "") -> int:
        if result_file is not None:
            atomic_write_json(
                result_file,
                {
                    "approved": approved,
                    "cycle_id": latest,
                    "error": error,
                },
            )
        if error:
            print(f"compact focus: {error}", file=sys.stderr)
            return 1
        print("review approved" if approved else "review cancelled")
        return 0 if approved else 1

    if not latest:
        return finish(False, "no prepared cycle for this project")
    cycle = paths.cycle(latest)
    trace = load_json(cycle / "trace.json", {})
    proposal = load_json(cycle / "proposal.initial.json", {})
    review = load_json(cycle / "review.draft.json", None) or new_review(proposal)
    if not trace or not proposal:
        return finish(False, "the requested cycle is incomplete")

    def save(value: Dict[str, Any]) -> None:
        atomic_write_json(cycle / "review.draft.json", value)

    try:
        approved = run_review(trace, proposal, review, save)
    except KeyboardInterrupt:
        return finish(False, "review interrupted")
    except Exception as exc:
        return finish(False, f"editor failed: {exc}")
    return finish(approved)


def _recall(args: argparse.Namespace) -> int:
    paths = _paths_for_cwd(args.session)
    record = recall_record(paths.project / "recovery.sqlite3", args.id)
    if not record:
        print(f"compact focus: no recovery record {args.id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
    else:
        print(f"{record['id']} · {record.get('title')} · {record.get('kind')}")
        print(record.get("text", ""))
    return 0


def _search(args: argparse.Namespace) -> int:
    paths = _paths_for_cwd(args.session)
    values = search_records(
        paths.project / "recovery.sqlite3",
        " ".join(args.terms),
        args.limit,
    )
    if args.json:
        print(json.dumps(values, indent=2, ensure_ascii=False))
    else:
        for value in values:
            excerpt = " ".join(str(value.get("text") or "").split())[:240]
            print(f"{value['id']} · {value.get('title')}\n  {excerpt}")
    return 0 if values else 1


def _doctor(_args: argparse.Namespace) -> int:
    checks = []
    checks.append(("python", sys.version_info >= (3, 9), sys.version.split()[0]))
    claude = shutil.which("claude")
    codex = shutil.which("codex")
    discovered = []
    if claude:
        discovered.append(f"Claude={claude}")
    if codex:
        discovered.append(f"Codex={codex}")
    checks.append(
        (
            "host CLI",
            bool(claude or codex),
            " · ".join(discovered) if discovered else "neither Claude nor Codex found",
        )
    )
    root = state_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor"
        probe.touch()
        probe.unlink()
        state_ok = True
        state_detail = str(root)
    except OSError as exc:
        state_ok = False
        state_detail = str(exc)
    checks.append(("state directory", state_ok, state_detail))
    try:
        command, launcher = terminal_launch_command(root / ".doctor-review.command")
        terminal_ok = True
        terminal_detail = f"{launcher} · {command[0]}"
    except CompanionError as exc:
        terminal_ok = False
        terminal_detail = str(exc)
    checks.append(("review terminal", terminal_ok, terminal_detail))
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE f USING fts5(text)")
        connection.close()
        fts_detail = "FTS5"
    except sqlite3.Error as exc:
        fts_detail = f"fallback LIKE search only: {exc}"
    checks.append(("recovery search", True, fts_detail))
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'FAIL':<4} {name:<18} {detail}")
    return 0 if all(ok for _name, ok, _detail in checks) else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="compact-focus",
        description="Human-reviewed compaction for Claude Code and Codex",
    )
    root.add_argument("--version", action="version", version=VERSION)
    root.add_argument(
        "--state-root",
        help="override the persistent state directory (same as COMPACT_FOCUS_STATE_DIR)",
    )
    commands = root.add_subparsers(dest="command", required=True)

    hook = commands.add_parser(
        "hook",
        help="host lifecycle integration (internal)",
    )
    hook.add_argument(
        "event",
        choices=(
            "precompact",
            "prepare",
            "prepare-dispatch",
            "postcompact",
            "feedback",
            "session-start",
        ),
    )

    status = commands.add_parser(
        "status",
        help="show this project's current compaction cycle",
    )
    status.add_argument("--session")
    status.add_argument("--json", action="store_true")

    review = commands.add_parser("review", help="reopen the latest prepared review")
    review.add_argument("--session")
    review.add_argument("--cycle")
    review.add_argument("--result-file", help=argparse.SUPPRESS)

    recall = commands.add_parser(
        "recall",
        help="restore one demoted evidence record",
    )
    recall.add_argument("id")
    recall.add_argument("--session")
    recall.add_argument("--json", action="store_true")

    search = commands.add_parser(
        "search",
        help="search demoted evidence in this project",
    )
    search.add_argument("terms", nargs="+")
    search.add_argument("--session")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")

    commands.add_parser(
        "doctor",
        help="check CLI, terminal, state, and recovery capabilities",
    )
    return root


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    if args.state_root:
        os.environ["COMPACT_FOCUS_STATE_DIR"] = str(args.state_root)
    if args.command == "hook":
        return _hook(args.event)
    if args.command == "status":
        return _status(args)
    if args.command == "review":
        return _review(args)
    if args.command == "recall":
        return _recall(args)
    if args.command == "search":
        return _search(args)
    if args.command == "doctor":
        return _doctor(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
