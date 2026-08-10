#!/usr/bin/env bash
# compact-focus-list.sh — read-only lister with a STABLE command string.
#
# Prints the thread listing passed as its single argument (or, with no
# argument, the last pause's saved prompts). Exists so the listing lands as
# a Bash tool RESULT — which the terminal renders collapsed with a
# ctrl+o expand — behind a command you approve once with "don't ask again".
# No writes, no network, no side effects: safe to allow permanently.
set -u
if [ "$#" -gt 0 ]; then
  printf '%s\n' "$1"
else
  S="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
  if [ -r "$S/prompts.txt" ]; then cat "$S/prompts.txt"; else echo "(no saved thread list)"; fi
fi
