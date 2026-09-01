"""Bounded, session-scoped goal inference over Claude chat evidence.

The transcript/event store is authoritative evidence; goals are mutable,
user-supervised derived state. Initial refresh asks for a complete tree, while
later refreshes ask for conservative operations against the existing tree.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import chat_state as CS, goals as GM, providers as P
from ..platform_compat import detached_popen_kwargs, maybe_fchmod, pid_alive


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
Infer completion only from explicit completion evidence.
Prefer 1-4 top-level goals, depth at most 3.

WHAT IS A GOAL AND WHAT IS A TODO. A goal is an outcome someone wanted. A
todo is a step taken toward one. Steps go in that goal's "todos" -- its own
checklist, shown beside it -- and never become goals of their own. Writing
a step as a subgoal is the most common way to get this wrong: a tree of a
dozen goals then grows forty leaves that are really a checklist, and every
goal's list sits empty.

  "Let two people share one goal tree"        -- a goal
  "Add the members table"                     -- a todo of that goal
  "Fix the ambiguous column in hc_add_member" -- a todo of that goal
  "Make goal inference notice new turns"      -- a different goal

A leaf with no children is almost always a todo that was written as a goal.
Before making a subgoal, ask whether it is an outcome or a step; if it is a
step, put it in the parent's todos instead. Give each goal the todos its
evidence shows, done and undone alike.
Never use private assistant thinking. Copy only supplied event ids.

Each goal also carries "relevance": how it stands to the project's stated
objective, given under OBJECTIVE below. Three answers only:
  "core"       -- serves the objective directly
  "supporting" -- does not, but unblocks something that does
  "unrelated"  -- a genuinely different thread of work
Judge the work, not the words: fixing a broken hook is not the objective and
is usually "supporting", because the objective cannot be reached through it.
Say why in one short clause. When no objective is given, every goal is
"core" and why is empty -- there is nothing to be unrelated to.

Each goal also has "sections": the goal's own markdown document, which the
user reads and edits by hand. objective and in_my_words are plain sentences;
decisions, built, blockers and open_questions are lists of short bullet
strings. The workspace's TODO list is not part of the document: never write
todos into a section. Write only what THIS chat's evidence supports and leave
a section empty when it supports nothing. Never invent, pad, or restate the
title.

Return ONLY minified JSON:
{"goals":[{"id":"g1","title":"","status":"active|in_progress|completed|archived","parent_goal_id":null,"description":"","priority":"normal|high|urgent","relevance":"core|supporting|unrelated","relevance_why":"","evidence_ids":[],"todos":[{"text":"","done":false,"evidence_ids":[]}],"sections":{"objective":"","in_my_words":"","decisions":[],"built":[],"blockers":[],"open_questions":[]}}],"important":{"items":[]}}

OBJECTIVE:
<<OBJECTIVE>>

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
{"op":"set_status","goal_id":"","status":"active|in_progress|completed|archived"}
{"op":"new_goal","parent_goal_id":"<id or null>","title":"","description":"","evidence_ids":[],"todos":[],"distinct_because":"","status":"active|in_progress|completed|archived","relevance":"core|supporting|unrelated","relevance_why":""}
{"op":"set_relevance","goal_id":"","relevance":"core|supporting|unrelated","relevance_why":""}
{"op":"append_section","goal_id":"","section":"objective|in_my_words|decisions|built|blockers|open_questions","text":""}

HOW EACH GOAL STANDS TO THE OBJECTIVE. Every goal gets one of three, judged
against the OBJECTIVE below:

  "core"       -- serves the objective directly
  "supporting" -- does not, but unblocks something that does
  "unrelated"  -- a genuinely different thread of work

Judge the work, not the words. Most sessions contain all three, and a tree
where everything is "core" usually means the question was not asked: fixing
a broken tool, chasing a flaky test, or tuning something incidental is
rarely the objective itself.

  objective "Let two people share one goal tree"
    "Add project membership so a teammate can sign in"  -- core
    "Fix the hook so goal inference notices new turns"  -- supporting
    "Diagnose why a queued build's rows failed"         -- unrelated

Set it on every new_goal. Use set_relevance for a goal already in the tree
whose standing the evidence now shows differently -- including one carrying
"core" only because nothing judged it yet. With no objective, everything is
"core".

A new_goal carries the status the evidence shows it in. Work that began
and finished inside this same evidence is created "completed", not
"active": this window is the only time it will be looked at, so a goal born
active here stays active for good. Use "active" only for work still open.

add_todo puts a next action on that goal's own checklist, beside it. It is
not a subgoal and does not appear in the tree: use new_goal only for a
distinct objective, never for a step toward one that already exists.

Rules: infer completion only from explicit evidence. A top-level new_goal needs
an explicitly distinct objective in distinct_because. Prefer attaching evidence
or creating a todo/subgoal. Do not rename, move, merge, delete, or edit
priority, prompt_ids, important links, or manually authored content. A goal's
notes are one markdown document the user owns; you may only APPEND to it via
append_section, one section at a time, with markdown lines ("- …" bullets for
the list sections). Never repeat a line the section already holds.

CURRENT STATE:
<<TREE>>

OBJECTIVE:
<<OBJECTIVE>>

PROJECT CONTEXT:
<<CONTEXT>>

NEW EVIDENCE:
<<EVENTS>>"""


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


