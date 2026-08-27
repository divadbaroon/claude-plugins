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
# `curl … | bash` leaves this script's stdin on the pipe, at EOF, and the
# installer reads that as nobody being here to answer: it would print the
# command that installs Claude Code instead of offering to run it, and skip
# connecting an account. The terminal is still attached; hand it back.
if [ -r /dev/tty ]; then
  exec npx --yes engelbart-cli@latest </dev/tty
fi
exec npx --yes engelbart-cli@latest
