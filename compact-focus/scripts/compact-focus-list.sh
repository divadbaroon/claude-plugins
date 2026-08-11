#!/usr/bin/env bash
# compact-focus-list.sh — selection-scoped thread renderer, STABLE command prefix.
#
# Renders from a stored JSON thread file so the collapsed Bash result the
# user expands with ctrl+o contains ONLY the current selection — never the
# whole prompt list. Approve once with "don't ask again"; read-only except
# the explicit `save` mode, which writes one JSON file under the state dir.
#
# Modes:
#   save '<json>'                validate + store threads.json, print labels
#   show <keys> [drop <nums>]    all prompts of the selected categories,
#                                globally numbered; drop omits those numbers
#   show                         no keys selected -> prints nothing useful,
#                                on purpose
#   demote keep <keys> [drop <nums>]
#                                write everything NOT kept (unselected
#                                categories + dropped numbers, resolved
#                                against show's numbering over <keys>) to
#                                demoted.jsonl with stable IDs D1,D2,…;
#                                print a receipt. Demotion, never deletion:
#                                entries are recoverable by ID.
#   recall <id|all>              print a demoted entry by ID (or all)
#   guidelines                   print guidelines.md (+ its path), seeding
#                                a starter file on first use
#   record '<json>'              append one instrumentation event to
#                                log.jsonl (ts + event fields preserved)
#   (no args)                    labels + counts of the stored file
#
# Stored shape: {"threads":{"1":{"label":"…","prompts":["…",…]}, …}}
set -u

S="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
F="$S/threads.json"

if ! command -v jq >/dev/null 2>&1; then
  echo "(jq not found — install jq to use the thread renderer)"
  exit 0
fi

labels() {
  if [ -r "$F" ]; then
    jq -r '.threads | to_entries | sort_by(.key | tonumber)[]
           | "\(.key). \(.value.label) (\(.value.prompts | length) prompt\(if (.value.prompts | length) == 1 then "" else "s" end))"' \
      "$F" 2>/dev/null || echo "(thread file unreadable)"
  else
    echo "(no saved thread list)"
  fi
}

