"""Goal-aware state layer: durable derived goal tree + user-marked important
items. Raw Vault evidence stays immutable; goals.json/important.json are
regenerable derived state; user corrections live separately in
corrections.json and survive regeneration (supervision signal)."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .secure_io import atomic_write_json, atomic_write_text, secure_dir

TTY = sys.stdout.isatty()
def c(code, s): return f"\033[{code}m{s}\033[0m" if TTY else s
def bold(s): return c("1", s)
def dim(s): return c("2", s)
def green(s): return c("32", s)
def cyan(s): return c("36", s)
def star(s): return c("1;33", s)
SEP = "─" * 40
STOP = set("the a an of to for and in on with that this is are was were be it".split())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _toks(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in STOP and len(w) > 2}


# Text this system generated and typed into a session on the user's behalf.
# It is not something the user said, so it never counts as their intent.
MACHINE_PROMPT_PREFIXES = ("Work on my Vault goal ",)


def _machine_authored(text: str) -> bool:
    stripped = str(text or "").lstrip()
    return any(stripped.startswith(p) for p in MACHINE_PROMPT_PREFIXES)


# A goal's `notes` field is one markdown document, not a scratch string. The
# headers below are the sections inference knows how to write under: it appends
# under them, and the human writes wherever they like. A goal opens as an EMPTY
# document -- no spine of empty headings is seeded, and none is re-added; a
# heading appears only when something is written under it (by either party).
# TODOs is deliberately NOT one of them: the workspace rail's list is its own
# store (`todo_items`, persisted in todos.json), and nothing -- inference
# included -- writes todos into the notes or reads them back out. `prompt_md`
# is the reader's own prompt, kept the same way.
DOC_SECTIONS = ("Objective", "In my words", "Decisions", "Built",
                "Blockers", "Open questions")
SECTION_KEYS = {"objective": "Objective",
                "in_my_words": "In my words",
                "decisions": "Decisions", "built": "Built",
                "blockers": "Blockers", "open_questions": "Open questions"}
_H1 = re.compile(r"^# (.*)$")
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")


def default_doc() -> str:
    """The document a goal opens as: empty. Headings arrive with content."""
    return ""


def _scan_doc(notes):
    """Locate each section as a line span, and report an unterminated fence.

    Returns ``(lines, spans, open_fence)`` where each span is
    ``(title, start, end)``. Fence-aware on purpose: a ``# install deps``
    comment at column 0 inside a fenced code block is not a heading, and
    treating it as one would tear the user's code block across two sections
    and leave the fence unterminated -- in state that is persisted and
    injected into later sessions. ``open_fence`` is the same scan's answer to
    "does this document end inside a code block", so callers never have to
    parse it a second time to find out.
    """
    lines = str(notes or "").splitlines()
    fence, heads = None, []
    for index, line in enumerate(lines):
        body = line.lstrip(" ")
        if len(line) - len(body) <= 3:
            marker = _FENCE.match(body)
            if marker:
                char, run = marker.group(1)[0], len(marker.group(1))
                rest = marker.group(2)
                if fence is None:
                    # An opening backtick fence may not carry a backtick in
                    # its info string; anything else opens one.
                    if not (char == "`" and "`" in rest):
                        fence = (char, run)
                elif char == fence[0] and run >= fence[1] and not rest.strip():
                    fence = None
                continue
        if fence is None:
            heading = _H1.match(line)
            if heading:
                heads.append((heading.group(1).strip(), index))
    spans = []
    first = heads[0][1] if heads else len(lines)
    if any(line.strip() for line in lines[:first]):
        spans.append(("", 0, first))
    for position, (title, index) in enumerate(heads):
        following = heads[position + 1][1] if position + 1 < len(heads) else len(lines)
        spans.append((title, index + 1, following))
    return lines, spans, fence is not None


def split_doc(notes):
    """Parse a notes document into ``[(h1 title, body)]`` in document order.

    A list, not a mapping: a person may write the same heading twice, or write
    them in an order of their own, and a dict would silently swallow the second
    one. Only ``# `` at column 0 outside a code fence starts a section -- an
    ``##`` heading is body text belonging to the section above it. Text written
    before the first header keeps the ``""`` title.
    """
    lines, spans, _ = _scan_doc(notes)
    return [(title, "\n".join(lines[start:end]).strip("\n"))
            for title, start, end in spans]


def section_body(notes, section_title):
    """The body of the first section with this title, or None if absent."""
    for title, body in split_doc(notes):
        if title == section_title:
            return body
    return None


def join_doc(sections) -> str:
    """Render ``[(title, body)]`` back to markdown, in the order given.

    Order-preserving and duplicate-preserving, so ``join_doc(split_doc(x))``
    returns *x* -- a document the user arranged their own way survives a round
    trip through this module unchanged.
    """
    blocks = []
    for title, body in sections:
        body = str(body or "").strip("\n")
        if not title:
            if body.strip():
                blocks.append(body)
            continue
        blocks.append(f"# {title}\n{body}" if body.strip() else f"# {title}")
    return "\n\n".join(blocks) + "\n" if blocks else ""


def ensure_doc_sections(notes: str) -> str:
    """The document as it is. Kept for callers; no headings are added.

    Empty headings were the old spine, and the reader asked for a document
    that is theirs alone until something is written: append_to_section adds
    the one heading it needs at the moment it has text for it.
    """
    return str(notes or "")


def strip_empty_spine(notes: str) -> str:
    """Drop DOC_SECTIONS headings that have nothing under them.

    A one-way tidy for documents seeded by the old spine: a ``# Decisions``
    with an empty body is noise, and one the reader wrote text under stays.
    Headings outside DOC_SECTIONS are never touched, however empty.
    """
    document = str(notes or "")
    if not document.strip():
        return ""
    _, spans, open_fence = _scan_doc(document)
    if open_fence:
        return document
    sections = split_doc(document)
    kept = [(title, body) for title, body in sections
            if not (title in DOC_SECTIONS and not body.strip())]
    if len(kept) == len(sections):
        return document
    if not any(body.strip() or title not in DOC_SECTIONS
               for title, body in kept):
        return ""
    return join_doc(kept)


def append_to_section(notes: str, section_title: str, text: str) -> str:
    """Add *text* to the end of one section, leaving every other one alone.

    Append-only by construction: a refresh may extend the record of a goal but
    never rewrites the human's sentences, and a line the section already holds
    is dropped, so re-running inference over the same evidence is a no-op
    rather than a growing pile of duplicates. Edits land as line surgery on the
    document rather than a re-render, so untouched sections stay byte-identical
    down to their own spacing. A document ending inside an unterminated code
    fence is left exactly as it is: there is no position in it that is provably
    outside the user's code block.
    """
    document = ensure_doc_sections(notes)
    block = str(text or "").strip()
    if not block:
        return document
    lines, spans, open_fence = _scan_doc(document)
    if open_fence:
        return document          # never write inside an unterminated fence
    span = next((s for s in spans if s[0] == section_title), None)
    if span is None:
        fresh = [line for line in block.splitlines() if line.strip()]
        lead = document.rstrip("\n") + "\n\n" if document.strip() else ""
        return (lead + f"# {section_title}\n" + "\n".join(fresh) + "\n")
    _, start, end = span
    present = {line.strip() for line in lines[start:end] if line.strip()}
    fresh = [line for line in block.splitlines()
             if line.strip() and line.strip() not in present]
    if not fresh:
        return document
    cut = end
    while cut > start and not lines[cut - 1].strip():
        cut -= 1       # trailing blank lines belong after the new paragraph
    addition = ([""] if cut > start else []) + fresh
    return "\n".join(lines[:cut] + addition + lines[cut:]) + \
        ("\n" if document.endswith("\n") else "")


# --- the rail's TODO list -------------------------------------------------
#
# `todo_items` is the list the workspace rail edits: one row per line, each
# with a stable id the reader never sees, its text, its depth, and the state a
# build run gives it. `todos_md` is derived from it -- the same list as
# markdown bullets, four spaces to the level -- for the injected context, the
# copied prompt, and anything else that reads the list as text.

TODO_INDENT = "    "
TODO_STATUSES = ("", "queued", "building", "asking", "done", "failed")
_TODO_ID = re.compile(r"^t[0-9a-z]{4,24}$")

# A screenshot pasted into a row: the row's text gets "[attachment #N]" where
# the caret was, and the row's `attachments` remembers which file each N
# names. N counts up across the whole list, never reused, so a marker means
# the same file wherever it is later moved. A marker the reader deletes from
# the text un-cites its file: what leaves the rail names only the markers
# still present in some row.
_MARKER = re.compile(r"\[attachment #(\d+)\]")
MAX_ATTACHMENTS = 20

# A row the reader reopened: the run that ended keeps its verdict and what
# they said was wrong with it, so the next run can read both and the rail can
# show the row's runs stacked under it. Entry i is run i+1; the run now
# happening is len(history)+1. Only ever appended to, and only by the server.
MAX_HISTORY = 12


def normalize_attachments(value) -> list:
    out, seen = [], set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        path = str(item.get("path") or "")[:1000]
        if n <= 0 or n in seen or not path:
            continue
        seen.add(n)
        out.append({"n": n, "path": path,
                    "name": str(item.get("name") or "")[:200]})
        if len(out) >= MAX_ATTACHMENTS:
            break
    return out


def normalize_history(value) -> list:
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "")
        if state not in TODO_STATUSES or not state:
            continue
        out.append({"state": state,
                    "note": str(item.get("note") or "")[:400]})
        if len(out) >= MAX_HISTORY:
            break
    return out


def todo_attachments(items) -> list:
    """The files the list still cites: each row's attachments whose marker
    appears in SOME row's text, ordered by number."""
    cited = set()
    for row in items or []:
        if isinstance(row, dict):
            cited.update(int(n) for n in _MARKER.findall(str(row.get("text") or "")))
    out, seen = [], set()
    for row in items or []:
        if not isinstance(row, dict):
            continue
        for att in normalize_attachments(row.get("attachments")):
            if att["n"] in cited and att["n"] not in seen:
                seen.add(att["n"])
                out.append(att)
    return sorted(out, key=lambda a: a["n"])