# Three answers, and the order is worth keeping: a goal that unblocks the
# objective is not the same as one that has nothing to do with it, and only
# the last is worth folding away. A binary judgement would hide the plumbing
# that makes the objective reachable at all.
RELEVANCE = ("core", "supporting", "unrelated")


def _inherit_sources(proposed, project_sources):
    """Give every goal the project's own context to stand on.

    A source saved to the project -- the spec, the ticket, the design note --
    is context for everything in it, and re-attaching it goal by goal is
    work nobody should have to repeat. Inherited, never overwritten: a goal
    that names its own sources keeps them, and the project's are added to
    them rather than in place of them.
    """
    if not project_sources:
        return
    for goal in proposed.get("goals") or []:
        own = goal.get("sources")
        own = list(own) if isinstance(own, list) else []
        seen = {str(s.get("label") or "").strip().lower()
                for s in own if isinstance(s, dict)}
        for src in project_sources:
            label = str(src.get("label") or "").strip()
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            own.append({"id": src.get("id") or ("p%d" % (len(own) + 1)),
                        "type": src.get("type") or "doc", "label": label,
                        "from_project": True})
        goal["sources"] = GM.normalize_sources(own)


def _stamp_relevance_for(proposed, before, objective):
    """Record which objective each fresh verdict was made against.

    A verdict outlives the sentence that produced it. Keeping the two
    together is what lets a reader -- or a later pass -- see that a goal was
    ruled unrelated to something the project no longer says.
    """
    text = str(objective or "").strip()[:2000]
    for goal in proposed.get("goals") or []:
        gid = goal.get("id")
        if gid not in before or before.get(gid) != goal.get("relevance"):
            goal["relevance_for"] = text


def project_context_sources(cwd_value: Optional[str], root=None):
    """The sources saved to this project, for its goals to inherit."""
    try:
        from . import project_store as PS
        return PS.load_project(root, cwd_value).get("sources") or []
    except Exception:      # noqa: BLE001 - inference must not fail on this
        return []


def project_objective(cwd_value: Optional[str], root=None) -> str:
    """What this project says it is for, if anything.

    Written by the reader, never inferred -- which is why an empty one means
    "no opinion" rather than "nothing matters". Everything is core then.
    """
    try:
        from . import project_store as PS
        return str(PS.load_project(root, cwd_value).get("objective") or "")
    except Exception:      # noqa: BLE001 - inference must not fail on this
        return ""


def objective_block(objective: str) -> str:
    text = str(objective or "").strip()
    if not text:
        return ("(none given -- every goal is \"core\" and relevance_why is "
                "empty)")
    return text[:2000]


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
        Path.home() / ".claude" / "projects" / CS._project_key(cwd) / "memory"
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
    # The on-device path is stashed for this release. It fails closed on
    # purpose: quietly answering with the claude provider would ship a digest
    # off-device to the one person who explicitly asked it not to.
    if kind == "ollama" and os.environ.get("HC_EXPERIMENTAL") != "1":
        raise P.ProviderError(
            "ollama is experimental in this release; set HC_EXPERIMENTAL=1")
    model = os.environ.get("HC_CHAT_MODEL")
    return P.make(kind, "synthesize", model)


_INITIAL_FIELDS = ("status", "parent_goal_id", "description", "priority",
                   "relevance", "relevance_why")


