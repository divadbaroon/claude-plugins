#!/usr/bin/env bash
# compact-focus.sh — PreCompact hook (plugin, v0.2.0)
#
# Pauses one compaction per session and shows the user their recent prompts —
# grouped into labeled threads by a fast Claude model when possible, verbatim
# list otherwise — so they can rerun /compact with a focus instead of letting
# the default summarizer guess.
#
# Behavior:
#   manual /compact WITH a focus argument  -> allow immediately (user already chose)
#   already paused once this session       -> allow (never block twice)
#   otherwise                              -> block via JSON {"decision":"block"},
#                                             thread list in reason (the channel
#                                             that renders in the block notice)
#
# Grouping is a garnish on the live-tested verbatim path: ANY failure of the
# child call (no CLI, timeout, auth error, malformed output) falls back to
# the flat verbatim list. Disable grouping with COMPACT_FOCUS_NO_GROUPING=1;
# change the model with COMPACT_FOCUS_GROUP_MODEL (default: haiku).
#
# Fails OPEN everywhere: if jq is missing, the transcript is unreadable, or
# state can't be written, compaction proceeds untouched.
#
# State lives in (first match wins):
#   $COMPACT_FOCUS_STATE_DIR      explicit override / test harness
#   $CLAUDE_PLUGIN_DATA           plugin install (persists across plugin updates)
#   ~/.claude/compact-focus       bare project-level install

set -uo pipefail

INPUT=$(cat)

STATE_DIR="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
PLUGIN_VERSION="0.4.7"

# No jq -> warn once (hand-written JSON, no dependencies), then fail open forever.
if ! command -v jq >/dev/null 2>&1; then
  MARKER="$STATE_DIR/jq-warned"
  if [ ! -e "$MARKER" ] && mkdir -p "$STATE_DIR" 2>/dev/null && touch "$MARKER" 2>/dev/null; then
    printf '{"systemMessage":"compact-focus: jq not found, so the compaction pause is disabled. Install jq (e.g. brew install jq) to enable it. Compaction ran normally."}\n'
  fi
  exit 0
fi

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0

SESSION_ID=$(jq -r '.session_id // empty' <<<"$INPUT")
TRIGGER=$(jq -r '.trigger // empty' <<<"$INPUT")
TRANSCRIPT=$(jq -r '.transcript_path // empty' <<<"$INPUT")
FOCUS=$(jq -r '.custom_instructions // empty' <<<"$INPUT")

# Trigger-aware pause expiry: a human at the keyboard revisits /compact on a
# minutes timescale, so manual pauses expire fast (bare /compact twice in
# quick succession = accept defaults; minutes later = fresh menu). Auto keeps
# a long window for unattended flow.
if [[ "$TRIGGER" == "manual" ]]; then
  PAUSE_TTL="${COMPACT_FOCUS_PAUSE_TTL_MANUAL:-120}"
else
  PAUSE_TTL="${COMPACT_FOCUS_PAUSE_TTL:-1800}"
fi

LOG="$STATE_DIR/log.jsonl"
SENTINEL="$STATE_DIR/paused-${SESSION_ID:-unknown}"

log() { # $1 = action, $2 = list_mode (optional)
  jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg sid "$SESSION_ID" \
         --arg trig "$TRIGGER" --arg act "$1" --arg foc "$FOCUS" \
         --arg lm "${2:-}" --arg v "$PLUGIN_VERSION" \
         '{ts:$ts, v:$v, event:"PreCompact", session_id:$sid, trigger:$trig,
           action:$act, custom_instructions:$foc, list_mode:$lm}
          | with_entries(select(.value != ""))' >>"$LOG" 2>/dev/null || true
}

# A manual /compact that already carries a focus: the user has answered the
# question this tool exists to ask. Log the stated focus and allow.
if [[ "$TRIGGER" == "manual" && -n "$FOCUS" ]]; then
  rm -f "$SENTINEL" 2>/dev/null || true
  log allow_focused
  exit 0
fi

# A pause is pending for this session. The pass-through is a single-use
# coupon, not a time window: a fresh ticket means the user saw the menu and
# declined, so this ONE attempt proceeds with defaults and the ticket is
# consumed -- the next attempt, whenever it comes, menus fresh. Age only
# distinguishes declining-now from abandoned-long-ago: a stale ticket
# (manual 2 min / auto 30 min) re-menus instead of silently spending itself.
if [[ -e "$SENTINEL" ]]; then
  PAUSED_AT=$(cat "$SENTINEL" 2>/dev/null | tr -cd '0-9')
  NOW=$(date +%s)
  if [[ -n "$PAUSED_AT" ]] && (( NOW - PAUSED_AT < PAUSE_TTL )); then
    rm -f "$SENTINEL" 2>/dev/null || true
    log allow_pause_expired
    jq -nc '{systemMessage:
      "compact-focus: no focus given during the pause - compacting with the default summary."}'
    exit 0
  fi
  # stale or unreadable: treat as a fresh sitting and pause again below
