#!/usr/bin/env bash
# compact-focus-notify.sh — UserPromptSubmit hook. Two duties:
# (1) when background ledger prep has finished, make the model surface the
#     ledger FIRST at the next turn — the in-conversation interrupt;
# (2) when demoted/graveyard stores are non-empty, carry the standing
#     revealed-loss critic instruction (daily-path parity with the study
#     wrapper's hook).
set -u
command -v jq >/dev/null 2>&1 || exit 0
S="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
SCRIPT="$HOME/.claude/skills/compact-focus/scripts/compact-focus-list.sh"

CTX=""
if [ -r "$S/ledger-ready" ]; then
  CTX="compact-focus: background ledger prep is DONE. Before addressing the user's message, surface the ledger: run \`$SCRIPT ledger load\`, print the ledger in the standard markdown shape with per-item context percentages, run \`$SCRIPT tui-inject\`, run \`$SCRIPT surfaced\` to clear the marker, then wait for the user's ledger reply (or handle their current message if it IS a ledger reply)."
fi

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
