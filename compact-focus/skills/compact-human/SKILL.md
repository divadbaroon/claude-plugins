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
  (editor surface — chat prints nothing), run `surfaced` after routing. After edits via
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

2. SURFACE THE EDITOR — never the ledger as chat text. The interactive
   curses editor IS the surface; the chat only routes to it.

   First persist the ledger for the editor: `ledger save '<json>'` with
   each item as {"id","tag","label","cat":"keep|summarize|contested|drop",
   "prov","pct":<summed>,"children":[{"u":<unit number from costs>,
   "text":"<the actual prompt, verbatim>","checked":true,"pct":<from
   costs>}]}, plus "classes":[{"id":"first_n","state":"keep","n":30},
   {"id":"file_changes","state":"keep","pct":…},{"id":"subagents",
   "state":"summarize","pct":…},{"id":"todos","state":"keep","pct":…}] —
   class pcts from the `costs` classes table; every child MUST carry its
   "u" ref (finalize rejects ungrounded items). Apply the DEFAULT
   PARTITION RULE when composing: preserve = critical ongoing work, recent
   decisions, active files; summarize = completed tasks, resolved issues,
   older discussion; drop = redundant, outdated; cannot confidently place
   → contested (the human decides, never you). Items in the user's OWN
   words; never paraphrased labels.

   Then run `tui-inject` and branch on its output:
   - `OPENED: …` → say ONE line only, e.g. "Ledger editor open in <where>
     — ⇧↑↓ moves categories, ← expands into prompts, space selects, enter
     submits. Say **done** here when finished." NOTHING else in chat — no
     ledger, no widget, no summary of items.
   - `FALLBACK: …` → only now print the markdown ledger (headings,
     checkboxes, grammar tags, per-item ~%, drop candidates cost-first,
     under ~25 lines) and accept the typed grammar: numbers flip
     (`5, not 2`) · text → constraint · `? 3` provenance · `race` · `ok`.
   Record '{"event":"ledger_surfaced","via":"tmux|window|fallback"}'.

3. HANDLE THE RETURN. "done" (or the editor's finalized line) →
   `ledger load` is ground truth → step 4. Typed replies (fallback path
   or impatient users — tolerate prose and typos; apply ALL parts of a mixed
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
