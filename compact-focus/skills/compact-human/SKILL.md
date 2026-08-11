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
Otherwise run the FAST FLOW — total interaction target: ONE ledger, ONE
typed reply, compact.

## FAST FLOW

1. PREPARE SILENTLY (no listings, no questions). Load threads (no-args
   call; if <2 categories, group and `save`). Derive triage items (open /
   solved / unclear) and `triage save` them. Run `lens` and `guidelines`;
   refresh Domain lens / Active task model in lens.md if seed or stale.
   Construe the session through two rival representations (progress/state
   vs causal-debugging, or better per the Domain lens) and note where they
   DISAGREE. Record '{"event":"ledger_prep","items":N,"contested":N}'.

2. PRINT THE LEDGER — formatted markdown, INLINE (never collapsed), exactly
   this shape (Obsidian-style: headings, task-list checkboxes, bold tags,
   the item's OWN words from the transcript — never paraphrased labels, no
   invented descriptions):

   ## ⏸ Compaction ledger
   > One typed reply handles everything · `ok` accepts as shown

   ### Keep
   - [x] **1 · open** — <item in the user's own words> → *next:* <step>
   - [x] **2 · decision** — <choice + rationale> `[threads 1]`
   - [x] **3 · dead-end** — <what was falsified>; *do not retry*

   ### ⚡ Contested
   - [ ] **4** — <span>: <construal A> *(lens A)* **or** <construal B> *(lens B)*?

   ### Drop → demoted, recoverable
   - [ ] **5 · mechanical** — <item> 
   - [ ] **6** — <superseded item>

   > **Anything the next agent must not MISINTERPRET?** Say it in your reply.

   `reply:` numbers flip (`5, not 2`) · free text → constraint · `? 3` = provenance · `race` · `ok`

   Rules: grammar tags from the universal grammar (decision · test ·
   contradiction · dead-end · constraint; open/solved/mechanical allowed as
   states); open items first, mechanical last; contested = rival-lens
   disagreements + unresolved unclear triage items; stable numbering; keep
   the whole ledger under ~25 lines — merge aggressively, this is a
   partition, not an inventory. END YOUR TURN on the reply line — the user
   answers in the normal prompt box. No AskUserQuestion in the fast flow.

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

4. APPLY AND COMPACT. `demote keep <keys> drop <nums>` for the drop set;
   `graveyard add` a trace per dropped item. Then via SlashCommand:
     /compact focus on Preserve exactly this state, expanding each labeled
     item faithfully: <kept ledger lines + constraints verbatim>. Also
     state: removed context is recoverable — demoted.jsonl / graveyard.jsonl
     in `<state-dir>`, via `compact-focus-list.sh graveyard query <terms>`
     or `recall <id>`.
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
