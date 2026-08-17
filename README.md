# Papert Tools for Claude Code and Codex

This repository is a Claude Code and Codex plugin marketplace. Its published package is
[Compact Focus](./compact-focus): an inline, human-reviewed replacement for
blind context compaction.

It also contains [`hc`](./hc), the local goal-state runtime for Claude Code.
The [`human-vault`](./human-vault) npm package installs that runtime and the
Claude Code integration together, so setup does not require Homebrew, pipx, or
jq.

## Install chat-scoped goals

Requirements: macOS or Linux, Node.js 18+, and Claude Code 2.1.175+.

```bash
npx human-vault
```

The installer takes no options and asks no questions. It installs the `hc`
runtime, the Claude Code hooks, and the `/goals-ui` command. It captures
nothing and analyzes nothing on its own.

Start a new Claude Code session (or run `/reload-plugins`), then type:

```text
/goals-ui
```

That opens the goal workspace for the current chat in your browser. From then
on, that chat's goals are inferred with your own authenticated Claude CLI and
injected back into the chat as context: the whole goals document the first
time, then only what changed since the last message. Subagents and tool
batches receive it too. `/goals-ui disable` turns the injection off for that
chat; running `/goals-ui` again turns it back on and re-sends the whole
document.

Chats where `/goals-ui` has never run are left alone.

See the [hc documentation](./hc/README.md) for persistence, event ingestion,
the inference data boundary, and the separation between chat-scoped and global
state.

## Experimental (HC_EXPERIMENTAL=1)

The global layer — cross-chat conversation capture, its history analysis, the
cross-chat goal tree at `hc ui`, goal-bound agent runs, and the older `hc`
subcommands — is still in the tree but disconnected from this release.
`HC_EXPERIMENTAL=1` re-enables those commands and the HTTP routes behind them;
installing with `HC_EXPERIMENTAL=1 npx human-vault` additionally wires the
global capture hooks. A vault that was already enabled stops capturing after a
plain reinstall until it is reinstalled with the flag.
[`STASHED.md`](./STASHED.md) is the full inventory of what was disconnected,
where its code lives, and how to switch each piece back on.

## Install once, use in every local chat

Requirements: macOS or Linux, Python 3.9+, and either Claude Code 2.1.227+
or Codex CLI 0.147.0+.

Claude Code:

```bash
claude plugin marketplace add divadbaroon/claude-plugins
claude plugin install compact-focus@papert-tools
```

If Claude Code shows a plugin-source warning, review and accept it. Then start
a new Claude Code session and use the ordinary `/compact`.
Claude's `/hooks` menu is useful for verifying that the plugin hooks loaded,
but it is read-only and is not a separate trust step.

Codex:

```bash
codex plugin marketplace add divadbaroon/claude-plugins
codex plugin add compact-focus@papert-tools
```

Start a new Codex session, open `/hooks`, review and trust the Compact Focus
hook definition, then use the ordinary `/compact`.

The plugin opens its review inside that terminal. It does not require a second
command, pasted focus directive, browser, or skill invocation. Installation is
user-scoped by default, so it applies to new sessions across projects.

## Friend beta

Test Compact Focus in a real conversation containing several decisions, one
changed assumption, and one unresolved question. Run `/compact`, edit anything
the ledger misconstrues, then approve or cancel. Report friction through the
[Compact Focus beta feedback form](https://github.com/divadbaroon/claude-plugins/issues/new?template=compact-focus-beta.yml).

Compact Focus has no telemetry. Review corrections remain on the tester's
machine unless they deliberately include them in a report.

See the [Compact Focus documentation](./compact-focus/README.md) for the
interaction model, privacy boundary, configuration, recovery commands, and
development workflow.

## Repository scope

- `compact-focus/` is the supported marketplace plugin.
- `hc/` is the Python goal-state backend bundled by the npm installer.
- `human-vault/` is the one-command npm installer published as
  `human-vault`.

## Development

```bash
python3 -m unittest discover -s tests -v
(cd human-vault && npm test)
claude plugin validate . --strict
uv run --with pyyaml python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py ./compact-focus
claude --plugin-dir ./compact-focus
```

Compact Focus has no third-party runtime dependencies.

## License

MIT. See [LICENSE](./LICENSE).
