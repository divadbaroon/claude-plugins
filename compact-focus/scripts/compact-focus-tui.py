#!/usr/bin/env python3
"""compact-focus-tui — full-screen ledger editor (stdlib curses, no deps).

Runs in the SAME terminal as the Claude Code chat: the user types `! cf`
(the ! prefix attaches the terminal), the editor takes over the screen,
and q returns to the conversation with a summary line in the transcript.

Text wraps: labels, notes, and the prompts behind an item render across
as many lines as they need — nothing is truncated.

Keys:
  up/down, j/k        navigate (headers and wrapped lines are skipped)
  shift/opt/ctrl + up/down (or K/J) move the item one CATEGORY tier
                      (up = toward preserve, down = toward remove).
                      cmd+arrows never reach terminal apps; ctrl+arrows
                      reach them only if macOS Mission Control shortcuts
                      are off — shift/option always arrive.
  left                expand item into the verbatim prompts behind it
  right               collapse
  space               select/deselect (item: keep<->remove · prompt: check)
  tab                 cycle category (fallback)
  e / n               edit label / note      a  add a constraint
  enter               CONFIRM + submit to the compaction process
  q                   save + quit            Q  quit WITHOUT saving
  ?                   toggle key help

Usage: compact-focus-tui.py <ledger.json> [--dump]"""

import curses
import json
import sys
import textwrap

CATS = ["keep", "summarize", "contested", "drop"]
CAT_TITLES = {
    "keep": "PRESERVE — critical ongoing work, recent decisions, active files",
    "summarize": "SUMMARIZE — completed tasks, resolved issues, older discussion",
    "contested": "⚡ CONTESTED — you decide",
    "drop": "REMOVE — redundant / outdated → demoted, recoverable",
}
CAT_MARK = {"keep": "[x]", "summarize": "[~]", "contested": "[?]", "drop": "[ ]"}
CHECK = {True: "[x]", False: "[ ]"}


DEFAULT_CLASSES = [
    {"id": "first_n", "label": "first {n}% of session (detail beyond decisions)", "state": "keep", "n": 30},
    {"id": "file_changes", "label": "file-change detail (diffs, edit contents)", "state": "keep"},
    {"id": "subagents", "label": "subagent transcripts", "state": "keep"},
    {"id": "todos", "label": "todo/task bookkeeping", "state": "keep"},
]
CLASS_STATES = {"todos": ["keep", "drop"]}


def class_states(c):
    return CLASS_STATES.get(c["id"], ["keep", "summarize", "drop"])


def class_line(c):
    label = c["label"].replace("{n}", str(c.get("n", "")))
    pct = f"  ~{c['pct']:.1f}%" if isinstance(c.get("pct"), (int, float)) else ""
    extra = "  (e edits N)" if c["id"] == "first_n" else ""
    return f"[{c['state']:^9}] {label}{pct}{extra}"


def load(path):
    with open(path) as f:
        data = json.load(f)
    data.setdefault("constraints", [])
    data.setdefault("finalized", False)
    if not data.get("classes"):
        data["classes"] = [dict(c) for c in DEFAULT_CLASSES]
    defaults = {c["id"]: c for c in DEFAULT_CLASSES}
    for c in data["classes"]:
        d = defaults.get(c.get("id"), {})
        for k, v in d.items():
            c.setdefault(k, v)
        c.setdefault("label", c.get("id", "?"))
        c.setdefault("state", "keep")
    for it in data.get("items", []):
        it.setdefault("cat", "keep")
        it.setdefault("tag", "")
        it.setdefault("note", "")
        it.setdefault("expanded", False)
        it.setdefault("children", [])
        for c in it["children"]:
            c.setdefault("checked", True)
    return data


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def pct_of(x):
    p = x.get("pct")
    return f"  {p:.1f}%" if isinstance(p, (int, float)) else ""


def item_head(it):
    mark = CAT_MARK.get(it["cat"], "[ ]")
    exp = "▾" if it["expanded"] else ("▸" if it["children"] else "·")
    tag = f"{it['tag']} · " if it["tag"] else ""
    prov = f"  [{it.get('prov')}]" if it.get("prov") else ""
    pct = it.get("pct")
    if pct is None and it["children"]:
        vals = [c.get("pct") for c in it["children"] if isinstance(c.get("pct"), (int, float))]
        if vals:
            pct = round(sum(vals), 1)
    ptxt = f"  ~{pct:.1f}% ctx" if isinstance(pct, (int, float)) else ""
    return f"{mark} {exp} {tag}{it['label']}{prov}{ptxt}"


