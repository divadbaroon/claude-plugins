# human-compact

Study launcher for the Human-Driven Compaction project. Colleagues test the
compaction flow against **forks of their own real sessions** — the original
chat is never modified, and the fork is a sandbox.

## Install & run

```bash
npm i -g @papertlab/human-compact   # or: npx from the repo
human-compact                       # native session picker → forked sandbox
human-compact <session-id>          # fork a specific session
```

Options: `--participant <name>` (labels the state dir), `--hook <path>`
(PreCompact hook override), `--dry-run` (show what would launch).

## What a session looks like

- **Fork, not resume**: launched as `claude --resume --fork-session`, so the
  picked conversation continues under a new session ID; the production JSONL
  is untouched (verified byte-identical in testing).
- **Everything study-specific rides a `--settings` overlay** for that one
  invocation — the participant's own settings, hooks, and sessions never see
  any of it. No install into their Claude config, nothing to uninstall.
- **Persistent statusline banner**: `⚠ SANDBOX FORK · original chat
  untouched · file changes off · ctx N%` — nudges `/compact` when context is
  high, or `/resume` (pick a fuller chat) when under 50%.
- **Session-start notice** repeats the sandbox explanation once, with the
  same under-50% redirect.
- **Restricted, not isolated**: Edit/Write/NotebookEdit denied via
  permissions; Bash blocked by a PreToolUse guard except single unchained
  invocations of the instrument script (first-token match, all shell
  metacharacters denied). KNOWN LIMIT: `--settings` is ADDITIVE — the
  participant's own user/project plugins, hooks, permission allows, and
  MCP servers remain active, so this is a restriction layer, not an
  isolation boundary. Do not distribute for adversarial or unattended use
  until the study runs inside an OS sandbox/container with normal setting
  sources excluded (per Anthropic's permissions guidance) and
  instrumentation exposed via a narrow MCP API instead of Bash.
- **`/compact` runs the study hook**: PreCompact wired to (in order)
  `--hook`/`$HUMAN_COMPACT_HOOK`, the repo-sibling
  `compact-focus/scripts/compact-focus.sh`, or a skills-dir install. Swap in
  a different hook without touching this package.
- **Isolated instrument state**: `COMPACT_FOCUS_STATE_DIR` points at
  `~/.human-compact/state/<participant>/` — per-participant corpora
  (log.jsonl, demoted.jsonl, threads.json, lens.md, guidelines.md) are just
  directories.

## Graduation path

The sandbox is stage one. If results are good, participants adopt the real
thing by installing the compact-focus plugin and invoking the
`compact-human` skill in any production chat — same flow, no restrictions.

## Known limits

- Context % is estimated from the transcript's last usage entry against a
  200k window; 1M-window sessions will read low.
- The under-50% redirect is a nudge, not a gate — the native picker cannot
  filter by context fill.
- The Bash guard fails open if `jq` is missing (Edit/Write remain denied
  regardless).