case "${1:-}" in
  save)
    if [ -z "${2:-}" ]; then
      echo "usage: save '<json>'"
      exit 0
    fi
    if printf '%s' "$2" | jq -e '
        .threads | type == "object" and length > 0
        and all(.[]; (.label | type == "string" and length > 0)
                 and (.prompts | type == "array" and length > 0
                      and all(.[]; type == "string")))' >/dev/null 2>&1; then
      mkdir -p "$S" 2>/dev/null || { echo "(could not create $S)"; exit 0; }
      # Normalize: collapse whitespace (keeps prompts one-line and tab-free
      # for the renderer), cap at 300 chars.
      if printf '%s' "$2" | jq '
          .threads |= map_values(
            .prompts |= map(gsub("\\s+"; " ")
                            | if length > 300 then .[0:300] + "…" else . end))
          | {threads}' >"$F" 2>/dev/null; then
        labels
      else
        echo "(could not write $F)"
      fi
    else
      echo "(invalid thread JSON — nothing saved)"
    fi
    ;;
  show)
    shift
    KEYS="${1:-}"
    DROP=""
    [ "${2:-}" = "drop" ] && DROP="${3:-}"
    if [ -z "$KEYS" ]; then
      echo "(no categories selected — nothing to show)"
      exit 0
    fi
    if [ ! -r "$F" ]; then
      echo "(no saved thread list — save one first)"
      exit 0
    fi
    # jq emits tagged lines (H=header, P=prompt, M=missing key); awk assigns
    # global prompt numbers — stable for a given key order regardless of
    # drops, so drop indices stay valid across re-renders.
    jq -r --arg keys "$KEYS" '
      ($keys | split(",") | map(gsub("\\s"; "") | select(length > 0))) as $ks
      | .threads as $t
      | $ks[]
      | . as $k
      | if $t[$k] then
          "H\t\($k). \($t[$k].label)",
          ($t[$k].prompts[] | "P\t\(.)")
        else "M\t\($k)" end
    ' "$F" 2>/dev/null | awk -F'\t' -v drop="$DROP" '
      BEGIN {
        n = split(drop, d, /[, ]+/)
        for (i = 1; i <= n; i++) if (d[i] != "") want_drop[d[i]] = 1
      }
      $1 == "H" { if (started) print ""; started = 1; print $2; next }
      $1 == "P" {
        idx++
        if (idx in want_drop) { hit[idx] = 1; dropped = dropped ? dropped ", " idx : idx; next }
        printf "   [%d] %s\n", idx, $2; next
      }
      $1 == "M" { miss = miss ? miss ", " $2 : $2 }
      END {
        for (k in want_drop) if (!(k in hit)) bad = bad ? bad ", " k : k
        if (dropped) printf "\n(dropped: %s)\n", dropped
        if (bad)     printf "(no prompt numbered: %s)\n", bad
        if (miss)    printf "(unknown categories: %s)\n", miss
        if (!started && !miss) print "(no matching categories)"
      }
    '
    ;;
  demote)
    shift
    [ "${1:-}" = "keep" ] || { echo "usage: demote keep <keys> [drop <nums>]"; exit 0; }
    KEYS="${2:-}"
    DROP=""
    [ "${3:-}" = "drop" ] && DROP="${4:-}"
    if [ -z "$KEYS" ]; then
      echo "(nothing kept — demote refuses to demote everything; use plain /compact instead)"
      exit 0
    fi
    [ -r "$F" ] || { echo "(no saved thread list — save one first)"; exit 0; }
    D="$S/demoted.jsonl"
    BASE=0
    [ -r "$D" ] && BASE=$(wc -l <"$D" | tr -cd '0-9')
    # Demoted set = every prompt of unselected categories, plus prompts of
    # kept categories whose global show-numbering index is in the drop list.
    ROWS=$(jq -c --arg keys "$KEYS" --arg drop "$DROP" '
      ($keys | split(",") | map(gsub("\\s"; "") | select(length > 0))) as $ks
      | ($drop | split(",") | map(gsub("\\s"; "") | select(length > 0) | tonumber)) as $ds
      | .threads as $t
      | ([ $ks[] | select($t[.] != null) ]) as $kept
      | ([ $kept[] | . as $k | $t[$k].prompts[] | {k: $k, p: .} ]
         | to_entries
         | map(select((.key + 1) as $n | $ds | index($n) != null))
         | map({thread: .value.k, label: $t[.value.k].label, prompt: .value.p, reason: "dropped"})) as $droppedRows
      | ([ .threads | to_entries[] | select(.key as $k | $kept | index($k) | not)
           | .key as $k | .value.label as $l | .value.prompts[]
           | {thread: $k, label: $l, prompt: ., reason: "unselected"} ]) as $unselRows
      | ($droppedRows + $unselRows)[]
    ' "$F" 2>/dev/null)
    if [ -z "$ROWS" ]; then
      echo "(nothing to demote — everything is kept)"
      exit 0
    fi
    TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    N=$BASE
    printf '%s\n' "$ROWS" | while IFS= read -r row; do
      N=$((N + 1))
      jq -nc --arg id "D$N" --arg ts "$TS" --argjson r "$row" \
        '{id: $id, ts: $ts} + $r' >>"$D"
    done
    COUNT=$(printf '%s\n' "$ROWS" | wc -l | tr -cd '0-9')
    echo "Demoted $COUNT item(s): D$((BASE + 1))–D$((BASE + COUNT)) → $D"
    echo "(recoverable: … recall D<n> — demotion, never deletion)"
    ;;
  recall)
    D="$S/demoted.jsonl"
    [ -r "$D" ] || { echo "(no demoted store yet)"; exit 0; }
    if [ "${2:-all}" = "all" ]; then
      jq -r '"[\(.id)] (\(.label)) \(.prompt)"' "$D" 2>/dev/null
    else
      jq -r --arg id "${2}" 'select(.id == $id) | "[\(.id)] (\(.label)) \(.prompt)"' "$D" 2>/dev/null \
        | grep . || echo "(no entry ${2})"
    fi
    ;;
  guidelines)
    G="$S/guidelines.md"
    if [ ! -r "$G" ]; then
      mkdir -p "$S" 2>/dev/null
      cat >"$G" <<'SEED'
# Compaction guidelines (per-project, evolves via /compact-focus:compact-learn)

- Preserve verbatim: user corrections, decisions with rationale, and any
  constraint stated once and relied on later.
- Preserve: file paths that were edited, failing test names, open TODOs.
- Compress freely: tool output, exploration that ended in a decision.
- Never silently drop a constraint — demote it with an ID instead.
SEED
    fi
    echo "── $G"
    cat "$G"
    ;;
  record)
    if [ -n "${2:-}" ] && printf '%s' "$2" | jq -e 'type == "object"' >/dev/null 2>&1; then
      mkdir -p "$S" 2>/dev/null
      printf '%s' "$2" | jq -c --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{ts: $ts, event: (.event // "signal")} + .' >>"$S/log.jsonl" 2>/dev/null \
        && echo "(recorded)" || echo "(could not write log)"
    else
      echo "usage: record '<json object>'"
    fi
    ;;
  *)
    labels
    ;;
esac
