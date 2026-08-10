---
name: compact-human
description: Interactively choose what a conversation compaction should preserve, then run the focused compaction. Use when the user wants to steer /compact, or after a compact-focus pause suggested picking a focus.
---

The user wants to compact this conversation but choose what the summary
preserves in detail. Context is nearly full — keep every step terse.

1. From this session's actual user requests (ignore tool output and system
   noise), identify the distinct work threads — at most 6. Give each a
   2-4 word label. Decide the order now and use that SAME order everywhere
   below (prose list and picker must match by position).

2. Print the threads as a plain NUMBERED list in your reply — do NOT use the
   Bash tool or any other tool for this (a tool call triggers a permission
   prompt; plain text does not). Format, exactly:

   Work threads from this session:
   1. <Label>
      - "<opening words of one of that thread's prompts…>"
   2. <Label>
      - "<…>"

   Numbering starts at 1 and matches the picker order below. Keep each quoted
   prompt to its opening words + "…". Never reproduce pasted artifacts
   (code, logs, configs) — name them instead.

3. Then ask which thread(s) to preserve using the AskUserQuestion tool:
   - Question string: short, one line — "Keep which threads?"
   - One option PER THREAD, in the SAME ORDER as the numbered list, at most
     4 (AskUserQuestion caps options). If there are more than 4 threads,
     offer the first 4 and rely on the "Something else" affordance for the
     rest — the numbered list above already shows them all.
   - Option label = the thread's exact label from the list. Do NOT add an
     option description — the picker clips it, and the numbered list already
     carries the detail. Label only.
   - Multi-select is fine.
   If the tool is unavailable, ask in plain text by number instead. The user
   may ask to see any thread's prompts in full before choosing.

4. On selection:
   - "Default summary" / none: output `/compact` on its own line for the
     user to run.
   - Otherwise: compose a one-line focus instruction naming the chosen
     thread(s) in the user's own words, then run `/compact focus on <that
     instruction>` via the SlashCommand tool. If SlashCommand cannot run
     /compact, output that exact command on its own line for the user to run.

Do not re-summarize the conversation yourself — the compaction will do that.
