---
name: compact-learn
description: Update the per-project compaction guidelines and lens from accumulated signals (draft edits, demotions, revealed losses, mis-encodings) logged by compact-human. Use when the user asks to improve compaction behavior, review compaction signals, or runs the learn loop.
---

You are the guideline-learning loop from the Negotiated Demotion design:
convert imperfect human judgments already logged into durable policy, so
future compactions ask less and preserve better. Policy lives in TWO
files: guidelines.md (inclusion rules — what to keep) and lens.md
(encoding schema — how to construe what is kept).

1. Read the signal log and current policy:
   - `~/.claude/skills/compact-focus/scripts/compact-focus-list.sh guidelines`
     (prints guidelines.md and its path)
   - `~/.claude/skills/compact-focus/scripts/compact-focus-list.sh lens`
     (prints lens.md and its path)
   - Read `log.jsonl` and `demoted.jsonl` from the same state directory
     (the guidelines output shows the directory). Relevant events:
     `draft` vs `edit` vs `approved` (what users add/remove relative to
     machine drafts), `revealed_loss` (what compaction lost that
     mattered), `lens` (frames derived in PASS 1), `phase1` (what users
     keep vs demote).

2. Diagnose patterns, not instances — and CLASSIFY each revealed_loss
   before weighing it:
   - OMISSION: the content was absent from the summary → guideline
     evidence (an inclusion rule failed).
   - MIS-ENCODING: the content was PRESENT in the summary but wrongly
     construed — labeled with the wrong episode type, compressed under a
     frame the project does not use, a dead-end recorded as a success →
     LENS evidence, not guideline evidence. No inclusion rule can fix a
     wrong schema; adding one would only bloat guidelines.md.
   Mis-encodings are the highest-weight signal: a wrong lens fails by
   confident silent loss, which the user only catches after the fact.
   Weight: mis-encoded revealed_loss (lens) > omitted revealed_loss
   (guidelines) > edits > phase1 selections (selection tracks current
   salience, not future need — that is why the log exists).
   An edit that happened once is an anecdote; the same *class* of edit
   twice (e.g. "user keeps re-adding file paths", "revealed losses are
   always constraints stated early") is a candidate rule.

3. Propose at most 3 diffs total across the two files, each as: the rule
   or frame change (one line), which file it belongs in (per the
   classification above), the evidence (which events, counts), and what
   it would have changed. Show the diff to the user; apply only what they
   approve by editing guidelines.md / lens.md directly (Write/Edit
   tools). Lens diffs may revise Subject, Active frames, or the episode
   taxonomy — never delete a taxonomy entry that logged episodes still
   use.

4. Record the outcome (one event per file actually changed):
   …/compact-focus-list.sh record '{"event":"guideline_update","rules_added":N,"evidence_events":M}'
   …/compact-focus-list.sh record '{"event":"lens_revision","frames_changed":N,"evidence_events":M}'

5. If the log has fewer than ~5 signal events, say so and stop — do not
   invent rules from noise. Report what signal classes are still empty
   (e.g. no revealed_loss events yet, no mis-encodings classified yet) so
   the user knows what the loop is blind to.
