---
name: compact-human
description: Interactively steer a conversation compaction — loss-framed selection, demotion (never deletion) of the rest, then a preservation draft the user edits and approves before compaction runs. Use when the user wants to steer /compact, or after a compact-focus pause suggested picking a focus.
---

The user wants to compact this conversation while controlling what survives.
Context is nearly full — keep prose terse. This flow implements negotiated
demotion: nothing is deleted, unkept content is demoted to an ID-addressable
store; the compaction summary is drafted, edited, and approved BEFORE
/compact runs.

INVARIANTS:
- Collapsed Bash results (ctrl+o) only ever contain the CURRENT selection.
- Phase 1 selection happens BEFORE any draft is shown (draft anchors the
  editor — never reverse the order).
- Every signal is recorded (record mode) — this is research instrumentation.

All script calls use EXACTLY this command shape (stable prefix, one
"don't ask again" approval covers every mode):

   ~/.claude/skills/compact-focus/scripts/compact-focus-list.sh <mode> …

If the script is missing or denied, run the same flow in plain text and
skip recording.

1. LOAD. Run the script with no arguments (labels + counts from the
   thread file the pause saved). If it shows <2 categories, group the
   session's actual user requests yourself into 2-6 categories (2-4 word
   labels, prompts verbatim, single-line) and store via:
     …/compact-focus-list.sh save '<JSON>'
   with {"threads":{"1":{"label":"…","prompts":["…",…]},…}}, single-quoted,
   inner single quotes replaced with ’. Also run `guidelines` mode now and
   keep its content in mind for step 4.

2. PHASE 1 — LOSS-FRAMED SELECTION (before any draft exists).
   AskUserQuestion, multiSelect: true:
   - Question, exactly: "After compaction, what must the agent NOT have
     forgotten? (unpicked threads are demoted with recall IDs, not
     deleted · type a number to preview)"
   - One option per category, numeric order, max 4 (more → first 4, rest
     by number via Other). Label = category label. Description: REQUIRED,
     ~60 chars, front-loaded with real information.
   - Preview loop: bare number → run `show <n>`, re-ask.
   - Per-prompt refinement: after category pick, run `show 1,3`; ask
     "Keep all N prompts?" — "Keep all (Recommended)" / "Drop some —
     type the [numbers]" / "None — default summary". Drops accumulate;
     re-run `show 1,3 drop 2,5` after each round.
   Record: …/compact-focus-list.sh record '{"event":"phase1","kept":"1,3","dropped":"2,5"}'

3. DEMOTE THE REST. Nothing selected → output `/compact` alone, record
   {"event":"declined"}, stop. Otherwise:
     …/compact-focus-list.sh demote keep 1,3 drop 2,5
   The receipt (D-ids + store path) prints collapsed. Tell the user in one
   line: "Unkept content demoted, recoverable by ID — nothing deleted."

4. PHASE 2 — DRAFT, EDIT, APPROVE. Compose the preservation draft
   yourself from the kept selection and the full session: a bullet list of
   the concrete state the compaction summary MUST contain — decisions with
   rationale, corrections verbatim, edited file paths, failing tests, open
   TODOs — following the guidelines from step 1. Print the draft INLINE
   (it is the object under review, never collapse it). Record:
     record '{"event":"draft","text":"<the draft, compressed to one line each>"}'
   AskUserQuestion: "Approve this preservation draft?"
   - "Approve (Recommended)" — proceed
   - "Edit" — description: "Say what to change/add/remove in Other"
   - "Restore demoted" — description: "Name a D-id or topic to pull back in"
   - "Default summary" — plain /compact instead
   On Edit/Restore: apply, re-print the full revised draft, record
     '{"event":"edit","text":"<user words>"}', re-ask. Loop until Approve.

5. COMPACT. Record '{"event":"approved","text":"<final draft one-lined>"}'.
   Run via SlashCommand:
     /compact focus on Preserve exactly this state summary, expanding each
     bullet faithfully: <final draft bullets, semicolon-separated>. Also
     state in the summary: additional context was demoted, recoverable via
     `<state-dir>/demoted.jsonl` by ID (D1…), readable with
     `compact-focus-list.sh recall <id>`.
   If SlashCommand cannot run /compact, print that command for the user.

6. REVEALED LOSS (standing instruction, mention once after compacting):
   "If you notice the summary lost something, say 'the compaction lost X'
   — I'll log it and restore from the demoted store." When that happens,
   in ANY later turn: run `recall all`, restore the relevant content into
   context, and record '{"event":"revealed_loss","text":"<what was lost>"}'.

Do not re-summarize the conversation yourself — the compaction does that;
your draft only pins what it must contain.
