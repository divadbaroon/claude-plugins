#!/usr/bin/env bash
# hc — compatibility redirect to the human-vault npm installer
#   curl -fsSL https://raw.githubusercontent.com/divadbaroon/claude-plugins/main/install.sh | bash
set -eu

if ! command -v npx >/dev/null 2>&1; then
  printf '%s\n' 'hc now installs with npm. Install Node.js 18+ and run:' >&2
  printf '%s\n' '  npx human-vault' >&2
  exit 1
fi

printf '%s\n' 'Redirecting to the human-vault npm installer...'
exec npx --yes human-vault@latest
