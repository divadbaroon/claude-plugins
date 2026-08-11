---
name: compact-human
description: Context Prism — steer a compaction through a one-screen markdown ledger the user answers with a single typed reply (flips, constraints, provenance, race, ok). Study mode expands to the granular multi-round flow. Use when the user wants to steer /compact, or after a compact-focus pause.
---

The user wants to compact while controlling what survives and what it
MEANS. Context is nearly full — be fast. Default is KEEP; the user vetoes.
Nothing is deleted: drops are demoted, recoverable. Misconstrual, not
omission, is the dominant failure.

Script (stable prefix, one approval):
   ~/.claude/skills/compact-focus/scripts/compact-focus-list.sh <mode> …

First run `studymode`. If it prints `1`, use the STUDY FLOW (end of file).
Otherwise run the FAST FLOW — and check `prep-status` FIRST:

- `none`   → run `prep-bg`, then ask the PRECOMMIT question via
  AskUserQuestion (the native dialog — this widget call is allowed in
  addition to the ledger surface call, since it happens in the kick turn):
  - Question, exactly: "While it prepares — what would be catastrophic
    for the next agent to MISINTERPRET about this session?"
  - Exactly 2 options, NO content suggestions (proposing candidate
    misinterpretations would anchor the very judgment this question
    exists to capture un-anchored): "Skip — the ledger will surface on
    its own" (description: "Nothing stands out right now") / "I'll say
    it" (description: "Not what to keep — what, construed wrongly after
    compaction, would cost you the most. Type it in Other."). The real
    answer arrives via the Other free-text affordance.
  Record any answer as '{"event":"precommit","text":"…"}' and carry it
  into constraints. END YOUR TURN. Do NOT block on prep.
- `running`→ if precommit not yet asked, ask it now (same widget rule);
  otherwise say it's still preparing and answer whatever else the user
  asked.
- `failed: …` → do the preparation yourself IN-SESSION via step 1 below,
  then continue.
- `ready`  → skip step 1: `ledger load` is your ledger (trust its
  partition as the prior; refresh anything obviously stale). Go to step 2
  (print + widget surface), run `surfaced` after printing. After edits via
  any surface, `ledger load` is ground truth → step 4.
  (The asyncRewake hook usually delivers this state right when prep
  finishes — surface immediately on that rewake.)

## FAST FLOW

1. PREPARE IN-SESSION (only when background prep failed or study mode
   needs it). Load threads (no-args call; if <2 categories, group and
   `save`). Run `costs` for per-prompt percentages. Derive triage items
   and `triage save`. Run `lens` and `guidelines`; refresh if stale.
   Construe through two rival representations and note DISAGREEMENTS.
   Record '{"event":"ledger_prep","items":N,"contested":N}'.

