# vault — chat-scoped goals for Claude Code

Maintain per-chat goal state through `/bart`, and — experimentally —
persist Claude Code conversation history that survives context compaction.
There is no telemetry; persisted state stays on your machine.

## Chat-scoped goals (always available after install)

Run `/bart` inside Claude Code. The browser UI is keyed to that chat's
stable session ID and stored at `~/.claude-vault/chat-sessions/<session-id>/`.
User prompts can be linked many-to-many with goals. Inference also observes
assistant plans/progress, tool activity, task events, and completion evidence.

From install, the hooks record each chat's own prompts and events into that
directory — the same conversation Claude Code already keeps in
`~/.claude/projects/` — so that `/bart`, run mid-chat, still sees the chat
from its beginning. Nothing is analyzed or injected until it is run.

From the moment `/bart` runs in a chat, that chat's goals document is
injected back into it as context — whole the first time, then as a diff
against what the chat was last shown — and subagents and tool batches receive
it too. `/bart disable` turns analysis and injection off again for the
chat; `/bart` turns them back on. A chat that never ran `/bart` is
still recorded, but is never analyzed and never injected into.

This chat-scoped layer does not require the shim or `CLAUDE_VAULT=1`.
Its default goal analyzer sends a bounded chat/context digest through your
authenticated Claude CLI. On-device inference via `HC_CHAT_PROVIDER=ollama` is
experimental in this release and additionally needs `HC_EXPERIMENTAL=1`; it
fails closed rather than falling back off-device. The localhost server binds to
`127.0.0.1`, rejects cross-origin writes, and chat artifacts are owner-only.

## Install

Requirements: macOS or Linux and Claude Code 2.1.175+.
Install the managed Python runtime and Claude Code integration together:

    curl -fsSL https://berkeley.mathetic.com/engelbart/install.sh | sh

The installer takes no required options and asks no questions: it installs the
runtime, the hooks, and `/bart`. Nothing is analyzed or injected until
`/bart` runs in a chat.
Start a new Claude Code session (or run `/reload-plugins`) and use `/bart`.
No Homebrew, pipx, jq, shell-profile edit, or manual `hc` command is required.

## Experimental (HC_EXPERIMENTAL=1)

The global history layer is disconnected in this release: its capture hooks are
not installed and `hc setup --global-vault yes` is refused unless
`HC_EXPERIMENTAL=1` is set. Install with `HC_EXPERIMENTAL=1 engelbart install` to
wire those hooks; a plain reinstall un-wires them again, so a vault that was
already enabled stops capturing until it is reinstalled with the flag. Legacy
selective installs can still opt in per session with `CLAUDE_VAULT=1` or
`claude --vault`.

**What it stores.** For each Vault-enabled session, under
`~/.claude-vault/sessions/<YYYY-MM-DD>/<session_id>/` (local date the session
first started; resumes on later days stay in the original folder): a
write-once `metadata.json`; `conversation.jsonl`, the most complete transcript
available from Claude Code, refreshed at session start, before every
compaction, and at session end; an immutable copy of the transcript as it
stood before compaction N at
`snapshots/pre-compact-NNN-<utc>-<trigger>.jsonl`, never overwritten; the
compact summary the platform produced for compaction N at
`compactions/summary-NNN-<utc>.json` when PostCompact delivers one (Vault
never depends on that file existing); and one line per session-end event
(reason + time) in `ends.jsonl`.
The original Claude transcript is only ever read, never modified.

**Backfill and use.** Enabling the global Vault imports all transcripts Claude
Code still has on disk — the Python import is atomic, owner-only, and safe to
rerun — tagging them `start_source: "backfill"`; reach is limited by Claude
Code's transcript retention (`cleanupPeriodDays`). After that, ordinary
`claude` sessions are captured. `VAULT_DEBUG=1 claude` appends one JSON line
per hook action to `~/.claude-vault/debug.log`, and `CLAUDE_VAULT_DIR`
relocates the vault.

**Acceptance test.** With `HC_EXPERIMENTAL=1`, enable the global Vault, hold a
several-turn Claude conversation, and exit; `ls ~/.claude-vault/sessions/`
shows a directory named by the session id, and its `conversation.jsonl` holds
the most complete transcript available (Claude Code writes the transcript
asynchronously, so the very last lines of the final turn can trail by moments;
the SessionEnd snapshot captures whatever is on disk at exit). Reinstalling
*without* the flag un-wires the capture hooks, so a conversation after that
creates no new session directory even though the vault is still enabled on
disk; `hc setup --global-vault no` turns it off for good.

**Known platform caveats.** Claude Code writes the transcript asynchronously,
so snapshots copy what is on disk at hook time — nothing is unrecoverable,
because Claude's own transcript persists independently of Vault. On Claude Code
2.1.226, plugin-delivered PostCompact has been observed registered but not
dispatching; Vault stores compact summaries when they arrive and loses nothing
when they don't. SessionEnd hooks share a short time budget by default, so
Vault declares an explicit timeout for the final snapshot.

## Uninstall

Remove the managed runtime and Claude integration directories after preserving
any state you want to keep:

    rm -rf ~/.human-compact ~/.claude/skills/vault ~/.claude/skills/bart ~/.claude/skills/hc-ui

The command used to be `/hc-ui`. Installing over that release removes its
`~/.claude/skills/hc-ui` directory when the installer recognizes it as its
own; anything it does not recognize is left for you to remove by hand, which
is why it is listed above.

Global and per-chat state remains in `~/.claude-vault` until removed
separately.
