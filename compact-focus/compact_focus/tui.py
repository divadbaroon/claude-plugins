from __future__ import annotations

import copy
import curses
import textwrap
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .draft import (
    DraftError,
    apply_generated_summary,
    apply_revision,
    approve_draft,
    draft_max_chars,
    edit_draft,
    ensure_draft,
    run_revision_worker,
    run_summary_worker,
)
from .proposal import first_fraction_source_ids
from .review import (
    WORK_STATES,
    add_clarification,
    change_first_percent,
    create_item,
    effective_source_retention,
    ensure_review_shape,
    invalidate_draft,
    merge_items,
    move_source,
    record_action,
    resolve_item,
    review_errors,
    set_item_field,
    set_precommit,
    set_source_retention,
    set_work_state,
    split_source,
    toggle_rule,
)
from .state import utc_now


RETENTION_ORDER = ("preserve", "summarize", "demote")
RETENTION_LABEL = {
    "preserve": "PRESERVE",
    "summarize": "COMPACT",
    "demote": "DELETE",
}
RETENTION_MARK = {"preserve": "●", "summarize": "◐", "demote": "○"}
WORK_STATE_LABEL = {
    "todo": "TODO",
    "in_progress": "IN PROGRESS",
    "done": "DONE",
    "blocked": "BLOCKED",
}
KIND_LABEL = {
    "user_prompt": "PROMPT",
    "assistant_text": "ASSISTANT",
    "tool_use": "TOOL CALL",
    "tool_result": "TOOL RESULT",
    "subagent": "SUBAGENT",
    "subagent_transcript": "SUBAGENT",
    "compact_summary": "PRIOR SUMMARY",
    "message": "MESSAGE",
}
VISIBLE_SOURCE_KINDS = {"user_prompt"}
VISIBLE_SOURCE_CLASSES = {"subagents"}


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


def _is_delete_key(key: int) -> bool:
    """Recognize delete variants emitted by common terminal/terminfo pairs."""
    if key in (ord("d"), 4, 8, 127, curses.KEY_DC):
        return True
    try:
        name = curses.keyname(key)
    except (curses.error, ValueError):
        return False
    return bool(name and (name == b"KEY_DC" or name.startswith(b"kDC")))


