#!/usr/bin/env bash
# Thin Claude Code hook adapter. Chat-scoped capture is always active once
# installed; unlike vault-hook.sh it does not require CLAUDE_VAULT=1.

set -uo pipefail

# Internal Claude CLI inference also loads installed hooks. Without this guard,
# one goal-analysis subprocess would create another chat and recurse forever.
[ "${HC_CHAT_INFERENCE:-}" = "1" ] && exit 0

INPUT=$(cat) || exit 0
IS_EXPANSION=0
if [[ "$INPUT" =~ \"hook_event_name\"[[:space:]]*:[[:space:]]*\"UserPromptExpansion\" ]]; then
  IS_EXPANSION=1
fi

HC_CMD=""
if [ -n "${HC_EXECUTABLE:-}" ] && [ -x "$HC_EXECUTABLE" ]; then
  HC_CMD="$HC_EXECUTABLE"
elif [ -x "$HOME/.human-compact/bin/hc" ]; then
  HC_CMD="$HOME/.human-compact/bin/hc"
else
  HC_CMD=$(command -v hc 2>/dev/null || true)
fi

if [ -z "$HC_CMD" ]; then
  if [ "$IS_EXPANSION" = "1" ]; then
    printf '%s\n' '{"decision":"block","reason":"goals-ui could not open: its runtime is unavailable; rerun npx engelbart-cli, then restart Claude Code"}'
  elif [[ "$INPUT" =~ \"hook_event_name\"[[:space:]]*:[[:space:]]*\"SessionStart\" ]]; then
    # Installed from the marketplace without the runtime: hooks would otherwise
    # do nothing at all, which reads as a broken plugin rather than a missing
    # dependency. Say it once, at the only moment it is actionable.
    printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"The vault plugin is installed but its runtime is not. Goals, /goals-ui and goal-bound sessions stay inactive until you run: npx engelbart-cli"}}'
  fi
  exit 0
fi

OUTPUT=$(printf '%s' "$INPUT" | "$HC_CMD" chat-hook "$@" 2>/dev/null)
STATUS=$?
if [ -n "$OUTPUT" ]; then
  printf '%s\n' "$OUTPUT"
elif [ "$IS_EXPANSION" = "1" ]; then
  printf '%s\n' "{\"decision\":\"block\",\"reason\":\"goals-ui could not open: hc chat-hook exited without a response (status $STATUS)\"}"
fi
exit 0
