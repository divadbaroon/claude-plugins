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

2. Put the FULL numbered listing into a collapsed block, not into your
   prose. Do this by running the Bash tool with EXACTLY this command shape:

   ~/.claude/skills/compact-focus/scripts/compact-focus-list.sh '<LISTING>'

   where <LISTING> is the complete numbered list — every thread as
   "N. Label" and every one of that thread's prompts on its own line
   beneath it, prompts trimmed to their opening words + "…". Single-quote
   the argument and escape any single quotes inside it. This script is
   read-only and prints its argument; its long output renders as a
   collapsed tool result the user expands with ctrl+o.
   - The FIRST time, the user sees one approval dialog: tell them to pick
     "Yes, and don't ask again" so it never appears again.
   - If the user denies the approval or the script is missing, fall back
     to printing the same numbered list as plain text in your reply.
   In your prose, print ONLY one line: "Full thread list above (ctrl+o to
   expand). Pick below:" — no inline listing.

3. Then ask which thread(s) to preserve using the AskUserQuestion tool:
   - Question string, exactly: "Keep which threads? (ctrl+o to show
     prompts for each thread)"
   - One option PER THREAD, in the SAME ORDER as the numbered list, at most
     4 (AskUserQuestion caps options). If there are more than 4 threads,
     offer the first 4; the numbered list above already shows all of them
     and the user can pick the "Something else" affordance for the rest.
   - Option label = the thread's exact label from the list.
   - Option description: REQUIRED and non-empty (an empty description makes
     the tool call fail). Target ~60 characters of real information. The
     visible width depends on the user's terminal — it may clip anywhere —
     so FRONT-LOAD: put the most informative words first and let detail
     trail, so a clip amputates the tail, never the meaning. Good:
     "Consent gate, email + probe automation still open". Bad: "The
     outstanding items regarding the consent gate" (meaning arrives last).
     Not "Thread N", not filler.
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
