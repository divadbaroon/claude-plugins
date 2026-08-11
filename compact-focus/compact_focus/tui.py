from __future__ import annotations

import copy
import curses
import textwrap
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .review import (
    change_first_percent,
    create_item,
    merge_items,
    move_source,
    record_action,
    resolve_item,
    review_errors,
    set_item_field,
    set_precommit,
    split_source,
    toggle_rule,
)
from .proposal import first_fraction_source_ids
from .state import utc_now


RETENTION_ORDER = ("preserve", "summarize", "demote")
RETENTION_TITLE = {
    "preserve": "PRESERVE — ongoing work, decisions, constraints, active artifacts",
    "summarize": "SUMMARIZE — completed or resolved work, outcome only",
    "demote": "DEMOTE — redundant or outdated; kept in searchable recovery",
}
RETENTION_MARK = {"preserve": "●", "summarize": "◐", "demote": "○"}
TYPE_ORDER = (
    "decision",
    "constraint",
    "hypothesis",
    "test",
    "result",
    "dead_end",
    "open_question",
    "artifact",
    "mechanical",
    "context",
)
STATUS_ORDER = ("active", "resolved", "unclear")


@dataclass(frozen=True)
class Target:
    kind: str
    item: Optional[int] = None
    source: Optional[int] = None
    rule: Optional[int] = None


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_add(screen: Any, y: int, x: int, value: str, width: int, attr: int = 0) -> None:
    if width <= 0 or y < 0:
        return
    clean = value.replace("\t", "  ").replace("\x00", "")
    try:
        screen.addnstr(y, x, clean, width, attr)
    except curses.error:
        pass


def _set_cursor(visibility: int) -> None:
    try:
        curses.curs_set(visibility)
    except curses.error:
        pass


