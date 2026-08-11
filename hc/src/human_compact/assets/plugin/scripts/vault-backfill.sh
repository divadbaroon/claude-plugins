#!/usr/bin/env bash
# vault-backfill.sh — one-time import of existing Claude Code transcripts
# into the Vault layout. Safe to re-run: sessions already in the vault
# (live-captured or previously imported) are skipped. Originals untouched.
#
#   vault-backfill.sh            import everything found
#   vault-backfill.sh --dry-run  show what would be imported
#
# Metadata marks imported sessions with start_source "backfill".

set -uo pipefail

VAULT_ROOT="${CLAUDE_VAULT_DIR:-$HOME/.claude-vault}"
PROJECTS="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY=1

command -v jq >/dev/null 2>&1 || { echo "jq required (brew install jq)"; exit 1; }
[ -d "$PROJECTS" ] || { echo "no transcripts directory at $PROJECTS"; exit 1; }

imported=0; skipped=0; failed=0

for tr in "$PROJECTS"/*/*.jsonl; do
  [ -e "$tr" ] || continue
  sid=$(basename "$tr" .jsonl)

  # already in the vault (any date folder)?
  found=""
  for d in "$VAULT_ROOT/sessions"/*/"$sid"; do
    [ -d "$d" ] && found=1 && break
  done
  if [ -n "$found" ]; then skipped=$((skipped+1)); continue; fi

  first=$(head -1 "$tr" 2>/dev/null) || first=""
  ts=$(jq -r '.timestamp // empty' <<<"$first" 2>/dev/null) || ts=""
  cwd=$(jq -r '.cwd // empty' <<<"$first" 2>/dev/null) || cwd=""

  # local date from the first entry's timestamp; file mtime as fallback
  day=""
  if [ -n "$ts" ]; then
    epoch=$(jq -rn --arg t "$ts" '$t | sub("\\.[0-9]+Z$";"Z") | fromdate? // empty' 2>/dev/null) || epoch=""
    if [ -n "$epoch" ]; then
      day=$(date -r "$epoch" +%Y-%m-%d 2>/dev/null) || day=$(date -d "@$epoch" +%Y-%m-%d 2>/dev/null) || day=""
    fi
  fi
  [ -n "$day" ] || day=$(date -r "$tr" +%Y-%m-%d 2>/dev/null || date -d "@$(stat -c %Y "$tr" 2>/dev/null)" +%Y-%m-%d 2>/dev/null) || day="unknown-date"

  if [ -n "$DRY" ]; then
    echo "would import: $sid  ->  sessions/$day/  (${cwd:-cwd unknown})"
    imported=$((imported+1)); continue
  fi

  base="$VAULT_ROOT/sessions/$day/$sid"
  if mkdir -p "$base" 2>/dev/null \
     && cp "$tr" "$base/conversation.jsonl.tmp" 2>/dev/null \
     && mv "$base/conversation.jsonl.tmp" "$base/conversation.jsonl" 2>/dev/null \
     && jq -n --arg sid "$sid" --arg cwd "$cwd" --arg ts "$ts" --arg tp "$tr" \
          --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
          '{session_id:$sid, cwd:$cwd, started_at:$ts, transcript_path:$tp,
            start_source:"backfill", imported_at:$now}' \
          > "$base/metadata.json" 2>/dev/null; then
    echo "imported: $sid -> sessions/$day/"
    imported=$((imported+1))
  else
    echo "FAILED:   $sid" >&2
    failed=$((failed+1))
  fi
done

echo
echo "backfill complete: $imported imported, $skipped already present, $failed failed"
