#!/usr/bin/env bash
# session-start.sh — one-time banner when the forked session opens: what the
# sandbox is, plus a redirect suggestion when the picked chat is under 50%
# context (the study wants sessions near the compaction point).
set -u

IN=$(cat 2>/dev/null || true)
command -v jq >/dev/null 2>&1 || exit 0

T=$(printf '%s' "$IN" | jq -r '.transcript_path // empty' 2>/dev/null)
. "$(dirname "$0")/lib-window.sh"
PCT=""
if [ -n "$T" ] && [ -r "$T" ]; then
  TOK=$(tail -80 "$T" | jq -rs '
    [ .[] | select(.message.usage?) | .message.usage
      | (.input_tokens // 0) + (.cache_creation_input_tokens // 0)
        + (.cache_read_input_tokens // 0) ] | last // 0' 2>/dev/null)
  if [ -n "$TOK" ] && [ "$TOK" -gt 0 ] 2>/dev/null; then
    infer_window "$IN" "$T"
    PCT=$(( TOK * 100 / WINDOW ))
  fi
fi

MSG="human-compact study sandbox: this is a FORK — your original chat is untouched and file changes are disabled here. Work normally, then run /compact to test the study's compaction flow."
if [ -n "$PCT" ]; then
  if [ "$PCT" -lt 50 ]; then
    MSG="$MSG This chat is only at ~${PCT}% context — the study works best near the limit; consider /resume and picking a fuller conversation."
  else
    MSG="$MSG Context is at ~${PCT}%."
  fi
fi

jq -nc --arg m "$MSG" '{systemMessage: $m}'
exit 0