def build_display(data, width):
    """targets: navigable units [(kind, item, child_idx|None)]
       display: [(target_idx|None, text, style)] — one tuple per SCREEN line;
       wrapped continuations carry the same target_idx."""
    targets, display = [], []

    def emit(tidx, text, style, first_indent, cont_indent):
        lines = textwrap.wrap(text, max(20, width - 1),
                              initial_indent=first_indent,
                              subsequent_indent=cont_indent) or [first_indent]
        for ln in lines:
            display.append((tidx, ln, style))

    if data.get("classes"):
        display.append((None, "── CLASS RULES (whole categories of content) ", "hdr:contested"))
        for c in data["classes"]:
            tidx = len(targets)
            targets.append(("class", c, None))
            emit(tidx, class_line(c), "item", " ", "            ")

    for cat in CATS:
        items = [it for it in data["items"] if it["cat"] == cat]
        if not items and cat == "contested":
            continue
        display.append((None, f"── {CAT_TITLES[cat]} ", "hdr:" + cat))
        for it in items:
            tidx = len(targets)
            targets.append(("item", it, None))
            emit(tidx, item_head(it), "item", " ", "       ")
            if it.get("note"):
                emit(tidx, it["note"], "note", "       ↳ ", "         ")
            if it["expanded"]:
                for ci, c in enumerate(it["children"]):
                    ctidx = len(targets)
                    targets.append(("child", it, ci))
                    emit(ctidx, f"{CHECK[c['checked']]}{pct_of(c)} {c['text']}", "child",
                         "        ", "            ")
    return targets, display


def dump(data):
    targets, display = build_display(data, 78)
    out = [ln for _, ln, _ in display]
    for con in data["constraints"]:
        out.append(f"> constraint: {con}")
    return "\n".join(out)


def footer_input(scr, prompt, initial=""):
    h, w = scr.getmaxyx()
    curses.curs_set(1)
    buf = list(initial)
    while True:
        scr.move(h - 1, 0)
        scr.clrtoeol()
        line = f"{prompt} {''.join(buf)}"
        scr.addstr(h - 1, 0, line[-(w - 2):], curses.A_BOLD)
        scr.refresh()
        ch = scr.getch()
        if ch in (10, 13):
            break
        if ch == 27:
            buf = list(initial)
            break
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= ch < 256 and ch != 127:
            buf.append(chr(ch))
    curses.curs_set(0)
    return "".join(buf).strip()


