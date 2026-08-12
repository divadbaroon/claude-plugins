#!/usr/bin/env bash
# human-compact (hc) — one-command installer
#   curl -fsSL https://raw.githubusercontent.com/divadbaroon/claude-plugins/main/install.sh | bash
set -u
REPO_URL="git+https://github.com/divadbaroon/claude-plugins.git@main#subdirectory=hc"
say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\n  x %s\n' "$*" >&2; exit 1; }
if [ ! -t 0 ] && [ -e /dev/tty ]; then exec </dev/tty; fi
say "human-compact installer"
command -v python3 >/dev/null 2>&1 || die "python3 is required (xcode-select --install)"
if ! command -v jq >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 && { note "installing jq..."; brew install --quiet jq; }
  command -v jq >/dev/null 2>&1 || die "jq is required (brew install jq)"
fi
if ! command -v pipx >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then note "installing pipx..."; brew install --quiet pipx
  else note "installing pipx via pip..."; python3 -m pip install --user --quiet pipx; fi
fi
pipx ensurepath >/dev/null 2>&1 || true
export PATH="$HOME/.local/bin:$PATH"
say "Installing hc from GitHub..."
pipx install --force "$REPO_URL" >/dev/null || die "pipx install failed"
command -v hc >/dev/null 2>&1 || die "hc not on PATH — open a new terminal and rerun"
note "hc installed"
if [ "${HC_INSTALL_ONLY:-0}" = "1" ]; then
  say "Install-only mode. Next: hc backup, hc refresh, hc goals --rebuild, hc ui"
  exit 0
fi
if [ -f "$HOME/.claude-vault/trajectory/config.json" ]; then
  say "Existing setup detected — vault, analysis, and goal tree are kept. Opening the UI..."
  exec hc ui
fi
say "Step 1/3 - Vault: capture + import your Claude Code history"
hc backup || die "hc backup did not complete"
say "Step 2/3 - First analysis (you choose the model; a few minutes)"
hc refresh || die "hc refresh did not complete"
say "Step 3/3 - Inferring your goal tree"
hc goals --rebuild --no-interact || die "goal inference did not complete"
say "Done. Opening your goals..."
exec hc ui