def render_attachments(items) -> str:
    """One line per cited file, marker to path; "" when nothing is cited."""
    lines = [f"[attachment #{a['n']}]: {a['path']}"
             for a in todo_attachments(items)]
    return "\n".join(lines) + "\n" if lines else ""


def todo_id() -> str:
    """A fresh id for a list row: short, opaque, never shown."""
    import secrets
    return "t" + secrets.token_hex(4)


# What the goal's work is actually for: the situation the reader is in, in
# their own words, and what they want answered about it. Its own field rather
# than a section of the notes, because every build of the goal's rows opens on
# it -- the document is theirs to shape, and a heading a build depended on
# would be a heading they could not rename.
MAX_QUESTIONS = 12
MAX_SCENARIO = 4000
# A question is a thread, not a line: Claude answers it in GIVEN/WHEN/THEN and
# the reader follows up in the same place. The answers are kept because they
# are the understanding -- a build reading the questions alone would be reading
# what was not known yet.
MAX_TURNS = 8
MAX_ANSWER = 4000
# The screenshots a scenario was written from. Bounded like a row's
# attachments and for the same reason: what is cited is opened.
MAX_SHOTS = 8
_QUESTION_ID = re.compile(r"^q[0-9a-z]{4,24}$")


def question_id() -> str:
    """A fresh id for a question: like a row's, and never shown either."""
    import secrets
    return "q" + secrets.token_hex(4)