def _normalize_initial(data: Dict[str, Any], valid_ids: set) -> Dict[str, Any]:
    raw = data.get("goals")
    if not isinstance(raw, list):
        raise ValueError("chat synthesis response is missing goals")
    out = {"version": 1, "goals": []}
    seen = set()
    for index, value in enumerate(raw[:60], 1):
        if not isinstance(value, dict):
            continue
        # Whitelisted, not copied wholesale: a model that returns "notes" or
        # "opening" must not land text in fields the user owns. Everything the
        # schema does allow is normalized below.
        goal = {key: deepcopy(value[key]) for key in _INITIAL_FIELDS
                if key in value}
        gid = str(value.get("id") or f"g{index}")[:80]
        if gid in seen:
            gid = f"g{index}"
        seen.add(gid)
        goal["id"] = gid
        goal["title"] = str(value.get("title") or "Untitled goal")[:120]
        goal["origin"] = "inferred"
        goal["prompt_ids"] = []
        goal["important_item_ids"] = []
        goal["evidence_ids"] = [
            eid for eid in (value.get("evidence_ids") or [])
            if isinstance(eid, str) and eid in valid_ids
        ][:40]
        # A goal's tasks go on its own checklist, beside it -- not into the
        # tree as child goals. The legacy "todos" list is left empty so
        # promote_todos, which turns that field into subgoals, finds
        # nothing to promote.
        goal["todo_items"] = []
        for todo in (value.get("todos") or [])[:30]:
            text = todo.get("text") if isinstance(todo, dict) else todo
            row = GM.add_todo_row(goal, text)
            if row and isinstance(todo, dict) and todo.get("done"):
                row["status"] = "done"
        goal["todos"] = []
        sections = value.get("sections")
        if isinstance(sections, dict):
            goal["sections"] = {
                key: _section_text(sections.get(key)) for key in GM.SECTION_KEYS
            }
        else:
            goal.pop("sections", None)
        out["goals"].append(goal)
    GM.sanitize(out)
    return out


