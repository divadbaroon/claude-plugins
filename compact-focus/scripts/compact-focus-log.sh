#!/usr/bin/env bash
# compact-focus-log.sh — PostCompact hook (plugin, v0.3.4)
#
#   0. FLIGHT RECORDER: unconditionally record that we were invoked, and how
#      much stdin arrived — pure shell, before jq can silently no-op.
#   1. Clear the pause ticket for this session.
#   2. Append the generated compact_summary next to the stated focus.
#
# State dir resolution MUST match compact-focus.sh exactly.

set -uo pipefail

INPUT=$(cat)

STATE_DIR="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

printf '{"ts":"%s","event":"PostCompactProbe","stdin_bytes":%s}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${#INPUT}" >>"$STATE_DIR/log.jsonl" 2>/dev/null || true

command -v jq >/dev/null 2>&1 || exit 0

SESSION_ID=$(jq -r '.session_id // empty' <<<"$INPUT")
rm -f "$STATE_DIR/paused-${SESSION_ID:-unknown}" 2>/dev/null || true

jq -c --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{ts:$ts, event:"PostCompact", session_id:.session_id,
    trigger:.trigger, compact_summary:.compact_summary}' \
  <<<"$INPUT" >>"$STATE_DIR/log.jsonl" 2>/dev/null || true

exit 0