def normalize_thread(value) -> list:
    """One question's conversation: what was asked, what came back.

    A turn with no answer in it is dropped -- a question whose answer never
    arrived is still just the question, and quoting it back with an empty
    answer reads as an answer of nothing.
    """
    out = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        said = str(row.get("a") or "").strip()[:MAX_ANSWER]
        if not said:
            continue
        out.append({"q": " ".join(str(row.get("q") or "").split())[:400],
                    "a": said})
        if len(out) >= MAX_TURNS:
            break
    return out


def normalize_shots(value) -> list:
    """The screenshots a scenario was made from: a path each, named once."""
    out, seen = [], set()
    for row in value if isinstance(value, list) else []:
        typed = isinstance(row, dict)
        path = str((row.get("path") if typed else row) or "")[:1000]
        if not path or path in seen:
            continue
        seen.add(path)
        name = str(row.get("name") or "")[:200] if typed else ""
        out.append({"path": path, "name": name or Path(path).name})
        if len(out) >= MAX_SHOTS:
            break
    return out


def normalize_understanding(value) -> dict:
    """Coerce whatever was stored or posted into the tab's whole shape.

    ``{scenario, shots, questions}``, where a question carries the thread it
    has been answered in. Questions with nothing in them are dropped: the tab
    always keeps one empty box on offer and that box is the browser's, not
    something to store.
    """
    value = value if isinstance(value, dict) else {}
    raw = value.get("questions")
    out, seen = [], set()
    for row in raw if isinstance(raw, list) else []:
        text = row.get("text") if isinstance(row, dict) else row
        text = " ".join(str(text or "").split())[:400]
        if not text:
            continue
        qid = str(row.get("id") or "") if isinstance(row, dict) else ""
        if not _QUESTION_ID.match(qid) or qid in seen:
            qid = question_id()
        seen.add(qid)
        out.append({"id": qid, "text": text,
                    "thread": normalize_thread(
                        row.get("thread") if isinstance(row, dict) else None)})
        if len(out) >= MAX_QUESTIONS:
            break
    return {"scenario": str(value.get("scenario") or "")[:MAX_SCENARIO],
            "shots": normalize_shots(value.get("shots")),
            "questions": out}


def render_understanding(goal) -> list:
    """The scenario and its questions as prompt lines; [] when unwritten.

    Read by ``build.compose_prompt``, so what the reader wrote in the
    Understanding tab is what every build of this goal's rows opens on --
    including the screenshots the scenario was made from and the answers the
    tab has already given, indented under the question each one settled.
    """
    held = normalize_understanding((goal or {}).get("understanding"))
    scenario = held["scenario"].strip()
    if not scenario and not held["questions"] and not held["shots"]:
        return []
    lines = ["# The scenario this goal is for", ""]
    lines.append(scenario or "(not described)")
    if held["shots"]:
        lines += ["", "Screenshots of it. Open each one -- they are part of"
                  " the description, not decoration.", ""]
        lines += ["- " + s["path"] for s in held["shots"]]
    if held["questions"]:
        lines += ["", "Questions the user wants answered about it. Answer each"
                  " one in your reply; where a question decides the work, let"
                  " the answer decide it. Some already carry an answer given"
                  " in the tab, indented under them in GIVEN/WHEN/THEN: take"
                  " it as settled unless the work shows otherwise.", ""]
        for question in held["questions"]:
            lines.append("- " + question["text"])
            for turn in question["thread"]:
                if turn["q"] and turn["q"] != question["text"]:
                    lines.append("  - and then: " + turn["q"])
                lines += ["    " + line for line in turn["a"].splitlines()]
    return lines


def parse_todos(text) -> list:
    """Markdown bullets -> rows. Every non-blank line is a row; a line that
    is not a bullet is one all the same, so nothing a person typed is lost.

    Ids are derived from the line's position and text, not minted: the same
    markdown parses to the same rows every time, so a document that has only
    ever been markdown reads back stably until its rows are first saved.
    """
    import hashlib
    rows = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        lead = re.match(r"^[ \t]*", line).group(0).replace("\t", TODO_INDENT)
        body = line[len(re.match(r"^[ \t]*", line).group(0)):]
        body = re.sub(r"^[-*] ", "", body)
        digest = hashlib.sha1(f"{len(rows)}:{body}".encode("utf-8")).hexdigest()
        rows.append({"id": "t" + digest[:8], "text": body,
                     "depth": len(lead) // len(TODO_INDENT),
                     "status": "", "question": ""})
    ceiling = 0
    for row in rows:
        row["depth"] = max(0, min(row["depth"], ceiling))
        ceiling = row["depth"] + 1
    return rows


def render_todos(items) -> str:
    """Rows -> markdown bullets. Blank rows are left out; an empty list is ""."""
    lines = []
    for row in items or []:
        text = str(row.get("text") or "")
        if not text.strip():
            continue
        depth = max(0, int(row.get("depth") or 0))
        lines.append(TODO_INDENT * depth + "- " + text)
    return "\n".join(lines) + "\n" if lines else ""


