---
name: compact-learn
description: Update the per-project compaction guidelines from accumulated signals (draft edits, demotions, revealed losses) logged by compact-human. Use when the user asks to improve compaction behavior, review compaction signals, or runs the learn loop.
---

You are the guideline-learning loop from the Negotiated Demotion design:
convert imperfect human judgments already logged into durable policy, so
future compactions ask less and preserve better.

1. Read the signal log and current policy:
   - `~/.claude/skills/compact-focus/scripts/compact-focus-list.sh guidelines`
     (prints guidelines.md and its path)
   - Read `log.jsonl` and `demoted.jsonl` from the same state directory
     (the guidelines output shows the directory). Relevant events:
     `draft` vs `edit` vs `approved` (what users add/remove relative to
     machine drafts), `revealed_loss` (what compaction lost that mattered
     — highest-weight signal), `phase1` (what users keep vs demote).

2. Diagnose patterns, not instances. An edit that happened once is an
   anecdote; the same *class* of edit twice (e.g. "user keeps re-adding
   file paths", "revealed losses are always constraints stated early") is
   a candidate rule. Weight: revealed_loss > edits > phase1 selections
   (selection tracks current salience, not future need — that is why the
   log exists).

3. Propose at most 3 guideline diffs, each as: the rule (one line), the
   evidence (which events, counts), and what it would have changed. Show
   the diff to the user; apply only what they approve by editing
   guidelines.md directly (Write/Edit tools).

4. Record the outcome:
   …/compact-focus-list.sh record '{"event":"guideline_update","rules_added":N,"evidence_events":M}'

5. If the log has fewer than ~5 signal events, say so and stop — do not
   invent rules from noise. Report what signal classes are still empty
   (e.g. no revealed_loss events yet) so the user knows what the loop is
   blind to.
