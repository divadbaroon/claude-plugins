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
2. Print the threads as a plain text list in your reply — do NOT use the
   Bash tool or any other tool for this (a tool call triggers a permission
   prompt; plain text does not). One line per thread: the label, then that
   thread's user prompts indented beneath it, each on its own line. Keep
   each prompt to its opening words + "…" if long. This inline list is the
   full picture; there is no separate expandable view.
3. Then ask which thread(s) to preserve using the AskUserQuestion tool.
   The tool truncates aggressively — respect these hard limits or text is
   silently cut off:
   - Question string: short, fits one line (e.g. "Keep which threads?").
     Do not restate "which thread(s) should the compaction preserve".
   - At most 4 options. If step 2 found more than 4 threads, offer the 4
     most substantial; the plain-text list above already showed them all,
     and the user can pick "Something else" to name others in words.
   - Option label: 2-4 words. Option description: 8 words MAX, no trailing
     clause that needs finishing — a fragment that reads complete when cut.
   - Multi-select is fine (several threads can be kept).
   If the tool is unavailable, ask in plain text instead. The user may ask
   to see any thread's prompts in full before choosing.
4. On selection:
   - "Default summary": output `/compact` on its own line and tell the user
     to run it.
   - Otherwise: compose a one-line focus instruction describing the chosen
     thread(s) in the user's own words, then run `/compact focus on <that
     instruction>` via the SlashCommand tool. If the SlashCommand tool
     cannot run /compact, output that exact command on its own line for the
     user to run themselves.

Do not re-summarize the conversation yourself — the compaction will do that.
