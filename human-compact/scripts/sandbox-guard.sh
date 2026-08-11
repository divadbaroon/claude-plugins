#!/usr/bin/env bash
# sandbox-guard.sh — PreToolUse gate for Bash in the study sandbox.
# Edit/Write/NotebookEdit are already denied via permissions; this closes the
# Bash escape hatch while still allowing the compact-focus instrument script
# (which only writes inside the study state dir).
# Allowed: a single invocation whose first token is .../compact-focus-list.sh,
# with no shell chaining (;|&) that could smuggle other commands.
set -u

IN=$(cat 2>/dev/null || true)
command -v jq >/dev/null 2>&1 || exit 0   # fail open: guard needs jq

TOOL=$(printf '%s' "$IN" | jq -r '.tool_name // empty' 2>/dev/null)
[ "$TOOL" = "Bash" ] || exit 0

CMD=$(printf '%s' "$IN" | jq -r '.tool_input.command // empty' 2>/dev/null)

if printf '%s' "$CMD" | grep -Eq '^[[:space:]]*[^;|&]*compact-focus-list\.sh([[:space:]]|$)' \
   && ! printf '%s' "$CMD" | grep -q '[;|&]'; then
  exit 0
fi

jq -nc '{hookSpecificOutput: {hookEventName: "PreToolUse",
         permissionDecision: "deny",
         permissionDecisionReason:
           "Study sandbox: shell commands are disabled here (only the compact-focus listing script is allowed). This fork makes no changes to your machine."}}'
exit 0
