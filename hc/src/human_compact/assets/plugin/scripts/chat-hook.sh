#!/usr/bin/env bash
# Thin Claude Code hook adapter. Chat-scoped capture is always active once
# installed; unlike vault-hook.sh it does not require CLAUDE_VAULT=1.

set -uo pipefail

# Internal Claude CLI inference also loads installed hooks. Without this guard,
# one goal-analysis subprocess would create another chat and recurse forever.
[ "${HC_CHAT_INFERENCE:-}" = "1" ] && exit 0

command -v hc >/dev/null 2>&1 || exit 0

INPUT=$(cat) || exit 0
printf '%s' "$INPUT" | hc chat-hook 2>/dev/null || true
exit 0
