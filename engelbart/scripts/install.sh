#!/bin/sh
# Engelbart installer: a self-contained binary, so the machine needs nothing
# first -- no Node, no npm, no Python. Everything after the download is the
# same engelbart CLI that npm installs; this script only fetches the right
# binary, checks its hash, and runs it.
#
#   curl -fsSL https://berkeley.mathetic.com/engelbart/install.sh | sh
#
# The whole script runs inside main() so a truncated download executes
# nothing at all.
set -eu

REPO="divadbaroon/claude-plugins"
BASE="https://github.com/$REPO/releases/download/engelbart-latest"

say() { printf '%s\n' "$*" >&2; }
fail() { say "engelbart: $*"; exit 1; }

detect_target() {
  os=$(uname -s)
  case "$os" in
    Darwin) os=darwin ;;
    Linux)  os=linux ;;
    *) fail "unsupported OS: $os (engelbart supports macOS and Linux)" ;;
  esac
  arch=$(uname -m)
  case "$arch" in
    arm64|aarch64) arch=arm64 ;;
    x86_64)        arch=x64 ;;
    *) fail "unsupported architecture: $arch" ;;
  esac
  libc=""
  if [ "$os" = linux ] && ldd --version 2>&1 | grep -qi musl; then
    libc="-musl"
  fi
  printf '%s' "$os-$arch$libc"
}

verify() {
  # The .sha256 file names the binary it hashes, so -c checks the pair.
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$1" && sha256sum -c "$2.sha256" >/dev/null)
  elif command -v shasum >/dev/null 2>&1; then
    (cd "$1" && shasum -a 256 -c "$2.sha256" >/dev/null)
  else
    fail "neither sha256sum nor shasum is available to verify the download"
  fi
}

main() {
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  target=$(detect_target)
  dest="${ENGELBART_INSTALL_DIR:-$HOME/.local/bin}"
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/engelbart-install.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT

  say "Downloading engelbart-$target..."
  curl -fsSL "$BASE/engelbart-$target" -o "$tmp/engelbart-$target"
  curl -fsSL "$BASE/engelbart-$target.sha256" -o "$tmp/engelbart-$target.sha256"
  verify "$tmp" "engelbart-$target" || fail "downloaded binary failed its SHA-256 check"

  mkdir -p "$dest"
  chmod +x "$tmp/engelbart-$target"
  mv "$tmp/engelbart-$target" "$dest/engelbart"
  say "Installed $dest/engelbart"

  case ":$PATH:" in
    *":$dest:"*) ;;
    *) say ""
       say "Note: $dest is not on your PATH. Add this to your shell profile:"
       say "    export PATH=\"$dest:\$PATH\"" ;;
  esac

  # Hand off to the real installer; it explains everything from here.
  "$dest/engelbart" install "$@"
}

main "$@"
