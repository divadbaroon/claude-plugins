#!/usr/bin/env python3
"""compact-focus-tui — full-screen ledger editor (stdlib curses, no deps).

Reads/writes ledger.json in the compact-focus state dir. Launched by the
user via `! …/compact-focus-list.sh tui` (the ! prefix attaches the
terminal; the model's Bash tool cannot run interactive programs).

Keys:
  up/down, j/k     navigate (headers are skipped)
  space            item: toggle keep <-> drop · child prompt: check/uncheck
  tab              cycle item keep -> contested -> drop
  right/l, enter   expand item (show the prompts behind it)
  left/h           collapse
  e                edit item label (footer input)
  a                add a constraint ("must not be misinterpreted…")
  q                save + finalize + quit
  Q                quit WITHOUT saving
  ?                toggle help
On quit, prints a one-line summary to stdout so the decision lands in the
conversation transcript.

Usage: compact-focus-tui.py <ledger.json> [--dump]
--dump renders the current state as plain text and exits (testing without
a TTY)."""

import curses
import json
import sys

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
        it.setdefault("expanded", False)
        it.setdefault("children", [])
        for c in it["children"]:
            c.setdefault("checked", True)
    return data


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def rows_of(data):
    """Flatten to visible rows: ('hdr', cat) | ('item', item) | ('child', item, idx)."""
    rows = []
    for cat in CATS:
        items = [it for it in data["items"] if it["cat"] == cat]
        if cat != "contested" and not items:
            rows.append(("hdr", cat))
            continue
        if items or cat == "keep":
            rows.append(("hdr", cat))
        for it in items:
            rows.append(("item", it))
            if it["expanded"]:
                for i, _ in enumerate(it["children"]):
                    rows.append(("child", it, i))
    return rows


def item_line(it):
    mark = CHECK[it["cat"] != "drop"]
    tag = f" {it['tag']} ·" if it["tag"] else ""
    exp = "▾" if it["expanded"] else ("▸" if it["children"] else " ")
    prov = f"  [{it.get('prov')}]" if it.get("prov") else ""
    return f" {mark} {exp}{tag} {it['label']}{prov}"


def dump(data):
    out = []
    for cat in CATS:
        out.append(f"## {CAT_TITLES[cat]}")
        for it in [i for i in data["items"] if i["cat"] == cat]:
            out.append(item_line(it))
            if it["expanded"]:
                for c in it["children"]:
                    out.append(f"      {CHECK[c['checked']]} {c['text']}")
    for con in data["constraints"]:
        out.append(f"> constraint: {con}")
    return "\n".join(out)


def footer_input(scr, prompt, initial=""):
    h, w = scr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    scr.move(h - 1, 0)
    scr.clrtoeol()
    scr.addstr(h - 1, 0, prompt[: w - 2], curses.A_BOLD)
    scr.refresh()
    win = curses.newwin(1, w - len(prompt) - 2, h - 1, len(prompt) + 1)
    if initial:
        win.addstr(0, 0, initial[: w - len(prompt) - 3])
    win.keypad(True)
    curses.noecho()
    buf = list(initial)
    while True:
        win.move(0, min(len(buf), win.getmaxyx()[1] - 1))
        win.clrtoeol()
        win.addstr(0, 0, "".join(buf)[-(win.getmaxyx()[1] - 1):])
        win.refresh()
        ch = win.getch()
        if ch in (10, 13):
            break
        if ch == 27:
            buf = list(initial)
            break
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= ch < 127:
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
    hdr_color = {"keep": 1, "contested": 2, "drop": 3}
    sel, off, show_help = 0, 0, False

    def selectable(rows):
        return [i for i, r in enumerate(rows) if r[0] != "hdr"]

    while True:
        rows = rows_of(data)
        sels = selectable(rows)
        if not sels:
            sel = 0
        else:
            sel = max(0, min(sel, len(sels) - 1))
        h, w = scr.getmaxyx()
        body_h = h - 3
        cur_row = sels[sel] if sels else 0
        if cur_row < off:
            off = cur_row
        if cur_row >= off + body_h:
            off = cur_row - body_h + 1
        scr.erase()
        title = " Compaction ledger — space toggle · tab category · →/← expand · e edit · a constraint · q save+quit · ? help "
        scr.addstr(0, 0, title[: w - 1], curses.A_REVERSE)
        for y, ri in enumerate(range(off, min(off + body_h, len(rows)))):
            kind = rows[ri][0]
            focused = sels and ri == sels[sel]
            attr = curses.A_REVERSE if focused else curses.A_NORMAL
            if kind == "hdr":
                cat = rows[ri][1]
                scr.addstr(y + 1, 0, f"── {CAT_TITLES[cat]} ".ljust(w - 1, "─")[: w - 1],
                           curses.color_pair(hdr_color[cat]) | curses.A_BOLD)
            elif kind == "item":
                it = rows[ri][1]
                scr.addstr(y + 1, 0, item_line(it)[: w - 1], attr)
            else:
                it, ci = rows[ri][1], rows[ri][2]
                c = it["children"][ci]
                scr.addstr(y + 1, 0, f"       {CHECK[c['checked']]} {c['text']}"[: w - 1],
                           attr | curses.color_pair(4))
        ncon = len(data["constraints"])
        status = f" {sum(1 for i in data['items'] if i['cat'] != 'drop')} keep · " \
                 f"{sum(1 for i in data['items'] if i['cat'] == 'drop')} drop · {ncon} constraint(s)"
        scr.addstr(h - 2, 0, status[: w - 1], curses.A_DIM)
        if show_help:
            scr.addstr(h - 1, 0, " space=toggle tab=cycle-cat →=expand e=edit a=constraint q=save Q=abort"[: w - 1])
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
        elif sels:
            kind = rows[sels[sel]][0]
            if kind == "item":
                it = rows[sels[sel]][1]
                if ch == ord(" "):
                    it["cat"] = "drop" if it["cat"] != "drop" else "keep"
                elif ch == 9:  # tab
                    it["cat"] = CATS[(CATS.index(it["cat"]) + 1) % len(CATS)]
                elif ch in (curses.KEY_RIGHT, ord("l"), 10, 13):
                    it["expanded"] = bool(it["children"])
                elif ch in (curses.KEY_LEFT, ord("h")):
                    it["expanded"] = False
                elif ch == ord("e"):
                    new = footer_input(scr, "label:", it["label"])
                    if new:
                        it["label"] = new
                        it["edited"] = True
            elif kind == "child":
                it, ci = rows[sels[sel]][1], rows[sels[sel]][2]
                if ch == ord(" "):
                    it["children"][ci]["checked"] = not it["children"][ci]["checked"]
                elif ch in (curses.KEY_LEFT, ord("h")):
                    it["expanded"] = False
            if ch == ord("a"):
                con = footer_input(scr, "constraint (must not be misinterpreted):")
                if con:
                    data["constraints"].append(con)


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
        dropped_children = sum(
            1 for i in state["items"] for c in i["children"]
            if i["cat"] != "drop" and not c["checked"])
        print(f"ledger finalized: {keep} keep · {drop} drop · "
              f"{dropped_children} individual prompts unchecked · "
              f"{len(state['constraints'])} constraint(s) — tell Claude to proceed")
    else:
        print("ledger NOT saved (aborted) — tell Claude to re-show or proceed with the previous state")
