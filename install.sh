#!/bin/sh
# Compatibility entrypoint for the standalone Engelbart installer.
#   curl -fsSL https://raw.githubusercontent.com/divadbaroon/claude-plugins/main/install.sh | sh
set -eu

URL="https://berkeley.mathetic.com/engelbart/install.sh"
command -v curl >/dev/null 2>&1 || {
  printf '%s\n' 'engelbart: curl is required' >&2
  exit 1
}

printf '%s\n' 'Redirecting to the standalone Engelbart installer...' >&2
tmp=$(mktemp "${TMPDIR:-/tmp}/engelbart-redirect.XXXXXX")
trap 'rm -f "$tmp"' EXIT HUP INT TERM
curl -fsSL "$URL" -o "$tmp"
sh "$tmp" "$@"
