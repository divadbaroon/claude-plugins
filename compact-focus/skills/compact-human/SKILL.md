---
name: compact-human
description: Interactively choose what a conversation compaction should preserve, then run the focused compaction. Use when the user wants to steer /compact, or after a compact-focus pause suggested picking a focus.
---

The user wants to compact this conversation but choose what the summary
preserves in detail. Context is nearly full — keep prose terse.

1. From this session's actual user requests (ignore tool output and system
   noise), identify the distinct work threads — at most 6. Give each a
   2-4 word label. Decide the order now and use that SAME order everywhere
   below (prose list and picker must match by position).

2. Print the threads as a plain NUMBERED list in your reply — do NOT use the
   Bash tool or any other tool for this (a tool call triggers a permission
   prompt; plain text does not). Format, exactly — a blank line between
   threads, and EVERY one of that thread's user prompts listed vertically
   beneath it, one per line:

   Work threads from this session:

   **1. <Label>**
   - "<opening words of first prompt in this thread…>"
   - "<opening words of second prompt…>"

   **2. <Label>**
   - "<…>"

   CRITICAL rendering rules: each numbered label is BOLD (**N. Label**) —
   bold makes it a paragraph instead of a list item, which is what forces
   visible spacing between threads in the terminal. Dash lines start at the
   LEFT MARGIN, not indented. Blank line before every bold label. List
   all of a thread's prompts, not just one. Keep each quoted prompt to
   its opening words + "…". Never reproduce pasted artifacts (code, logs,
   configs) — name them instead. Numbering starts at 1 and matches the
   picker order below.

3. Then ask which thread(s) to preserve using the AskUserQuestion tool:
   - Question string: short, one line — "Keep which threads?"
   - One option PER THREAD, in the SAME ORDER as the numbered list, at most
     4 (AskUserQuestion caps options). If there are more than 4 threads,
     offer the first 4; the numbered list above already shows all of them
     and the user can pick the "Something else" affordance for the rest.
   - Option label = the thread's exact label from the list.
   - Option description: REQUIRED and non-empty (an empty description makes
     the tool call fail). HARD LIMIT 25 CHARACTERS — the UI clips at ~28,
     so count characters, not words, and make it read complete: "Consent +
     open items", "Invite-token accounts", "Day 6-8 booking". Not "Thread
     N", not filler, no sentence that needs finishing.
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
