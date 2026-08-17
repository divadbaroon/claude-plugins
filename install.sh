#!/usr/bin/env bash
# hc — compatibility redirect to the engelbart-cli npm installer
#   curl -fsSL https://raw.githubusercontent.com/divadbaroon/claude-plugins/main/install.sh | bash
set -eu

if ! command -v npx >/dev/null 2>&1; then
  printf '%s\n' 'hc now installs with npm. Install Node.js 18+ and run:' >&2
  printf '%s\n' '  npx engelbart-cli' >&2
  exit 1
fi

printf '%s\n' 'Redirecting to the engelbart-cli npm installer...'
exec npx --yes engelbart-cli@latest
