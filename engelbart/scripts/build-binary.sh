#!/usr/bin/env bash
# Build the standalone engelbart binary with Bun.
#
# The npm package stays the source of truth: this compiles the same bin/lib
# code, with the vendored wheel embedded, into a self-contained executable
# that needs no Node on the target machine.
#
# Usage:
#   scripts/build-binary.sh                 # build for this machine
#   scripts/build-binary.sh darwin-arm64 linux-x64 ...
#
# Known targets: darwin-arm64 darwin-x64 linux-x64 linux-arm64
#                linux-x64-musl linux-arm64-musl windows-x64 windows-arm64
#
# Requires bun on PATH (or set BUN=/path/to/bun).
set -euo pipefail
cd "$(dirname "$0")/.."

BUN="${BUN:-bun}"
command -v "$BUN" >/dev/null || {
  echo "bun not found; install it or set BUN=/path/to/bun" >&2
  exit 1
}

bun_target() {
  case "$1" in
    darwin-arm64|darwin-x64|linux-x64|linux-arm64|linux-x64-musl|linux-arm64-musl|windows-x64|windows-arm64)
      echo "bun-$1" ;;
    *)
      echo "unknown target: $1" >&2
      exit 1 ;;
  esac
}

# Bun appends .exe to a Windows --compile output; name the artifact to match
# so its checksum file and the installer that downloads it agree.
target_outfile() {
  case "$1" in
    windows-*) echo "dist/engelbart-$1.exe" ;;
    *)         echo "dist/engelbart-$1" ;;
  esac
}

host_target() {
  local os arch
  case "$(uname -s)" in
    Darwin) os=darwin ;;
    Linux)  os=linux ;;
    *) echo "unsupported host: $(uname -s)" >&2; exit 1 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64)        arch=x64 ;;
    *) echo "unsupported host arch: $(uname -m)" >&2; exit 1 ;;
  esac
  echo "$os-$arch"
}

targets=("$@")
[ ${#targets[@]} -eq 0 ] && targets=("$(host_target)")

# Stage the embedded assets under stable names: compile/entry.js references
# them statically, while the wheel's real filename changes every release.
wheel=$(node -p "require('./vendor/manifest.json').wheel")
version=$(node -p "require('./package.json').version")
rm -rf compile/assets
mkdir -p compile/assets dist
cp vendor/manifest.json compile/assets/manifest.json
cp "vendor/$wheel" compile/assets/backend.whl

checksum() {
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi
}

for target in "${targets[@]}"; do
  out="$(target_outfile "$target")"
  "$BUN" build compile/entry.js \
    --compile \
    --target="$(bun_target "$target")" \
    --outfile "$out"
  base="$(basename "$out")"
  (cd dist && checksum "$base" > "$base.sha256")
  echo "built $out ($version)"
done