def main(scr, data, path):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    hdr_pair = {"keep": 1, "summarize": 4, "contested": 2, "drop": 3}
    sel, off, show_help = 0, 0, False

    def read_key():
        ch = scr.getch()
        if ch != 27:
            return ch
        scr.nodelay(True)
        seq = ""
        while True:
            c = scr.getch()
            if c == -1 or len(seq) > 6:
                break
            seq += chr(c) if 0 <= c < 256 else ""
        scr.nodelay(False)
        if seq.endswith("A"):
            return "CHORD_UP"      # ESC[1;3A / 1;9A etc: option/alt + up
        if seq.endswith("B"):
            return "CHORD_DOWN"
        return 27

    def move_tier(it, direction):
        i = CATS.index(it["cat"]) + direction
        it["cat"] = CATS[max(0, min(len(CATS) - 1, i))]

    while True:
        h, w = scr.getmaxyx()
        targets, display = build_display(data, w)
        if targets:
            sel = max(0, min(sel, len(targets) - 1))
        first_line = next((i for i, (t, _, _) in enumerate(display) if t == sel), 0)
        body_h = h - 3
        if first_line < off:
            off = first_line
        if first_line >= off + body_h:
            off = first_line - body_h + 1
        scr.erase()
        scr.addstr(0, 0,
                   " ledger — ⇧/⌥/^↑↓ move category · ← expand · space select · enter submit · e/n edit · a constraint "[: w - 1],
                   curses.A_REVERSE)
        for y, di in enumerate(range(off, min(off + body_h, len(display)))):
            tidx, text, style = display[di]
            focused = targets and tidx == sel
            attr = curses.A_REVERSE if focused else curses.A_NORMAL
            if style.startswith("hdr:"):
                scr.addstr(y + 1, 0, text.ljust(w - 1, "─")[: w - 1],
                           curses.color_pair(hdr_pair[style[4:]]) | curses.A_BOLD)
            elif style == "note":
                scr.addstr(y + 1, 0, text[: w - 1], attr | curses.A_DIM)
            elif style == "child":
                scr.addstr(y + 1, 0, text[: w - 1], attr | curses.color_pair(4))
            else:
                scr.addstr(y + 1, 0, text[: w - 1], attr)
        counts = {c: sum(1 for i in data["items"] if i["cat"] == c) for c in CATS}
        scr.addstr(h - 2, 0,
                   f" {counts['keep']} preserve · {counts['summarize']} summarize · "
                   f"{counts['contested']} contested · {counts['drop']} remove · "
                   f"{len(data['constraints'])} constraint(s)"[: w - 1],
                   curses.A_DIM)
        if show_help:
            scr.addstr(h - 1, 0,
                       " ⇧/⌥↑↓ or K/J=move category · ←=expand →=collapse · space=select · enter=submit · e/n/a edit · q=save Q=abort"[: w - 1])
        scr.refresh()

        ch = read_key()
        SR = getattr(curses, "KEY_SR", -9)
        SF = getattr(curses, "KEY_SF", -9)
        chord_up = ch in ("CHORD_UP", SR, ord("K"))
        chord_down = ch in ("CHORD_DOWN", SF, ord("J"))
        if ch in (10, 13, curses.KEY_ENTER):
            yn = footer_input(scr, "Submit ledger to the compaction process? type y to confirm:")
            if yn.lower().startswith("y"):
                data["finalized"] = True
                save(path, data)
                return True
            continue
        if ch == ord("q"):
            data["finalized"] = True
            save(path, data)
            return True
        if ch == ord("Q"):
            return False
        if ch == ord("?"):
            show_help = not show_help
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel += 1
        elif ch in (curses.KEY_UP, ord("k")):
            sel -= 1
        elif ch == ord("a"):
            con = footer_input(scr, "constraint (must not be misinterpreted):")
            if con:
                data["constraints"].append(con)
        elif targets:
            kind, it, ci = targets[sel]
            if kind == "class":
                states = class_states(it)
                if chord_up or chord_down:
                    i = states.index(it["state"]) + (-1 if chord_up else 1)
                    it["state"] = states[max(0, min(len(states) - 1, i))]
                elif ch == ord(" "):
                    it["state"] = "drop" if it["state"] == "keep" else "keep"
                elif ch == 9:
                    it["state"] = states[(states.index(it["state"]) + 1) % len(states)]
                elif ch == ord("e") and it["id"] == "first_n":
                    new = footer_input(scr, "N (percent of session):", str(it.get("n", 30)))
                    if new.isdigit() and 0 < int(new) <= 100:
                        it["n"] = int(new)
            elif kind == "item":
                if chord_up:
                    move_tier(it, -1)
                elif chord_down:
                    move_tier(it, +1)
                elif ch == ord(" "):
                    it["cat"] = "drop" if it["cat"] != "drop" else "keep"
                elif ch == 9:
                    it["cat"] = CATS[(CATS.index(it["cat"]) + 1) % len(CATS)]
                elif ch == curses.KEY_LEFT:
                    it["expanded"] = bool(it["children"])
                elif ch == curses.KEY_RIGHT:
                    it["expanded"] = False
                elif ch == ord("e"):
                    new = footer_input(scr, "label:", it["label"])
                    if new and new != it["label"]:
                        it["label"], it["edited"] = new, True
                elif ch == ord("n"):
                    new = footer_input(scr, "note:", it.get("note", ""))
                    it["note"] = new
            else:
                if chord_up or chord_down:
                    move_tier(it, -1 if chord_up else +1)
                elif ch == ord(" "):
                    it["children"][ci]["checked"] = not it["children"][ci]["checked"]
                elif ch == curses.KEY_RIGHT:
                    it["expanded"] = False


def render_lines(data, width=78):
    """Line-mode render: numbered, wrapped, with classes as C1.., items 1..,
    children i.j — the multiline view for TTY-less shells."""
    out = []
    for i, c in enumerate(data.get("classes", []), 1):
        out += textwrap.wrap(f"C{i} {class_line(c)}", width, subsequent_indent="      ")
    for cat in CATS:
        items = [(n, it) for n, it in enumerate(data["items"], 1) if it["cat"] == cat]
        if not items and cat == "contested":
            continue
        out.append(f"── {CAT_TITLES[cat]}")
        for n, it in items:
            out += textwrap.wrap(f"{n:>2} {item_head(it)}", width, subsequent_indent="      ")
            if it.get("note"):
                out += textwrap.wrap(f"   ↳ {it['note']}", width, subsequent_indent="     ")
            if it["expanded"]:
                for j, ch in enumerate(it["children"], 1):
                    out += textwrap.wrap(f"   {n}.{j} {CHECK[ch['checked']]}{pct_of(ch)} {ch['text']}",
                                         width, subsequent_indent="        ")
    for con in data["constraints"]:
        out.append(f"> constraint: {con}")
    return "\n".join(out)


