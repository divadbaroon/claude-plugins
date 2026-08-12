# human-compact (`hc`)

Local goal state and conversation persistence for Claude Code.

## Install

Run the single installer from a terminal where Claude Code is installed:

```bash
npx human-compact
```

It installs a managed `hc` runtime plus the Claude Code hooks and `/hc-ui`
skill. It then asks whether to enable the global Vault (`1` yes, `2` no). If
you choose yes, it separately asks whether to infer global goals now. The
second opt-in sends bounded conversation-derived digests through your own
authenticated Claude Code CLI.

Start a new Claude Code session (or run `/reload-plugins`), then type:

```text
/hc-ui
```

The command opens a localhost page tied to that Claude session. Its state is
stored under `~/.claude-vault/chat-sessions/<session-id>/`, so reopening the
same chat restores its goals and prompt links. The server binds to
`127.0.0.1`, rejects cross-origin writes, and exits after an idle period. Chat
state files are owner-only (`0700` directory, `0600` artifacts).

## Global context layer

The global layer is separate and opt-in. Choosing `1` for global Vault imports
existing Claude Code transcripts and enables future capture. Choosing `1` for
global goals then analyzes that history before rebuilding the cross-chat goal
tree; rebuilding alone would have no extraction cache on a fresh install.

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

The runtime uses Python's standard library. The npm package carries the exact
Python wheel it installs; it does not fetch executable code from a mutable Git
branch.
