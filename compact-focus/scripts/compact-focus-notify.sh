#!/usr/bin/env bash
# compact-focus-notify.sh — UserPromptSubmit hook. Two duties:
# (1) when background ledger prep has finished, make the model surface the
#     ledger FIRST at the next turn — the in-conversation interrupt;
# (2) when demoted/graveyard stores are non-empty, carry the standing
#     revealed-loss critic instruction (daily-path parity with the study
#     wrapper's hook).
set -u
command -v jq >/dev/null 2>&1 || exit 0
BASE="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
SID=$(cat "$BASE/current-session" 2>/dev/null | tr -cd 'A-Za-z0-9-')
S="$BASE/sessions/${SID:-unknown}"
SCRIPT="$HOME/.claude/skills/compact-focus/scripts/compact-focus-list.sh"

CTX=""

C=0
for F in "$S/graveyard.jsonl" "$S/demoted.jsonl"; do
  [ -r "$F" ] && C=$((C + $(wc -l <"$F" | tr -cd '0-9')))
done
if [ "$C" -gt 0 ]; then
  [ -n "$CTX" ] && CTX="$CTX "
  CTX="${CTX}Context critic: $C demoted/buried entries exist. If the user's prompt references anything you do not recognize, run \`$SCRIPT graveyard query <terms>\` before answering."
fi

[ -n "$CTX" ] || exit 0
jq -nc --arg c "$CTX" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $c}}'
exit 0