def _section_text(value: Any) -> str:
    """Render one model-supplied section as the markdown it will become."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(
            f"- {' '.join(str(item).split())}"
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        )
    return ""


def _apply_sections(goals: Dict[str, Any]) -> Dict[str, Any]:
    """Fold each goal's inferred sections into its markdown document.

    First run writes the document; every run after that appends under the same
    headers. Inference never gets to replace notes, because the field it would
    overwrite is the one the user writes in.
    """
    for goal in goals.get("goals", []):
        sections = goal.pop("sections", None)
        if not isinstance(sections, dict):
            continue
        notes = str(goal.get("notes") or "")
        if not notes.strip():
            goal["notes"] = GM.join_doc(
                (title, _section_text(sections.get(key)))
                for key, title in GM.SECTION_KEYS.items()
            )
            continue
        document = notes
        for key, title in GM.SECTION_KEYS.items():
            text = _section_text(sections.get(key))
            if not text:
                continue
            candidate = GM.append_to_section(document, title, text)
            # Only a section that really gained text earns a write. Materializing
            # an absent header is not a gain, so a run with nothing new to say
            # leaves the user's document exactly as they left it.
            if (GM.section_body(candidate, title) or "") != \
                    (GM.section_body(document, title) or ""):
                document = candidate
        if document != notes:
            goal["notes"] = document
    return goals


def _filtered_ops(data: Dict[str, Any], valid_ids: set) -> List[Dict[str, Any]]:
    allowed = {"attach_evidence", "add_todo", "complete_todo", "set_status",
               "new_goal", "append_section"}
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
            # Everything on this list is authored or decided in the browser,
            # never by the model — including the two bookkeeping lists that
            # record *which* prompt links were machine-made and which the user
            # tore off. Drop those and the next pass re-links a prompt the
            # person deliberately detached.
            for field in ("prompt_ids", "auto_prompt_ids",
                          "detached_prompt_ids", "important_item_ids",
                          "notes", "priority", "sources"):
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


def _worker_path(session_id: str, root: Optional[Path] = None) -> Path:
    return CS.paths(session_id, root).session_dir / "analyzer.json"


def _read_worker_record(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_worker_record(path: Path, value: Dict[str, Any]) -> None:
    """Publish a worker lease atomically with owner-only permissions."""
    token = str(value.get("token") or secrets.token_hex(16))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{token}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            maybe_fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _record_owned_by(record: Dict[str, Any], token: Optional[str]) -> bool:
    if not token or record.get("token") != token:
        return False
    try:
        return int(record.get("pid")) == os.getpid()
    except (TypeError, ValueError):
        return False


def _worker_record_alive(record: Dict[str, Any], session_id: str) -> bool:
    if not record or record.get("session_id") not in (None, session_id):
        return False
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid == os.getpid() and record.get("mode") == "direct":
        return True
    if record.get("mode", "detached") == "detached":
        return _worker_process_matches(pid, session_id)
    return pid_alive(pid)


def _claim_refresh(
    session_id: str, root: Optional[Path]
) -> Dict[str, Any]:
    """Claim this session's analyzer lease or coalesce into its live owner."""
    supplied_token = os.environ.get("HC_CHAT_WORKER_TOKEN")
    with CS.session_lock(session_id, root, wait_s=5):
        worker = _worker_path(session_id, root)
        record = _read_worker_record(worker)
        if _record_owned_by(record, supplied_token):
            owner_token = str(supplied_token)
        elif (
            not supplied_token
            and record.get("pid") == os.getpid()
            and not record.get("token")
        ):
            # Upgrade a worker record written by a pre-token version.
            owner_token = secrets.token_hex(16)
            record.update(token=owner_token, mode="direct")
            _write_worker_record(worker, record)
        elif _worker_record_alive(record, session_id):
            requested = CS.request_analysis(session_id, root=root)
            return {
                "owned": False,
                "requested_ordinal": int(requested.get("requested_ordinal") or 0),
            }
        else:
            worker.unlink(missing_ok=True)
            analyzer = CS.get_analyzer_state(session_id, root)
            if analyzer.get("status") == "running":
                CS.set_analyzer_state(
                    session_id,
                    status="pending",
                    error="recovered stale analyzer worker",
                    root=root,
                )
            owner_token = supplied_token or secrets.token_hex(16)
            _write_worker_record(worker, {
                "schema_version": 1,
                "pid": os.getpid(),
                "session_id": session_id,
                "token": owner_token,
                "mode": "detached" if supplied_token else "direct",
                "started_at": time.time(),
            })

        manifest = CS.load_manifest(session_id, root)
        target = int(manifest.get("last_ordinal") or 0)
        if target:
            CS.set_analyzer_state(
                session_id,
                status="running",
                requested_ordinal=target,
                error=None,
                root=root,
            )
        return {"owned": True, "token": owner_token, "target": target}


def _settle_refresh(
    session_id: str,
    root: Optional[Path],
    owner_token: str,
    *,
    error: Optional[Exception] = None,
    handoff_if_pending: bool = False,
) -> Dict[str, Any]:
    """Set status and release the exact owned lease in one critical section."""
    with CS.session_lock(session_id, root, wait_s=5):
        state = CS.get_analyzer_state(session_id, root)
        cursor = int(state.get("last_analyzed_ordinal") or 0)
        requested = int(state.get("requested_ordinal") or 0)
        pending = requested > cursor
        if error is not None:
            status = "error"
            error_value: Any = error
        else:
            status = "pending" if pending else "idle"
            error_value = None
        state = CS.set_analyzer_state(
            session_id, status=status, error=error_value, root=root
        )
        worker = _worker_path(session_id, root)
        record = _read_worker_record(worker)
        if _record_owned_by(record, owner_token):
            worker.unlink(missing_ok=True)
        return {
            "state": state,
            "needs_handoff": bool(
                error is None and pending and handoff_if_pending
            ),
        }


def _goal_snapshot(
    session_id: str, root: Optional[Path]
) -> tuple:
    """Read goal content and its CAS revision under one session lock."""
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        revision = CS.goal_revision(session_id, root)
    return goals, important, revision


