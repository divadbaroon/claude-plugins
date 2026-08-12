"""Goal-aware state layer: durable derived goal tree + user-marked important
items. Raw Vault evidence stays immutable; goals.json/important.json are
regenerable derived state; user corrections live separately in
corrections.json and survive regeneration (supervision signal)."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def load(trajdir: Path):
    def j(name, default):
        try:
            return json.loads((trajdir / name).read_text())
        except (OSError, json.JSONDecodeError):
            return default
    return (j("goals.json", {"version": 1, "goals": []}),
            j("important.json", {"items": []}))


def save(trajdir: Path, goals, important):
    goals["generated_at"] = _now()
    for name, obj in (("goals.json", goals), ("important.json", important)):
        tmp = trajdir / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1))
        import os
        os.replace(tmp, trajdir / name)
    write_goal_context(trajdir, goals, important)


def next_goal_id(goals):
    ns = [int(g["id"][1:]) for g in goals["goals"]
          if re.fullmatch(r"g\d+", g.get("id", ""))]
    return f"g{max(ns) + 1 if ns else 1}"


def by_id(goals, gid):
    return next((g for g in goals["goals"] if g.get("id") == gid), None)


def depth(goals, gid, seen=None):
    seen = seen or set()
    g = by_id(goals, gid)
    if not g or gid in seen or not g.get("parent_goal_id"):
        return 1
    return 1 + depth(goals, g["parent_goal_id"], seen | {gid})


def sanitize(goals):
    """Structural guardrails: parents must exist, depth<=3, statuses legal."""
    ids = {g.get("id") for g in goals["goals"]}
    for g in goals["goals"]:
        if g.get("parent_goal_id") not in ids:
            g["parent_goal_id"] = None
        if g.get("status") not in ("active", "in_progress", "completed", "abandoned"):
            g["status"] = "active"
        g.setdefault("evidence_ids", []); g.setdefault("todos", [])
        g.setdefault("important_item_ids", [])
        raw_prompt_ids = g.setdefault("prompt_ids", [])
        if not isinstance(raw_prompt_ids, list):
            raw_prompt_ids = []
        g["prompt_ids"] = list(dict.fromkeys(
            pid for pid in raw_prompt_ids if isinstance(pid, str)))
        g.setdefault("updated_at", _now())
        g.setdefault("priority", "normal"); g.setdefault("notes", "")
        g.setdefault("description", "")
        if g["priority"] not in ("urgent", "high", "normal"):
            g["priority"] = "normal"
    for g in goals["goals"]:
        if depth(goals, g["id"]) > 3:
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
            g["todos"].append({"text": o.get("text", ""), "done": False,
                               "evidence_ids": o.get("evidence_ids", [])})
            g["updated_at"] = _now(); changes.append(f"todo + {o.get('text','')[:44]}")
        elif op == "complete_todo" and g:
            tt = _toks(o.get("text_match", o.get("text", "")))
            for t in g["todos"]:
                if tt and tt <= _toks(t["text"]) | tt & _toks(t["text"]) and \
                   len(tt & _toks(t["text"])) / max(1, len(tt)) >= 0.5 and not t["done"]:
                    t["done"] = True; g["updated_at"] = _now()
                    changes.append(f"todo ✓ {t['text'][:44]}"); break
        elif op == "new_goal":
            top = not o.get("parent_goal_id")
            if top:
                new_top += 1
                if new_top > max_new_top_level or not o.get("distinct_because"):
                    changes.append(f"REFUSED new top-level goal: {o.get('title','')[:40]}")
                    continue
            gid = next_goal_id(goals)
            goals["goals"].append(sanitize({"goals": [{
                "id": gid, "title": o.get("title", ""), "status": "active",
                "parent_goal_id": o.get("parent_goal_id"),
                "evidence_ids": o.get("evidence_ids", []),
                "todos": [{"text": t.get("text", ""), "done": bool(t.get("done")),
                           "evidence_ids": t.get("evidence_ids", [])}
                          for t in o.get("todos", []) if isinstance(t, dict)],
                "important_item_ids": [], "updated_at": _now()}]})["goals"][0])
            changes.append(f"goal + {o.get('title','')[:44]}")
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
                dst["todos"] += src["todos"]
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
            if parent:
                parent["todos"].append({"text": g["title"], "done": g["status"] == "completed",
                                        "evidence_ids": g["evidence_ids"][:4]})
                for ch in goals["goals"]:
                    if ch.get("parent_goal_id") == g["id"]:
                        ch["parent_goal_id"] = parent["id"]
                goals["goals"].remove(g); parent["updated_at"] = _now()
                changes.append(f"demoted {g['title'][:34]} to todo")
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
            gt = _toks(g["title"]) | set().union(*[_toks(t["text"]) for t in g["todos"]] or [set()])
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
        for t in g["todos"]:
            if t["done"] and not show_all and len(g["todos"]) > 4:
                continue
            rows.append(("todo", t))
        for iid in g["important_item_ids"]:
            if iid in items:
                rows.append(("imp", items[iid]))
        kids = children(g["id"])
        for i, (kind, obj) in enumerate(rows):
            elbow = "└─ " if (i == len(rows) - 1 and not kids) else "├─ "
            if kind == "todo":
                mark = green("✓") if obj["done"] else cyan("→")
                line(child_prefix + elbow, f"{mark} {obj['text']} " +
                     n_of({"kind": "todo", "obj": obj, "goal": g}))
            else:
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
        for t in g["todos"]:
            if not t["done"]:
                lines.append(f"{'  ' * (ind + 1)}- TODO: {t['text']}")
        for iid in g["important_item_ids"][:3]:
            if iid in items:
                lines.append(f"{'  ' * (ind + 1)}- IMPORTANT: {items[iid]['text'][:120]}")
        for ch in [x for x in goals["goals"] if x.get("parent_goal_id") == g["id"]]:
            emit(ch, ind + 1)
    for g in [x for x in goals["goals"]
              if not x.get("parent_goal_id") and x["status"] in ("active", "in_progress")]:
        emit(g, 0)
    txt = "\n".join(lines)[:1900]
    (trajdir / "goal_context.md").write_text(txt)
