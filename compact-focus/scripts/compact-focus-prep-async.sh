#!/usr/bin/env bash
# compact-focus-prep-async.sh — async PreCompact hook (async: true,
# asyncRewake: true). Replaces the hand-rolled nohup/PID/notification
# protocol: when the sync gate blocks a compaction, this hook prepares the
# ledger and its completion REWAKES Claude immediately with instructions to
# surface it — no waiting for the user's next message.
#
# Gating: runs only when the sync hook actually blocked (pause-blocked
# marker, polled briefly to absorb sync/async start-order races). All other
# PreCompact firings exit silently.
set -u
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)
BASE="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' | tr -cd 'A-Za-z0-9-')
SESSD="$BASE/sessions/${SID:-unknown}"

n=0
while [ $n -lt 30 ] && [ ! -e "$SESSD/pause-blocked" ]; do
  perl -e 'select(undef,undef,undef,0.1)' 2>/dev/null || sleep 1
  n=$((n + 1))
done
[ -e "$SESSD/pause-blocked" ] || exit 0
rm -f "$SESSD/pause-blocked" 2>/dev/null

SCRIPT="$HOME/.claude/skills/compact-focus/scripts/compact-focus-list.sh"
[ -x "$SCRIPT" ] || SCRIPT="$(dirname "$0")/compact-focus-list.sh"

COMPACT_FOCUS_STATE_DIR="$BASE" "$SCRIPT" prep-run >/dev/null 2>&1

if [ -r "$SESSD/ledger-ready" ]; then
  jq -nc --arg s "$SCRIPT" '{hookSpecificOutput: {hookEventName: "PreCompact",
    additionalContext: ("compact-focus: background ledger prep is DONE (async hook). Surface it NOW: run `" + $s + " ledger load`, print the markdown ledger with per-item context percentages, offer the checkbox widget / typed replies / `! cf` editor, run `" + $s + " surfaced`, then wait for the user'\''s decision.")}}'
else
  jq -nc '{hookSpecificOutput: {hookEventName: "PreCompact",
    additionalContext: "compact-focus: background ledger prep FAILED — prepare the ledger in-session via the compact-human skill (step 1) when the user next engages."}}'
fi
exit 0
