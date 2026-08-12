# vault

Persist per-session Claude Code conversation history that survives context
compaction. Local-first: no network, no telemetry, nothing leaves your machine.

Inert unless `CLAUDE_VAULT=1`. The `claude` shim sets that for you when you
run `claude --vault`; plain `claude` is a pure pass-through.

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

1. Plugin (skills-directory install):

       cp -R vault ~/.claude/skills/vault
       chmod +x ~/.claude/skills/vault/scripts/vault-hook.sh
       claude plugin list   # expect vault@skills-dir, 0.1.0

2. Shim:

       mkdir -p ~/.claude-vault/bin
       cp vault/shim/claude ~/.claude-vault/bin/claude
       chmod +x ~/.claude-vault/bin/claude
       echo 'export PATH="$HOME/.claude-vault/bin:$PATH"' >> ~/.zshrc

   Open a new terminal (or `source ~/.zshrc; hash -r`), then verify:

       which claude          # -> ~/.claude-vault/bin/claude (the shim)
       claude --version      # still your normal Claude Code version

Requires `jq` (`brew install jq`).

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

    rm -rf ~/.claude/skills/vault ~/.claude-vault
    # and remove the PATH line from ~/.zshrc
