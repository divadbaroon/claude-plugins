---
name: compact-human
description: Interactively choose what a conversation compaction should preserve, then run the focused compaction. Use when the user wants to steer /compact, or after a compact-focus pause suggested picking a focus.
---

The user wants to compact this conversation but choose what the summary
preserves in detail. Context is nearly full — keep every step terse.

1. From this session's actual user requests (ignore tool output and system
   noise), identify the distinct work threads — at most 6.
   Quote budget, strict: for each thread, a 2-4 word label plus at most ONE
   fragment of the user's own phrasing, 10 words maximum. Never reproduce
   pasted artifacts (code, logs, configs, documents) — name them instead,
   e.g. "the settings.json you pasted". Long prompts get their opening
   words + "…", never the full text.
2. Emit the COMPLETE listing as a Bash tool call — `bash -c 'cat <<"EOF" …'`
   printing every thread label and, under each, every one of that thread's
   user prompts verbatim, one per line. Long output folds automatically in
   the terminal; tell the user it's expandable (ctrl+o) if they want the
   full list. The quote budget above applies to your prose only — this
   block is where the unabridged prompts live.
3. Ask which thread(s) to preserve using the AskUserQuestion tool: option
   label = the thread label, option description = the one short fragment;
   plus a "Default summary (no focus)" option. Mention the user can ask to
   see more of any thread before choosing — that is the expand affordance.
   If the tool is unavailable, ask in plain text.
4. On selection:
   - "Default summary": output `/compact` on its own line and tell the user
     to run it.
   - Otherwise: compose a one-line focus instruction describing the chosen
     thread(s) in the user's own words, then run `/compact focus on <that
     instruction>` via the SlashCommand tool. If the SlashCommand tool
     cannot run /compact, output that exact command on its own line for the
     user to run themselves.

Do not re-summarize the conversation yourself — the compaction will do that.
