# compact-focus (plugin)

Pauses one Claude Code compaction per session and shows your own recent
prompts, so you can rerun `/compact focus on <what matters>` instead of
letting the default summarizer guess. Logs every {stated focus → resulting
summary} pair for later analysis.

Validated with `claude plugin validate` on Claude Code 2.1.226.
Requires `jq` (`brew install jq`) — without it the plugin warns once and
disables itself; compaction is never affected.

## v0.7 (MVP): negotiated demotion

Implements the core of the "Compaction as Negotiated Demotion" proposal
(Linear: Human-Driven Compaction project). Design thesis: make human error
cheap, not human judgment accurate.

- **Demotion, never deletion** — everything not kept goes to
  `demoted.jsonl` with stable IDs (`D1…`); `recall <id>` reads it back.
  The compaction summary itself names the store, so the post-compaction
  agent knows losses are recoverable.
- **Two-phase elicitation** — Phase 1: loss-framed selection ("what must
  the agent NOT have forgotten?") happens *before* any draft exists, so
  the machine draft can't anchor it. Phase 2: the model drafts the
  preservation summary; the user edits/approves; only then does
  `/compact focus` run. This is the preview-with-approval loop no shipped
  tool has (see ecosystem survey).
- **Guidelines document** — `guidelines.md` per state dir conditions every
  draft; `/compact-focus:compact-learn` converts logged signals (draft
  edits > selections; revealed losses > everything) into proposed
  guideline diffs the user approves. The automation ramp: ask less over
  time.
- **Instrumentation** — every phase-1 selection, draft, edit, approval,
  demotion, and revealed-loss report is appended to `log.jsonl`. This is
  the S1 study corpus: {signal → compaction → downstream outcome}.
- **Revealed loss** — say "the compaction lost X" any time; the agent logs
  it and restores from the demoted store.

New script modes (same stable command prefix, approve once):
`demote keep <keys> [drop <nums>]` · `recall <id|all>` · `guidelines` ·
`record '<json>'`.

Not in the MVP (research, not plumbing): uncertainty-gated asking,
automatic revealed-loss detection, breakpoint detection for timing.

## v0.6: selection-scoped ctrl+o

The grouping model now outputs JSON — categories keyed `"1"…"6"`, each
holding ALL of its member prompts — stored as `threads.json` in the state
dir. `compact-focus-list.sh` renders from that file instead of echoing a
preformatted blob, so the collapsed Bash result ctrl+o expands always
matches the current selection:

- before any selection: labels + counts only, never the full prompt list
- no categories selected: nothing is rendered at all (plain `/compact`)
- categories selected: `show 1,3` prints every prompt of just those
  categories, globally numbered `[1]…[N]`
- per-prompt deselect: `show 1,3 drop 2,5` re-renders what remains
  (numbering stays stable across drops); defaults are keep-all or none

Interfaces considered and rejected: one AskUserQuestion option per prompt
(4-option cap, labels can't hold prompt text) and one-shot exclusion
without re-render (the expanded doc would show the pre-drop set, breaking
the "ctrl+o = current selection" invariant).

## v0.2: grouped thread view

At pause time the hook now asks a fast Claude model (one `claude -p
--safe-mode --model haiku` child call, 10s cap, ~fractions of a cent, billed
to your account) to cluster the last 25 prompts into 2–5 labeled threads,
with up to 3 verbatim prompts nested under each. The model only *sorts and
labels* — every prompt shown is your untouched text. On ANY failure (no CLI,
timeout, auth error, malformed output) the pause falls back to the flat
verbatim list from v0.1. Each `paused` log line records `list_mode`
(`grouped` | `verbatim`) and the plugin version, so the two presentations
can be compared later.

- `COMPACT_FOCUS_NO_GROUPING=1` — disable the child call entirely
- `COMPACT_FOCUS_GROUP_MODEL=<alias>` — use a different model (default `haiku`)

**Upgrading:** replace the folder (`cp -R compact-focus
~/.claude/skills/`), re-run the `chmod`, then restart your session —
skills-dir hook changes load on restart or `/reload-plugins`, not live.

## Install for yourself (no marketplace needed)

Copy this folder to your personal skills directory:

```bash
cp -R compact-focus ~/.claude/skills/compact-focus
chmod +x ~/.claude/skills/compact-focus/scripts/*.sh
```

On the next session it loads as `compact-focus@skills-dir` in **every
project**. Confirm with `claude plugin list` or `/hooks` (both PreCompact and
PostCompact should show Plugin Hooks entries).

**If you previously installed the project-level version** (hooks in a repo's
`.claude/settings.json`): remove those entries from that repo, or the pause
fires twice there — plugin and project copies of a hook run separately, and
they keep separate sentinels in different state dirs.

## Distribute to others (marketplace)

1. Create a git repo (e.g. `claude-plugins`) containing `compact-focus/` and
   a `.claude-plugin/marketplace.json` at the repo root — adapt
   `marketplace.example.json` (the `source` path is relative to the repo root).
2. Others then run, inside Claude Code:
   ```
   /plugin marketplace add <github-user>/claude-plugins
   /plugin install compact-focus@papert-tools
   ```
3. Updates: bump `version` in `.claude-plugin/plugin.json` per release, or
   omit it to ship on every commit. `claude plugin validate ./compact-focus
   --strict` in CI catches schema drift.

## Where things live

- **State + log**: `~/.claude/plugins/data/<plugin-id>/log.jsonl` when
  installed as a plugin (survives updates); `~/.claude/compact-focus/`
  for a bare project-level install; `COMPACT_FOCUS_STATE_DIR` overrides both.
- **Sentinels**: `paused-<session_id>` files beside the log — delete one to
  re-arm the pause for that session (`rm .../paused-*`).

## How it behaves (v0.3 semantics)

- The pause covers **one compaction attempt**. If you don't act, the next
  attempt (usually triggered by your next message) proceeds automatically,
  with a visible "no focus given" notice — never silently.
- A pause older than 30 minutes is treated as abandoned: the next attempt
  pauses fresh instead of expiring. Expiry is trigger-aware: manual pauses expire after
  `COMPACT_FOCUS_PAUSE_TTL_MANUAL` (default 120s), auto pauses after
  `COMPACT_FOCUS_PAUSE_TTL` (default 1800s).
- A manual `/compact focus on X` always passes through (and X is logged).
- Fails **open** on every error: missing jq, unreadable transcript,
  unwritable state — compaction proceeds untouched, worst case silently.
- The prompt list is verbatim (last 8 user prompts, 100-char truncation),
  filtered to exclude tool results, command wrappers, meta lines, and prior
  summaries.

## Known limits

- The auto-trigger path is wired identically to manual but has had less live
  testing; pair with `/autocompact` set well below your model's limit so any
  block lands in the proactive branch (blocking a compaction that is
  recovering from a context-limit error fails that request).
- Only the first compaction per session gets steered by the pause; later
  ones run the default template unless you `/compact focus` manually.
- Transcript parsing is defensive but the JSONL schema is undocumented and
  may shift across CLI versions.
