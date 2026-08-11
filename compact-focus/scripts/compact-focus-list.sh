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
#   lens                         print lens.md (+ its path), seeding a
#                                starter template on first use. The lens is
#                                an ENCODING SCHEMA (how to construe the
#                                session), distinct from guidelines
#                                (inclusion rules: what to keep)
#   record '<json>'              append one instrumentation event to
#                                log.jsonl (ts + event fields preserved)
#   (no args)                    labels + counts of the stored file
#
# Stored shape: {"threads":{"1":{"label":"…","prompts":["…",…]}, …}}
set -u

# State layout (v2, namespaced):
#   $BASE/projects/<cwd-hash>/   policy: lens.md, guidelines.md
#   $BASE/sessions/<session-id>/ per-session: trace, ledger, costs, threads,
#                                demoted, graveyard, triage, markers
#   $BASE/log.jsonl              global research log (events carry ids)
# The PreCompact hook writes $BASE/current-session; ad-hoc invocations fall
# back to a per-project session bucket.
BASE="${COMPACT_FOCUS_STATE_DIR:-${CLAUDE_PLUGIN_DATA:-$HOME/.claude/compact-focus}}"
PHASH=$(pwd | shasum 2>/dev/null | cut -c1-12)
PROJ="$BASE/projects/${PHASH:-default}"
SID=$(cat "$BASE/current-session" 2>/dev/null | tr -cd 'A-Za-z0-9-')
SESS="$BASE/sessions/${SID:-adhoc-${PHASH:-default}}"
mkdir -p "$PROJ" "$SESS" 2>/dev/null
# one-time policy migration from the flat layout
for f in lens.md guidelines.md; do
  [ -r "$BASE/$f" ] && [ ! -e "$PROJ/$f" ] && mv "$BASE/$f" "$PROJ/$f" 2>/dev/null
done
S="$SESS"
F="$SESS/threads.json"

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
    G="$PROJ/guidelines.md"
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
  lens)
    L="$PROJ/lens.md"
    if [ ! -r "$L" ]; then
      mkdir -p "$S" 2>/dev/null
      cat >"$L" <<'SEED'
# Compaction lens — three levels (evolves via /compact-focus:compact-learn)
<!-- compact-focus seed: unedited template. Fill Domain lens and Active task
     model with this project's real content (compact-human's lens pass does
     this); the lens stays inactive until this marker line is removed.
     Level 1 is fixed vocabulary; expert knowledge lives in level 2; level 3
     changes every few sessions. -->

## Universal grammar
decision · test · contradiction · dead-end · constraint
(fixed episode vocabulary — every chunk in a draft is labeled with one)

## Domain lens
- (the project-specific interpretive layer — the concepts an expert uses to
  construe events that a generic reader would miss. e.g. for a clock-sync
  project: timestamp basis; offset vs drift; source-of-truth transcript;
  alignment pipeline stages)

## Active task model
- (what is currently favored and what gets compared next. e.g.: cumulative
  drift currently favored; compare beginning/end anchors next)
