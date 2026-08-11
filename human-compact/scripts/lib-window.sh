# lib-window.sh — infer the model's context window from session metadata.
# Sourced by statusline.sh and session-start.sh. Sets WINDOW (tokens).
# Order: explicit env override → context-window fields if the host payload
# carries them → model id from payload or transcript (1M-window models
# self-identify with a "1m" marker) → 200k default. First payload per state
# dir is dumped for schema discovery so the field list can improve.
#   infer_window '<stdin-json>' '<transcript-path>'
infer_window() {
  local in="$1" t="${2:-}" w=""
  if [ -n "${HUMAN_COMPACT_WINDOW:-}" ]; then
    WINDOW="$HUMAN_COMPACT_WINDOW"; return
  fi
  if command -v jq >/dev/null 2>&1 && [ -n "$in" ]; then
    w=$(printf '%s' "$in" | jq -r '
      .context_window.context_window_size // .context_window.total_tokens //
      .context_window.max_tokens // .context.window_size //
      .model.context_window // empty' 2>/dev/null)
    if [ -n "$w" ] && [ "$w" -gt 0 ] 2>/dev/null; then WINDOW="$w"; return; fi
    local mid
    mid=$(printf '%s' "$in" | jq -r '.model.id // .model.display_name // empty' 2>/dev/null)
    if [ -z "$mid" ] && [ -n "$t" ] && [ -r "$t" ]; then
      mid=$(tail -40 "$t" | jq -rs '[ .[] | .message.model? // empty ] | last // empty' 2>/dev/null)
    fi
    case "$(printf '%s' "$mid" | tr '[:upper:]' '[:lower:]')" in
      *1m*) WINDOW=1000000; return ;;
    esac
    if [ -n "${COMPACT_FOCUS_STATE_DIR:-}" ] && [ ! -e "$COMPACT_FOCUS_STATE_DIR/statusline-schema.json" ]; then
      printf '%s' "$in" >"$COMPACT_FOCUS_STATE_DIR/statusline-schema.json" 2>/dev/null || true
    fi
  fi
  WINDOW=200000
}