2. PRINT THE LEDGER — formatted markdown, INLINE (never collapsed), exactly
   this shape (Obsidian-style: headings, task-list checkboxes, bold tags,
   the item's OWN words from the transcript — never paraphrased labels, no
   invented descriptions). DEFAULT PARTITION RULE: preserve = critical
   ongoing work, recent decisions, active files; summarize = completed
   tasks, resolved issues, older discussion; remove = redundant
   information, outdated attempts. Anything you cannot confidently place →
   contested: the human decides those, never you.

   ## ⏸ Compaction ledger
   > One typed reply handles everything · `ok` accepts as shown

   ### Class rules
   - [x] first **30**% of session — keep decisions only *(edit N in editor)*
   - [x] file-change detail · ~N% — keep / summarize / drop
   - [~] subagent transcripts · ~N% — *summarize to outcome lines*
   - [x] todo bookkeeping · ~N%

   ### Preserve — ongoing work, recent decisions, active files
   - [x] **1 · open** — <item in the user's own words> → *next:* <step> · ~N%

   ### Summarize — completed, resolved, older
   - [~] **2 · decision** — <choice + rationale> `[threads 1]` · ~N%
   - [~] **3 · dead-end** — <what was falsified>; *do not retry* · ~N%

   ### ⚡ Contested — you decide
   - [?] **4** — <span>: <construal A> *(lens A)* **or** <construal B> *(lens B)*?

   ### Remove — redundant, outdated → demoted, recoverable
   - [ ] **5 · mechanical** — <item> · ~N%

   > **Anything the next agent must not MISINTERPRET?** Say it in your reply.

   `reply:` numbers flip (`5, not 2`) · free text → constraint · `? 3` = provenance · `race` · `ok`

   Rules: grammar tags from the universal grammar (decision · test ·
   contradiction · dead-end · constraint; open/solved/mechanical allowed as
   states); open items first, mechanical last; contested = rival-lens
   disagreements + unresolved unclear triage items; stable numbering; keep
   the whole ledger under ~25 lines — merge aggressively, this is a
   partition, not an inventory.

   Every ledger line ends with its context share (`· ~8.0%` — sum of its
   prompts' pcts from `costs`); order Drop candidates by pct descending so
   the expensive removals are visible first.

   BEFORE printing, also persist the same ledger for the interactive
   editor: `ledger save '<json>'` with each item as {"id","tag","label",
   "cat":"keep|summarize|contested|drop","prov","pct":<summed>,
   "children":[{"text":"<the actual prompt, verbatim>","checked":true,
   "pct":<from costs>}]}, plus "classes":[{"id":"first_n","state":"keep",
   "n":30},{"id":"file_changes","state":"keep","pct":<from costs
   classes>},{"id":"subagents","state":"summarize","pct":…},{"id":"todos",
   "state":"keep","pct":…}] — class pcts come from the `costs` classes
   table; subagents default to summarize.
   Then present the NO-TYPING SURFACE: one single AskUserQuestion call
   (the only widget call allowed in the fast flow — it is one round-trip),
   with up to 4 questions built from the ledger:
   - Q1 (single): "Compact as shown?" — "Yes — compact (Recommended)" /
     "Adjust below first" / "Full editor" (description: "line editor in
     this terminal via ! cf, or tmux split") / "Race the summaries first".
   - Q2 (multiSelect, if any non-drop items): "Demote one tier
     (preserve→summarize→remove):" — top-4 costliest non-drop items.
   - Q3 (multiSelect, if any dropped items): "Rescue from Remove:" — the
     dropped items.
   - Q4 (multiSelect, if any contested): "Contested — select what to
     PRESERVE (unselected → summarize):" — the contested items. The
     user's explicit selection here IS the human decision.
   CAP RULES (AskUserQuestion holds 4 options × 4 questions — never let
   the cap silently decide anything):
   - CONTESTED OUTRANKS EVERYTHING for widget slots. If contested items
     exceed one question's 4 options, give contested a second question
     (drop Q2/Q3 to make room); if they exceed 8, skip the widget path for
     contested entirely and require the editor or typed replies — contested
     is never resolved by omission, and compaction still blocks on it.
   - Overflow elsewhere is safe by construction: items not shown in the
     widget keep their model-assigned tier, are fully visible in the
     printed ledger (which has no cap), and stay addressable by number in
     the typed grammar and both editors. When a question shows a subset,
     SAY SO in its description ("4 costliest of 7 — others by number or
     editor"), always chosen by context cost, never arbitrarily.
   - Never add a 5th option or 5th question; the call fails. Q1's fixed
     action options already fill one question.
   Apply all answers; record which items were widget-visible vs overflow
   in ledger_final. If "Adjust below first" or anything ambiguous, the
   typed grammar still works in the next message: numbers flip
   (`5, not 2`) · text → constraint · `? 3` provenance · `race` ·
   `window` · `ok`. If "Full editor": run `tui-inject` (tmux split when
   available, else it prints `! cf` — the editor now runs line-mode in
   this terminal, no TTY needed) and wait for **done**. If the user
   ran the TUI (their next message follows a "ledger finalized:" line in
   the transcript, or they say so): `ledger load`, and treat it as ground
   truth — cat=drop items and unchecked children are the drop set;
   constraints[] are precommit constraints; items with "edited":true are
   relabels (record '{"event":"tui_edit","id":"…"}' each). Record
   '{"event":"ledger_final","via":"tui|reply","kept":"…","dropped":"…"}'.

3. PARSE THE REPLY (tolerate prose and typos; apply ALL parts of a mixed
   reply):
   - numbers / "not N" → flip between keep and drop; `triage set` to match;
     record '{"event":"veto","flips":"…"}'. Unaddressed contested items
     default to KEEP.
   - free text → constraint on the summary; record
     '{"event":"precommit","text":"…"}'.
   - `? N` → show that item's provenance (`show`, `recall`, `graveyard
     query`), then re-print ONLY the changed lines, wait again.
   - `race` → build a brief neutral summary, run
     `race '<neutral>' '<ledger-as-draft>'`, show, wait again; record race.
   - `ok` / empty / affirmative prose → proceed.
   If a flip is ambiguous ("drop the lens thing" matching two items), ask
   in one plain-text line, no widget. Record
   '{"event":"ledger_final","kept":"…","dropped":"…"}'.

4. APPLY AND COMPACT — deterministically. Contested items still
   unresolved BLOCK: ask in one plain-text line first; never silently
   default a contested item. Then: write the user's final decisions back
   into the ledger (`ledger save '<final json>'` — categories, checked
   flags, constraints, classes; children keep their "u" unit refs), and
   run `finalize`. It validates the schema and coverage invariants
   (unique ids, legal categories, every active unit covered exactly once,
   no invented refs, no ungrounded items) and, ONLY if valid, writes the
   demotion and graveyard records itself from the unit refs and prints
   the compiled directive. If it prints INVALID, fix the ledger and rerun
   — never hand-derive demote sets from thread keys or prompt numbers.
   Run via SlashCommand:
     /compact focus on <the finalize directive, verbatim>
   If SlashCommand cannot run /compact, print the command. Then say once:
   "If anything seems forgotten or misread, say 'the compaction lost X'."
   On that, in ANY later turn: `graveyard query`, inject findings, record
   '{"event":"revealed_loss","text":"…","kind":"omitted|mis-encoded"}'.

## STUDY FLOW (COMPACT_FOCUS_STUDY=1 — granular signals for the corpus)

Same mechanisms, expanded to one question per judgment, AskUserQuestion
allowed. Order: precommit question (before any construal) → triage save +
list → resolve each unclear item singly (Open/Solved/Remove) → veto pass
(multiSelect) → demote + bury → lens refresh → rival chunkings with
per-disagreement adjudication (A/B when two chunks compete; record
ab_choice) → merged draft with provenance tags, edit/approve loop →
optional race → compact + revealed-loss standing instruction. Record every
event exactly as in the fast flow, plus '{"event":"rival_adjudication",…}'
and '{"event":"unclear_resolved",…}' per item.
