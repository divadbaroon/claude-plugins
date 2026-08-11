#!/usr/bin/env bash
# statusline.sh — persistent study banner: sandbox notice + live context %.
# Receives statusline JSON on stdin; context is estimated from the last
# assistant usage entry in the transcript (input + cache tokens / 200k).
# Fails soft: with no transcript or jq, prints the banner without a %.
set -u

IN=$(cat 2>/dev/null || true)
. "$(dirname "$0")/lib-window.sh"
PCT=""
if command -v jq >/dev/null 2>&1 && [ -n "$IN" ]; then
  T=$(printf '%s' "$IN" | jq -r '.transcript_path // empty' 2>/dev/null)
  if [ -n "$T" ] && [ -r "$T" ]; then
    TOK=$(tail -80 "$T" | jq -rs '
      [ .[] | select(.message.usage?) | .message.usage
        | (.input_tokens // 0) + (.cache_creation_input_tokens // 0)
          + (.cache_read_input_tokens // 0) ] | last // 0' 2>/dev/null)
    if [ -n "$TOK" ] && [ "$TOK" -gt 0 ] 2>/dev/null; then
      infer_window "$IN" "$T"
      PCT=$(( TOK * 100 / WINDOW ))
      [ "$PCT" -gt 100 ] && PCT=100
    fi
  fi
fi

Y=$'\033[33m'; R=$'\033[31m'; G=$'\033[32m'; N=$'\033[0m'
LINE="${Y}⚠ SANDBOX FORK${N} · original chat untouched · file changes off"
if [ -n "$PCT" ]; then
  if [ "$PCT" -lt 50 ]; then
    LINE="$LINE · ctx ${G}${PCT}%${N} — low; ${Y}/resume${N} to pick a fuller chat"
  else
    LINE="$LINE · ctx ${R}${PCT}%${N} — run ${Y}/compact${N} when ready"
  fi
fi
printf '%s' "$LINE"
