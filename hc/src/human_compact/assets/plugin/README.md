# vault + chat goals

Maintain per-chat goal state, and optionally persist Claude Code conversation
history that survives context compaction. There is no telemetry; persisted
state stays on your machine.

## Chat-scoped goals (always available after install)

Run `/hc-ui` inside Claude Code. The browser UI is keyed to that chat's stable
session ID and stored at `~/.claude-vault/chat-sessions/<session-id>/`.
User prompts can be linked many-to-many with goals. Inference also observes
assistant plans/progress, tool activity, task events, and completion evidence.

This chat-scoped layer does not require the shim or `CLAUDE_VAULT=1`.
Its default goal analyzer sends a bounded chat/context digest through your
authenticated Claude CLI. Set `HC_CHAT_PROVIDER=ollama` for on-device
inference. The localhost server binds to `127.0.0.1`, rejects cross-origin
writes, and chat artifacts are owner-only.

The global history layer below is inert unless `CLAUDE_VAULT=1`. The `claude`
shim sets that for you when you run `claude --vault`; plain `claude` is a pure
pass-through for global Vault capture.

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

Install the Python package and its Claude Code integration:

    brew install pipx jq
    pipx ensurepath
    pipx install "git+https://github.com/divadbaroon/claude-plugins.git@main#subdirectory=hc"

If `pipx ensurepath` changed your shell configuration, open a new terminal
before continuing. Install only the chat-scoped `/hc-ui` command with:

    hc install

If an older standalone `uv` makes `pipx install` report a backend-version
conflict, rerun that install command with `--backend pip`.

Start a new Claude Code session (or run `/reload-plugins`) and use `/hc-ui`.
This path does not install the shim or enable global Vault capture.

To add the optional global history layer, run:

    hc backup

That command installs the shim and asks whether to import prior conversations
and whether future global capture should be always on or limited to
`claude --vault`. `jq` is required by these optional global Vault hooks, but
not by the chat-scoped Python hooks.

## Backfill existing history

Import all transcripts Claude Code still has on disk (one-time, re-run safe):

    ~/.claude/skills/vault/scripts/vault-backfill.sh --dry-run   # preview
    ~/.claude/skills/vault/scripts/vault-backfill.sh             # import

Imported sessions carry `start_source: "backfill"`. Reach is limited by
Claude Code's transcript retention (cleanupPeriodDays).

## Use

    claude --vault        # Vault-enabled session
    claude                # normal session, Vault fully inert

Always-on mode: `export CLAUDE_VAULT=1` in your shell profile enables Vault
for every session without the flag.

Debugging: `VAULT_DEBUG=1 claude --vault` appends one JSON line per hook
action to `~/.claude-vault/debug.log`. `CLAUDE_VAULT_DIR` relocates the vault.

## Acceptance test

1. `claude --vault`, hold a several-turn conversation, exit.
2. `ls ~/.claude-vault/sessions/` — a directory named by the session id exists.
3. Inspect `conversation.jsonl` — the most complete transcript available from
   Claude Code (the transcript file is written asynchronously by Claude Code,
   so the very last lines of the final turn can trail by moments; the
   SessionEnd snapshot captures whatever is on disk at exit).
4. `claude` (no flag) — no new directories, no Vault output anywhere.

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

    pipx uninstall human-compact
    rm -rf ~/.claude/skills/vault ~/.claude/skills/hc-ui ~/.claude-vault
    # and remove the PATH line from ~/.zshrc
