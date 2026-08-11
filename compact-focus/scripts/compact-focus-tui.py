#!/usr/bin/env python3
"""compact-focus-tui — full-screen ledger editor (stdlib curses, no deps).

Runs in the SAME terminal as the Claude Code chat: the user types `! cf`
(the ! prefix attaches the terminal), the editor takes over the screen,
and q returns to the conversation with a summary line in the transcript.

Text wraps: labels, notes, and the prompts behind an item render across
as many lines as they need — nothing is truncated.

Keys:
  up/down, j/k     navigate (headers and wrapped lines are skipped)
  space            item: toggle keep <-> drop · prompt: check/uncheck
  tab              cycle item keep -> contested -> drop
  right/l, enter   expand item into the verbatim prompts behind it
  left/h           collapse
  e                edit item label   n  edit/add a note (renders wrapped)
  a                add a constraint ("must not be misinterpreted…")
  q                save + finalize + quit      Q  quit WITHOUT saving
  ?                toggle key help

Usage: compact-focus-tui.py <ledger.json> [--dump]"""

import curses
import json
import sys
import textwrap

CATS = ["keep", "contested", "drop"]
CAT_TITLES = {"keep": "KEEP", "contested": "⚡ CONTESTED", "drop": "DROP → demoted, recoverable"}
CHECK = {True: "[x]", False: "[ ]"}


def load(path):
    with open(path) as f:
        data = json.load(f)
    data.setdefault("constraints", [])
    data.setdefault("finalized", False)
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


def item_head(it):
    mark = CHECK[it["cat"] != "drop"]
    exp = "▾" if it["expanded"] else ("▸" if it["children"] else "·")
    tag = f"{it['tag']} · " if it["tag"] else ""
    prov = f"  [{it.get('prov')}]" if it.get("prov") else ""
    return f"{mark} {exp} {tag}{it['label']}{prov}"


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
                    emit(ctidx, f"{CHECK[c['checked']]} {c['text']}", "child",
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
    hdr_pair = {"keep": 1, "contested": 2, "drop": 3}
    sel, off, show_help = 0, 0, False

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
                   " ledger — space toggle · tab category · → expand · e/n edit · a constraint · q save "[: w - 1],
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
        keep_n = sum(1 for i in data["items"] if i["cat"] != "drop")
        drop_n = sum(1 for i in data["items"] if i["cat"] == "drop")
        scr.addstr(h - 2, 0,
                   f" {keep_n} keep · {drop_n} drop · {len(data['constraints'])} constraint(s)"[: w - 1],
                   curses.A_DIM)
        if show_help:
            scr.addstr(h - 1, 0,
                       " space=toggle tab=cat →=expand ←=collapse e=label n=note a=constraint q=save Q=abort"[: w - 1])
        scr.refresh()

        ch = scr.getch()
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
            if kind == "item":
                if ch == ord(" "):
                    it["cat"] = "drop" if it["cat"] != "drop" else "keep"
                elif ch == 9:
                    it["cat"] = CATS[(CATS.index(it["cat"]) + 1) % len(CATS)]
                elif ch in (curses.KEY_RIGHT, ord("l"), 10, 13):
                    it["expanded"] = bool(it["children"])
                elif ch in (curses.KEY_LEFT, ord("h")):
                    it["expanded"] = False
                elif ch == ord("e"):
                    new = footer_input(scr, "label:", it["label"])
                    if new and new != it["label"]:
                        it["label"], it["edited"] = new, True
                elif ch == ord("n"):
                    new = footer_input(scr, "note:", it.get("note", ""))
                    it["note"] = new
            else:
                if ch == ord(" "):
                    it["children"][ci]["checked"] = not it["children"][ci]["checked"]
                elif ch in (curses.KEY_LEFT, ord("h")):
                    it["expanded"] = False


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
    finalized = curses.wrapper(main, state, ledger_path)
    if finalized:
        keep = sum(1 for i in state["items"] if i["cat"] != "drop")
        drop = sum(1 for i in state["items"] if i["cat"] == "drop")
        unchecked = sum(1 for i in state["items"] for c in i["children"]
                        if i["cat"] != "drop" and not c["checked"])
        print(f"ledger finalized: {keep} keep · {drop} drop · {unchecked} prompts unchecked · "
              f"{len(state['constraints'])} constraint(s) — tell Claude to proceed")
    else:
        print("ledger NOT saved (aborted) — tell Claude to re-show or proceed with the previous state")