def normalize_todo_items(value) -> list:
    """Coerce whatever was stored or posted into well-formed rows."""
    out = []
    seen = set()
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "")
        if not _TODO_ID.match(rid) or rid in seen:
            rid = todo_id()
        seen.add(rid)
        try:
            depth = max(0, min(8, int(row.get("depth") or 0)))
        except (TypeError, ValueError):
            depth = 0
        status = str(row.get("status") or "")
        if status not in TODO_STATUSES:
            status = ""
        clean = {"id": rid, "text": str(row.get("text") or ""),
                 "depth": depth, "status": status,
                 "question": str(row.get("question") or "")[:400]
                 if status == "asking" else ""}
        # Only present when there is something to hold: the browser's rows
        # and the server's must compare equal field for field, and a row
        # without a screenshot has no field at all on either side.
        attachments = normalize_attachments(row.get("attachments"))
        if attachments:
            clean["attachments"] = attachments
        # What building this row actually spent, once it has been built alone
        # -- the rail prints it in place of its estimate. Same rule: absent
        # until there is a number, on both sides of the wire.
        try:
            tokens = int(row.get("tokens") or 0)
        except (TypeError, ValueError):
            tokens = 0
        if tokens > 0:
            clean["tokens"] = tokens
        history = normalize_history(row.get("history"))
        if history:
            clean["history"] = history
        out.append(clean)
    ceiling = 0
    for row in out:
        row["depth"] = max(0, min(row["depth"], ceiling))
        ceiling = row["depth"] + 1
    return out


# ``chat`` is never inferred from a label -- a session id looks like
# nothing in particular -- so it arrives only when a caller says so.
SOURCE_TYPES = ("github", "local", "doc", "chat")


def source_type(label: str) -> str:
    """Classify an attached source by what it plainly is."""
    text = str(label or "").strip()
    lowered = text.lower()
    if "github.com" in lowered or re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        return "github"
    if text.startswith(("/", "~", "./")) and not re.search(r"\.\w{1,5}$", text):
        return "local"
    if text.startswith(("http://", "https://")):
        return "doc"
    return "local" if text.startswith(("/", "~", "./")) else "doc"


def normalize_sources(value):
    """Accept plain strings or typed rows; always store typed rows."""
    out, seen = [], set()
    for entry in (value if isinstance(value, list) else [])[:20]:
        if isinstance(entry, str):
            entry = {"label": entry}
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()[:300]
        if not label or label in seen:
            continue
        seen.add(label)
        kind = entry.get("type")
        if kind not in SOURCE_TYPES:
            kind = source_type(label)
        out.append({"id": str(entry.get("id") or f"s{len(out) + 1}")[:40],
                    "type": kind, "label": label})
    return out


