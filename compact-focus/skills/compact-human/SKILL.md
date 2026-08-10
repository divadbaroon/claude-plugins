---
name: compact-human
description: Interactively choose what a conversation compaction should preserve, then run the focused compaction. Use when the user wants to steer /compact, or after a compact-focus pause suggested picking a focus.
---

The user wants to compact this conversation but choose what the summary
preserves in detail. Context is nearly full — keep prose terse.

INVARIANT for the whole flow: the collapsed Bash results the user expands
with ctrl+o must only ever contain the CURRENT selection. Never print the
full prompt list — inline or as a tool result — before categories are
selected.

All script calls below use EXACTLY this command shape (stable prefix, so
one "Yes, and don't ask again" approval covers every mode):

   ~/.claude/skills/compact-focus/scripts/compact-focus-list.sh <mode> …

If the user denies the approval or the script is missing, fall back to
plain text: print category labels only, and scoped prompt lists only after
selection, following the same steps.

1. LOAD CATEGORIES. Run the script with no arguments. It prints the labels
   and counts of the thread file the PreCompact pause saved
   (threads.json: {"threads":{"1":{"label":"…","prompts":["…",…]},…}}).
   - If it shows 2+ categories, reuse them as-is — their numbers match the
     pause notice the user already saw.
   - If it shows nothing, or a single fallback category ("Recent prompts"),
     group this session's actual user requests yourself (ignore tool output
     and system noise) into 2-6 categories with 2-4 word labels, every
     relevant prompt in exactly one category, prompts verbatim and
     single-line. Store them by running:
       …/compact-focus-list.sh save '<JSON>'
     with the JSON in the shape above, single-quoted, any single quotes
     inside replaced with the typographic ’. The script validates, saves,
     and echoes the labels back.

2. PICK CATEGORIES with the AskUserQuestion tool:
   - Question string, exactly: "Keep which threads? (type a number to
     preview one · none = default summary)"
   - multiSelect: true. One option PER CATEGORY, in numeric order, at most
     4 (AskUserQuestion caps options). If there are more than 4, offer the
     first 4 and say the rest are pickable by number via "Something else".
   - Option label = the category's exact label.
   - Option description: REQUIRED and non-empty (an empty description makes
     the tool call fail). Target ~60 characters of real information.
     Terminal width may clip anywhere, so FRONT-LOAD: most informative
     words first. Good: "Consent gate, email + probe automation still
     open". Bad: "The outstanding items regarding the consent gate".
   - PREVIEW LOOP: if the answer is just a category number (e.g. "2" or
     "2?"), do NOT compact. Run `show 2` (that one category, collapsed;
     ctrl+o expands it), then re-ask the SAME question. Repeat as needed.

3. NOTHING SELECTED ("Default summary", empty pick, or "none"): do NOT run
   the show command — there is nothing for ctrl+o to open. Output `/compact`
   on its own line for the user to run. Done.

4. RENDER THE SELECTION. For selected categories, e.g. 1 and 3, run:
       …/compact-focus-list.sh show 1,3
   This collapsed result IS the review doc: every prompt of the selected
   categories, globally numbered [1]…[N]. In prose print ONLY: "Your
   selection is above (ctrl+o to expand) — N prompts, all kept by default."

5. PER-PROMPT DESELECT with AskUserQuestion:
   - Question: "Keep all N prompts from <labels>?"
   - Options, exactly 3: "Keep all (Recommended)" — proceed with
     everything shown; "Drop some" — description: "Type the [numbers] to
     drop in Other, e.g. drop 2 5"; "None — default summary".
   - On "Drop some" / typed numbers: re-run
       …/compact-focus-list.sh show 1,3 drop 2,5
     (same keys, same order — numbering stays stable). The new collapsed
     result shows what remains; re-ask this question with updated N until
     "Keep all" or a typed confirmation. Drops accumulate: pass ALL dropped
     numbers each time.
   - On "None": treat as step 3.

6. COMPACT. Compose a one-line focus instruction naming the kept
   category(ies) in the user's own words; if prompts were dropped, add
   "; omit <what the dropped prompts covered>". Run `/compact focus on
   <that instruction>` via the SlashCommand tool; if SlashCommand cannot
   run /compact, output that exact command on its own line for the user.

Do not re-summarize the conversation yourself — the compaction will do that.
