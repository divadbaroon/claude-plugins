---
name: compact-human
description: Interactively steer a conversation compaction — triage the session into open/solved/unclear items, let the user VETO rather than select, demote (never delete) the removed, then draft-edit-approve the preservation summary through the project lens before compaction runs. Use when the user wants to steer /compact, or after a compact-focus pause suggested picking a focus.
---

The user wants to compact this conversation while controlling what survives.
Context is nearly full — keep prose terse. Default is KEEP: the user vetoes
what to remove; nothing vetoed is deleted, only demoted to recoverable
stores.

INVARIANTS:
- Veto, not selection: never ask "what do you want to keep?" — ask what to
  resolve, remove, or deprioritize. Unvetoed content survives.
- Collapsed Bash results (ctrl+o) only ever show the current working set.
- The lens pass precedes the draft; the draft precedes /compact.
- Every signal is recorded (record mode) — research instrumentation.
- When YOU are unsure whether to keep or remove an item: ask directly, one
  item at a time. When unsure which of TWO chunks matters more: A/B it —
  AskUserQuestion with exactly those two as options ("Which matters more?"),
  record '{"event":"ab_choice","a":"…","b":"…","chose":"…"}'.

All script calls use EXACTLY this command shape (stable prefix, one
approval):
   ~/.claude/skills/compact-focus/scripts/compact-focus-list.sh <mode> …
If the script is missing/denied, run the same flow in plain text, skip
recording.

1. LOAD. Run with no args (thread labels). If <2 categories, group the
   session's user requests into 2-6 categories and `save` them (shape:
   {"threads":{"1":{"label":"…","prompts":["…"]}}}, quotes → ’). Run
   `guidelines` and `lens` modes; keep both in mind.

2. TRIAGE LEDGER. From the whole session, extract the semantic items — new
   ideas, features, side questions, decisions, bugs — and categorize each as
   open (unresolved, still matters), solved (resolved, artifact exists), or
   unclear (you cannot tell). Link each to its thread key(s). Store:
     …/compact-focus-list.sh triage save '{"items":[{"id":"T1","label":"…","category":"open","threads":"1"}…]}'
   Then run `triage list` (collapsed; ctrl+o shows the ledger). Record
   '{"event":"triage","open":N,"solved":N,"unclear":N}'.

3. RESOLVE UNCLEAR — one by one. For each unclear item, AskUserQuestion:
   "Is '<label>' still open, or solved?" — options: "Open" / "Solved" /
   "Remove — not worth carrying". Apply with `triage set <id> <category>`.
   Record each as '{"event":"unclear_resolved","id":"T…","to":"…"}'.

4. VETO PASS. Show the resolved ledger (`triage list`). AskUserQuestion,
   multiSelect: "Anything here you deliberately want REMOVED or
   recategorized before compaction? (default: everything stays)" — options
   are up to 4 item labels you judge most removal-worthy (solved things
   fully captured in files/commits first), plus Other for ids/words. Apply
   via `triage set <id> removed` (or category changes). If you are unsure
   about an item, ask directly; if two items compete for priority, A/B them
   (see invariants). Record '{"event":"veto","removed":"T2,T7"}'.

5. DEMOTE THE REMOVED. Removed items' threads/prompts (and any thread with
   no surviving items) go to the demoted store:
     …/compact-focus-list.sh demote keep <surviving-thread-keys> drop <nums>
   For each removed item also bury a message-level trace:
     …/compact-focus-list.sh graveyard add '{"text":"<the user message(s) behind it, verbatim-ish>","topic":"<label>","source":"T<id>"}'
   One line to the user: "Removed items are demoted + buried, recoverable —
   nothing deleted."

6. WHAT MUST SURVIVE (optional, one question). AskUserQuestion: "What should
   NOT be forgotten during compaction? (optional — skip if the ledger covers
   it)" — options "The ledger covers it (Recommended)" / "Let me add
   something" (free text via Other). Record any answer as
   '{"event":"preserve_request","text":"…"}'; it becomes a hard constraint
   on the draft.

7. LENS PASS (before drafting). Run `lens`; if seed/stale, derive the
   project frames from session + MEMORY.md/CLAUDE.md, show 2-4 lines,
   update lens.md, record '{"event":"lens","text":"…"}'.

8. DRAFT THROUGH THE LENS. Compose the preservation draft: surviving OPEN
   items first (with state + next step), then solved items' one-line
   outcomes, chunked into taxonomy-labeled episodes per the lens; honor
   every preserve_request verbatim. Print INLINE. Record
   '{"event":"draft","text":"…"}'. AskUserQuestion: "Approve this
   preservation draft?" — Approve / Edit (Other) / Restore demoted (name a
   D/G id or topic) / Default summary. Loop on edits (record each); on
   Approve record '{"event":"approved","text":"…"}'.

9. COMPACT. Via SlashCommand:
     /compact focus on Preserve exactly this state summary, expanding each
     labeled episode faithfully: <final draft>. Also state in the summary:
     removed context is recoverable — demoted.jsonl and graveyard.jsonl in
     `<state-dir>`, searchable with `compact-focus-list.sh graveyard query
     <terms>` and `recall <id>`.
   If SlashCommand cannot run /compact, print the command for the user.

10. AFTER COMPACTION (standing instruction, say once): "If anything seems
    forgotten, say 'the compaction lost X' — I'll query the graveyard and
    restore it." When that happens in ANY later turn: run
    `graveyard query <terms>` (and `recall all` if needed), inject the
    findings back into context, record
    '{"event":"revealed_loss","text":"…","recovered":"G…/D…"}'.

Do not re-summarize the conversation yourself — the compaction does that;
your draft only pins what it must contain.
