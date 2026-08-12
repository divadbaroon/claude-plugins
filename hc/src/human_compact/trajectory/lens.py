"""Terminal-first Context Lens: render, evidence, corrections, and the
default-vs-lens-guided compaction comparison. Plain ANSI, no dependencies."""
import hashlib
import json
import shutil
import sys
import textwrap
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

from .secure_io import atomic_write_json, secure_dir

TTY = sys.stdout.isatty()
def c(code, s): return f"\033[{code}m{s}\033[0m" if TTY else s
def bold(s): return c("1", s)
def dim(s): return c("2", s)
def green(s): return c("32", s)
def cyan(s): return c("36", s)
def star(s): return c("1;33", s)
def red(s): return c("31", s)

BUCKETS = [("preserve", star("★"), "PRESERVE"),
           ("active_context", cyan("•"), "ACTIVE CONTEXT"),
           ("safe_to_compress", dim("·"), "SAFE TO COMPRESS")]
ALIAS = {"critical": "preserve", "never_lose": "preserve",
         "prioritize": "active_context", "compress": "safe_to_compress"}
def _bucket(name):
    return ALIAS.get(name, name)
SEP = "─" * 40
STOP = set("the a an of to for and in on with that this is are was were be been it its "
           "into from as at by or not no do does did what how why when user claude".split())


def _toks(s):
    import re
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}


def load(trajdir: Path):
    def j(name, default):
        p = trajdir / name
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return default
    return j("analysis.json", {}), j("corrections.json", {}), j("evidence_index.json", {})


