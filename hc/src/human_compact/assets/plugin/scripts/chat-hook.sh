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

if ! command -v hc >/dev/null 2>&1; then
  if [ "$IS_EXPANSION" = "1" ]; then
    printf '%s\n' '{"decision":"block","reason":"hc-ui could not open: hc is unavailable to Claude Code; run hc install, then restart Claude Code"}'
  fi
  exit 0
fi

OUTPUT=$(printf '%s' "$INPUT" | hc chat-hook 2>/dev/null)
STATUS=$?
if [ -n "$OUTPUT" ]; then
  printf '%s\n' "$OUTPUT"
elif [ "$IS_EXPANSION" = "1" ]; then
  printf '%s\n' "{\"decision\":\"block\",\"reason\":\"hc-ui could not open: hc chat-hook exited without a response (status $STATUS)\"}"
fi
exit 0