def evidence_prompts(trajdir: Path):
    """The user's own turns, as assignable prompts for the global tree.

    Chat scope keeps a prompts.json per session; the global tree never had an
    equivalent, so its prompt panel had nothing to show. But the evidence index
    already holds every turn with its role, and goals already cite those ids —
    the human half of it *is* the prompt list. Newest last, so the UI's
    recency ordering has something monotonic to sort on.
    """
    try:
        idx = json.loads((trajdir / "evidence_index.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(idx, dict):
        return []
    rows = [
        (str(record.get("date") or ""), str(turn_id), record)
        for turn_id, record in idx.items()
        if isinstance(record, dict) and record.get("role") == "user"
        and isinstance(record.get("text"), str) and record["text"].strip()
        and not _machine_authored(record["text"])
    ]
    rows.sort()
    return [
        {"id": turn_id, "role": "user", "text": record["text"],
         "created_at": date or None, "ordinal": ordinal,
         "session_id": record.get("session_id")}
        for ordinal, (date, turn_id, record) in enumerate(rows, start=1)
    ]


def link_evidence_prompts(goals, prompts):
    """Attach the prompts inference already cited as evidence for a goal.

    A prompt's id *is* its evidence id, so a goal citing one has already been
    judged to belong with it — linking costs no inference and invents no
    association. Links the user removed stay removed: a detach is a decision,
    not a gap to refill. The machine-made subset stays labelled so the UI can
    distinguish it from what the user chose.
    """
    known = {p.get("id") for p in prompts}
    for g in goals.get("goals", []):
        if not isinstance(g, dict):
            continue
        detached = set(g.get("detached_prompt_ids") or [])
        links = list(g.get("prompt_ids") or [])
        automatic = set(g.get("auto_prompt_ids") or [])
        for eid in g.get("evidence_ids") or []:
            if eid in known and eid not in detached and eid not in links:
                links.append(eid)
                automatic.add(eid)
        g["prompt_ids"] = links
        g["auto_prompt_ids"] = [pid for pid in links if pid in automatic]
    return goals


# The rail's rows persist in their own file, todos.json -- a mapping of goal
# id to rows -- never inside the notes and never re-derived from them. The
# goal dicts still carry `todo_items` in memory (every reader works on the
# joined shape); the split is a storage fact, applied on the way to disk and
# undone on the way back.

def split_todo_store(goals) -> dict:
    """The rows lifted out of each goal, keyed by goal id, for todos.json."""
    rows = {}
    for g in goals.get("goals", []):
        items = normalize_todo_items(g.get("todo_items"))
        if items:
            rows[str(g.get("id") or "")] = items
    return rows


def overlay_todo_store(goals, held):
    """Lay todos.json's rows back onto the goals. A goal the store never
    heard of keeps whatever rows it carries inline -- the shape every store
    had before the split, read once and rewritten split on the next save."""
    held = held if isinstance(held, dict) else {}
    for g in goals.get("goals", []):
        rows = held.get(str(g.get("id") or ""))
        if isinstance(rows, list):
            g["todo_items"] = normalize_todo_items(rows)
    return goals


def strip_todo_items(goals):
    """A copy of *goals* fit for goals.json: the rows live in todos.json."""
    lean = dict(goals)
    lean["goals"] = [dict(g, todo_items=[]) for g in goals.get("goals", [])]
    return lean


def load(trajdir: Path):
    def j(name, default):
        try:
            return json.loads((trajdir / name).read_text())
        except (OSError, json.JSONDecodeError):
            return default
    todos = j("todos.json", {})
    return (overlay_todo_store(j("goals.json", {"version": 1, "goals": []}),
                               todos.get("todos") if isinstance(todos, dict)
                               else {}),
            j("important.json", {"items": []}))


def save(trajdir: Path, goals, important):
    root = trajdir.parent
    secure_dir(trajdir, root)
    link_evidence_prompts(goals, evidence_prompts(trajdir))
    goals["generated_at"] = _now()
    for name, obj in (("goals.json", strip_todo_items(goals)),
                      ("important.json", important),
                      ("todos.json", {"version": 1,
                                      "todos": split_todo_store(goals)})):
        atomic_write_json(trajdir / name, obj, root=root)
    write_goal_context(trajdir, goals, important)


def next_goal_id(goals):
    ns = [int(g["id"][1:]) for g in goals["goals"]
          if re.fullmatch(r"g\d+", g.get("id", ""))]
    return f"g{max(ns) + 1 if ns else 1}"


def child_goal_id(goals, parent_id):
    """A stable id under *parent_id*: g1a -> g1a1, g1a2, …"""
    taken = {g.get("id") for g in goals["goals"]}
    for n in range(1, 1000):
        candidate = f"{parent_id}{n}"
        if candidate not in taken:
            return candidate
    return next_goal_id(goals)


def new_goal(gid, title, parent_id=None, **fields):
    """One shape for every node, at every depth."""
    goal = {"id": gid, "title": str(title or "Untitled")[:120],
            "status": "active", "parent_goal_id": parent_id,
            "evidence_ids": [], "todos": [], "important_item_ids": [],
            "prompt_ids": [], "auto_prompt_ids": [], "detached_prompt_ids": [],
            "description": "", "priority": "normal", "notes": "",
            "todos_md": "", "todo_items": [], "prompt_md": "",
            "sources": [], "opening": "",
            # The situation this goal's work happens in, and what the reader
            # wants answered about it. Written in the rail's Understanding
            # tab; carried into every build of this goal's rows.
            "understanding": {"scenario": "", "shots": [], "questions": []},
            # How this goal stands to the project's objective, and the
            # objective it was judged against -- kept together, because a
            # verdict outlives the sentence that produced it and a stale one
            # should be visible as stale rather than trusted.
            "relevance": "core", "relevance_why": "", "relevance_for": "",
            # Which project's directory this goal's work belongs in. Empty
            # for the ordinary goal, which is about the project the chat was
            # started in; set when the goal was made under another one, and
            # then a build of its TODO rows runs there rather than here.
            "project_cwd": "",
            "origin": "inferred", "updated_at": _now()}
    goal.update(fields)
    return goal


def add_todo_row(goal, text, depth=0):
    """Put a next action on the goal's own list, not in the tree.

    Inference used to emit these as child goals -- every next action became
    a node, and a tree of forty leaves was mostly checklist. They belong on
    the goal they serve: the rail beside it, where they can be built,
    answered and ticked off without crowding the tree.

    Returns the row, or None when the goal already has that line.
    """
    text = str(text or "").strip()[:400]
    if not text:
        return None
    rows = goal.get("todo_items")
    if not isinstance(rows, list):
        rows = goal["todo_items"] = []
    lowered = text.lower()
    for row in rows:
        if str(row.get("text") or "").strip().lower() == lowered:
            return None
    row = {"id": todo_id(), "text": text,
           "depth": max(0, min(8, int(depth or 0))),
           "status": "", "question": ""}
    rows.append(row)
    return row


def promote_todos(goals):
    """Make every node in the tree the same kind of thing.

    A goal, a subgoal and a next action are all goals — only their depth
    differs. Todos used to be ``{text, done, evidence_ids}`` dicts nested
    inside a goal, so they could not carry a status, a description, prompt
    links or an execution run, and the UI had to special-case them at every
    turn. Inference still finds it natural to emit todos, so this accepts them
    as an input shape and converts them in place. Idempotent: a tree with no
    todos passes through untouched.
    """
    for parent in list(goals.get("goals", [])):
        todos = parent.get("todos") or []
        if not isinstance(todos, list) or not todos:
            parent["todos"] = []
            continue
        existing = {str(g.get("title") or ""): g for g in goals["goals"]
                    if g.get("parent_goal_id") == parent.get("id")}
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            title = str(todo.get("text") or "").strip()[:120]
            if not title:
                continue
            done = bool(todo.get("done"))
            evidence = [e for e in (todo.get("evidence_ids") or [])
                        if isinstance(e, str)]
            prior = existing.get(title)
            if prior is not None:
                # Promoted once already, then re-emitted by inference: keep the
                # node the user may have edited and fold in any new evidence.
                prior["evidence_ids"] = list(dict.fromkeys(
                    list(prior.get("evidence_ids") or []) + evidence))
                if done and prior.get("status") == "active":
                    prior["status"] = "completed"
                continue
            child = new_goal(
                child_goal_id(goals, parent.get("id") or "g"), title,
                parent.get("id"),
                status="completed" if done else "active",
                evidence_ids=evidence,
                origin=parent.get("origin", "inferred"))
            goals["goals"].append(child)
            existing[title] = child
        parent["todos"] = []
    return goals


def by_id(goals, gid):
    return next((g for g in goals["goals"] if g.get("id") == gid), None)


def depth(goals, gid, seen=None):
    seen = seen or set()
    g = by_id(goals, gid)
    if not g or gid in seen or not g.get("parent_goal_id"):
        return 1
    return 1 + depth(goals, g["parent_goal_id"], seen | {gid})


MAX_DEPTH = 4          # a next action is a goal, one level below its subgoal


def sanitize(goals):
    """Structural guardrails: parents must exist, depth<=4, statuses legal."""
    promote_todos(goals)
    ids = {g.get("id") for g in goals["goals"]}
    for g in goals["goals"]:
        if g.get("parent_goal_id") not in ids:
            g["parent_goal_id"] = None
        if g.get("status") not in ("active", "in_progress", "completed", "abandoned"):
            g["status"] = "active"
        # An unrecognised verdict is "core": the fold hides work, so the
        # failure to understand one must not be what hides it.
        if g.get("relevance") not in ("core", "supporting", "unrelated"):
            g["relevance"] = "core"
        g["relevance_why"] = str(g.get("relevance_why") or "")[:200]
        g["relevance_for"] = str(g.get("relevance_for") or "")[:2000]
        g["project_cwd"] = str(g.get("project_cwd") or "")[:1000]
        g.setdefault("evidence_ids", []); g.setdefault("todos", [])
        g.setdefault("important_item_ids", [])
        for key in ("prompt_ids", "auto_prompt_ids", "detached_prompt_ids"):
            raw = g.get(key)
            g[key] = list(dict.fromkeys(
                pid for pid in raw if isinstance(pid, str))) \
                if isinstance(raw, list) else []
        g.setdefault("updated_at", _now())
        g.setdefault("priority", "normal")
        # The goal's whole markdown document. Coerced, never truncated: a cap
        # here would silently eat the tail of something a person wrote.
        g["notes"] = str(g.get("notes") or "")
        # The reader's prompt lives beside the notes, not in them. A goal
        # saved before that carried it as a section of the document -- lift
        # it out once, and drop the empty spine the old default document
        # seeded. The rail's TODO list is NOT lifted: todos are their own
        # store, and a "# TODOs" heading in the notes is just the reader's
        # own writing, never read as the list and never deleted for it.
        g["todos_md"] = str(g.get("todos_md") or "")
        g["prompt_md"] = str(g.get("prompt_md") or "")
        if g["notes"]:
            sections = split_doc(g["notes"])
            changed = False
            if not g["prompt_md"].strip():
                body = section_body(g["notes"], "Prompt")
                if body and body.strip():
                    g["prompt_md"] = body.strip("\n") + "\n"
                    changed = True
            if changed or any(t == "Prompt" for t, _ in sections):
                sections = [(t, b) for t, b in sections if t != "Prompt"]
                g["notes"] = join_doc(sections)
            g["notes"] = strip_empty_spine(g["notes"])
        # Rows are canonical; the markdown is derived from them. A goal that
        # only ever had the markdown is parsed into rows once, each with a
        # fresh id.
        items = normalize_todo_items(g.get("todo_items"))
        if not items and g["todos_md"].strip():
            items = parse_todos(g["todos_md"])
        g["todo_items"] = items
        g["todos_md"] = render_todos(items)
        g.setdefault("description", "")
        g.setdefault("opening", "")
        # The scenario the goal's work is for. Always present and always this
        # shape, so a build can read it without asking whether it is there.
        g["understanding"] = normalize_understanding(g.get("understanding"))
        # Extra context the user chose to attach. Never inferred — a local
        # path here widens what a launched session may read.
        g["sources"] = normalize_sources(g.get("sources"))
        if g["priority"] not in ("urgent", "high", "normal"):
            g["priority"] = "normal"
    # A model response or imported browser snapshot can name valid parents and
    # still form a cycle. Break the edge owned by the first goal that observes
    # each cycle so every node remains reachable from a top-level root.
    for g in goals["goals"]:
        seen = {g.get("id")}
        parent_id = g.get("parent_goal_id")
        while parent_id:
            if parent_id in seen:
                g["parent_goal_id"] = None
                break
            seen.add(parent_id)
            parent = by_id(goals, parent_id)
            parent_id = parent.get("parent_goal_id") if parent else None
    for g in goals["goals"]:
        if depth(goals, g["id"]) > MAX_DEPTH:
            g["parent_goal_id"] = None
    return goals


def apply_ops(goals, important, ops, max_new_top_level=1):
    """Deterministically apply structured operations (from classification or
    approved corrections). Returns list of human-readable change lines."""
    changes, new_top = [], 0
    for o in ops or []:
        op = o.get("op")
        g = by_id(goals, o.get("goal_id", ""))
        if op == "attach_evidence" and g:
            new = [e for e in o.get("evidence_ids", []) if e not in g["evidence_ids"]]
            g["evidence_ids"] += new
            if new:
                g["updated_at"] = _now(); changes.append(f"evidence → {g['title'][:40]}")
        elif op == "add_todo" and g:
            # On the goal's own list, beside it -- not a node in the tree.
            row = add_todo_row(g, o.get("text"))
            if row:
                g["updated_at"] = _now()
                changes.append(f"todo + {row['text'][:44]}")
        elif op == "complete_todo" and g:
            tt = _toks(o.get("text_match", o.get("text", "")))
            done = False
            # The goal's own list first, which is where inference puts them.
            for row in g.get("todo_items") or []:
                if row.get("status") == "done":
                    continue
                rt = _toks(str(row.get("text") or ""))
                if tt and len(tt & rt) / max(1, len(tt)) >= 0.5:
                    row["status"] = "done"; g["updated_at"] = _now()
                    changes.append(f"✓ {str(row.get('text'))[:44]}")
                    done = True
                    break
            # Older trees put them in the tree; still close those.
            if not done:
                for c in goals["goals"]:
                    if (c.get("parent_goal_id") != g["id"]
                            or c["status"] == "completed"):
                        continue
                    ct = _toks(c["title"])
                    if tt and len(tt & ct) / max(1, len(tt)) >= 0.5:
                        c["status"] = "completed"; c["updated_at"] = _now()
                        changes.append(f"✓ {c['title'][:44]}"); break
        elif op == "new_goal":
            parent_id = o.get("parent_goal_id")
            if parent_id and not by_id(goals, parent_id):
                parent_id = None
            top = not parent_id
            if top:
                new_top += 1
                if new_top > max_new_top_level or not o.get("distinct_because"):
                    changes.append(f"REFUSED new top-level goal: {o.get('title','')[:40]}")
                    continue
            gid = next_goal_id(goals)
            title = str(o.get("title") or "Untitled goal")[:120]
            # A goal can be born finished. Each pass sees one window of
            # evidence, and work that starts and ends inside it is noticed
            # only once -- created, and then never revisited, because the
            # evidence that would close it is the same evidence that made
            # it. Without this such a goal stays active for good.
            born = o.get("status")
            if born not in ("active", "in_progress", "completed", "abandoned"):
                born = "active"
            # How it stands to the objective, when the model said. Dropped
            # here until now, so every goal created by an incremental pass
            # came out "core" whatever the model judged -- and the tags on
            # a rebuilt tree looked uniform because they were.
            stands = o.get("relevance")
            if stands not in ("core", "supporting", "unrelated"):
                stands = "core"
            made = new_goal(
                gid, title, parent_id,
                status=born,
                relevance=stands,
                relevance_why=str(o.get("relevance_why") or "")[:200],
                evidence_ids=o.get("evidence_ids", []),
                description=str(o.get("description") or "")[:600])
            for t in o.get("todos", []) or []:
                if isinstance(t, dict):
                    row = add_todo_row(made, t.get("text"))
                    if row and t.get("done"):
                        row["status"] = "done"
                elif isinstance(t, str):
                    add_todo_row(made, t)
            goals["goals"].append(made)
            sanitize(goals)
            changes.append(f"goal + {title[:44]}")
        elif (op == "set_relevance" and g
              and o.get("relevance") in ("core", "supporting", "unrelated")):
            # Only when the standing actually changed: a verdict restated
            # every pass would rewrite updated_at and churn the tree.
            if g.get("relevance") != o["relevance"]:
                g["relevance"] = o["relevance"]
                g["relevance_why"] = str(o.get("relevance_why") or "")[:200]
                g["updated_at"] = _now()
                changes.append(f"relevance {g['id']} -> {o['relevance']}")
        elif op == "set_status" and g and o.get("status") in ("active", "in_progress", "completed", "abandoned"):
            g["status"] = o["status"]; g["updated_at"] = _now()
            changes.append(f"{g['title'][:36]} → {o['status']}")
        elif op == "rename_goal" and g and o.get("title"):
            changes.append(f"rename {g['title'][:30]} → {o['title'][:30]}")
            g["title"] = o["title"]; g["updated_at"] = _now()
        elif op == "move_goal" and g:
            np = o.get("new_parent_id")
            if np is None or (by_id(goals, np) and np != g["id"]):
                g["parent_goal_id"] = np; g["updated_at"] = _now()
                changes.append(f"moved {g['title'][:34]} under "
                               f"{(by_id(goals, np) or {'title':'top level'})['title'][:30]}")
        elif op == "merge_goals":
            src, dst = by_id(goals, o.get("from_id", "")), by_id(goals, o.get("into_id", ""))
            if src and dst and src is not dst:
                dst["evidence_ids"] += [e for e in src["evidence_ids"]
                                        if e not in dst["evidence_ids"]]
                dst["important_item_ids"] += src["important_item_ids"]
                dst["prompt_ids"] += [pid for pid in src.get("prompt_ids", [])
                                      if pid not in dst["prompt_ids"]]
                for ch in goals["goals"]:
                    if ch.get("parent_goal_id") == src["id"]:
                        ch["parent_goal_id"] = dst["id"]
                goals["goals"].remove(src); dst["updated_at"] = _now()
                changes.append(f"merged {src['title'][:28]} into {dst['title'][:28]}")
        elif op == "demote_to_todo" and g:
            parent = by_id(goals, o.get("parent_goal_id", "")) or \
                     by_id(goals, g.get("parent_goal_id", ""))
            if parent and parent is not g:
                g["parent_goal_id"] = parent["id"]
                g["updated_at"] = parent["updated_at"] = _now()
                changes.append(f"moved {g['title'][:34]} under {parent['title'][:30]}")
        elif op == "append_section" and g:
            # The only write inference gets on a goal's document, and it can
            # only add to the end of one named section. Nothing is written --
            # not even a missing header -- unless that section really gained
            # text, so a repeated inference leaves the document untouched.
            title = SECTION_KEYS.get(str(o.get("section") or ""))
            text = str(o.get("text") or "").strip()
            before = str(g.get("notes") or "")
            after = append_to_section(before, title, text) if title and text \
                else before
            if (section_body(after, title) or "") != \
                    (section_body(before, title) or ""):
                g["notes"] = after
                g["updated_at"] = _now()
                changes.append(f"notes → {g['title'][:30]} / {title}")
        elif op == "attach_important":
            it = next((i for i in important["items"] if i["id"] == o.get("item_id")), None)
            tgt = by_id(goals, o.get("goal_id", ""))
            if it and tgt:
                it["goal_id"] = tgt["id"]
                if it["id"] not in tgt["important_item_ids"]:
                    tgt["important_item_ids"].append(it["id"])
                changes.append(f"★ attached to {tgt['title'][:36]}")
    sanitize(goals)
    return changes


def mark_important(trajdir, goals, important, text, session_id=None, turn_id=None,
                   why=None, goal_id=None):
    iid = f"i{len(important['items']) + 1}"
    assoc = "explicit" if goal_id else "inferred"
    if not goal_id:                    # cheap inferred association by title overlap
        best, score = None, 0.0
        tt = _toks(text)
        for g in goals["goals"]:
            kids = [c["title"] for c in goals["goals"]
                    if c.get("parent_goal_id") == g["id"]]
            gt = _toks(" ".join([g["title"]] + kids))
            s = len(tt & gt) / max(1, len(tt))
            if s > score:
                best, score = g, s
        if best and score >= 0.25:
            goal_id = best["id"]
    important["items"].append({"id": iid, "text": text, "session_id": session_id,
                               "turn_id": turn_id, "goal_id": goal_id, "why": why,
                               "association": assoc, "origin": "user",
                               "marked_at": _now()})
    if goal_id and (g := by_id(goals, goal_id)):
        g["important_item_ids"].append(iid)
    return iid, goal_id


def render(goals, important, show_all=False):
    """Terminal tree per the sketch. Returns itemmap for the evidence flow."""
    print()
    print(bold("CURRENT WORK"))
    print(dim(SEP))
    items = {i["id"]: i for i in important["items"]}
    itemmap = []

    def n_of(obj):
        itemmap.append(obj); return dim(f"[{len(itemmap)}]")

    def visible(g):
        return show_all or g["status"] in ("active", "in_progress")

    def children(pid):
        return [g for g in goals["goals"] if g.get("parent_goal_id") == pid and visible(g)]

    def line(prefix, s):
        print(prefix + s)

    def emit(g, prefix, last, root=False):
        tag = "" if g["status"] == "active" else dim(" (" + g["status"].replace("_", " ") + ")")
        head = "" if root else ("└─ " if last else "├─ ")
        line(prefix + head, bold(g["title"]) + tag + " " +
             n_of({"kind": "goal", "obj": g}) + dim(f" {g['id']}"))
        child_prefix = prefix + ("" if root else ("   " if last else "│  "))
        rows = []
        for iid in g["important_item_ids"]:
            if iid in items:
                rows.append(("imp", items[iid]))
        kids = children(g["id"])
        for i, (kind, obj) in enumerate(rows):
            elbow = "└─ " if (i == len(rows) - 1 and not kids) else "├─ "
            line(child_prefix + elbow, star("★ ") + obj["text"][:70] + " " +
                 n_of({"kind": "important", "obj": obj}))
        for i, k in enumerate(kids):
            emit(k, child_prefix, i == len(kids) - 1)

    tops = children(None)
    for i, g in enumerate(tops):
        emit(g, "", i == len(tops) - 1, root=True)
        if i < len(tops) - 1:
            print(dim("│"))
    unassigned = [i for i in important["items"] if not i.get("goal_id")]
    if unassigned:
        print(dim(SEP))
        print(star("IMPORTANT — not yet tied to a goal"))
        for it in unassigned:
            print(" " + star("★ ") + it["text"][:70] + " " +
                  dim(f"[{len(itemmap) + 1}]"))
            itemmap.append({"kind": "important", "obj": it})
    print(dim(SEP))
    print(dim("[C] Correct   [M] Mark important   [E] Evidence   [Q] Quit"))
    return itemmap


def write_goal_context(trajdir: Path, goals, important):
    """Small markdown injected into new Claude sessions via the vault
    SessionStart hook — the goal state as persistent context."""
    lines = ["# Your current goals (derived from your recent work; correct via `hc goals`)"]
    items = {i["id"]: i for i in important["items"]}
    def emit(g, ind):
        lines.append(f"{'  ' * ind}- {g['title']} [" + g["status"].replace("_", " ") + "]")
        for iid in g["important_item_ids"][:3]:
            if iid in items:
                lines.append(f"{'  ' * (ind + 1)}- IMPORTANT: {items[iid]['text'][:120]}")
        for ch in [x for x in goals["goals"] if x.get("parent_goal_id") == g["id"]]:
            emit(ch, ind + 1)
    for g in [x for x in goals["goals"]
              if not x.get("parent_goal_id") and x["status"] in ("active", "in_progress")]:
        emit(g, 0)
    txt = "\n".join(lines)[:1900]
    atomic_write_text(trajdir / "goal_context.md", txt, root=trajdir.parent)