def _is_visible_source(source: Dict[str, Any]) -> bool:
    return source.get("kind") in VISIBLE_SOURCE_KINDS or source.get("class") in VISIBLE_SOURCE_CLASSES


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
        ensure_review_shape(self.review)
        self.save_callback = save_callback
        first = next(iter(self.review.get("items", [])), {})
        self.expanded: set[str] = {str(first.get("id"))} if first.get("id") else set()
        self.expanded_sources: set[str] = set()
        self.selection = 0
        self.offset = 0
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
            if not curses.has_colors():
                return
            curses.start_color()
        except curses.error:
            return
        max_pairs = int(getattr(curses, "COLOR_PAIRS", 0) or 0)
        if max_pairs <= 1:
            return
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
            "done": (curses.COLOR_GREEN, -1),
            "blocked": (curses.COLOR_RED, -1),
        }
        for number, (name, values) in enumerate(pairs.items(), 1):
            if number >= max_pairs:
                break
            try:
                curses.init_pair(number, *values)
                self.colors[name] = curses.color_pair(number)
            except (curses.error, ValueError):
                self.colors[name] = 0

    def save(self) -> None:
        if self.save_callback:
            self.save_callback(self.review)

    def mutate(self, callback: Callable[[], None]) -> None:
        before = copy.deepcopy(self.review)
        try:
            callback()
        except (ValueError, IndexError, DraftError) as exc:
            self.status = str(exc)
            return
        self.history.append(before)
        if len(self.history) > 80:
            self.history.pop(0)
        self.save()

    def undo(self) -> None:
        if not self.history:
            self.status = "Nothing to undo."
            return
        current_actions = len(self.review.get("actions", []))
        self.review.clear()
        self.review.update(self.history.pop())
        ensure_review_shape(self.review)
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
        tokens = sum(
            (self.sources.get(source_id) or {}).get("tokens_estimate", 0)
            for source_id in item.get("source_ids", [])
        )
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
        return round(
            sum(source.get("tokens_estimate", 0) for source in source_list) * 100.0 / denominator,
            2,
        )

    def _source_kind(self, source: Dict[str, Any]) -> str:
        if source.get("class") == "file_changes":
            return "FILE CHANGE"
        kind = str(source.get("kind") or "source")
        return KIND_LABEL.get(kind, kind.replace("_", " ").upper())

    def _inventory(self, item: Dict[str, Any]) -> str:
        counts: Dict[str, int] = {}
        for source_id in item.get("source_ids", []):
            source = self.sources.get(source_id, {})
            if not _is_visible_source(source):
                continue
            label = self._source_kind(source).lower()
            counts[label] = counts.get(label, 0) + 1
        parts = [f"{value} {key}{'' if value == 1 else 's'}" for key, value in counts.items()]
        visible_count = sum(counts.values())
        unit_label = "unit" if visible_count == 1 else "units"
        return f"{visible_count} reviewable {unit_label}" + (" · " + " · ".join(parts) if parts else "")

    def build_lines(self, width: int) -> Tuple[List[Target], List[Tuple[Optional[int], str, str]]]:
        targets: List[Target] = []
        lines: List[Tuple[Optional[int], str, str]] = []

        def target(value: Target) -> int:
            targets.append(value)
            return len(targets) - 1

        def emit(target_index: Optional[int], values: Sequence[str], style: str) -> None:
            for value in values:
                lines.append((target_index, value, style))

        worker = self.proposal.get("worker") or {}
        worker_status = str(worker.get("status") or self.proposal.get("generator") or "ready").upper()
        low = sum(1 for item in self.review.get("items", []) if item.get("confidence") == "low")
        source_count = sum(_is_visible_source(source) for source in self.sources.values())
        lines.append((None, " COMPACTION ANALYSIS", "agent-label"))
        source_label = "unit" if source_count == 1 else "units"
        lines.append(
            (
                None,
                f" {worker_status} · {len(self.review.get('items', []))} clusters · {source_count} reviewable {source_label} · {low} low-confidence",
                "agent",
            )
        )
        lines.append(
            (
                None,
                " Review clusters, expand evidence, and stage clarifications. Nothing reaches compaction until the submit row.",
                "intro",
            )
        )
        lines.append((None, "", "dim"))

        for item_index, item in enumerate(self.review.get("items", [])):
            tid = target(Target("cluster", item=item_index))
            item_id = str(item.get("id") or "")
            retention = str(item.get("retention") or "preserve")
            work_state = str(item.get("work_state") or "todo")
            arrow = "▾" if item_id in self.expanded else "▸"
            confidence = str(item.get("confidence") or "low").upper()
            review_flag = " · NEEDS REVIEW" if item.get("needs_review") and not item.get("reviewed") else ""
            head = (
                f" {arrow} {RETENTION_MARK.get(retention, '●')} {RETENTION_LABEL.get(retention, retention.upper())}"
                f"  {WORK_STATE_LABEL.get(work_state, work_state.upper())}  {confidence}{review_flag}  "
                f"{item.get('title')}"
            )
            emit(tid, _wrapped(head, width - 1, subsequent="      "), "cluster:" + retention)
            rationale = str(item.get("rationale") or item.get("summary") or "").strip()
            if rationale:
                emit(tid, _wrapped(rationale, width - 7, initial="      ", subsequent="      "), "rationale")
            percent = self.item_pct(item)
            inventory = "      " + self._inventory(item)
            if percent is not None:
                inventory += f" · ~{percent:.2f}% context"
            clarifications = [str(value).strip() for value in item.get("clarifications", []) if str(value).strip()]
            if clarifications:
                inventory += f" · {len(clarifications)} staged clarification{'s' if len(clarifications) != 1 else ''}"
            emit(tid, [inventory], "inventory")
            for clarification in clarifications:
                emit(
                    tid,
                    _wrapped(clarification, width - 13, initial="      ↳ user · ", subsequent="               "),
                    "clarification",
                )

            if item_id in self.expanded:
                for source_index, source_id in enumerate(item.get("source_ids", [])):
                    source = self.sources.get(source_id, {"id": source_id, "text": "(source unavailable)"})
                    if not _is_visible_source(source):
                        continue
                    source_review = self.review.get("source_reviews", {}).get(source_id) or {}
                    source_retention = effective_source_retention(self.review, item, source_id)
                    source_work = str(source_review.get("work_state") or work_state)
                    source_target = target(Target("source", item=item_index, source=source_index))
                    body = _one_line(source.get("text"))
                    source_arrow = "▾" if source_id in self.expanded_sources else "▸"
                    source_head = (
                        f"{source_arrow} {self._source_kind(source)}  {RETENTION_LABEL.get(source_retention, source_retention.upper())}"
                        f"  {WORK_STATE_LABEL.get(source_work, source_work.upper())}  {body[:110]}"
                    )
                    emit(
                        source_target,
                        _wrapped(source_head, width - 10, initial="      ├─ ", subsequent="      │  "),
                        "source-head:" + source_retention,
                    )
                    if body:
                        body_limit = 1800 if source_id in self.expanded_sources else 320
                        emit(
                            source_target,
                            _wrapped(body[:body_limit], width - 12, initial="      │  ", subsequent="      │  "),
                            "source-body",
                        )
                        if len(body) > body_limit:
                            emit(
                                source_target,
                                ["      │  … Space to inspect the full source excerpt"],
                                "source-meta",
                            )
                    pct = self.source_pct(source)
                    meta = f"      │  {source_id} · {source.get('class') or 'other'}"
                    timestamp = source.get("timestamp")
                    if timestamp:
                        meta += f" · {timestamp}"
                    if pct is not None:
                        meta += f" · ~{pct:.3f}% context"
                    emit(source_target, [meta], "source-meta")
                    for clarification in source_review.get("clarifications", []):
                        if str(clarification).strip():
                            emit(
                                source_target,
                                _wrapped(
                                    str(clarification),
                                    width - 15,
                                    initial="      │  ↳ user · ",
                                    subsequent="                   ",
                                ),
                                "clarification",
                            )
            lines.append((None, "", "dim"))

        submit = target(Target("submit"))
        lines.append((submit, " ┌────────────────────────────────────────────────────────────────────┐", "submit"))
        lines.append((submit, " │  GENERATE COMPACTION SUMMARY                                    │", "submit"))
        lines.append((submit, " │  Enter generates a concise draft for chat, editing, or approval. │", "submit"))
        lines.append((submit, " └────────────────────────────────────────────────────────────────────┘", "submit"))
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
            first = next(
                (index for index, (option_index, _line) in enumerate(rendered) if option_index == selected),
                0,
            )
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
            elif key == curses.KEY_NPAGE:
                offset = min(max(0, len(lines) - (height - 2)), offset + height - 3)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - height + 3)
            elif key in (27, ord("q"), 10, 13):
                return

    def class_rules_view(self) -> None:
        selected = 0
        while True:
            rules = self.review.get("class_rules", [])
            if not rules:
                self.show_document("CLASS RULES", ["No class rules are configured."])
                return
            selected = max(0, min(selected, len(rules) - 1))
            height, width = self.screen.getmaxyx()
            self.screen.erase()
            _safe_add(self.screen, 0, 0, " CLASS RULES · defaults only; explicit labels win ".ljust(width - 1), width - 1, curses.A_REVERSE)
            _safe_add(self.screen, 2, 2, "These are priors for untouched clusters and source units.", width - 4, curses.A_DIM)
            for index, rule in enumerate(rules):
                mark = "[x]" if rule.get("enabled") else "[ ]"
                pct = self.class_pct(rule)
                parameter = f" · first {rule.get('percent', 30)}%" if rule.get("id") == "first_n" else ""
                context = f" · ~{pct:.2f}% context" if pct is not None else ""
                line = (
                    f" {mark} {rule.get('label')} → "
                    f"{RETENTION_LABEL.get(str(rule.get('retention')), str(rule.get('retention')).upper())}"
                    f"{parameter}{context}"
                )
                _safe_add(
                    self.screen,
                    4 + index,
                    1,
                    line,
                    width - 2,
                    curses.A_REVERSE if index == selected else 0,
                )
            _safe_add(self.screen, height - 2, 0, " Space toggle · [ ] adjust first percentage · Esc return", width - 1)
            _safe_add(self.screen, height - 1, 0, " Explicit cluster/source choices are never overwritten.", width - 1, curses.A_DIM)
            self.screen.refresh()
            key = self.screen.getch()
            if key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(rules) - 1, selected + 1)
            elif key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key == ord(" "):
                self.mutate(lambda: toggle_rule(self.review, selected, self.trace))
            elif key in (ord("["), ord("]")) and rules[selected].get("id") == "first_n":
                delta = -5 if key == ord("[") else 5
                self.mutate(lambda: change_first_percent(self.review, delta, self.trace))
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
            f"Each row attributes a full conversational episode—prompt plus assistant/tool aftermath—to the prompt that started it. Percentages use {basis}."
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
        self.show_document("PROMPT / TURN CONTEXT COSTS", paragraphs)

    def help_view(self) -> None:
        self.show_document(
            "KEYS",
            [
                "↑↓ / j k navigate. Space or Enter expands a cluster or source excerpt. PgUp/PgDn scroll.",
                "p preserves, c compacts to an outcome, and Delete / Ctrl-D / d removes from active context. The same keys work on a cluster or one source unit.",
                "x cycles TODO → IN PROGRESS → DONE → BLOCKED. e stages a clarification without rewriting original evidence. T edits a cluster title; E edits its summary.",
                "f accepts or resolves a low-confidence interpretation. r opens class-rule priors. Explicit cluster/source labels win over rules.",
                "! edits the global non-negotiable interpretation. / searches. u undoes. $ shows context cost. v shows rival representations.",
                "Only Enter on the final Submit row advances. The next screen shows the exact draft; c chats with the model, e edits directly, b returns here, and Enter confirms.",
                "q cancels the pending compaction. DELETE means remove from carried context; recovery retains the source locally.",
            ],
        )

    def selected_target(self, targets: Sequence[Target]) -> Optional[Target]:
        if not targets:
            return None
        self.selection = max(0, min(self.selection, len(targets) - 1))
        return targets[self.selection]

    def _source_id(self, target: Target) -> Optional[str]:
        if target.kind != "source" or target.item is None or target.source is None:
            return None
        source_ids = self.review["items"][target.item].get("source_ids", [])
        return str(source_ids[target.source]) if target.source < len(source_ids) else None

    def find_text(self, targets: Sequence[Target]) -> None:
        saved, query = self.edit_text("SEARCH CLUSTERS AND SOURCES", "", multiline=False)
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
            haystack = str(item.get("title", "")) + " " + str(item.get("summary", ""))
            source_id = self._source_id(target)
            if source_id:
                haystack += " " + str((self.sources.get(source_id) or {}).get("text", ""))
            if lowered in haystack.lower():
                self.selection = index
                self.status = f"Found “{query}”."
                return
        self.status = f"No match for “{query}”."

    def _set_retention(self, target: Target, value: str) -> None:
        if target.item is None:
            return
        before_actions = len(self.review.get("actions", []))
        source_id = self._source_id(target)
        if source_id:
            self.mutate(lambda: set_source_retention(self.review, target.item or 0, source_id, value))
        else:
            self.mutate(lambda: set_item_field(self.review, target.item or 0, "retention", value))
        if len(self.review.get("actions", [])) > before_actions:
            target_label = "Source" if source_id else "Cluster"
            retention_label = RETENTION_LABEL.get(value, value.upper())
            suffix = "; evidence remains recoverable" if value == "demote" else ""
            self.status = f"{target_label} marked {retention_label}{suffix}."

    def _cycle_work_state(self, target: Target) -> None:
        if target.item is None:
            return
        item = self.review["items"][target.item]
        source_id = self._source_id(target)
        if source_id:
            current = str((self.review.get("source_reviews", {}).get(source_id) or {}).get("work_state") or item.get("work_state") or "todo")
        else:
            current = str(item.get("work_state") or "todo")
        index = WORK_STATES.index(current) if current in WORK_STATES else 0
        value = WORK_STATES[(index + 1) % len(WORK_STATES)]
        self.mutate(lambda: set_work_state(self.review, target.item or 0, value, source_id=source_id))

    def _clarify(self, target: Target) -> None:
        if target.item is None:
            return
        source_id = self._source_id(target)
        title = "CLARIFY SOURCE · staged context, original evidence stays unchanged" if source_id else "CLARIFY CLUSTER · staged context, original evidence stays unchanged"
        saved, value = self.edit_text(title, "", multiline=True)
        if saved and value:
            self.mutate(lambda: add_clarification(self.review, target.item or 0, value, source_id=source_id))

    def _resolve(self, target: Target) -> None:
        if target.item is None:
            return
        item = self.review["items"][target.item]
        rivals = item.get("rival_interpretations") or []
        options = [str(value) for value in rivals] + ["Accept the current interpretation"]
        choice = self.choose("RESOLVE LOW-CONFIDENCE INTERPRETATION", options, len(rivals))
        if choice is not None:
            rival = choice if choice < len(rivals) else None
            self.mutate(lambda: resolve_item(self.review, target.item or 0, rival))

    def _advanced_key(self, key: int, target: Target) -> bool:
        if target.item is None:
            return False
        index = target.item
        item = self.review["items"][index]
        source_id = self._source_id(target)
        if key == ord("T") and not source_id:
            saved, value = self.edit_text("EDIT CLUSTER TITLE", str(item.get("title") or ""), multiline=False)
            if saved and value:
                self.mutate(lambda: set_item_field(self.review, index, "title", value))
            return True
        if key == ord("E") and not source_id:
            saved, value = self.edit_text("EDIT CLUSTER COMPACTION SUMMARY", str(item.get("summary") or ""), multiline=True)
            if saved:
                self.mutate(lambda: set_item_field(self.review, index, "summary", value))
            return True
        if key == ord("N") and not source_id:
            saved, value = self.edit_text("EDIT NEXT ACTION", str(item.get("next_step") or ""), multiline=True)
            if saved:
                self.mutate(lambda: set_item_field(self.review, index, "next_step", value))
            return True
        if key == ord("m") and source_id:
            options = [f"{candidate.get('retention')} · {candidate.get('title')}" for candidate in self.review["items"]]
            chosen = self.choose("MOVE SOURCE TO CLUSTER", options, index)
            if chosen is not None:
                self.mutate(lambda: move_source(self.review, source_id, chosen))
            return True
        if key == ord("S") and source_id:
            saved, title = self.edit_text("NEW CLUSTER TITLE", str(item.get("title") or "") + " — split", multiline=False)
            if saved and title:
                self.mutate(lambda: split_source(self.review, source_id, title, index))
            return True
        if key == ord("n") and not source_id:
            saved, title = self.edit_text("CREATE CLUSTER", "", multiline=False)
            if saved and title:
                self.mutate(lambda: create_item(self.review, title, retention=item.get("retention", "preserve"), after=index))
            return True
        if key == ord("M") and not source_id and len(self.review["items"]) > 1:
            candidates = [
                (candidate_index, candidate)
                for candidate_index, candidate in enumerate(self.review["items"])
                if candidate_index != index
            ]
            chosen = self.choose(
                "MERGE CURRENT CLUSTER INTO",
                [f"{candidate.get('retention')} · {candidate.get('title')}" for _candidate_index, candidate in candidates],
            )
            if chosen is not None:
                other = candidates[chosen][0]
                self.mutate(lambda: merge_items(self.review, index, other))
            return True
        return False

    def _draft_lines(self, width: int) -> List[Tuple[str, str]]:
        state = ensure_draft(self.trace, self.review)
        lines: List[Tuple[str, str]] = []
        messages = list(state.get("messages", []))[-6:]
        if messages:
            lines.append((" REVIEW CHAT", "chat-head"))
            for message in messages:
                role = "YOU" if message.get("role") == "user" else "MODEL"
                wrapped = _wrapped(str(message.get("text") or ""), width - 10, initial=f" {role} · ", subsequent="       ")
                lines.extend((value, "chat") for value in wrapped)
            lines.append(("", "dim"))
        heading = {
            "model": " GENERATED COMPACTION SUMMARY",
            "human": " EDITED COMPACTION SUMMARY",
        }.get(str(state.get("generated_by") or ""), " FALLBACK COMPACTION DRAFT")
        lines.append((heading, "draft-head"))
        for raw in str(state.get("draft") or "").splitlines():
            if raw.startswith("#"):
                style = "draft-heading"
                value = raw.lstrip("# ")
            elif raw.startswith("-"):
                style = "draft-item"
                value = raw
            else:
                style = "draft"
                value = raw
            if value:
                lines.extend((line, style) for line in _wrapped(value, width - 4, initial="  ", subsequent="  "))
            else:
                lines.append(("", "dim"))
        return lines

    def _draw_draft(self, offset: int, *, progress: str = "", status: str = "") -> int:
        height, width = self.screen.getmaxyx()
        lines = self._draft_lines(width)
        body_height = max(1, height - 4)
        offset = max(0, min(offset, max(0, len(lines) - body_height)))
        self.screen.erase()
        revisions = int((self.review.get("draft_review") or {}).get("revision_count") or 0)
        chars = len(str((self.review.get("draft_review") or {}).get("draft") or "").strip())
        title = f" compact focus · review generated summary · {chars:,} chars · {revisions} revision{'s' if revisions != 1 else ''} "
        _safe_add(self.screen, 0, 0, title.ljust(width - 1), width - 1, curses.A_REVERSE)
        for y, (value, style) in enumerate(lines[offset : offset + body_height], 1):
            attr = 0
            if style in {"chat-head", "draft-head", "draft-heading"}:
                attr = curses.A_BOLD | self.colors.get("accent", 0)
            elif style == "chat":
                attr = self.colors.get("warning", 0)
            elif style == "draft":
                attr = curses.A_DIM
            _safe_add(self.screen, y, 0, value, width - 1, attr)
        footer = progress or status or " Enter confirm · c chat/refine · e edit exact draft · b back to clusters · q cancel"
        _safe_add(self.screen, height - 2, 0, " " + footer, width - 1, self.colors.get("warning", 0) if progress or status else 0)
        _safe_add(self.screen, height - 1, 0, " No context is cleared until Enter confirms this screen.", width - 1, curses.A_DIM)
        self.screen.refresh()
        return offset

    def _draw_summary_generation(self, elapsed: int, frame: int) -> None:
        height, width = self.screen.getmaxyx()
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.screen.erase()
        title = " compact focus · generating compaction summary "
        _safe_add(self.screen, 0, 0, title.ljust(width - 1), width - 1, curses.A_REVERSE)
        _safe_add(
            self.screen,
            3,
            2,
            f"{spinner[frame % len(spinner)]} Compressing the reviewed contract into a concise carry-forward summary…",
            width - 4,
            curses.A_BOLD | self.colors.get("accent", 0),
        )
        _safe_add(self.screen, 5, 2, f"{elapsed}s elapsed", width - 4, curses.A_DIM)
        _safe_add(
            self.screen,
            height - 1,
            0,
            " The generated summary will appear here for chat refinement before anything is cleared.",
            width - 1,
            curses.A_DIM,
        )
        self.screen.refresh()

    def _generate_summary_with_progress(self) -> Tuple[bool, str]:
        result: Dict[str, Any] = {}

        def worker() -> None:
            try:
                result["summary"] = run_summary_worker(self.trace, self.review)
            except Exception as exc:
                result["error"] = str(exc)

        thread = threading.Thread(target=worker, name="compact-focus-summary-generation", daemon=True)
        thread.start()
        started = time.monotonic()
        frame = 0
        while thread.is_alive():
            self._draw_summary_generation(int(time.monotonic() - started), frame)
            frame += 1
            time.sleep(0.12)
        thread.join()
        if result.get("error"):
            return False, str(result["error"])
        try:
            apply_generated_summary(self.review, result["summary"])
        except Exception as exc:
            return False, str(exc)
        self.save()
        return True, "Generated a concise summary from the reviewed context."

    def _refine_with_progress(self, feedback: str, offset: int) -> Tuple[bool, str]:
        result: Dict[str, Any] = {}

        def worker() -> None:
            try:
                result["revision"] = run_revision_worker(self.trace, self.review, feedback)
            except Exception as exc:  # surfaced inside the review instead of killing the hook
                result["error"] = str(exc)

        thread = threading.Thread(target=worker, name="compact-focus-draft-review", daemon=True)
        thread.start()
        started = time.monotonic()
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        frame = 0
        while thread.is_alive():
            elapsed = int(time.monotonic() - started)
            self._draw_draft(
                offset,
                progress=f"{spinner[frame % len(spinner)]} Model is refining the draft · {elapsed}s elapsed",
            )
            frame += 1
            time.sleep(0.12)
        thread.join()
        if result.get("error"):
            return False, result["error"]
        try:
            reply = apply_revision(self.trace, self.review, feedback, result["revision"])
        except Exception as exc:
            return False, str(exc)
        self.save()
        return True, reply

    def draft_review(self) -> str:
        state = ensure_draft(self.trace, self.review)
        self.save()
        offset = 0
        status = ""
        if state.get("generated_by") not in {"model", "human"}:
            generated, detail = self._generate_summary_with_progress()
            status = detail if generated else "Summary generation failed; showing the safe fallback. " + detail
        while True:
            height, _width = self.screen.getmaxyx()
            offset = self._draw_draft(offset, status=status)
            status = ""
            key = self.screen.getch()
            if key in (curses.KEY_DOWN, ord("j")):
                offset += 1
            elif key in (curses.KEY_UP, ord("k")):
                offset = max(0, offset - 1)
            elif key == curses.KEY_NPAGE:
                offset += max(1, height - 6)
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - max(1, height - 6))
            elif key == ord("c"):
                saved, feedback = self.edit_text(
                    "CHAT WITH THE COMPACTION DRAFT · state the correction or refinement",
                    "",
                    multiline=True,
                )
                if saved and feedback:
                    ok, detail = self._refine_with_progress(feedback, offset)
                    status = detail if not ok else "Applied: " + detail
                    offset = 0
            elif key == ord("e"):
                state = ensure_draft(self.trace, self.review)
                saved, value = self.edit_text("EDIT EXACT CARRY-FORWARD DRAFT", str(state.get("draft") or ""), multiline=True)
                if saved:
                    self.mutate(lambda: edit_draft(self.review, value))
                    offset = 0
            elif key == ord("b") or key == 27:
                return "back"
            elif key == ord("q"):
                return "cancel"
            elif key in (10, 13, curses.KEY_ENTER):
                state = self.review.get("draft_review") or {}
                draft_chars = len(str(state.get("draft") or "").strip())
                maximum = draft_max_chars()
                allow_oversized = bool(state.get("oversized_approval_armed"))
                if draft_chars > maximum and not allow_oversized:
                    generated, detail = self._generate_summary_with_progress()
                    if generated:
                        status = f"Compressed {draft_chars:,} characters into a concise summary; review it, then press Enter again."
                        offset = 0
                    else:
                        state["oversized_approval_armed"] = True
                        self.save()
                        status = (
                            f"Automatic compression failed: {detail}. "
                            f"Press Enter again to approve all {draft_chars:,} characters anyway."
                        )
                    continue
                try:
                    approve_draft(self.review, allow_oversized=allow_oversized)
                except DraftError as exc:
                    status = str(exc)
                    continue
                self.review["outcome"] = "approved"
                self.review["completed_at"] = utc_now()
                record_action(self.review, "approve")
                self.save()
                return "approved"

    def _select_error(self, targets: Sequence[Target], error: str) -> None:
        if "item " not in error:
            return
        try:
            item_number = int(error.split("item ", 1)[1].split(" ", 1)[0]) - 1
            self.selection = next(
                index
                for index, value in enumerate(targets)
                if value.item == item_number and value.kind == "cluster"
            )
        except (ValueError, StopIteration):
            return

    def _context_label(self) -> str:
        context = self.trace.get("context", {})
        observed = context.get("used_pct_observed")
        if isinstance(observed, (int, float)):
            return f" · {observed:.1f}% context used"
        if context.get("used_tokens_observed"):
            return f" · {int(context['used_tokens_observed']) / 1000:.0f}k tokens · window unknown"
        return " · context size unavailable"

    def run(self) -> bool:
        self.setup()
        while True:
            height, width = self.screen.getmaxyx()
            if height < 12 or width < 60:
                self.screen.erase()
                _safe_add(self.screen, 0, 0, " compact focus · terminal too small", width - 1, curses.A_REVERSE)
                _safe_add(self.screen, 2, 1, "Resize to at least 60×12. q cancels compaction.", width - 2)
                self.screen.refresh()
                if self.screen.getch() == ord("q"):
                    self.review["outcome"] = "cancelled"
                    self.save()
                    return False
                continue

            targets, lines = self.build_lines(width)
            target = self.selected_target(targets)
            first_line = next(
                (index for index, (target_index, _text, _style) in enumerate(lines) if target_index == self.selection),
                0,
            )
            body_height = height - 4
            if first_line < self.offset:
                self.offset = first_line
            if first_line >= self.offset + body_height:
                self.offset = first_line - body_height + 1
            self.offset = max(0, min(self.offset, max(0, len(lines) - body_height)))

            self.screen.erase()
            title = " compact focus · refine the compaction input" + self._context_label()
            _safe_add(self.screen, 0, 0, title.ljust(width - 1), width - 1, curses.A_REVERSE)
            for y, (target_index, value, style) in enumerate(lines[self.offset : self.offset + body_height], 1):
                selected = target_index is not None and target_index == self.selection
                attr = curses.A_REVERSE if selected else 0
                if style.startswith("cluster:"):
                    attr |= self.colors.get(style.split(":", 1)[1], 0) | curses.A_BOLD
                elif style.startswith("source-head:"):
                    attr |= self.colors.get(style.split(":", 1)[1], 0)
                elif style in {"agent-label", "submit"}:
                    attr |= self.colors.get("accent", 0) | curses.A_BOLD
                elif style == "agent":
                    attr |= self.colors.get("warning", 0)
                elif style in {"intro", "rationale", "inventory", "source-body", "source-meta", "dim"}:
                    attr |= curses.A_DIM
                elif style == "clarification":
                    attr |= self.colors.get("warning", 0)
                _safe_add(self.screen, y, 0, value, width - 1, attr)

            errors = review_errors(self.trace, self.review)
            counts = {
                retention: sum(1 for item in self.review["items"] if item.get("retention") == retention)
                for retention in RETENTION_ORDER
            }
            summary = (
                f" {counts['preserve']} preserve · {counts['summarize']} compact · {counts['demote']} delete"
                f" · {sum(1 for error in errors if 'contested' in error)} unresolved · {len(self.review.get('actions', []))} edits"
            )
            _safe_add(self.screen, height - 3, 0, summary, width - 1, curses.A_DIM)
            footer = (
                " " + self.status
                if self.status
                else " ↑↓ navigate · Space expand · p preserve · c compact · Delete/Ctrl-D/d delete · x state · e clarify · r rules · ? help"
            )
            _safe_add(self.screen, height - 2, 0, footer, width - 1, self.colors.get("warning", 0) if self.status else 0)
            _safe_add(
                self.screen,
                height - 1,
                0,
                " Enter only advances on Submit · deleted evidence remains locally recoverable",
                width - 1,
                curses.A_DIM,
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
                source_id = self._source_id(target)
                if source_id:
                    self.expanded_sources.add(source_id)
                else:
                    self.expanded.add(str(self.review["items"][target.item].get("id")))
            elif key == curses.KEY_LEFT and target and target.item is not None:
                source_id = self._source_id(target)
                if source_id:
                    self.expanded_sources.discard(source_id)
                else:
                    self.expanded.discard(str(self.review["items"][target.item].get("id")))
            elif key == ord("?"):
                self.help_view()
            elif key == ord("r"):
                self.class_rules_view()
            elif key == ord("v"):
                self.representation_view()
            elif key == ord("$"):
                self.context_cost_view()
            elif key == ord("/"):
                self.find_text(targets)
            elif key == ord("u"):
                self.undo()
            elif key == ord("!"):
                saved, value = self.edit_text(
                    "NON-NEGOTIABLE INTERPRETATION · what would be catastrophic to misconstrue?",
                    str(self.review.get("precommit") or ""),
                    multiline=True,
                    empty_enter_saves=True,
                )
                if saved:
                    self.mutate(lambda: set_precommit(self.review, value))
            elif key == ord("q"):
                self.review["outcome"] = "cancelled"
                self.review["completed_at"] = utc_now()
                record_action(self.review, "cancel")
                self.save()
                return False
            elif (key in (ord("p"), ord("c")) or _is_delete_key(key)) and target and target.kind in {"cluster", "source"}:
                value = "preserve" if key == ord("p") else "summarize" if key == ord("c") else "demote"
                self._set_retention(target, value)
            elif key == ord("x") and target and target.kind in {"cluster", "source"}:
                self._cycle_work_state(target)
            elif key == ord("e") and target and target.kind in {"cluster", "source"}:
                self._clarify(target)
            elif key == ord("f") and target and target.item is not None:
                self._resolve(target)
            elif key == ord(" ") and target and target.kind == "cluster" and target.item is not None:
                item_id = str(self.review["items"][target.item].get("id"))
                if item_id in self.expanded:
                    self.expanded.remove(item_id)
                else:
                    self.expanded.add(item_id)
            elif key == ord(" ") and target and target.kind == "source":
                source_id = self._source_id(target)
                if source_id in self.expanded_sources:
                    self.expanded_sources.remove(str(source_id))
                elif source_id:
                    self.expanded_sources.add(source_id)
            elif key in (10, 13, curses.KEY_ENTER) and target:
                if target.kind == "submit":
                    errors = review_errors(self.trace, self.review)
                    if errors:
                        error = next((value for value in errors if "contested" in value), errors[0])
                        self.status = "Cannot submit: " + error + ". Clarify or press f to accept."
                        self._select_error(targets, error)
                    else:
                        outcome = self.draft_review()
                        if outcome == "approved":
                            return True
                        if outcome == "cancel":
                            self.review["outcome"] = "cancelled"
                            self.review["completed_at"] = utc_now()
                            record_action(self.review, "cancel_from_draft")
                            self.save()
                            return False
                        self.status = "Returned to clusters; the prior draft will regenerate after any edit."
                elif target.kind == "cluster" and target.item is not None:
                    item_id = str(self.review["items"][target.item].get("id"))
                    if item_id in self.expanded:
                        self.expanded.remove(item_id)
                    else:
                        self.expanded.add(item_id)
                elif target.kind == "source":
                    source_id = self._source_id(target)
                    if source_id in self.expanded_sources:
                        self.expanded_sources.remove(str(source_id))
                    elif source_id:
                        self.expanded_sources.add(source_id)
            elif target and self._advanced_key(key, target):
                pass


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