fi

# Manual pause: a human just typed /compact, so the menu is one command away.
# Skip transcript parsing and the grouping call entirely; point at the
# interactive picker. Auto pauses below keep the full thread list.
if [[ "$TRIGGER" == "manual" ]]; then
  date +%s >"$SENTINEL" 2>/dev/null || true
  log paused "pointer"
  MSG="
⏸ Paused. Pick what to keep:  /compact-human   ·   run /compact again for the default summary"
  jq -nc --arg msg "$MSG" '{decision:"block", reason:$msg, systemMessage:$msg}'
  exit 0
fi

# Extract the user's own prompt turns from the transcript, raw, one per line.
# Tool results, command wrappers, meta lines, and prior compact summaries all
# arrive as "user"-typed entries and are filtered out.
RAW=""
if [[ -n "$TRANSCRIPT" && -r "$TRANSCRIPT" ]]; then
  RAW=$(jq -rs '
    [ .[]
      | select(.type? == "user")
      | select(.isMeta? != true)
      | select(.isCompactSummary? != true)
      | .message.content?
      | if type == "string" then .
        elif type == "array" then
          ([ .[] | select(.type? == "text") | .text ] | join(" "))
        else empty end
    ]
    | map(gsub("\\s+"; " ") | ltrimstr(" "))
    | map(select(length > 0))
    | map(select(startswith("<") | not))
    | map(select(startswith("/") | not))
    | .[-25:]
    | map(if length > 100 then .[0:100] + "…" else . end)
    | join("\n")
  ' "$TRANSCRIPT" 2>/dev/null) || RAW=""
fi

# Verbatim fallback: the last 12, bulleted. This is the live-tested path.
THREADS=""
[[ -n "$RAW" ]] && THREADS=$(printf '%s\n' "$RAW" | tail -8 | sed 's/^/- /')

# Portable timeout: GNU timeout, gtimeout (brew coreutils), or perl alarm
# (present on stock macOS, where /usr/bin/timeout does not exist).
run_with_timeout() {
  local s="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$s" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$s" "$@"
  elif command -v perl >/dev/null 2>&1; then perl -e 'alarm shift; exec @ARGV' "$s" "$@"
  else "$@"; fi
}

# Grouping: ask a fast Claude model to cluster the prompts into labeled
# threads, individual verbatim prompts nested under each. --safe-mode skips
# customizations in the child (including this plugin), keeping it fast.
LIST_MODE="verbatim"
if [[ -n "$RAW" && -z "${COMPACT_FOCUS_NO_GROUPING:-}" ]] && command -v claude >/dev/null 2>&1; then
  GROUP_MODEL="${COMPACT_FOCUS_GROUP_MODEL:-haiku}"
  GROUPED=$(printf '%s\n' "$RAW" | run_with_timeout 10 \
    claude -p --safe-mode --model "$GROUP_MODEL" \
    "Below are recent user prompts from one coding session, one per line, oldest first. Group them into 2-5 topical threads. Output format, exactly: for each thread, one line with a short thread label followed by ' (N prompts)'; then up to 3 of that thread's prompts, each on its own line prefixed with '  - ', copied verbatim from the input; if the thread has more than 3 prompts, add a final line '  … (+K more)'. Output only this list: no preamble, no markdown fences, no commentary. Never invent, merge, or edit prompts." \
    2>/dev/null | sed '/^```/d') || GROUPED=""
  # Accept only output with our nested shape; anything else keeps the fallback.
  if [[ -n "$GROUPED" ]] && printf '%s\n' "$GROUPED" | grep -q '^  - '; then
    THREADS=$(printf '%s\n' "$GROUPED" | awk 'NR>1 && /^[^ -]/ {print ""} {print}')
    LIST_MODE="grouped"
  fi
fi

[[ -z "$THREADS" ]] && THREADS="- (could not read this session's prompts)"

# Ticket goes down BEFORE the block goes out: a crash between these two steps
# means "paused once, never again" — annoying, not catastrophic. The grouping
# call sits before the ticket on purpose: if the hook is killed mid-call, no
# sentinel exists, so the next compaction simply retries the pause.
date +%s >"$SENTINEL" 2>/dev/null || true
log paused "$LIST_MODE"

MSG="
⏸ Paused — this attempt only; auto-resumes on the next.

${THREADS}

→ /compact focus on <what matters, in words>
  /compact-human to pick interactively  ·  plain /compact = default summary"

# Hook output strings are capped at 10,000 chars; stay safely under.
MSG=${MSG:0:9000}

# Exit 0 + JSON is required: JSON on stdout is only processed on exit 0.
# reason carries the list because it provably renders in the block notice;
# systemMessage rides along as a rendering probe for the auto path.
jq -nc --arg msg "$MSG" '{
  decision: "block",
  reason: $msg,
  systemMessage: $msg
}'
exit 0
