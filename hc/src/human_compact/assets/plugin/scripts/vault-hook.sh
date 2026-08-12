#!/usr/bin/env bash
# vault-hook.sh — single dispatcher for all Vault lifecycle events (v0.1.0)
#
# Inert unless CLAUDE_VAULT=1. Local-only: jq, mkdir, cp, mv, date. Never
# blocks anything, never writes into the session, never touches the original
# transcript except to read it. Every failure path is a silent exit 0
# (visible in ~/.claude-vault/debug.log when VAULT_DEBUG=1).

[ "${CLAUDE_VAULT:-}" = "1" ] || exit 0

set -uo pipefail

VAULT_ROOT="${CLAUDE_VAULT_DIR:-$HOME/.claude-vault}"

dbg() {
  [ "${VAULT_DEBUG:-}" = "1" ] || return 0
  printf '{"ts":"%s","event":"%s","msg":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${EVENT:-?}" "$1" \
    >> "$VAULT_ROOT/debug.log" 2>/dev/null || true
}

INPUT=$(cat) || exit 0
command -v jq >/dev/null 2>&1 || exit 0

EVENT=$(jq -r '.hook_event_name // empty' <<<"$INPUT" 2>/dev/null) || exit 0
SID=$(jq -r '.session_id // empty' <<<"$INPUT" 2>/dev/null) || exit 0
TP=$(jq -r '.transcript_path // empty' <<<"$INPUT" 2>/dev/null) || TP=""
[ -n "$EVENT" ] && [ -n "$SID" ] || { dbg "missing event or session_id"; exit 0; }

# Sessions are filed under the LOCAL date they first started:
#   sessions/YYYY-MM-DD/<session_id>/
# Later events (including resumes on a different day) must find the existing
# folder rather than recomputing the date, so look up first, create second.
BASE=""
for d in "$VAULT_ROOT/sessions"/*/"$SID"; do
  [ -d "$d" ] && BASE="$d" && break
done
[ -n "$BASE" ] || BASE="$VAULT_ROOT/sessions/$(date +%Y-%m-%d)/$SID"
mkdir -p "$BASE" 2>/dev/null || { dbg "mkdir failed"; exit 0; }
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
NOW_FS=$(date -u +%Y%m%dT%H%M%SZ)

# Copy the transcript to a destination, atomically. Silent no-op when the
# transcript is missing/unreadable (documented: written asynchronously).
snap() { # $1 = destination path
  [ -n "$TP" ] && [ -r "$TP" ] || { dbg "transcript unavailable"; return 0; }
  cp "$TP" "$1.tmp" 2>/dev/null && mv "$1.tmp" "$1" 2>/dev/null \
    && dbg "snapshot -> ${1##*/}" || dbg "snapshot failed"
}

case "$EVENT" in
  SessionStart)
    if [ ! -e "$BASE/metadata.json" ]; then
      SRC=$(jq -r '.source // empty' <<<"$INPUT" 2>/dev/null) || SRC=""
      CWD=$(jq -r '.cwd // empty' <<<"$INPUT" 2>/dev/null) || CWD=""
      jq -n --arg sid "$SID" --arg cwd "$CWD" --arg ts "$NOW" \
            --arg tp "$TP" --arg src "$SRC" \
        '{session_id:$sid, cwd:$cwd, started_at:$ts,
          transcript_path:$tp, start_source:$src}' \
        > "$BASE/metadata.json" 2>/dev/null || dbg "metadata write failed"
    fi
    snap "$BASE/conversation.jsonl"
    # Goal-aware context: surface the user's derived goal state to the new
    # session (written by `hc`; absent file = silent no-op).
    GOALCTX="$VAULT_ROOT/trajectory/goal_context.md"
    if [ -r "$GOALCTX" ]; then
      jq -n --rawfile ctx "$GOALCTX" \
        '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}' \
        2>/dev/null && dbg "goal context injected" || true
    fi
    ;;
  PreCompact)
    TRIGGER=$(jq -r '.trigger // "unknown"' <<<"$INPUT" 2>/dev/null) || TRIGGER="unknown"
    N=$(cat "$BASE/.compaction-counter" 2>/dev/null | tr -cd '0-9'); N=$(( ${N:-0} + 1 ))
    printf '%s\n' "$N" > "$BASE/.compaction-counter" 2>/dev/null || true
    NN=$(printf '%03d' "$N")
    mkdir -p "$BASE/snapshots" 2>/dev/null || true
    snap "$BASE/snapshots/pre-compact-$NN-$NOW_FS-$TRIGGER.jsonl"
    snap "$BASE/conversation.jsonl"
    ;;
  PostCompact)
    if jq -e 'has("compact_summary")' >/dev/null 2>&1 <<<"$INPUT"; then
      N=$(cat "$BASE/.compaction-counter" 2>/dev/null | tr -cd '0-9'); N=${N:-0}
      NN=$(printf '%03d' "$N")
      mkdir -p "$BASE/compactions" 2>/dev/null || true
      jq --arg ts "$NOW" '{ts:$ts, trigger:.trigger, compact_summary:.compact_summary}' \
        <<<"$INPUT" > "$BASE/compactions/summary-$NN-$NOW_FS.json" 2>/dev/null \
        && dbg "summary $NN stored" || dbg "summary write failed"
    else
      dbg "PostCompact without compact_summary"
    fi
    ;;
  SessionEnd)
    REASON=$(jq -r '.reason // "unknown"' <<<"$INPUT" 2>/dev/null) || REASON="unknown"
    snap "$BASE/conversation.jsonl"
    printf '{"ended_at":"%s","reason":"%s"}\n' "$NOW" "$REASON" \
      >> "$BASE/ends.jsonl" 2>/dev/null || true
    # Continuous Context Lens: enqueue this conversation and nudge a worker.
    # Enqueue is a file touch (idempotent); the worker exits if one is active.
    if command -v hc >/dev/null 2>&1; then
      QDIR="$VAULT_ROOT/trajectory/queue"
      mkdir -p "$QDIR" 2>/dev/null && printf '%s\n' "$NOW" > "$QDIR/$SID" 2>/dev/null || true
      ( nohup hc worker >> "$VAULT_ROOT/trajectory/worker.log" 2>&1 & ) 2>/dev/null || true
      dbg "enqueued for lens update"
    fi
    ;;
  *) dbg "ignored event" ;;
esac

exit 0
