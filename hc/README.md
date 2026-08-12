# human-compact (`hc`)

Local goal state and conversation persistence for Claude Code.

## Install `/hc-ui` only

This path installs the chat-scoped goal UI without enabling the global Vault
history or context lens:

```bash
brew install pipx jq
pipx ensurepath
pipx install "git+https://github.com/divadbaroon/claude-plugins.git@main#subdirectory=hc"
```

If `pipx ensurepath` changed your shell configuration, open a new terminal
before continuing. Then install the Claude Code integration:

```bash
hc install
```

Start a new Claude Code session (or run `/reload-plugins`), then type:

```text
/hc-ui
```

The command opens a localhost page tied to that Claude session. Its state is
stored under `~/.claude-vault/chat-sessions/<session-id>/`, so reopening the
same chat restores its goals and prompt links. The server binds to
`127.0.0.1`, rejects cross-origin writes, and exits after an idle period. Chat
state files are owner-only (`0700` directory, `0600` artifacts).

## Add the global context layer

The global layer is separate and opt-in. It imports recent conversations,
derives a cross-chat context lens, and supports the original global goal UI:

```bash
hc backup
hc refresh
hc goals --rebuild
hc ui
```

`hc backup` installs the same `/hc-ui` integration as `hc install`, then asks
whether to import history and whether future global capture should be always
on or limited to `claude --vault` sessions.

## Chat goal model

- Every Claude session has an independent goal tree and cached goal context.
- Human prompts are stored once and linked many-to-many: one prompt can belong
  to several goals, and one goal can reference several prompts.
- Goal inference consumes the broader conversation event stream—including
  assistant plans and progress, tool activity, task status, compact summaries,
  and completion evidence—not only human prompts.
- Hooks ingest incrementally and deduplicate by stable event identity. They do
  not rewrite Claude Code's transcript.
- UI edits persist locally and remain authoritative while later turns update
  evidence, todos, and statuses.
- The prompt picker is newest-first, scrollable, and fuzzy-searchable. Prompt
  links are durable across UI imports and later inference.

## Inference data boundary

The state store and web server are local. Goal inference is not necessarily
on-device: by default, `hc` sends a bounded digest of goal-relevant chat events
and bounded project context to the user's authenticated Claude CLI. The
subprocess runs with tools and session persistence disabled, and it cannot
trigger `hc` hooks recursively. Inference may include `AGENTS.md`, `CLAUDE.md`,
`README.md`, the project's Claude `MEMORY.md` index and relevant linked notes,
and text files explicitly referenced in the conversation. Symlinks and files
outside the project are rejected.

To keep inference on-device, configure Ollama before starting Claude Code:

```bash
export HC_CHAT_PROVIDER=ollama
export HC_CHAT_MODEL=llama3.1
```

Providers never silently fall back. `HC_CHAT_STATE_DIR` relocates chat state;
`HC_CHAT_UI_IDLE_SECONDS` changes the scoped server's idle timeout.

## Development

```bash
python3 -m unittest discover -s ../tests -v
python3 -m py_compile src/human_compact/*.py src/human_compact/trajectory/*.py
uv build
```

The runtime uses Python's standard library. `jq` is required only by the
optional global Vault shell hooks; the chat-scoped Python hooks do not parse
state with `jq`.
