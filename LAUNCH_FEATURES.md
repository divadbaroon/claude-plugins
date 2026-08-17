# /goals-ui — what ships in 0.18.0

One command, one feature: a per-chat goal workspace for Claude Code, kept in
sync with the chat and injected back into it as context.

## Install (one step)

```bash
npx engelbart-cli
```

Takes no required options, asks no questions. Installs the `hc` runtime (the
Python wheel is bundled and never fetched; if no compatible local Python is
found, a pinned `uv` binary is downloaded once from GitHub to provision one),
the Claude Code hooks, and `/goals-ui`. Restart Claude Code (or
`/reload-plugins`).

## What `/goals-ui` does

- **Opens the workspace for this chat, silently.** One line appears —
  `goals-ui: http://127.0.0.1:PORT` — the browser opens; Claude sends nothing.
- **Infers this chat's goals** with your own authenticated `claude` CLI, into a
  tree with status (active / in progress / done / all — the page opens on
  *All*), priority, description, and prompt links.
- **One markdown document per goal**, rendered inline (headings, lists,
  checkboxes, bold/code) with default sections: `# Objective`, `# In my words`,
  `# Decisions`, `# Built`, `# Blockers`, `# Open questions`. Inference writes
  under those headings and only ever *appends* on later runs; your text is
  never rewritten, reordered, or truncated. Saved to disk per chat and to
  localStorage — survives reload, a new server, and closing the tab.
- **Prompt linking**: attach or detach any of this chat's prompts to a goal
  (`+ add a prompt`, picker with search; `automatic` vs your own links).
- **Assembled prompt** (PROMPT tab): the goal's document + linked prompts,
  read-only, one-click **Copy prompt**.
- **Context injection, once then as diffs.** After `/goals-ui`, the goals
  document is injected into the chat: the whole file on the first message and
  after compaction, then only a unified diff of what changed (nothing when
  unchanged). Subagents get the full document when they start; tool batches
  get the diff before the next model call. A mirror lives at
  `~/.claude/projects/<project>/goals-ui/<session>.md`. No character caps.
- **Persistent.** Invoke once per chat; it stays on. `/goals-ui disable`
  turns analysis and injection off for that chat; `/goals-ui` turns them back
  on and re-sends the whole document.
- **Notices.** A small banner in the workspace when Claude finishes a turn or a
  subagent returns; the tab is titled `goals · <session>`.

## Data boundary

From install, hooks record each chat's own prompts and events to a local,
owner-only store under `~/.claude-vault/chat-sessions/<session>/` (the same
conversation Claude Code keeps in `~/.claude/projects/`). Nothing is analyzed
or injected until `/goals-ui` runs in that chat. Nothing leaves your machine
except the model calls your own `claude` CLI makes. No telemetry.

## Not in this release (kept in the tree, off by default)

Global cross-chat Vault, history backfill, Ollama/on-device inference, parallel
extraction, global analysis and its progress banner, the Conversations page and
full threads, `hc ui`, goal-bound agent runs, Agent/Review panes, model plans,
briefings, evidence graph, lens, important items, and a few smaller surfaces.
`HC_EXPERIMENTAL=1` re-enables them; [`STASHED.md`](./STASHED.md) is the
complete inventory, with the commit that disconnected each one and how to turn
it back on.

## Test it locally

```bash
git fetch origin && git checkout main
cd engelbart && npm pack && npx ./engelbart-cli-0.18.0.tgz  # one-install from the checkout
```
Then in any Claude Code chat: `/goals-ui` → workspace opens on *All*; type,
reload, re-run `/goals-ui` → the document persists; ask Claude "what are my
goals?" → it has them; `/goals-ui disable` → no more injection.

Full checks from the checkout:
`python3 -W error::ResourceWarning -m unittest discover -s tests` (803, incl.
real-browser tests), `cd engelbart && npm test && npm run test:pack`.

Rollback: `npx human-vault@0.17.89` — the last release under the package's
previous name. `engelbart-cli` starts at 0.18.0 and has nothing earlier.