SEED
    fi
    echo "── $L"
    cat "$L"
    ;;
  record)
    if [ -n "${2:-}" ] && printf '%s' "$2" | jq -e 'type == "object"' >/dev/null 2>&1; then
      mkdir -p "$S" 2>/dev/null
      CID=$(cat "$SESS/compaction-id" 2>/dev/null)
      printf '%s' "$2" | jq -c --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --arg sid "${SID:-}" --arg pid "${PHASH:-}" --arg cid "${CID:-}" \
        '{ts: $ts, schema_version: 2, session_id: $sid, project_id: $pid,
          compaction_id: $cid, event: (.event // "signal")} + .' >>"$BASE/log.jsonl" 2>/dev/null \
        && echo "(recorded)" || echo "(could not write log)"
    else
      echo "usage: record '<json object>'"
    fi
    ;;
  triage)
    # Semantic ledger over the session: items (ideas, features, side
    # questions, decisions) in categories open | solved | unclear | removed.
    # The veto flow asks the user to resolve unclear items and deliberately
    # remove/recategorize the rest — default is keep.
    TF="$S/triage.json"
    case "${2:-list}" in
      save)
        if [ -n "${3:-}" ] && printf '%s' "$3" | jq -e '
            .items | type == "array" and length > 0
            and all(.[]; (.id | type == "string") and (.label | type == "string")
                     and (.category | IN("open","solved","unclear","removed")))' >/dev/null 2>&1; then
          mkdir -p "$S" 2>/dev/null
          printf '%s' "$3" | jq '{items}' >"$TF" 2>/dev/null \
            && echo "(triage saved: $(jq '.items|length' "$TF") items)" || echo "(could not write $TF)"
        else
          echo "usage: triage save '{\"items\":[{\"id\":\"T1\",\"label\":\"…\",\"category\":\"open|solved|unclear|removed\",\"threads\":\"1,3\"}]}'"
        fi
        ;;
      list)
        [ -r "$TF" ] || { echo "(no triage ledger — save one first)"; exit 0; }
        jq -r '.items | group_by(.category)[] |
               "## \(.[0].category)",
               (.[] | "  [\(.id)] \(.label)\(if .threads then "  (threads \(.threads))" else "" end)"),
               ""' "$TF" 2>/dev/null
        ;;
      set)
        ID="${3:-}"; CAT="${4:-}"
        case "$CAT" in open|solved|unclear|removed) ;; *) echo "usage: triage set <id> open|solved|unclear|removed"; exit 0;; esac
        [ -r "$TF" ] || { echo "(no triage ledger)"; exit 0; }
        if jq -e --arg id "$ID" '.items | any(.id == $id)' "$TF" >/dev/null 2>&1; then
          T=$(jq --arg id "$ID" --arg c "$CAT" \
               '.items |= map(if .id == $id then .category = $c else . end)' "$TF" 2>/dev/null) \
            && printf '%s' "$T" >"$TF" && echo "($ID → $CAT)"
        else
          echo "(no item $ID)"
        fi
        ;;
      *) echo "usage: triage save '<json>' | list | set <id> <category>";;
    esac
    ;;
  graveyard)
    # Message-level store the post-compaction agent can query when it senses
    # missing context. Entries come from demotion (demoted.jsonl) and from
    # explicit adds (agent notices a user message it may lose track of).
    GY="$S/graveyard.jsonl"
    case "${2:-}" in
      add)
        if [ -n "${3:-}" ] && printf '%s' "$3" | jq -e 'type == "object" and (.text | type == "string")' >/dev/null 2>&1; then
          mkdir -p "$S" 2>/dev/null
          BASE=0; [ -r "$GY" ] && BASE=$(wc -l <"$GY" | tr -cd '0-9')
          printf '%s' "$3" | jq -c --arg id "G$((BASE + 1))" \
            --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{id: $id, ts: $ts} + .' >>"$GY" \
            && echo "(buried as G$((BASE + 1)))" || echo "(could not write $GY)"
        else
          echo "usage: graveyard add '{\"text\":\"<user message or context>\",\"topic\":\"…\",\"source\":\"…\"}'"
        fi
        ;;
      query)
        shift 2 2>/dev/null || true
        TERMS="$*"
        [ -n "$TERMS" ] || { echo "usage: graveyard query <terms>"; exit 0; }
        FOUND=0
        for F in "$GY" "$S/demoted.jsonl"; do
          [ -r "$F" ] || continue
          while IFS= read -r hit; do
            FOUND=1
            printf '%s\n' "$hit" | jq -r '"[\(.id)] (\(.topic // .label // "-")) \(.text // .prompt)"' 2>/dev/null
          done < <(grep -i -- "$TERMS" "$F" 2>/dev/null || true)
        done
        [ "$FOUND" -eq 0 ] && echo "(nothing in the graveyard matches: $TERMS)"
        ;;
      count)
        C=0
        for F in "$GY" "$S/demoted.jsonl"; do
          [ -r "$F" ] && C=$((C + $(wc -l <"$F" | tr -cd '0-9')))
        done
        echo "$C"
        ;;
      *) echo "usage: graveyard add '<json>' | query <terms> | count";;
    esac
    ;;
  race)
    # Future-preview: instead of proofreading summary prose, race two
    # candidate compactions. Each becomes the sole context of a fresh model
    # call given the SAME ambiguous continuation; their first three proposed
    # actions render side by side, making semantic loss observable before it
    # costs an hour. Usage:
    #   race '<default-summary>' '<lensed-summary>' ['<continuation>']
    A_SUM="${2:-}"; B_SUM="${3:-}"; CONT="${4:-continue fixing it}"
    if [ -z "$A_SUM" ] || [ -z "$B_SUM" ]; then
      echo "usage: race '<default-summary>' '<lensed-summary>' ['<continuation>']"
      exit 0
    fi
    command -v claude >/dev/null 2>&1 || { echo "(claude CLI not found — cannot race)"; exit 0; }
    RACE_MODEL="${COMPACT_FOCUS_RACE_MODEL:-haiku}"
    with_timeout() {
      if command -v timeout >/dev/null 2>&1; then timeout 30 "$@"
      elif command -v gtimeout >/dev/null 2>&1; then gtimeout 30 "$@"
      elif command -v perl >/dev/null 2>&1; then perl -e 'alarm shift; exec @ARGV' 30 "$@"
      else "$@"; fi
    }
    ask() { # $1 = summary
      printf 'You are resuming a coding session. This summary is your ONLY context:\n\n%s\n\nThe user now says: "%s". Reply with EXACTLY your first three concrete actions, numbered 1-3, one line each, no preamble.' "$1" "$CONT" \
        | with_timeout claude -p --safe-mode --model "$RACE_MODEL" 2>/dev/null
    }
    echo "── race: \"$CONT\""
    echo "── A (default summary):"
    ask "$A_SUM" || echo "(A failed)"
    echo ""
    echo "── B (lensed summary):"
    ask "$B_SUM" || echo "(B failed)"
    ;;
  ledger)
    # Persisted handoff between the skill (which composes the ledger) and
    # the TUI (which the user edits in their own terminal via `!`).
    LJ="$S/ledger.json"
    case "${2:-}" in
      save)
        if [ -n "${3:-}" ] && printf '%s' "$3" | jq -e '.items | type == "array"' >/dev/null 2>&1; then
          mkdir -p "$S" 2>/dev/null
          printf '%s' "$3" | jq '.finalized = false
                                 | .constraints = (.constraints // [])
                                 | .items |= map(.children = (.children // []))' >"$LJ" 2>/dev/null \
            && echo "(ledger saved: $(jq '.items|length' "$LJ") items → $LJ)" || echo "(could not write $LJ)"
        else
          echo "usage: ledger save '{\"items\":[{\"id\":\"L1\",\"tag\":\"open\",\"label\":\"…\",\"cat\":\"keep|contested|drop\",\"prov\":\"threads 1\",\"children\":[{\"text\":\"…\",\"checked\":true}]}]}'"
        fi
        ;;
      load)
        [ -r "$LJ" ] && cat "$LJ" || echo '{"items":[],"finalized":false}'
        ;;
      *) echo "usage: ledger save '<json>' | load";;
    esac
    ;;
  tui)
    LJ="$S/ledger.json"
    [ -r "$LJ" ] || { echo "(no ledger.json — the compact-human skill prepares it first)"; exit 0; }
    exec python3 "$(dirname "$0")/compact-focus-tui.py" "$LJ"
    ;;
  tui-open)
    # Auto-launch: the model's Bash tool has no TTY, but it CAN open a real
    # terminal window running the TUI (macOS osascript). Falls back to
    # printing the manual `!` line.
    LJ="$S/ledger.json"
    [ -r "$LJ" ] || { echo "(no ledger.json — the compact-human skill prepares it first)"; exit 0; }
    SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    if command -v osascript >/dev/null 2>&1; then
      APP="${COMPACT_FOCUS_TERMINAL:-Terminal}"
      if [ "$APP" = "iTerm" ] || [ "$APP" = "iTerm2" ]; then
        osascript -e "tell application \"iTerm\"
          create window with default profile command \"env COMPACT_FOCUS_STATE_DIR='$S' '$SELF' tui\"
          activate
        end tell" >/dev/null 2>&1 && { echo "(ledger editor opened in an iTerm window)"; exit 0; }
      else
        osascript -e "tell application \"Terminal\"
          do script \"exec env COMPACT_FOCUS_STATE_DIR='$S' '$SELF' tui\"
          activate
        end tell" >/dev/null 2>&1 && { echo "(ledger editor opened in a Terminal window — edit, press q, then come back and say done)"; exit 0; }
      fi
    fi
    echo "(could not auto-open — run manually:  ! $SELF tui  )"
    ;;
  costs)
    # Per-prompt context cost (#7821's cost-annotated pick list). Reads the
    # transcript path the hook persisted (or takes one as an argument).
    TP="${2:-}"
    [ -z "$TP" ] && [ -r "$S/transcript.path" ] && TP=$(cat "$S/transcript.path")
    [ -n "$TP" ] && [ -r "$TP" ] || { echo "(no transcript path — run /compact once, or pass the path)"; exit 0; }
    SLF="${COMPACT_FOCUS_STATUSLINE:-$BASE/statusline-schema.json}"
    OUT=$(python3 "$(dirname "$0")/compact-focus-costs.py" "$TP" --statusline "$SLF" 2>/dev/null)
    if printf '%s' "$OUT" | jq -e '.units' >/dev/null 2>&1; then
      mkdir -p "$S" 2>/dev/null
      printf '%s' "$OUT" >"$S/costs.json"
      printf '%s' "$OUT" | jq -r '.units[] | "\(.i)\t\(.pct)%\t\(.prompt)"' \
        | awk -F'\t' '{printf " %2s. %-72s %6s\n", $1, substr($3,1,72), $2}'
      printf '%s' "$OUT" | jq -r '.classes | to_entries[] | select(.value.tokens > 0) | "  class \(.key): ~\(.value.pct)%"'
      printf '%s' "$OUT" | jq -r '"(window \(.window) tokens · total \(([.units[].pct] | add // 0) * 10 | round / 10)% across \(.units | length) prompts → costs.json)"'
    else
      echo "(costs failed: $(printf '%s' "$OUT" | jq -r '.error // "unknown"'))"
    fi
    ;;
  tui-inject)
    # Package-grade launch ladder only (no OS keystroke injection — that
    # needs per-machine Accessibility permission and cannot ship in a
    # package): (1) tmux → split a pane running the editor beside the chat,
    # zero typing, race-free; (2) fallback → print the two-letter line.
    if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
      tmux split-window -h "COMPACT_FOCUS_STATE_DIR='$S' '$HOME/.local/bin/cf' 2>/dev/null || COMPACT_FOCUS_STATE_DIR='$S' '$(dirname "$0")/cf'" 2>/dev/null \
        && { echo "(editor opened in a tmux split beside the chat)"; exit 0; }
    fi
    echo "(type  ! cf  to open the editor here)"
    ;;
  prep-bg)
    # Launch ledger preparation as a DETACHED worker so the user keeps
    # using the session while it runs. Surfacing on completion: ready
    # marker (picked up by the UserPromptSubmit hook at the next turn),
    # macOS notification, tmux auto-split into the editor.
    SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    [ -r "$S/transcript.path" ] || { echo "(no transcript path — run /compact once first)"; exit 0; }
    rm -f "$S/ledger-ready" "$S/ledger-failed" 2>/dev/null
    ( COMPACT_FOCUS_STATE_DIR="$BASE" nohup "$SELF" prep-run >/dev/null 2>&1 & echo $! >"$S/prep-running" ) 2>/dev/null
    echo "(ledger prep running in background — keep working; it will surface when ready)"
    ;;
  prep-run)
    # The worker body (invoked detached by prep-bg). Fails to a marker.
    fail() { printf '%s' "$1" >"$S/ledger-failed"; rm -f "$S/prep-running"; exit 0; }
    TP=$(cat "$S/transcript.path" 2>/dev/null)
    [ -n "$TP" ] && [ -r "$TP" ] || fail "no transcript"
    command -v claude >/dev/null 2>&1 || fail "no claude CLI"
    SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    COMPACT_FOCUS_STATUSLINE="$BASE/statusline-schema.json" "$SELF" costs "$TP" >/dev/null 2>&1
    printf '%s-%s' "${SID:-s}" "$(date -u +%Y%m%dT%H%M%SZ)" >"$SESS/compaction-id" 2>/dev/null
    COSTS=$(jq -r '.units[] | "\(.u). [\(.pct)%] \(.prompt)"' "$S/costs.json" 2>/dev/null | tail -120)
    CLS=$(jq -r '.classes | to_entries[] | "\(.key): \(.value.pct)%"' "$S/costs.json" 2>/dev/null)
    LENS=$("$SELF" lens 2>/dev/null | tail -n +2)
    GUIDE=$("$SELF" guidelines 2>/dev/null | tail -n +2)
    wt() { if command -v timeout >/dev/null 2>&1; then timeout 240 "$@"
           elif command -v gtimeout >/dev/null 2>&1; then gtimeout 240 "$@"
           elif command -v perl >/dev/null 2>&1; then perl -e 'alarm shift; exec @ARGV' 240 "$@"
           else "$@"; fi; }
    OUT=$(printf 'Prepare a compaction ledger for a coding session.\n\nUSER PROMPTS (with %% of context window each):\n%s\n\nCONTENT CLASS COSTS:\n%s\n\nPROJECT LENS:\n%s\n\nGUIDELINES:\n%s\n\nPartition the session into at most 12 items. cat rules: "keep" = critical ongoing work, recent decisions, active files; "summarize" = completed tasks, resolved issues, older discussion; "drop" = redundant information, outdated attempts; "contested" = you are genuinely unsure — the human decides. Labels use the user'\''s own words (<=120 chars). Children = the verbatim prompts behind the item, each carrying "u": the prompt NUMBER from the list above (this grounds every item in real sources — an item with no children is invalid). Every prompt number must appear under EXACTLY ONE item. Output ONLY strict JSON, no fences: {"items":[{"id":"L1","tag":"decision|test|contradiction|dead-end|constraint|open|solved|mechanical","label":"...","cat":"keep|summarize|contested|drop","pct":0.0,"children":[{"u":1,"text":"...","checked":true,"pct":0.0}]}],"classes":[{"id":"first_n","state":"keep","n":30},{"id":"file_changes","state":"keep","pct":0.0},{"id":"subagents","state":"summarize","pct":0.0},{"id":"todos","state":"keep","pct":0.0}]} — fill class pcts from the class costs above.' \
        "$COSTS" "$CLS" "$LENS" "$GUIDE" \
      | wt claude -p --safe-mode --model "${COMPACT_FOCUS_PREP_MODEL:-sonnet}" 2>/dev/null | sed '/^```/d') || OUT=""
    printf '%s' "$OUT" | jq -e '.items | type == "array" and length > 0' >/dev/null 2>&1 || fail "worker output invalid"
    "$SELF" ledger save "$OUT" >/dev/null 2>&1 || fail "ledger save failed"
    cp "$SESS/ledger.json" "$SESS/ledger.initial.json" 2>/dev/null
    date -u +%Y-%m-%dT%H:%M:%SZ >"$S/ledger-ready"
    rm -f "$S/prep-running" "$S/ledger-failed" 2>/dev/null
    "$SELF" record '{"event":"prep_bg_done"}' >/dev/null 2>&1
    command -v osascript >/dev/null 2>&1 && osascript -e \
      'display notification "Compaction ledger ready — send any message, or ! cf to edit" with title "compact-focus"' >/dev/null 2>&1
    exit 0
    ;;
  prep-status)
    if   [ -r "$S/ledger-ready" ];  then echo ready
    elif [ -r "$S/prep-running" ] && kill -0 "$(cat "$S/prep-running" 2>/dev/null)" 2>/dev/null; then echo running
    elif [ -r "$S/ledger-failed" ]; then echo "failed: $(cat "$S/ledger-failed")"
    else echo none; fi
    ;;
  surfaced)
    rm -f "$S/ledger-ready" "$S/ledger-failed" "$S/prep-running" 2>/dev/null
    echo "(markers cleared)"
    ;;
  finalize)
    # Deterministic finalization: ledger + costs -> /compact directive AND
    # demotion/graveyard records, derived ONLY from item children (u refs
    # into costs units). No model-mediated ID translation. Validates before
    # writing anything; invalid ledgers change nothing.
    LJ="$SESS/ledger.json"; CJ="$SESS/costs.json"
    [ -r "$LJ" ] && [ -r "$CJ" ] || { echo "INVALID: missing ledger.json or costs.json"; exit 0; }
    ERR=$(jq -rs --slurpfile costs "$CJ" '
      .[0] as $L | ($costs[0].units // []) as $units
      | [ ($units[] | select(.kind == "prompt") | .u) ] as $need
      | [ $L.items[].children[]? | .u ] as $got
      | [ ($L.items | group_by(.id)[] | select(length > 1) | .[0].id | "duplicate id \(.)"),
          ($L.items[] | select(.cat | IN("keep","summarize","contested","drop") | not) | "illegal cat in \(.id)"),
          ($L.items[] | select((.label // "") == "") | "empty label in \(.id)"),
          ($L.items[] | select((.children // []) | length == 0) | "ungrounded item \(.id) (no sources)"),
          ($L.items[].children[]? | select((.u | type) != "number") | "child without unit ref"),
          ($got | group_by(.)[] | select(length > 1) | "unit \(.[0]) covered more than once"),
          (($need - $got)[] | "unit \(.) uncovered"),
          (($got - $need - [0])[] | "invented unit ref \(.)")
        ] | .[]' "$LJ" 2>/dev/null)
    if [ -n "$ERR" ]; then
      printf 'INVALID:\n%s\n' "$ERR"
      exit 0
    fi
    CID=$(cat "$SESS/compaction-id" 2>/dev/null)
    TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    D="$SESS/demoted.jsonl"; GY="$SESS/graveyard.jsonl"
    BASEN=0; [ -r "$D" ] && BASEN=$(wc -l <"$D" | tr -cd '0-9')
    # demotion set = every child of a drop item + unchecked children elsewhere
    ROWS=$(jq -c --slurpfile costs "$CJ" '
      ($costs[0].units // []) as $units
      | [ .items[] as $it | $it.children[]?
          | select(($it.cat == "drop") or (.checked == false))
          | {u: .u, item: $it.id, label: $it.label,
             reason: (if $it.cat == "drop" then "dropped-item" else "unchecked-prompt" end),
             prompt: (.text // ($units[] | select(.u == .u) | .prompt))} ] | .[]' "$LJ" 2>/dev/null)
    N=$BASEN
    if [ -n "$ROWS" ]; then
      TMPD=$(mktemp); TMPG=$(mktemp)
      printf '%s\n' "$ROWS" | while IFS= read -r row; do
        N=$((N + 1))
        printf '%s' "$row" | jq -c --arg id "D$N" --arg ts "$TS" --arg sid "${SID:-}" --arg cid "${CID:-}" \
          '{id: $id, ts: $ts, schema_version: 2, session_id: $sid, compaction_id: $cid} + .' >>"$TMPD"
        printf '%s' "$row" | jq -c --arg id "G$N" --arg ts "$TS" --arg sid "${SID:-}" --arg cid "${CID:-}" \
          '{id: $id, ts: $ts, schema_version: 2, session_id: $sid, compaction_id: $cid,
            text: (.prompt // ""), topic: .label, source: .item}' >>"$TMPG"
      done
      cat "$TMPD" >>"$D" 2>/dev/null; cat "$TMPG" >>"$GY" 2>/dev/null
      rm -f "$TMPD" "$TMPG"
    fi
    NDEM=$(printf '%s\n' "$ROWS" | grep -c . || true)
    echo "── directive (verbatim for /compact focus on):"
    jq -r '
      def lines(cat; verb): [ .items[] | select(.cat == cat) |
        "- [\(.tag)] \(.label)\(if .note and .note != "" then " — " + .note else "" end)" ] |
        if length > 0 then verb, .[] else empty end;
      lines("keep"; "PRESERVE faithfully (expand each):"),
      lines("summarize"; "SUMMARIZE to outcome lines only:"),
      ( [ .classes[]? | select(.state != "keep") |
          if .id == "first_n" then "for the first \(.n // 30)% of the session keep only decisions and constraints"
          elif .id == "file_changes" and .state == "summarize" then "compress file-change detail to which files and why"
          elif .id == "file_changes" then "omit file-change mechanics entirely"
          elif .id == "subagents" and .state == "summarize" then "compress each subagent run to a single outcome line"
          elif .id == "subagents" then "omit subagent transcripts"
          elif .id == "todos" then "omit todo bookkeeping"
          else empty end ] | if length > 0 then "Class directives: " + join("; ") + "." else empty end ),
      ( [ .constraints[]? ] | if length > 0 then "MUST NOT be misinterpreted: " + join("; ") + "." else empty end )
    ' "$LJ"
    echo "Removed context is recoverable: demoted.jsonl / graveyard.jsonl in $SESS (recall <id> / graveyard query <terms>)."
    echo "── finalized: $NDEM unit(s) demoted+buried deterministically (D/G $((BASEN + 1))…)"
    ;;
  studymode)
    # The human-compact study wrapper launches sessions with
    # COMPACT_FOCUS_STUDY=1; the skill asks this mode which flow to run.
    echo "${COMPACT_FOCUS_STUDY:-off}"
    ;;
  *)
    labels
    ;;
esac
