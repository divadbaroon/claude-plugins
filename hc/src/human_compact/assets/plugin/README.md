# vault + chat goals

Maintain per-chat goal state, and optionally persist Claude Code conversation
history that survives context compaction. There is no telemetry; persisted
state stays on your machine.

## Chat-scoped goals (always available after install)

Run `/goals-ui` inside Claude Code. The browser UI is keyed to that chat's
stable session ID and stored at `~/.claude-vault/chat-sessions/<session-id>/`.
User prompts can be linked many-to-many with goals. Inference also observes
assistant plans/progress, tool activity, task events, and completion evidence.

This chat-scoped layer does not require the shim or `CLAUDE_VAULT=1`.
Its default goal analyzer sends a bounded chat/context digest through your
authenticated Claude CLI. Set `HC_CHAT_PROVIDER=ollama` for on-device
inference. The localhost server binds to `127.0.0.1`, rejects cross-origin
writes, and chat artifacts are owner-only.

The global history layer below is experimental in this release: its hooks are
not installed and `hc setup --global-vault yes` is refused unless
`HC_EXPERIMENTAL=1` is set. Legacy selective installs can still opt in per
session with `CLAUDE_VAULT=1` or `claude --vault`.

## What it stores

For each Vault-enabled session, under `~/.claude-vault/sessions/<YYYY-MM-DD>/<session_id>/` (local date the session first started; resumes on later days stay in the original folder):

- `metadata.json` — session_id, cwd, start time, transcript path, start source
  (written once, never rewritten)
- `conversation.jsonl` — the most complete transcript available from Claude
  Code, refreshed at session start, before every compaction, and at session end
- `snapshots/pre-compact-NNN-<utc>-<trigger>.jsonl` — an immutable copy of the
  transcript exactly as it stood before compaction N (never overwritten)
- `compactions/summary-NNN-<utc>.json` — the compact summary the platform
  produced for compaction N, when PostCompact delivers one. Vault never
  depends on this file existing
- `ends.jsonl` — one line per session-end event (reason + time)

The original Claude transcript is only ever read, never modified.

## Install

Requirements: macOS or Linux, Node.js 18+, and Claude Code 2.1.175+.
Install the managed Python runtime and Claude Code integration together:

    npx human-vault

The installer asks whether to enable the global Vault (`1` yes, `2` no). If
enabled, it separately asks whether to infer global goals now. That second
choice runs history analysis before rebuilding the goal tree and sends bounded
conversation-derived digests through your authenticated Claude Code CLI.

Start a new Claude Code session (or run `/reload-plugins`) and use `/goals-ui`.
Choosing `2` for global Vault installs only that chat-scoped path. No Homebrew,
pipx, jq, shell-profile edit, or manual `hc` command is required.

## Backfill existing history

Choosing `1` for global Vault imports all transcripts Claude Code still has on
disk. The Python import is atomic, owner-only, and safe to rerun.

Imported sessions carry `start_source: "backfill"`. Reach is limited by
Claude Code's transcript retention (cleanupPeriodDays).

## Use

After global Vault is enabled, ordinary `claude` sessions are captured. Legacy
selective mode remains available with `claude --vault`.

Debugging: `VAULT_DEBUG=1 claude` appends one JSON line per hook
action to `~/.claude-vault/debug.log`. `CLAUDE_VAULT_DIR` relocates the vault.

## Acceptance test

1. Enable global Vault in `npx human-vault`, hold a several-turn Claude
   conversation, and exit.
2. `ls ~/.claude-vault/sessions/` — a directory named by the session id exists.
3. Inspect `conversation.jsonl` — the most complete transcript available from
   Claude Code (the transcript file is written asynchronously by Claude Code,
   so the very last lines of the final turn can trail by moments; the
   SessionEnd snapshot captures whatever is on disk at exit).
4. Rerun `npx human-vault`, choose `2`, then start another conversation —
   no new global Vault session directory is created.

## Known platform caveats

- Claude Code writes the transcript asynchronously; snapshots copy what is on
  disk at hook time. Nothing is ever unrecoverable: Claude's own transcript
  file persists independently of Vault.
- On Claude Code 2.1.226, plugin-delivered PostCompact has been observed
  registered but not dispatching. Vault stores compact summaries when they
  arrive and loses nothing when they don't.
- SessionEnd hooks share a short time budget by default; Vault declares an
  explicit timeout so the final snapshot completes.

## Uninstall

Remove the managed runtime and Claude integration directories after preserving
any state you want to keep:

    rm -rf ~/.human-compact ~/.claude/skills/vault ~/.claude/skills/goals-ui

The command used to be `/hc-ui`. Installing over that release removes its
`~/.claude/skills/hc-ui` directory when the installer recognizes it as its
own; anything it does not recognize is left for you to remove by hand.

Global and per-chat state remains in `~/.claude-vault` until removed
separately.
