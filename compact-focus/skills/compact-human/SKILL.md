---
name: compact-human
description: Context Prism — interactively steer a compaction. Precommit what must not be misinterpreted, triage and veto (never select), construe the session through rival lenses and adjudicate only their disagreements, optionally race default vs lensed summaries on the same continuation, then compact. Use when the user wants to steer /compact, or after a compact-focus pause.
---

The user wants to compact while controlling what survives — and, more
importantly, what it MEANS. Context is nearly full; keep prose terse.
Default is KEEP: the user vetoes. Nothing vetoed is deleted — demoted and
recoverable. The dominant failure mode is misconstrual, not omission.

INVARIANTS:
- Veto, not selection. Unvetoed content survives.
- Precommit comes FIRST, before the user sees any machine construal.
- Adjudicate lens DISAGREEMENTS only — never make the user review spans
  the rivals agree on.
- Collapsed Bash results (ctrl+o) show only the current working set.
- Record every signal (record mode). Unsure keep/remove → ask directly,
  one item. Unsure which of two chunks matters more → A/B them, record
  '{"event":"ab_choice","a":"…","b":"…","chose":"…"}'.

Script (stable prefix, one approval):
   ~/.claude/skills/compact-focus/scripts/compact-focus-list.sh <mode> …
Missing/denied → same flow in plain text, no recording.

1. LOAD. No-args (thread labels; <2 → group and `save`). Run `guidelines`
   and `lens`.

2. PRECOMMIT (before any construal is shown). AskUserQuestion: "What would
   be catastrophic for the next agent to MISINTERPRET about this session?"
   — options "Nothing special — proceed" / "Let me say it" (Other, free
   text). Record '{"event":"precommit","text":"…"}'. The answer is a hard
   constraint on every construal below.

3. TRIAGE. Extract semantic items (ideas, features, side questions,
   decisions, bugs) → open | solved | unclear, linked to thread keys.
   `triage save '…'`, then `triage list` (collapsed). Record
   '{"event":"triage","open":N,"solved":N,"unclear":N}'.

4. RESOLVE UNCLEAR, one by one: "Is '<label>' still open, or solved?" —
   Open / Solved / Remove. `triage set <id> …`; record each.

5. VETO PASS. `triage list`; AskUserQuestion multiSelect: "Anything you
   deliberately want REMOVED or recategorized? (default: everything
   stays)". Apply; record '{"event":"veto","removed":"…"}'.

6. DEMOTE + BURY the removed: `demote keep <keys> drop <nums>`; per removed
   item `graveyard add '{"text":"<user msgs behind it>","topic":"…","source":"T…"}'`.
   One line: "Removed items demoted + buried — recoverable, not deleted."

7. LENS (three levels — maintain before constructing anything). Run `lens`.
   Level 1 (universal grammar: decision · test · contradiction · dead-end ·
   constraint) is fixed. If Domain lens or Active task model are seed or
   stale, derive them from session + MEMORY.md/CLAUDE.md — the Domain lens
   is the expert vocabulary (project-specific concepts a generic reader
   would miss); the Active task model is what's currently favored and what
   gets compared next. Show 2-4 lines, update lens.md, record
   '{"event":"lens","text":"…"}'.

8. RIVAL CHUNKINGS. Construe the surviving content through 2-3 rival
   problem representations — default set: progress/state lens,
   causal-debugging lens, constraint/coordination lens (swap per Domain
   lens when obvious). Find the spans where rivals produce DIFFERENT
   interpretations. Present ONLY those disagreements, one at a time:
   "Span: <what happened>. A construes it as <…>, B as <…>." — options A /
   B / "Merge — both" / "Neither" (relabel via Other). Record each as
   '{"event":"rival_adjudication","span":"…","a":"…","b":"…","chose":"…"}'.
   Spans the rivals agree on pass through silently.

9. MERGED DRAFT. Compose the preservation draft from the adjudicated
   construal: surviving OPEN items first (state + next step per the Active
   task model), solved outcomes one line each; every chunk labeled with
   universal-grammar type and tagged with provenance
   [threads 1,3 · D2 · G1] so it can expand back to prompts, tool results,
   diffs. Honor the precommit verbatim. Print INLINE; record draft.
   AskUserQuestion: Approve / Edit / Race it / Restore demoted / Default
   summary.
   RACE (on request or when the user hesitates): build a brief
   default-style summary (neutral, no lens), then:
     …/compact-focus-list.sh race '<default-summary>' '<merged-draft>' 'continue fixing it'
   Show both agents' first three actions side by side; record
   '{"event":"race","winner":"…","divergence":"…"}'. Loop edits until
   Approve; record approved.

10. COMPACT via SlashCommand:
      /compact focus on Preserve exactly this state summary, expanding each
      labeled episode faithfully: <merged draft>. Also state: removed
      context is recoverable — demoted.jsonl and graveyard.jsonl in
      `<state-dir>`, searchable via `compact-focus-list.sh graveyard query
      <terms>` / `recall <id>`.
    If SlashCommand cannot run /compact, print the command.

11. AFTER (say once): "If anything seems forgotten or misread, say 'the
    compaction lost X' — I'll query the graveyard and restore." On that, in
    ANY later turn: `graveyard query <terms>` (+ `recall all` if needed),
    inject findings, record '{"event":"revealed_loss","text":"…",
    "recovered":"…","kind":"omitted|mis-encoded"}'.

Do not re-summarize the conversation yourself — the compaction does that;
your draft pins what it must contain and what it must mean.
