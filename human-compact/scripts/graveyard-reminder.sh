#!/usr/bin/env bash
# graveyard-reminder.sh — UserPromptSubmit hook: the practical form of the
# "background critic". When the study state dir holds demoted/graveyard
# entries, every user prompt carries a one-line standing instruction telling
# the model to query the stores if the prompt references context it does not
# recognize. No model calls, no daemon — the main model becomes the critic.
set -u

command -v jq >/dev/null 2>&1 || exit 0
S="${COMPACT_FOCUS_STATE_DIR:-}"
[ -n "$S" ] || exit 0

C=0
for F in "$S/graveyard.jsonl" "$S/demoted.jsonl"; do
  [ -r "$F" ] && C=$((C + $(wc -l <"$F" | tr -cd '0-9')))
done
[ "$C" -gt 0 ] || exit 0

SCRIPT="$HOME/.claude/skills/compact-focus/scripts/compact-focus-list.sh"
[ -x "$SCRIPT" ] || SCRIPT="compact-focus-list.sh"

jq -nc --arg n "$C" --arg sc "$SCRIPT" '{hookSpecificOutput: {
  hookEventName: "UserPromptSubmit",
  additionalContext: ("Context critic: " + $n + " demoted/buried context entries exist from compaction. If this prompt references anything you do not recognize from the current context, FIRST run `" + $sc + " graveyard query <terms>` (or `recall <id>`) and use what it returns before answering.")}}'
exit 0