def refresh(session_id: str, root: Optional[Path] = None, provider=None) -> Dict[str, Any]:
    """Analyze through the latest requested ordinal, coalescing concurrent work."""
    CS.paths(session_id, root)
    try:
        claim = _claim_refresh(session_id, root)
    except TimeoutError:
        try:
            CS.request_analysis(session_id, root=root)
        except TimeoutError:
            pass
        return {"status": "coalesced", "session_id": session_id}
    if not claim["owned"]:
        return {
            "status": "coalesced",
            "session_id": session_id,
            "requested_ordinal": claim["requested_ordinal"],
            "needs_handoff": False,
        }
    owner_token = str(claim["token"])
    if not claim["target"]:
        settled = _settle_refresh(session_id, root, owner_token)
        return {
            "status": "empty",
            "session_id": session_id,
            "analyzed_through": 0,
            "changes": [],
            "needs_handoff": settled["needs_handoff"],
        }

    changes: List[str] = []
    passes = 0
    starting_cursor = int(
        CS.get_analyzer_state(session_id, root).get("last_analyzed_ordinal") or 0
    )
    try:
        while True:
            if passes >= MAX_REFRESH_PASSES:
                state = CS.get_analyzer_state(session_id, root)
                cursor = int(state.get("last_analyzed_ordinal") or 0)
                settled = _settle_refresh(
                    session_id,
                    root,
                    owner_token,
                    handoff_if_pending=cursor > starting_cursor,
                )
                return {"status": "updated" if changes else "coalesced",
                        "session_id": session_id,
                        "analyzed_through": cursor,
                        "changes": changes,
                        "needs_handoff": settled["needs_handoff"]}
            state = CS.get_analyzer_state(session_id, root)
            cursor = int(state.get("last_analyzed_ordinal") or 0)
            requested = int(state.get("requested_ordinal") or 0)
            events = [e for e in CS.new_events_since(session_id, cursor, root)
                      if int(e.get("ordinal") or 0) <= requested]
            if not events:
                # Re-check requested vs. analyzed while releasing the lease.
                # A turn can land after the empty read and before this lock.
                settled = _settle_refresh(
                    session_id, root, owner_token, handoff_if_pending=True
                )
                return {"status": "current" if not changes else "updated",
                        "session_id": session_id, "analyzed_through": cursor,
                        "changes": changes,
                        "needs_handoff": settled["needs_handoff"]}
            digest = _event_digest(events)
            if not digest:
                cursor = max(int(e.get("ordinal") or 0) for e in events)
                CS.set_analyzer_state(session_id, last_analyzed_ordinal=cursor, root=root)
                continue
            goals, important, revision = _goal_snapshot(session_id, root)
            judged_before = {g.get("id"): g.get("relevance")
                             for g in goals.get("goals") or []}
            passes += 1
            cwd_value = CS.load_manifest(session_id, root).get("cwd")
            context = project_context(cwd_value, events)
            objective = project_objective(cwd_value, root)
            objective_text = objective_block(objective)
            project_sources = project_context_sources(cwd_value, root)
            valid_ids = {row["id"] for row in digest}
            if not goals.get("goals"):
                prompt = (INITIAL_PROMPT.replace("<<CONTEXT>>", context)
                          .replace("<<OBJECTIVE>>", objective_text)
                          .replace("<<EVENTS>>", json.dumps(digest, ensure_ascii=False)))
                proposed = _normalize_initial(_provider(provider).generate_json(prompt), valid_ids)
                proposed = _apply_sections(
                    _merge_initial_with_manual(proposed, goals))
                new_important = important
                step_changes = [f"goal + {g['title']}" for g in proposed["goals"]]
            else:
                prompt = (INCREMENTAL_PROMPT
                          .replace("<<TREE>>", json.dumps(goals, ensure_ascii=False))
                          .replace("<<CONTEXT>>", context)
                          .replace("<<OBJECTIVE>>", objective_text)
                          .replace("<<EVENTS>>", json.dumps(digest, ensure_ascii=False)))
                response = _provider(provider).generate_json(prompt)
                ops = _filtered_ops(response, valid_ids)
                proposed, new_important = deepcopy(goals), deepcopy(important)
                step_changes = GM.apply_ops(
                    proposed, new_important, ops, max_new_top_level=1
                )
            # Stamp the objective onto the verdicts this pass actually made.
            # Only those: a goal the model did not revisit was judged against
            # whatever objective stood then, and saying otherwise would make
            # a stale verdict look freshly considered.
            _stamp_relevance_for(proposed, judged_before, objective)
            _inherit_sources(proposed, project_sources)

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
        settled = _settle_refresh(session_id, root, owner_token, error=exc)
        state = settled["state"]
        return {"status": "error", "session_id": session_id,
                "analyzed_through": int(state.get("last_analyzed_ordinal") or 0),
                "changes": changes, "error": str(exc),
                "needs_handoff": False}


