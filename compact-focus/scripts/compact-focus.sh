#!/usr/bin/env bash
# compact-focus.sh — PreCompact hook (plugin, v0.8.0)
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
PLUGIN_VERSION="0.10.0"

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
  # EXPERIMENTAL (v0.8, opt-in via COMPACT_FOCUS_LENS_STDOUT=1): inject the
  # lens frames into the summarizer through the undocumented
  # PreCompact-stdout channel. ONLY valid on this path — it is the one
  # allow path that emits no JSON, and plain text must NEVER mix with the
  # JSON-output paths (block / pause-expired). Requires a lens.md the user
  # or PASS 1 has actually written (seed marker absent). Every step is
  # guarded so failure cannot change the exit path: fail open, exit 0.
  if [[ "${COMPACT_FOCUS_LENS_STDOUT:-}" == "1" ]]; then
    LENS_FILE="$STATE_DIR/lens.md"
    if [[ -r "$LENS_FILE" ]] && ! grep -q 'compact-focus seed:' "$LENS_FILE" 2>/dev/null; then
      LENS_FRAMES=$(awk '/^## (Universal grammar|Domain lens|Active task model)/ {p=1; print; next}
                         /^## / {p=0} p' "$LENS_FILE" 2>/dev/null) || LENS_FRAMES=""
      if [[ -n "$LENS_FRAMES" ]]; then
        { printf 'Interpret and summarize this session through these frames:\n%s\n' \
            "$LENS_FRAMES" && log lens_stdout; } 2>/dev/null || true
      fi
    fi
  fi
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
    | map(if length > 160 then .[0:160] + "…" else . end)
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
# threads. The model outputs JSON (categories -> ALL member prompts), which
# is stored as threads.json for the selection-scoped renderer
# (compact-focus-list.sh show <keys>); the block notice shows only labels +
# the first 3 prompts per thread. --safe-mode skips customizations in the
# child (including this plugin), keeping it fast.
LIST_MODE="verbatim"
THREADS_JSON=""
if [[ -n "$RAW" && -z "${COMPACT_FOCUS_NO_GROUPING:-}" ]] && command -v claude >/dev/null 2>&1; then
  GROUP_MODEL="${COMPACT_FOCUS_GROUP_MODEL:-haiku}"
  GROUPED=$(printf '%s\n' "$RAW" | run_with_timeout 10 \
    claude -p --safe-mode --model "$GROUP_MODEL" \
    "Below are recent user prompts from one coding session, one per line, oldest first. Group them into 2-6 topical threads. Output ONLY a JSON object, no markdown fences, no commentary, of exactly this shape: {\"threads\":[{\"label\":\"<2-4 word thread label>\",\"prompts\":[\"<prompt>\",...]}]}. Every input line must appear in exactly one thread's prompts array, copied verbatim. Never invent, merge, or edit prompts." \
    2>/dev/null | sed '/^```/d') || GROUPED=""
  if [[ -n "$GROUPED" ]] && jq -e '
      .threads | type == "array" and length >= 1
      and all(.[]; (.label | type == "string" and length > 0)
               and (.prompts | type == "array" and length >= 1
                    and all(.[]; type == "string")))' <<<"$GROUPED" >/dev/null 2>&1; then
    THREADS_JSON=$(jq -c '{threads: (.threads[:6] | to_entries
      | map({key: ((.key + 1) | tostring),
             value: {label: .value.label,
                     prompts: (.value.prompts | map(gsub("\\s+"; " ")))}})
      | from_entries)}' <<<"$GROUPED" 2>/dev/null) || THREADS_JSON=""
  fi
  if [[ -n "$THREADS_JSON" ]]; then
    THREADS=$(jq -r '.threads | to_entries | sort_by(.key | tonumber)[]
      | "\(.key). \(.value.label) (\(.value.prompts | length) prompt\(if (.value.prompts | length) == 1 then "" else "s" end))",
        (.value.prompts[:3][] | "  - \(.)"),
        (if (.value.prompts | length) > 3
         then "  … (+\((.value.prompts | length) - 3) more)" else empty end),
        ""' <<<"$THREADS_JSON" 2>/dev/null) && LIST_MODE="grouped" || THREADS_JSON=""
  fi
fi

# Whatever happened above, leave a threads.json behind: grouped when the
# model delivered, a single "Recent prompts" category otherwise. The
# compact-human skill reads it (regrouping only the fallback shape), so the
# picker numbering always matches this notice.
if [[ -z "$THREADS_JSON" && -n "$RAW" ]]; then
  THREADS_JSON=$(printf '%s\n' "$RAW" | jq -Rsc \
    '{threads: {"1": {label: "Recent prompts",
                      prompts: (split("\n") | map(select(length > 0)))}}}' 2>/dev/null) || THREADS_JSON=""
fi
[[ -n "$THREADS_JSON" ]] && printf '%s\n' "$THREADS_JSON" >"$STATE_DIR/threads.json" 2>/dev/null

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

What should NOT be forgotten during compaction?
→ /compact focus on <your answer, in words>
  /compact-focus:compact-human to pick interactively · plain /compact = default summary"

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
