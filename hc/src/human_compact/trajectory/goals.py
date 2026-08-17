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
# headers below are its spine: inference writes under them and the human writes
# beside that, so both halves live in one editable place instead of a row of
# machine-owned textboxes the user may not overwrite.
DOC_SECTIONS = ("Objective", "In my words", "Decisions", "Built", "Blockers",
                "Open questions")
SECTION_KEYS = {"objective": "Objective", "in_my_words": "In my words",
                "decisions": "Decisions", "built": "Built",
                "blockers": "Blockers", "open_questions": "Open questions"}
_H1 = re.compile(r"^# (.*)$")
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")


def default_doc() -> str:
    """The empty document: every section header, no body under any of them."""
    return "\n\n".join(f"# {title}" for title in DOC_SECTIONS) + "\n"


def _scan_doc(notes):
    """Locate each section as a line span: ``(title, start, end)``.

    Fence-aware on purpose. A ``# install deps`` comment at column 0 inside a
    fenced code block is not a heading, and treating it as one would tear the
    user's code block across two sections and leave the fence unterminated --
    in state that is persisted and injected into later sessions.
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
    return lines, spans


def split_doc(notes):
    """Parse a notes document into ``[(h1 title, body)]`` in document order.

    A list, not a mapping: a person may write the same heading twice, or write
    them in an order of their own, and a dict would silently swallow the second
    one. Only ``# `` at column 0 outside a code fence starts a section -- an
    ``##`` heading is body text belonging to the section above it. Text written
    before the first header keeps the ``""`` title.
    """
    lines, spans = _scan_doc(notes)
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
    """Add the default headers a document lacks, at the end, changing nothing else.

    Deliberately not a re-join: headings the user wrote keep their place and
    their duplicates, and only the tail of the document is touched.
    """
    document = str(notes or "")
    if not document.strip():
        return default_doc()
    present = {title for title, _ in split_doc(document)}
    missing = [title for title in DOC_SECTIONS if title not in present]
    if not missing:
        return document
    tail = "\n\n".join(f"# {title}" for title in missing)
    return document.rstrip("\n") + "\n\n" + tail + "\n"


def append_to_section(notes: str, section_title: str, text: str) -> str:
    """Add *text* to the end of one section, leaving every other one alone.

    Append-only by construction: a refresh may extend the record of a goal but
    never rewrites the human's sentences, and a line the section already holds
    is dropped, so re-running inference over the same evidence is a no-op
    rather than a growing pile of duplicates. Edits land as line surgery on the
    document rather than a re-render, so untouched sections stay byte-identical
    down to their own spacing.
    """
    document = ensure_doc_sections(notes)
    block = str(text or "").strip()
    if not block:
        return document
    lines, spans = _scan_doc(document)
    span = next((s for s in spans if s[0] == section_title), None)
    if span is None:
        fresh = [line for line in block.splitlines() if line.strip()]
        return (document.rstrip("\n") + f"\n\n# {section_title}\n"
                + "\n".join(fresh) + "\n")
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


SOURCE_TYPES = ("github", "local", "doc")


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


def load(trajdir: Path):
    def j(name, default):
        try:
            return json.loads((trajdir / name).read_text())
        except (OSError, json.JSONDecodeError):
            return default
    return (j("goals.json", {"version": 1, "goals": []}),
            j("important.json", {"items": []}))


def save(trajdir: Path, goals, important):
    root = trajdir.parent
    secure_dir(trajdir, root)
    link_evidence_prompts(goals, evidence_prompts(trajdir))
    goals["generated_at"] = _now()
    for name, obj in (("goals.json", goals), ("important.json", important)):
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
            "sources": [], "opening": "",
            "origin": "inferred", "updated_at": _now()}
    goal.update(fields)
    return goal


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
        g.setdefault("description", "")
        g.setdefault("opening", "")
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
            # A next action is a goal one level down, not a second data type.
            text = str(o.get("text") or "").strip()[:120]
            if text and not any(
                    c.get("title") == text and c.get("parent_goal_id") == g["id"]
                    for c in goals["goals"]):
                goals["goals"].append(new_goal(
                    child_goal_id(goals, g["id"]), text, g["id"],
                    evidence_ids=[e for e in o.get("evidence_ids", [])
                                  if isinstance(e, str)]))
                g["updated_at"] = _now()
                changes.append(f"goal + {text[:44]}")
        elif op == "complete_todo" and g:
            tt = _toks(o.get("text_match", o.get("text", "")))
            for c in goals["goals"]:
                if c.get("parent_goal_id") != g["id"] or c["status"] == "completed":
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
            goals["goals"].append(new_goal(
                gid, title, parent_id,
                evidence_ids=o.get("evidence_ids", []),
                todos=[t for t in o.get("todos", []) if isinstance(t, dict)],
                description=str(o.get("description") or "")[:600]))
            sanitize(goals)
            changes.append(f"goal + {title[:44]}")
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