def save_correction(trajdir: Path, target, verdict, edit=None):
    p = trajdir / "corrections.json"
    cur = {}
    try:
        cur = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    cur[target] = {"target": target, "verdict": verdict, "edit": edit,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    atomic_write_json(p, cur, root=trajdir.parent)


NL_PROMPT = """You translate a user's natural-language feedback about their
"Context Lens" (a compaction policy) into explicit structured correction
operations. Reply ONLY with minified JSON:
{"interpretation":["short bullet, one per distinct correction"],
 "operations":[
  {"op":"set_objective","label":""} |
  {"op":"move_item","bucket":"critical|active_context|safe_to_compress","index":0,"to":"critical|active_context|safe_to_compress"} |
  {"op":"remove_item","bucket":"","index":0} |
  {"op":"edit_item","bucket":"","index":0,"label":""} |
  {"op":"add_item","bucket":"","label":"","reason":""} |
  {"op":"exclude_topic","topic":"","note":""} |
  {"op":"set_scope","label":""} |
  {"op":"set_allocation","preserve":0,"active_context":0,"safe_to_compress":0}]}

Rules: operations must reference existing bucket/index pairs shown below.
set_allocation values are context-budget percentages and must sum to 100;
use it when the user asks for more/less weight on a category ("much more
weight to X" -> raise its bucket, lower others proportionally).
Use exclude_topic when the user says某 evidence/project is unrelated or a
separate effort. Use add_item for missing rationale/constraints. Distinguish
an experiment from the higher-level goal by editing the objective and/or
demoting the experiment item. Interpretation bullets must be faithful and
concise. Empty operations is valid if the feedback is unactionable.

CURRENT OBJECTIVE: <<OBJ>>
LENS ITEMS:
<<ITEMS>>
USER FEEDBACK: <<RAW>>"""


def effective_lens(analysis, corrections):
    """Apply user corrections (moves/removals/edits/additions/exclusions)."""
    a = analysis.get("analysis", {})
    raw = a.get("context_lens", {}) or {}
    lens, alloc = {}, {}
    for k, _, _ in BUCKETS:
        v = raw.get(k) or raw.get({"preserve": "critical"}.get(k, ""), None) or []
        if isinstance(v, dict):
            lens[k] = [dict(it) for it in (v.get("items") or [])]
            alloc[k] = v.get("allocation")
        else:
            lens[k] = [dict(it) for it in v]
            alloc[k] = None
    ov = corrections.get("_allocation")
    if isinstance(ov, dict):
        for k in alloc:
            if ov.get(k) is not None:
                alloc[k] = ov[k]
    lens["_alloc"] = alloc
    moved = []
    for bucket in [b for b, _, _ in BUCKETS if b in lens]:
        keep = []
        for i, it in enumerate(lens[bucket]):
            cor = corrections.get(f"lens:{bucket}:{i}")
            if cor:
                v = cor.get("verdict", "")
                if v == "remove":
                    continue
                if cor.get("edit"):
                    it["label"] = cor["edit"]
                it["corrected"] = True
                if v.startswith("move:"):
                    it["_to"] = _bucket(v.split(":", 1)[1])
                    moved.append(it)
                    continue
            keep.append(it)
        lens[bucket] = keep
    for it in moved:
        lens.setdefault(it.pop("_to"), []).append(it)
    for add in corrections.get("_additions", []):
        lens.setdefault(_bucket(add.get("bucket", "critical")), []).append(
            {"label": add.get("label", ""), "reason": add.get("reason", "user-stated"),
             "evidence_ids": [], "corrected": True})
    ex = [_toks(t) for t in corrections.get("_exclusions", []) if _toks(t)]
    if ex:
        for bucket in [b for b, _, _ in BUCKETS if b in lens]:
            lens[bucket] = [it for it in lens[bucket]
                            if not any(tt <= _toks(it.get("label", "") + " " +
                                                   it.get("reason", "")) for tt in ex)]
    return lens


def render(analysis, corrections, trajdir: Path, show_global=False):
    a = analysis.get("analysis", {})
    obj = a.get("current_objective", {})
    lens = effective_lens(analysis, corrections)
    when = (analysis.get("generated_at", "")[:10]) or ""
    try:
        when = datetime.fromisoformat(when).strftime("%b %-d")
    except ValueError:
        pass
    print()
    if show_global:
        # GLOBAL STATE: ranked objective stack only — no lens buckets here.
        print(bold("GLOBAL STATE"))
        print(dim(f"Updated {when} · {analysis.get('sessions_analyzed', '?')} conversations"))
        print(dim(SEP))
        objs = sorted(a.get("objectives") or [], key=lambda x: x.get("rank", 9))[:3]
        for o in objs:
            st = (o.get("status") or "").upper()
            label = " ".join((o.get("label") or "").split())
            if len(label) > 72:
                label = label[:71] + "…"
            print(f" {o.get('rank','?')}  " + (bold(st) if st == "PRIMARY" else dim(st)))
            print(f"    {label}")
        if not objs:
            print(dim(" (no objective stack in this analysis — run hc refresh)"))
        print(dim(SEP))
        return [], a.get("current_objective", {})
    print(bold("CONTEXT LENS"))
    print(dim(f"Updated {when} · derived from {analysis.get('sessions_analyzed', '?')} conversations"))
    print(dim(SEP))
    print(dim("CURRENT OBJECTIVE"))
    label = obj.get("label", "(no objective inferred)")
    ocor = corrections.get("objective")
    if ocor and ocor.get("edit"):
        print(bold(ocor["edit"]))
        print(dim(f"  (your edit — model inferred: {label})"))
    else:
        print(bold(label))
        if ocor:
            print(dim(f"  you said: {ocor['verdict']}"))
    alloc = lens.get("_alloc", {})
    if any(v is not None for v in alloc.values()):
        chars = {"preserve": "█", "active_context": "▒", "safe_to_compress": "░"}
        bar = "".join(chars[k] * max(0, round((alloc.get(k) or 0) * 0.4))
                      for k, _, _ in BUCKETS)
        print(dim(bar))
    itemmap = []
    for bucket, mark, title in BUCKETS:
        items = lens.get(bucket) or []
        if not items:
            continue
        print(dim(SEP))
        pct = alloc.get(bucket)
        head = (star(title) if bucket == "preserve" else dim(title))
        if pct is not None:
            pad = max(1, 46 - len(title))
            head += " " * pad + (bold(f"{pct}%") if bucket == "preserve" else dim(f"{pct}%"))
        print(head)
        for it in items:
            n = len(itemmap) + 1
            itemmap.append((bucket, it))
            note = dim(" (edited)") if it.get("corrected") else ""
            print(f" {mark} {it.get('label','')}{note} {dim('['+str(n)+']')}")
    print()
    print(dim(SEP))
    print(dim("[T] Test lens   [C] Correct   [E] Evidence   [Q] Quit"))
    return itemmap, obj


def show_evidence(itemmap, obj, idx, pick):
    if pick == "o":
        title, ids, reason = obj.get("label", ""), obj.get("evidence_ids", []), ""
    else:
        try:
            bucket, it = itemmap[int(pick) - 1]
        except (ValueError, IndexError):
            print(dim("  no such item")); return
        title, ids, reason = it.get("label", ""), it.get("evidence_ids", []), it.get("reason", "")
    print()
    print(bold(title))
    if reason:
        print(dim("  why: " + reason))
    shown = 0
    for i in ids[:4]:
        v = idx.get(i)
        if not v:
            continue
        shown += 1
        print(dim(f"  {v['date']} · session {v['session_id'][:8]}"))
        print(f"    “{v['text'][:160]}”")
    if not shown:
        print(dim("  no directly resolvable turns — treat with suspicion"))


def _save_raw(trajdir, entry):
    p = trajdir / "corrections.json"
    cur = {}
    try:
        cur = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    cur.setdefault("_freeform", []).append(entry)
    atomic_write_json(p, cur, root=trajdir.parent)
    return cur


def _apply_ops(trajdir, ops):
    for o in ops:
        op = o.get("op")
        if op == "set_objective":
            save_correction(trajdir, "objective", "edit", o.get("label"))
        elif op == "move_item":
            save_correction(trajdir, f"lens:{o.get('bucket')}:{o.get('index')}",
                            f"move:{o.get('to')}")
        elif op == "remove_item":
            save_correction(trajdir, f"lens:{o.get('bucket')}:{o.get('index')}", "remove")
        elif op == "edit_item":
            save_correction(trajdir, f"lens:{o.get('bucket')}:{o.get('index')}",
                            "edit", o.get("label"))
        elif op in ("set_scope", "set_allocation"):
            p = trajdir / "corrections.json"
            cur = {}
            try:
                cur = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                pass
            if op == "set_scope":
                cur["_scope"] = o.get("label", "")
            else:
                vals = {k: max(0, int(o.get(k, 0) or 0))
                        for k in ("preserve", "active_context", "safe_to_compress")}
                t = sum(vals.values()) or 1
                vals = {k: round(v * 100 / t) for k, v in vals.items()}
                vals["preserve"] += 100 - sum(vals.values())
                cur["_allocation"] = vals
            atomic_write_json(p, cur, root=trajdir.parent)
        elif op in ("add_item", "exclude_topic"):
            p = trajdir / "corrections.json"
            cur = {}
            try:
                cur = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                pass
            if op == "add_item":
                cur.setdefault("_additions", []).append(
                    {"bucket": o.get("bucket", "preserve"),
                     "label": o.get("label", ""), "reason": o.get("reason", "")})
            else:
                cur.setdefault("_exclusions", []).append(o.get("topic", ""))
            atomic_write_json(p, cur, root=trajdir.parent)


def nl_correct_flow(analysis, corrections, trajdir, provider, read):
    raw = read("  What did I get wrong, or what should matter more?\n  > ").strip()
    if not raw:
        return False          # caller falls back to item shortcuts
    a = analysis.get("analysis", {})
    lens = effective_lens(analysis, corrections)
    items = "\n".join(f"{b}:{i} — {it.get('label','')}"
                      for b, _, _ in BUCKETS for i, it in enumerate(lens.get(b) or []))
    prompt = (NL_PROMPT.replace("<<OBJ>>", a.get("current_objective", {}).get("label", ""))
                       .replace("<<ITEMS>>", items).replace("<<RAW>>", raw))
    try:
        resp = provider.generate_json(prompt + "\n\nTranslate the feedback into "
                                      "correction operations now.")
    except Exception as e:                                   # noqa: BLE001
        print(red(f"  could not interpret: {e}"))
        return True
    interp = resp.get("interpretation") or []
    ops = resp.get("operations") or []
    print()
    print(dim("  I understood that as:"))
    for b in interp:
        print(f"   • {b}")
    if not ops:
        print(dim("  (no actionable operations)"))
        _save_raw(trajdir, {"raw": raw, "interpretation": interp, "operations": [],
                            "applied": False,
                            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return True
    yn = read("  Apply? [Y/n] ").strip().lower()
    applied = yn in ("", "y", "yes")
    _save_raw(trajdir, {"raw": raw, "interpretation": interp, "operations": ops,
                        "applied": applied,
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if applied:
        _apply_ops(trajdir, ops)
        print(green("  applied — lens recomputed."))
    else:
        print(dim("  saved but not applied."))
    return True


def correct_flow(itemmap, trajdir, read):
    pick = read("  correct what? item number, or 'o' for objective: ").strip().lower()
    if pick == "o":
        v = read("  [1] correct  [2] partially  [3] wrong  [4] edit: ").strip()
        m = {"1": "correct", "2": "partially correct", "3": "wrong"}
        if v == "4":
            save_correction(trajdir, "objective", "edit", read("  your version: ").strip() or None)
        elif v in m:
            save_correction(trajdir, "objective", m[v])
        print(green("  noted."))
        return
    try:
        bucket, it = itemmap[int(pick) - 1]
    except (ValueError, IndexError):
        print(dim("  no such item")); return
    i = [j for j, (b, x) in enumerate(itemmap) if b == bucket].index(int(pick) - 1) \
        if False else None
    # index within its bucket:
    within = sum(1 for b, _ in itemmap[:int(pick) - 1] if b == bucket)
    target = f"lens:{bucket}:{within}"
    v = read("  [c]ritical [a]ctive-context [s]afe-to-compress [x] not relevant [e]dit: ").strip().lower()
    dest = {"c": "critical", "a": "active_context", "s": "safe_to_compress"}
    if v in dest:
        save_correction(trajdir, target, f"move:{dest[v]}")
    elif v == "x":
        save_correction(trajdir, target, "remove")
    elif v == "e":
        save_correction(trajdir, target, "edit", read("  your version: ").strip() or None)
    print(green("  noted — these corrections are labels for what matters."))


def _lens_text(analysis, corrections):
    lens = effective_lens(analysis, corrections)
    alloc = lens.get("_alloc", {})
    parts = []
    for bucket, _, title in BUCKETS:
        if lens.get(bucket):
            pct = alloc.get(bucket)
            head = title + (f" (~{pct}% of post-compaction budget)" if pct is not None else "")
            parts.append(head + ": " + "; ".join(it["label"] for it in lens[bucket]))
    return "\n".join(parts)


def test_flow(analysis, corrections, trajdir, provider, days, read, log=print):
    from . import discover as D
    sessions = sorted(D.discover(days), key=lambda s: -s["user_turn_count"])[:4]
    if not sessions:
        log(dim("  no conversations available")); return
    print()
    print(bold("TEST LENS") + dim(" — pick a conversation to re-compact"))
    for i, s in enumerate(sessions, 1):
        print(f"  [{i}] {s['date']} · {s['session_id'][:8]} · {s['user_turn_count']} user turns")
    pick = read("  which? [1]: ").strip() or "1"
    try:
        sess = sessions[int(pick) - 1]
    except (ValueError, IndexError):
        sess = sessions[0]
    convo = "\n".join(f"{t['role']}: {t['text']}" for t in sess["turns"])[:12000]
    lens_txt = _lens_text(analysis, corrections)
    key = hashlib.sha256((convo + lens_txt + provider.identity()).encode()).hexdigest()[:16]
    cache = trajdir / "tests" / f"{sess['session_id'][:8]}-{key}.json"
    if cache.is_file():
        r = json.loads(cache.read_text())
    else:
        log(dim("  summarizing twice (default, then lens-guided)…"))
        base = ("Summarize this conversation so that future work can continue "
                "seamlessly. Concise, at most 180 words, plain text.\n\nCONVERSATION:\n")
        default = provider.generate(base + convo).strip()
        lensed = provider.generate(
            "You are compacting a conversation for a specific person. Apply this "
            "three-level policy derived from their longitudinal history — anything "
            "under CRITICAL that appears in the conversation MUST be kept "
            "explicitly; ACTIVE CONTEXT should remain, summarized; SAFE TO "
            "COMPRESS may be dropped.\n\nPOLICY:\n" + lens_txt +
            "\n\n" + base + convo).strip()
        r = {"default": default, "lensed": lensed}
        secure_dir(cache.parent, trajdir.parent)
        atomic_write_json(cache, r, root=trajdir.parent)
    _render_compare(r["default"], r["lensed"], analysis, corrections)


def _render_compare(default, lensed, analysis, corrections):
    w = max(60, min(shutil.get_terminal_size((100, 24)).columns, 130))
    col = (w - 3) // 2
    L = textwrap.wrap(default, col) or [""]
    R = textwrap.wrap(lensed, col) or [""]
    print()
    print(bold("DEFAULT".ljust(col)) + "   " + bold("LENS-GUIDED"))
    print(dim("─" * col) + "   " + dim("─" * col))
    for a, b in zip_longest(L, R, fillvalue=""):
        print(a.ljust(col) + "   " + b)
    lens = effective_lens(analysis, corrections)
    dt, lt = _toks(default), _toks(lensed)
    kept, lost = [], []
    for bucket in ("critical",):
        for it in lens.get(bucket) or []:
            toks = _toks(it["label"] + " " + it.get("reason", ""))
            if len(toks) < 2:
                continue
            in_l = len(toks & lt) / len(toks) >= 0.4
            in_d = len(toks & dt) / len(toks) >= 0.4
            if in_l and not in_d:
                kept.append(it["label"])
            elif not in_l and not in_d:
                lost.append(it["label"])
    print()
    if kept:
        print(star("★ PRESERVED BY LENS — ABSENT FROM DEFAULT:"))
        for k in kept:
            print("   " + green("✓ ") + k)
    else:
        print(dim("no lens-exclusive preservation detected in this conversation"))
    if lost:
        print(dim("not surfaced by either (may not appear in this conversation): "
                  + "; ".join(lost[:3])))
