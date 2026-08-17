# hc

Local goal state and conversation persistence for Claude Code. `hc` is the
runtime; [`human-vault`](../human-vault) is the npm package that installs it.

## Install

Requirements: macOS or Linux, Node.js 18+, and Claude Code 2.1.175+.
Run the single installer from a terminal where that Claude Code version is
installed:

```bash
npx human-vault
```

It installs a managed `hc` runtime plus the Claude Code hooks and the
`/goals-ui` command, and nothing else. The installer takes no required options
and asks no questions.

From install, the hooks record each chat's own prompts and events to a local,
owner-only store under `~/.claude-vault/chat-sessions/<session-id>/` — the
same conversation Claude Code already keeps in `~/.claude/projects/`. That
recording is what lets `/goals-ui`, run in the middle of a chat, still see the
chat from its beginning. Nothing is analyzed or injected until you run
`/goals-ui` in that chat, and nothing leaves your machine except the model
calls your own `claude` CLI makes.

Start a new Claude Code session (or run `/reload-plugins`), then type:

```text
/goals-ui
```

The command opens a localhost page tied to that Claude session, without
spending a Claude turn. Its state is stored under
`~/.claude-vault/chat-sessions/<session-id>/`, so reopening the same chat
restores its goals and prompt links. The server binds to `127.0.0.1`, rejects
cross-origin writes, and exits after an idle period. Chat state files are
owner-only (`0700` directory, `0600` artifacts).

## Goal context in the chat

Once `/goals-ui` has run in a chat, that chat's goals document is injected back
into it as context. The first injection carries the whole document under a
`# Goals for this Claude chat (full file: …)` header; later messages carry a
unified diff against what the chat was last shown, and nothing at all when the
document has not changed. `SessionStart` re-sends the whole document, so a
compaction never leaves a diff pointing at text the model can no longer see.
Subagents receive the whole document at `SubagentStart`, and a tool batch
receives the current diff.

A copy of the document is mirrored to `<claude project dir>/goals-ui/<session
id>.md`, and the injected header names that file.

`/goals-ui disable` stops the injection and the inference for that chat and
forgets the diff baseline; running `/goals-ui` again turns both back on and
re-sends the whole document. A chat that never ran `/goals-ui` — or one that
disabled it — keeps being recorded, but is never analyzed and never injected
into.

## Chat goal model

- Every chat where `/goals-ui` has run gets its own goal tree and cached goal
  context, independent of every other chat.
- Human prompts are stored once and linked many-to-many: one prompt can belong
  to several goals, and one goal can reference several prompts.
- Goal inference consumes the broader conversation event stream—including
  assistant plans and progress, tool activity, task status, compact summaries,
  and completion evidence—not only human prompts.
- Hooks ingest incrementally and deduplicate by stable event identity. They do
  not rewrite Claude Code's transcript.
- Each goal is one markdown document with default sections; inference appends
  to those sections and never overwrites what you wrote.
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

The on-device provider is experimental in this release (see below). Providers
never silently fall back: an unavailable or gated provider raises rather than
quietly answering through a different one. `HC_CHAT_STATE_DIR` relocates chat state;
`HC_CHAT_UI_IDLE_SECONDS` changes the scoped server's idle timeout.

## Experimental (HC_EXPERIMENTAL=1)

Everything below is present in this release but disconnected from it. Setting
`HC_EXPERIMENTAL=1` re-enables the `hc` subcommands (`ui`, `backup`,
`trajectory`, `lens`, `goals`, `work`, `mark`, `status`, `refresh`, `analyze`,
`worker`) and the HTTP routes and operations behind them; without it they exit
2 or answer `experimental in this release; set HC_EXPERIMENTAL=1`. Installing
with `HC_EXPERIMENTAL=1 npx human-vault` additionally wires the global capture
hooks — a plain reinstall un-wires them, so a vault that was already enabled
stops capturing until it is reinstalled with the flag.
[`STASHED.md`](../STASHED.md) is the full inventory.

**On-device inference.** Ollama is stashed for this release, as a chat
provider too. `HC_CHAT_PROVIDER=ollama` raises `ollama is experimental in this
release; set HC_EXPERIMENTAL=1` unless that flag is set; with it, chat
inference runs on-device:

```bash
export HC_EXPERIMENTAL=1
export HC_CHAT_PROVIDER=ollama
export HC_CHAT_MODEL=llama3.1
```

**Global context layer.** The global layer is separate and opt-in:
`HC_EXPERIMENTAL=1 hc setup --global-vault yes` imports the Claude Code
transcripts still on disk and enables future capture; adding `--goals yes`
analyzes that history before rebuilding the cross-chat goal tree, because
rebuilding alone would have no extraction cache on a fresh install. That
analysis has its own provider selection — `hc trajectory --provider ollama`
keeps it on-device, and it remembers the choice — separate from the chat-scope
`HC_CHAT_PROVIDER`. `hc ui` opens the cross-chat goal workspace, and
`hc status`, `hc lens`, `hc goals` and `hc mark` inspect and correct what it
derived.

**Working a goal with Claude.** `hc work <goal>` starts Claude Code bound to
one Vault goal (`hc work --list` shows the candidates; a title fragment works
as well as an id). The session receives a briefing for that goal alone — its
parent, description, notes, human-authored todos, and what earlier sessions
did — never the whole tree. While it runs, its `TaskCreate` / `TaskUpdate` /
`TaskList` calls are observed from the existing hooks into
`trajectory/agent-runs/<session>.json` and shown under CLAUDE'S PLAN in the
global goal workspace. The two layers stay separate on purpose: the Vault goal
is the source of truth for intent, Claude's task list is the source of truth
for the current agent plan, and an agent completing its own task never
completes the human goal or its todos.

## Development

```bash
python3 -m unittest discover -s ../tests -v
python3 -m py_compile src/human_compact/*.py src/human_compact/trajectory/*.py
uv build
```

The runtime uses Python's standard library. The npm package carries the exact
Python wheel it installs; it does not fetch executable code from a mutable Git
branch.