def line_mode(data, path):
    """Interactive editor over plain stdin/stdout — works in Claude Code's
    bang shell, which pipes stdio (no TTY, so curses cannot run there).
    Commands: 3=cycle category · 3k/3s/3c/3d=set · x3=expand · 3.2=toggle
    prompt · e3 <label> · n3 <note> · a <constraint> · N <pct> · C2=cycle
    class · p=reprint · q=save · Q=abort"""
    print(render_lines(data))
    print("commands: 3=cycle · 3k/3s/3c/3d=set · x3=expand · 3.2=toggle prompt · "
          "e3/n3 <text> · a <text> · N <pct> · C2=class · p=print · q=save · Q=abort")
    setcat = {"k": "keep", "s": "summarize", "c": "contested", "d": "drop"}
    while True:
        try:
            cmd = input("ledger> ").strip()
        except EOFError:
            print("(stdin closed — saving as-is)")
            data["finalized"] = True
            save(path, data)
            return True
        if not cmd:
            continue
        if cmd == "q":
            data["finalized"] = True
            save(path, data)
            return True
        if cmd == "Q":
            return False
        if cmd == "p":
            print(render_lines(data))
            continue
        try:
            if cmd.startswith("a "):
                data["constraints"].append(cmd[2:].strip())
            elif cmd.startswith("N "):
                n = int(cmd[2:].strip())
                for c in data["classes"]:
                    if c["id"] == "first_n" and 0 < n <= 100:
                        c["n"] = n
            elif cmd[0] == "C" and cmd[1:].isdigit():
                c = data["classes"][int(cmd[1:]) - 1]
                states = class_states(c)
                c["state"] = states[(states.index(c["state"]) + 1) % len(states)]
            elif cmd[0] == "x" and cmd[1:].isdigit():
                it = data["items"][int(cmd[1:]) - 1]
                it["expanded"] = not it["expanded"] and bool(it["children"])
            elif cmd[0] in "en" and " " in cmd and cmd[1:cmd.index(" ")].isdigit():
                idx, text = int(cmd[1:cmd.index(" ")]) - 1, cmd[cmd.index(" ") + 1:].strip()
                key = "label" if cmd[0] == "e" else "note"
                data["items"][idx][key] = text
                if cmd[0] == "e":
                    data["items"][idx]["edited"] = True
            elif "." in cmd:
                a, b = cmd.split(".", 1)
                ch = data["items"][int(a) - 1]["children"][int(b) - 1]
                ch["checked"] = not ch["checked"]
            elif cmd[:-1].isdigit() and cmd[-1] in setcat:
                data["items"][int(cmd[:-1]) - 1]["cat"] = setcat[cmd[-1]]
            elif cmd.isdigit():
                it = data["items"][int(cmd) - 1]
                it["cat"] = CATS[(CATS.index(it["cat"]) + 1) % len(CATS)]
            else:
                print("(?)  3=cycle · 3s=set · x3 · 3.2 · e3/n3 <text> · a <text> · N <pct> · C2 · p · q · Q")
                continue
        except (ValueError, IndexError):
            print("(no such number)")
            continue
        print(render_lines(data))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: compact-focus-tui.py <ledger.json> [--dump]")
        sys.exit(1)
    ledger_path = sys.argv[1]
    try:
        state = load(ledger_path)
    except Exception as e:
        print(f"(could not read {ledger_path}: {e})")
        sys.exit(1)
    if "--dump" in sys.argv:
        print(dump(state))
        sys.exit(0)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        finalized = line_mode(state, ledger_path)
    else:
        try:
            finalized = curses.wrapper(main, state, ledger_path)
        except curses.error:
            finalized = line_mode(state, ledger_path)
    if finalized:
        keep = sum(1 for i in state["items"] if i["cat"] in ("keep", "contested"))
        summ = sum(1 for i in state["items"] if i["cat"] == "summarize")
        drop = sum(1 for i in state["items"] if i["cat"] == "drop")
        unchecked = sum(1 for i in state["items"] for c in i["children"]
                        if i["cat"] != "drop" and not c["checked"])
        rules = [f"{c['id']}={c['state']}" + (f"(n={c['n']})" if c["id"] == "first_n" else "")
                 for c in state.get("classes", []) if c["state"] != "keep"]
        rtxt = f" · class rules: {', '.join(rules)}" if rules else ""
        print(f"ledger finalized: {keep} preserve · {summ} summarize · {drop} remove · "
              f"{unchecked} prompts unchecked · "
              f"{len(state['constraints'])} constraint(s){rtxt} — tell Claude to proceed")
    else:
        print("ledger NOT saved (aborted) — tell Claude to re-show or proceed with the previous state")
