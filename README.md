

https://github.com/user-attachments/assets/b77059b3-b9eb-4339-aece-1e1f9d53f43e


# Engelbart

**Open-Source Claude Code Plugin for Goals and TODOs**

## Packages

| | what it is |
|---|---|
| [`engelbart-cli`](./engelbart) | npm installer — puts `hc` and the Claude Code integration on a machine in one step |
| [`hc`](./hc) | the local goal-state runtime: capture, inference, workspace server, context injection |
| [`compact-focus`](./compact-focus) | inline, human-reviewed replacement for blind context compaction |

## Install

macOS or Linux, Node 18+, Claude Code 2.1.175+.

```bash
npx engelbart-cli          # no options, no questions
```

Restart Claude Code (or `/reload-plugins`).

## Use

```text
/goals-ui                # opens this chat's goal workspace; Claude says nothing
/goals-ui disable        # stops analysis and injection for this chat
```

- **Workspace** — goal tree, one markdown document per goal, linked prompts,
  assembled prompt. Per chat, on a local port.
- **Injection** — after the first `/goals-ui`, the goals document goes back
  into the chat: whole file on session start and after compaction, a diff
  afterwards. Subagents and tool batches read it too.
- **Persistence** — one invocation holds for the life of the chat.

## Data boundary

- Hooks record each chat's own prompts and events to
  `~/.claude-vault/chat-sessions/<session-id>/`, owner-only. This starts at
  install, not at `/goals-ui`.
- **Nothing is analyzed or injected until `/goals-ui` runs in that chat.**
- Inference runs through your own authenticated `claude` CLI. No telemetry,
  no network egress of your own.

## Experimental

`HC_EXPERIMENTAL=1` re-enables the disconnected global layer — cross-chat
capture and analysis, `hc ui`, goal-bound agent runs, older subcommands.
[`STASHED.md`](./STASHED.md) is the inventory; [`LAUNCH_FEATURES.md`](./LAUNCH_FEATURES.md)
is what ships.

## Develop

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests   # incl. real-browser tests
cd engelbart && npm test && npm run test:pack
cd engelbart && npm run build:vendor                            # re-vendor the wheel after hc/ changes
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) and [`hc/README.md`](./hc/README.md).