def _wrapped(value: str, width: int, initial: str = "", subsequent: str = "") -> List[str]:
    return textwrap.wrap(
        _one_line(value),
        width=max(12, width),
        initial_indent=initial,
        subsequent_indent=subsequent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [initial.rstrip()]


class ReviewUI:
    def __init__(
        self,
        screen: Any,
        trace: Dict[str, Any],
        proposal: Dict[str, Any],
        review: Dict[str, Any],
        save_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.screen = screen
        self.trace = trace
        self.proposal = proposal
        self.review = review
        self.save_callback = save_callback
        self.expanded: set[str] = set()
        self.selection = 0
        self.offset = 0
        self.show_rules = True
        self.show_help = False
        self.status = ""
        self.history: List[Dict[str, Any]] = []
        self.sources = {
            source["id"]: source
            for episode in trace.get("episodes", [])
            for source in episode.get("sources", [])
            if source.get("id")
        }
        context = trace.get("context", {})
        self.window = context.get("window_tokens")
        self.colors: Dict[str, int] = {}

    def setup(self) -> None:
        _set_cursor(0)
        self.screen.keypad(True)
        try:
            curses.set_escdelay(35)
        except (AttributeError, curses.error):
            pass
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        pairs = {
            "preserve": (curses.COLOR_GREEN, -1),
            "summarize": (curses.COLOR_CYAN, -1),
            "demote": (curses.COLOR_WHITE, -1),
            "warning": (curses.COLOR_YELLOW, -1),
            "danger": (curses.COLOR_RED, -1),
            "accent": (curses.COLOR_MAGENTA, -1),
        }
        for number, (name, values) in enumerate(pairs.items(), 1):
            try:
                curses.init_pair(number, *values)
                self.colors[name] = curses.color_pair(number)
            except curses.error:
                self.colors[name] = 0

    def save(self) -> None:
        if self.save_callback:
            self.save_callback(self.review)

    def snapshot(self) -> None:
        self.history.append(copy.deepcopy(self.review))
        if len(self.history) > 80:
            self.history.pop(0)

    def undo(self) -> None:
        if not self.history:
            self.status = "Nothing to undo."
            return
        current_actions = len(self.review.get("actions", []))
        self.review.clear()
        self.review.update(self.history.pop())
        record_action(self.review, "undo", prior_action_count=current_actions)
        self.status = "Undid the last edit."
        self.save()

    def percentage_denominator(self) -> Tuple[Optional[float], str]:
        if self.window:
            return float(self.window), "window"
        observed = (self.trace.get("context") or {}).get("used_tokens_observed")
        if observed:
            return float(observed), "used"
        return None, "unknown"

    def item_pct(self, item: Dict[str, Any]) -> Optional[float]:
        denominator, _basis = self.percentage_denominator()
        if not denominator:
            return None
        tokens = sum((self.sources.get(source_id) or {}).get("tokens_estimate", 0) for source_id in item.get("source_ids", []))
        return round(tokens * 100.0 / denominator, 2)

    def source_pct(self, source: Dict[str, Any]) -> Optional[float]:
        denominator, _basis = self.percentage_denominator()
        if not denominator:
            return None
        return round(float(source.get("tokens_estimate", 0)) * 100.0 / denominator, 3)

    def class_pct(self, rule: Dict[str, Any]) -> Optional[float]:
        denominator, _basis = self.percentage_denominator()
        if not denominator:
            return None
        source_list = list(self.sources.values())
        if rule.get("id") == "first_n":
            source_order = list(self.sources)
            selected = first_fraction_source_ids(
                source_order,
                self.sources,
                float(rule.get("percent", 30)),
            )
            source_list = [source for source in source_list if source.get("id") in selected]
        else:
            source_list = [source for source in source_list if source.get("class") == rule.get("id")]
        return round(sum(source.get("tokens_estimate", 0) for source in source_list) * 100.0 / denominator, 2)

    def build_lines(self, width: int) -> Tuple[List[Target], List[Tuple[Optional[int], str, str]]]:
        targets: List[Target] = []
        lines: List[Tuple[Optional[int], str, str]] = []

        def target(value: Target) -> int:
            targets.append(value)
            return len(targets) - 1

        def emit(target_index: Optional[int], values: Sequence[str], style: str) -> None:
            for value in values:
                lines.append((target_index, value, style))

        if self.show_rules:
            lines.append((None, "── CLASS RULES · defaults only; explicit item edits win ", "header:warning"))
            for rule_index, rule in enumerate(self.review.get("class_rules", [])):
                tid = target(Target("rule", rule=rule_index))
                mark = "[x]" if rule.get("enabled") else "[ ]"
                _denominator, basis = self.percentage_denominator()
                basis_label = "window" if basis == "window" else "used"
                percent = f" · ~{self.class_pct(rule):.2f}% {basis_label}" if self.class_pct(rule) is not None else ""
                parameter = f" · {rule.get('percent', 30)}%" if rule.get("id") == "first_n" else ""
                label = f" {mark} {rule.get('label')} → {rule.get('retention')}{parameter}{percent}"
                emit(tid, _wrapped(label, width - 1, subsequent="       "), "rule")

        for retention in RETENTION_ORDER:
            lines.append((None, "── " + RETENTION_TITLE[retention] + " ", "header:" + retention))
            matching = [(index, item) for index, item in enumerate(self.review.get("items", [])) if item.get("retention") == retention]
            if not matching:
                lines.append((None, "   (empty)", "dim"))
            for item_index, item in matching:
                tid = target(Target("item", item=item_index))
                item_id = str(item.get("id"))
                arrow = "▾" if item_id in self.expanded else "▸"
                warning = " ⚠ REVIEW" if item.get("needs_review") and not item.get("reviewed") else ""
                touched = " ✎" if item.get("user_touched") else ""
                percent = self.item_pct(item)
                _denominator, basis = self.percentage_denominator()
                basis_label = "window" if basis == "window" else "used"
                pct = f" · ~{percent:.2f}% {basis_label}" if percent is not None else ""
                head = (
                    f" {RETENTION_MARK[retention]} {arrow} [{item.get('type')} · {item.get('status')}] "
                    f"{item.get('title')}{pct}{warning}{touched}"
                )
                emit(tid, _wrapped(head, width - 1, subsequent="       "), "item:" + retention)
                summary = str(item.get("summary") or "").strip()
                if summary:
                    emit(tid, _wrapped(summary, width - 8, initial="       ", subsequent="       "), "summary")
                if item.get("next_step"):
                    emit(
                        tid,
                        _wrapped(str(item["next_step"]), width - 12, initial="       next · ", subsequent="              "),
                        "next",
                    )
                if item_id in self.expanded:
                    rivals = item.get("rival_interpretations") or []
                    for rival_index, rival in enumerate(rivals, 1):
                        emit(
                            tid,
                            _wrapped(str(rival), width - 15, initial=f"       rival {rival_index} · ", subsequent="                 "),
                            "rival",
                        )
                    for source_index, source_id in enumerate(item.get("source_ids", [])):
                        source = self.sources.get(source_id, {"id": source_id, "text": "(source unavailable)"})
                        source_target = target(Target("source", item=item_index, source=source_index))
                        percent = self.source_pct(source)
                        pct = f" · ~{percent:.3f}%" if percent is not None else ""
                        meta = f"       ├─ {source_id} · {source.get('kind')} · {source.get('class')}{pct}"
                        emit(source_target, [meta], "source-meta")
                        emit(
                            source_target,
                            _wrapped(str(source.get("text") or ""), width - 12, initial="       │  ", subsequent="       │  "),
                            "source",
                        )
        return targets, lines

    def _editor_layout(self, text: str, width: int) -> Tuple[List[str], List[Tuple[int, int]]]:
        lines = [""]
        positions: List[Tuple[int, int]] = [(0, 0)]
        row = col = 0
        for char in text:
            if char == "\n":
                row += 1
                col = 0
                lines.append("")
            else:
                lines[row] += char
                col += 1
                if col >= width:
                    row += 1
                    col = 0
                    lines.append("")
            positions.append((row, col))
        return lines, positions

    def edit_text(
        self,
        title: str,
        initial: str = "",
        *,
        multiline: bool = True,
        empty_enter_saves: bool = False,
    ) -> Tuple[bool, str]:
        buffer = list(initial)
        cursor = len(buffer)
        top = 0
        _set_cursor(1)
        try:
            while True:
                height, width = self.screen.getmaxyx()
                body_width = max(10, width - 4)
                rendered, positions = self._editor_layout("".join(buffer), body_width)
                row, col = positions[cursor]
                viewport = max(2, height - 6)
                if row < top:
                    top = row
                if row >= top + viewport:
                    top = row - viewport + 1
                self.screen.erase()
                _safe_add(self.screen, 0, 0, (" " + title + " ").ljust(width - 1), width - 1, curses.A_REVERSE)
                instruction = (
                    " Ctrl-S save · Enter newline · Esc cancel · arrows move"
                    if multiline
                    else " Enter save · Esc cancel · arrows move"
                )
                _safe_add(self.screen, 1, 0, instruction, width - 1, curses.A_DIM)
                for screen_row, value in enumerate(rendered[top : top + viewport], 3):
                    _safe_add(self.screen, screen_row, 2, value, body_width)
                _safe_add(self.screen, height - 1, 0, f" {len(buffer):,} characters", width - 1, curses.A_DIM)
                with suppress_curses():
                    self.screen.move(3 + row - top, 2 + col)
                self.screen.refresh()
                key = self.screen.getch()
                if key == 19:  # Ctrl-S
                    return True, "".join(buffer).strip()
                if key == 27:
                    return False, initial
                if key in (10, 13, curses.KEY_ENTER):
                    if not multiline or (empty_enter_saves and not buffer):
                        return True, "".join(buffer).strip()
                    buffer.insert(cursor, "\n")
                    cursor += 1
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    if cursor:
                        buffer.pop(cursor - 1)
                        cursor -= 1
                elif key == curses.KEY_DC:
                    if cursor < len(buffer):
                        buffer.pop(cursor)
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(buffer), cursor + 1)
                elif key == curses.KEY_HOME:
                    while cursor and buffer[cursor - 1] != "\n":
                        cursor -= 1
                elif key == curses.KEY_END:
                    while cursor < len(buffer) and buffer[cursor] != "\n":
                        cursor += 1
                elif key == curses.KEY_UP:
                    cursor = max(0, cursor - body_width)
                elif key == curses.KEY_DOWN:
                    cursor = min(len(buffer), cursor + body_width)
                elif 32 <= key <= 0x10FFFF and key != 127:
                    try:
                        buffer.insert(cursor, chr(key))
                        cursor += 1
                    except ValueError:
                        pass
        finally:
            _set_cursor(0)

    def precommit(self) -> None:
        title = "BEFORE THE DRAFT · what would be catastrophic for the next agent to misinterpret?"
        saved, value = self.edit_text(
            title,
            str(self.review.get("precommit") or ""),
            multiline=True,
            empty_enter_saves=True,
        )
        if saved:
            set_precommit(self.review, value)
            self.save()

    def choose(self, title: str, options: Sequence[str], initial: int = 0) -> Optional[int]:
        selected = max(0, min(initial, len(options) - 1))
        offset = 0
        while True:
            height, width = self.screen.getmaxyx()
            self.screen.erase()
            _safe_add(self.screen, 0, 0, (" " + title + " ").ljust(width - 1), width - 1, curses.A_REVERSE)
            rendered: List[Tuple[int, str]] = []
            for index, option in enumerate(options):
                for line in _wrapped(option, width - 7, initial=f" {index + 1}. ", subsequent="    "):
                    rendered.append((index, line))
            first = next((index for index, (option_index, _line) in enumerate(rendered) if option_index == selected), 0)
            body = max(1, height - 3)
            if first < offset:
                offset = first
            if first >= offset + body:
                offset = first - body + 1
            for y, (option_index, line) in enumerate(rendered[offset : offset + body], 1):
                attr = curses.A_REVERSE if option_index == selected else 0
                _safe_add(self.screen, y, 0, line, width - 1, attr)
            _safe_add(self.screen, height - 1, 0, " Enter choose · Esc cancel", width - 1, curses.A_DIM)
            self.screen.refresh()
            key = self.screen.getch()
            if key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(options) - 1, selected + 1)
            elif key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (10, 13, curses.KEY_ENTER):
                return selected
            elif key == 27:
                return None

    def show_document(self, title: str, paragraphs: Sequence[str]) -> None:
        offset = 0
        while True:
            height, width = self.screen.getmaxyx()
            lines: List[str] = []
            for paragraph in paragraphs:
                lines.extend(_wrapped(paragraph, width - 4, initial="  ", subsequent="  "))
                lines.append("")
            self.screen.erase()
            _safe_add(self.screen, 0, 0, (" " + title + " ").ljust(width - 1), width - 1, curses.A_REVERSE)
            for y, line in enumerate(lines[offset : offset + height - 2], 1):
                _safe_add(self.screen, y, 0, line, width - 1)
            _safe_add(self.screen, height - 1, 0, " ↑↓ scroll · Esc return", width - 1, curses.A_DIM)
            self.screen.refresh()
            key = self.screen.getch()
            if key in (curses.KEY_DOWN, ord("j")):
                offset = min(max(0, len(lines) - (height - 2)), offset + 1)
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key in (curses.KEY_NPAGE,):
                offset = min(max(0, len(lines) - (height - 2)), offset + height - 3)
            elif key in (curses.KEY_PPAGE,):
                offset = max(0, offset - height + 3)
            elif key in (27, ord("q"), 10, 13):
                return

    def representation_view(self) -> None:
        paragraphs: List[str] = []
        for representation in self.proposal.get("representations", []):
            paragraphs.append(f"{representation.get('title')} — {representation.get('thesis')}")
            for chunk in representation.get("chunks", []):
                paragraphs.append(f"• {chunk.get('label')} [{', '.join(chunk.get('source_ids', []))}]")
        if not paragraphs:
            paragraphs = ["No rival representations are available; the conservative fallback preserved transcript episodes directly."]
        self.show_document("RIVAL REPRESENTATIONS · proposal evidence, not a decision", paragraphs)

    def context_cost_view(self) -> None:
        context = self.trace.get("context") or {}
        if self.window:
            basis = "context window"
        elif context.get("used_tokens_observed"):
            basis = "observed used context"
        else:
            basis = "estimated visible transcript only"
        paragraphs = [
            f"Each row attributes a full conversational episode—prompt plus its assistant/tool aftermath—to the prompt that started it. Percentages use {basis}.",
        ]
        for episode in self.trace.get("episodes", []):
            if self.window and episode.get("window_pct_estimate") is not None:
                share = f"~{episode['window_pct_estimate']:.2f}% window"
            elif episode.get("used_context_pct_estimate") is not None:
                share = f"~{episode['used_context_pct_estimate']:.2f}% used"
            else:
                visible = max(1, int(context.get("visible_tokens_estimate") or 0))
                share = f"~{episode.get('tokens_estimate', 0) * 100.0 / visible:.2f}% visible"
            paragraphs.append(
                f"{int(episode.get('ordinal') or 0):>3}. {share} · ~{int(episode.get('tokens_estimate') or 0):,} tokens · {episode.get('title')}"
            )
        visible_share = context.get("visible_share_of_used_pct_estimate")
        if isinstance(visible_share, (int, float)) and visible_share < 95:
            paragraphs.append(
                f"Only ~{visible_share:.2f}% of observed used context is attributable to transcript episodes. The remainder is system instructions, tool schemas, host-side context, or other hidden material; it is not assigned to a prompt."
            )
        self.show_document("PROMPT / TURN CONTEXT COSTS", paragraphs)

    def help_view(self) -> None:
        self.show_document(
            "KEYS",
            [
                "↑↓ / j k navigate. ←→ collapse or expand provenance. PgUp/PgDn scroll.",
                "p / s / d move an item to preserve, summarize, or demote. Space cycles. x changes status; t changes knowledge type.",
                "e edits a title. E edits the multiline summary. N edits the next action. r resolves a contested interpretation and offers rival readings when present.",
                "On evidence: m moves it to another item; S splits it into a new item. On an item: n creates a new item; M merges with another item; K/J reorders.",
                "g shows or hides class rules. On a rule: Space toggles it; [ and ] adjust the first-part percentage. Explicit item edits override class defaults.",
                "c shows context cost per prompt/turn. v shows rival representations. / searches the visible ledger. u undoes. Enter approves without a confirmation phrase. q cancels compaction.",
            ],
        )

    def selected_target(self, targets: Sequence[Target]) -> Optional[Target]:
        if not targets:
            return None
        self.selection = max(0, min(self.selection, len(targets) - 1))
        return targets[self.selection]

    def item_target(self, target: Target) -> Optional[int]:
        return target.item if target.kind in {"item", "source"} else None

    def find_text(self, targets: Sequence[Target]) -> None:
        saved, query = self.edit_text("SEARCH LEDGER", "", multiline=False)
        if not saved or not query:
            return
        lowered = query.lower()
        start = self.selection + 1
        indices = list(range(start, len(targets))) + list(range(0, start))
        for index in indices:
            target = targets[index]
            if target.item is None:
                continue
            item = self.review["items"][target.item]
            haystack = item.get("title", "") + " " + item.get("summary", "")
            if target.kind == "source" and target.source is not None:
                source_id = item.get("source_ids", [])[target.source]
                haystack += " " + str((self.sources.get(source_id) or {}).get("text", ""))
            if lowered in haystack.lower():
                self.selection = index
                self.status = f"Found “{query}”."
                return
        self.status = f"No match for “{query}”."

    def mutate(self, callback: Callable[[], None]) -> None:
        before = copy.deepcopy(self.review)
        try:
            callback()
        except (ValueError, IndexError) as exc:
            self.status = str(exc)
            return
        self.history.append(before)
        if len(self.history) > 80:
            self.history.pop(0)
        self.save()

    def handle_item_key(self, key: int, target: Target) -> None:
        if target.item is None:
            return
        index = target.item
        item = self.review["items"][index]
        if target.kind == "source" and target.source is not None:
            source_ids = item.get("source_ids", [])
            if target.source >= len(source_ids):
                return
            source_id = source_ids[target.source]
            if key == ord("m"):
                options = [f"{candidate.get('retention')} · {candidate.get('title')}" for candidate in self.review["items"]]
                chosen = self.choose("MOVE EVIDENCE TO", options, index)
                if chosen is not None:
                    self.mutate(lambda: move_source(self.review, source_id, chosen))
            elif key == ord("S"):
                saved, title = self.edit_text("NEW ITEM TITLE", str(item.get("title") or "") + " — split", multiline=False)
                if saved and title:
                    self.mutate(lambda: split_source(self.review, source_id, title, index))
            return

        if key in (ord("p"), ord("s"), ord("d"), ord(" ")):
            if key == ord(" "):
                current = RETENTION_ORDER.index(item.get("retention", "preserve"))
                value = RETENTION_ORDER[(current + 1) % len(RETENTION_ORDER)]
            else:
                value = {ord("p"): "preserve", ord("s"): "summarize", ord("d"): "demote"}[key]
            self.mutate(lambda: set_item_field(self.review, index, "retention", value))
        elif key == ord("x"):
            current = STATUS_ORDER.index(item.get("status", "unclear"))
            value = STATUS_ORDER[(current + 1) % len(STATUS_ORDER)]
            self.mutate(lambda: set_item_field(self.review, index, "status", value))
        elif key == ord("t"):
            current = TYPE_ORDER.index(item.get("type", "context"))
            value = TYPE_ORDER[(current + 1) % len(TYPE_ORDER)]
            self.mutate(lambda: set_item_field(self.review, index, "type", value))
        elif key == ord("r"):
            rivals = item.get("rival_interpretations") or []
            choice = self.choose("RESOLVE INTERPRETATION", [str(value) for value in rivals] + ["Keep the current summary"], len(rivals))
            if choice is not None:
                rival = choice if choice < len(rivals) else None
                self.mutate(lambda: resolve_item(self.review, index, rival))
        elif key == ord("e"):
            saved, value = self.edit_text("EDIT TITLE", str(item.get("title") or ""), multiline=False)
            if saved and value:
                self.mutate(lambda: set_item_field(self.review, index, "title", value))
        elif key == ord("E"):
            saved, value = self.edit_text("EDIT COMPACTION SUMMARY", str(item.get("summary") or ""), multiline=True)
            if saved:
                self.mutate(lambda: set_item_field(self.review, index, "summary", value))
        elif key == ord("N"):
            saved, value = self.edit_text("EDIT NEXT ACTION", str(item.get("next_step") or ""), multiline=True)
            if saved:
                self.mutate(lambda: set_item_field(self.review, index, "next_step", value))
        elif key == ord("n"):
            saved, title = self.edit_text("CREATE ITEM", "", multiline=False)
            if saved and title:
                self.mutate(lambda: create_item(self.review, title, retention=item.get("retention", "preserve"), after=index))
        elif key == ord("M") and len(self.review["items"]) > 1:
            candidates = [(candidate_index, candidate) for candidate_index, candidate in enumerate(self.review["items"]) if candidate_index != index]
            chosen = self.choose("MERGE CURRENT ITEM INTO", [f"{candidate.get('retention')} · {candidate.get('title')}" for _candidate_index, candidate in candidates])
            if chosen is not None:
                other = candidates[chosen][0]
                self.mutate(lambda: merge_items(self.review, index, other))
        elif key in (ord("K"), ord("J")):
            other = index - 1 if key == ord("K") else index + 1
            if 0 <= other < len(self.review["items"]):
                def reorder() -> None:
                    self.review["items"][index], self.review["items"][other] = self.review["items"][other], self.review["items"][index]
                    record_action(self.review, "reorder_items", first=index, second=other)
                self.mutate(reorder)

    def run(self) -> bool:
        self.setup()
        self.precommit()
        while True:
            height, width = self.screen.getmaxyx()
            if height < 12 or width < 54:
                self.screen.erase()
                _safe_add(self.screen, 0, 0, " compact focus · terminal too small", width - 1, curses.A_REVERSE)
                _safe_add(self.screen, 2, 1, "Resize to at least 54×12. q cancels compaction.", width - 2)
                self.screen.refresh()
                key = self.screen.getch()
                if key == ord("q"):
                    self.review["outcome"] = "cancelled"
                    self.save()
                    return False
                continue

            targets, lines = self.build_lines(width)
            target = self.selected_target(targets)
            first_line = next((index for index, (target_index, _text, _style) in enumerate(lines) if target_index == self.selection), 0)
            body_height = height - 4
            if first_line < self.offset:
                self.offset = first_line
            if first_line >= self.offset + body_height:
                self.offset = first_line - body_height + 1
            self.offset = max(0, min(self.offset, max(0, len(lines) - body_height)))

            self.screen.erase()
            context = self.trace.get("context", {})
            observed = context.get("used_pct_observed")
            if isinstance(observed, (int, float)):
                context_label = f" · {observed:.1f}% window used"
            elif context.get("used_tokens_observed"):
                context_label = f" · {int(context['used_tokens_observed']) / 1000:.0f}k tokens · window unknown"
            else:
                context_label = " · context size unavailable"
            title = " compact focus · review before this /compact" + context_label
            _safe_add(self.screen, 0, 0, title.ljust(width - 1), width - 1, curses.A_REVERSE)
            for y, (target_index, text, style) in enumerate(lines[self.offset : self.offset + body_height], 1):
                selected = target_index is not None and target_index == self.selection
                attr = curses.A_REVERSE if selected else 0
                if style.startswith("header:"):
                    name = style.split(":", 1)[1]
                    attr = self.colors.get(name, 0) | curses.A_BOLD
                    text = text.ljust(width - 1, "─")
                elif style.startswith("item:"):
                    attr |= self.colors.get(style.split(":", 1)[1], 0) | curses.A_BOLD
                elif style in {"summary", "next", "dim"}:
                    attr |= curses.A_DIM
                elif style in {"rival", "rule"}:
                    attr |= self.colors.get("warning", 0)
                elif style.startswith("source"):
                    attr |= self.colors.get("accent", 0)
                _safe_add(self.screen, y, 0, text, width - 1, attr)

            errors = review_errors(self.trace, self.review)
            unresolved = sum(1 for error in errors if "contested" in error)
            counts = {retention: sum(1 for item in self.review["items"] if item.get("retention") == retention) for retention in RETENTION_ORDER}
            summary = (
                f" {counts['preserve']} preserve · {counts['summarize']} summarize · {counts['demote']} demote"
                f" · {unresolved} unresolved · {len(self.review.get('actions', []))} edits"
            )
            _safe_add(self.screen, height - 3, 0, summary, width - 1, curses.A_DIM)
            if self.status:
                footer = " " + self.status
            elif self.show_help:
                footer = " p/s/d retain · e/E edit · ←/→ provenance · m move · S split · M merge · c costs · Enter approve"
            else:
                footer = " ↑↓ navigate · ←/→ evidence · p/s/d retain · e/E edit · c costs · ? keys · Enter approve · q cancel"
            _safe_add(self.screen, height - 2, 0, footer, width - 1)
            warning = self.proposal.get("warnings", [])
            visible_share = context.get("visible_share_of_used_pct_estimate")
            if isinstance(visible_share, (int, float)) and visible_share < 95:
                bottom_text = (
                    f"Ledger attributes ~{visible_share:.2f}% of used tokens; system prompts, tools, and host compaction are outside item shares."
                )
                warning_style = True
            elif warning:
                bottom_text = str(warning[0])
                warning_style = True
            else:
                bottom_text = "Nothing is deleted; demoted evidence remains searchable."
                warning_style = False
            _safe_add(
                self.screen,
                height - 1,
                0,
                " " + bottom_text,
                width - 1,
                self.colors.get("warning", 0) if warning_style else curses.A_DIM,
            )
            self.screen.refresh()
            self.status = ""
            key = self.screen.getch()

            if key in (curses.KEY_DOWN, ord("j")):
                self.selection = min(max(0, len(targets) - 1), self.selection + 1)
            elif key in (curses.KEY_UP, ord("k")):
                self.selection = max(0, self.selection - 1)
            elif key == curses.KEY_NPAGE:
                self.selection = min(max(0, len(targets) - 1), self.selection + max(1, body_height // 2))
            elif key == curses.KEY_PPAGE:
                self.selection = max(0, self.selection - max(1, body_height // 2))
            elif key == curses.KEY_RIGHT and target and target.item is not None:
                self.expanded.add(str(self.review["items"][target.item].get("id")))
            elif key == curses.KEY_LEFT and target and target.item is not None:
                self.expanded.discard(str(self.review["items"][target.item].get("id")))
            elif key == ord("g"):
                self.show_rules = not self.show_rules
            elif key == ord("?"):
                self.help_view()
            elif key == ord("v"):
                self.representation_view()
            elif key == ord("c"):
                self.context_cost_view()
            elif key == ord("/"):
                self.find_text(targets)
            elif key == ord("u"):
                self.undo()
            elif key == ord("q"):
                self.review["outcome"] = "cancelled"
                self.review["completed_at"] = utc_now()
                record_action(self.review, "cancel")
                self.save()
                return False
            elif key == 27:
                self.status = "Escape ignored here; press q to cancel compaction."
            elif key in (10, 13, curses.KEY_ENTER):
                errors = review_errors(self.trace, self.review)
                if errors:
                    contested = next((error for error in errors if "contested" in error), errors[0])
                    self.status = "Cannot compact: " + contested
                    if "item " in contested:
                        try:
                            item_number = int(contested.split("item ", 1)[1].split(" ", 1)[0]) - 1
                            self.selection = next(
                                index for index, value in enumerate(targets) if value.item == item_number and value.kind == "item"
                            )
                        except (ValueError, StopIteration):
                            pass
                else:
                    self.review["outcome"] = "approved"
                    self.review["completed_at"] = utc_now()
                    record_action(self.review, "approve")
                    self.save()
                    return True
            elif target and target.kind == "rule" and target.rule is not None:
                if key == ord(" "):
                    self.mutate(lambda: toggle_rule(self.review, target.rule or 0, self.trace))
                elif key in (ord("["), ord("]")) and self.review["class_rules"][target.rule].get("id") == "first_n":
                    delta = -5 if key == ord("[") else 5
                    self.mutate(lambda: change_first_percent(self.review, delta, self.trace))
            elif target:
                self.handle_item_key(key, target)


class suppress_curses:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: Any) -> bool:
        return True


def run_review(
    trace: Dict[str, Any],
    proposal: Dict[str, Any],
    review: Dict[str, Any],
    save_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> bool:
    def main(screen: Any) -> bool:
        return ReviewUI(screen, trace, proposal, review, save_callback).run()

    return bool(curses.wrapper(main))