def inference_off(session_id: str = "", root: Optional[Path] = None) -> bool:
    """Whether this chat has been told to stop inferring goals.

    Kept as a file beside the chat's own state, not as an environment
    variable: analysis is started from three places -- the server's
    transcript follower, the hooks, and the CLI -- and only the first of
    them inherits the server's environment. A switch the hooks cannot see
    is not a switch. The variable still works, for a one-off run.
    """
    if str(os.environ.get("HC_CHAT_INFER", "")).strip() in ("0", "off", "no",
                                                            "false"):
        return True
    if not session_id:
        return False
    try:
        return _infer_off_path(session_id, root).exists()
    except (OSError, ValueError):
        return False


def _infer_off_path(session_id: str, root: Optional[Path] = None) -> Path:
    return CS.paths(session_id, root).session_dir / "inference_off"


def set_inference(session_id: str, on: bool, root: Optional[Path] = None) -> bool:
    """Turn goal inference for this chat on or off, and say what it now is."""
    path = _infer_off_path(session_id, root)
    if on:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("inference is off for this chat\n", encoding="utf-8")
    return not path.exists()


def spawn_refresh(session_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """Request analysis and start one detached, coalescing worker process."""
    if inference_off(session_id, root):
        return {"status": "off", "session_id": session_id}
    p = CS.paths(session_id, root)
    with CS.session_lock(session_id, root, wait_s=5):
        worker = _worker_path(session_id, root)
        prior = _read_worker_record(worker)
        analyzer = CS.get_analyzer_state(session_id, root)
        if _worker_record_alive(prior, session_id):
            requested = CS.request_analysis(session_id, root=root)
            return {"status": "coalesced", "session_id": session_id,
                    "requested_ordinal": requested.get("requested_ordinal", 0)}
        worker.unlink(missing_ok=True)
        if analyzer.get("status") == "running":
            # The prior worker crashed or its PID was reused. Reset the stale
            # lease so the replacement worker does not immediately coalesce
            # against a process that no longer owns this session.
            CS.set_analyzer_state(session_id, status="pending",
                                  error="recovered stale analyzer worker", root=root)
        requested = CS.request_analysis(session_id, root=root)
        command = [sys.executable, "-m", "human_compact.cli", "chat-refresh",
                   "--session", session_id]
        child_env = os.environ.copy()
        if root is not None:
            child_env["HC_CHAT_STATE_DIR"] = str(Path(root).expanduser().resolve())
        child_env["HC_CHAT_INFERENCE"] = "1"
        owner_token = secrets.token_hex(16)
        child_env["HC_CHAT_WORKER_TOKEN"] = owner_token
        log = p.session_dir / "analyzer.log"
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
        with log.open("ab", buffering=0) as handle:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=handle,
                stderr=subprocess.STDOUT, close_fds=True,
                env=child_env, **detached_popen_kwargs(),
            )
        _DETACHED_PROCESSES.append(process)
        _write_worker_record(worker, {
            "schema_version": 1,
            "pid": process.pid,
            "session_id": session_id,
            "token": owner_token,
            "mode": "detached",
            "started_at": time.time(),
        })
        return {"status": "spawned", "session_id": session_id,
                "pid": process.pid,
                "requested_ordinal": requested.get("requested_ordinal", 0)}


def _worker_process_matches(pid: Any, session_id: str) -> bool:
    """Reject stale/reused PIDs before coalescing a requested refresh."""
    if not pid_alive(pid):
        return False
    if os.name == "nt":
        return True
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return False
    command = result.stdout
    return (
        result.returncode == 0
        and "human_compact.cli chat-refresh" in command
        and f"--session {session_id}" in command
    )


def clear_worker_record(
    session_id: str,
    root: Optional[Path] = None,
    owner_token: Optional[str] = None,
) -> None:
    """Clear only this process's lease, never a successor's reused PID."""
    token = owner_token or os.environ.get("HC_CHAT_WORKER_TOKEN")
    with CS.session_lock(session_id, root, wait_s=5):
        worker = _worker_path(session_id, root)
        value = _read_worker_record(worker)
        legacy_owner = (
            not value.get("token")
            and not token
            and value.get("pid") in (None, os.getpid())
        )
        if _record_owned_by(value, token) or legacy_owner:
            worker.unlink(missing_ok=True)
