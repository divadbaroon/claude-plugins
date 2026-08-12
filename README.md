# Papert Tools for Claude Code and Codex

This repository is a Claude Code and Codex plugin marketplace. Its published package is
[Compact Focus](./compact-focus): an inline, human-reviewed replacement for
blind context compaction.

It also contains [`hc`](./hc), the local goal-state service for Claude Code.
The `human-compact` npm package installs its Python runtime and Claude Code
integration together, so setup does not require Homebrew, pipx, or jq.

## Install chat-scoped goals

```bash
npx human-compact
```

The installer adds the Claude Code integration automatically, then asks two
numeric questions:

1. Enable the global Vault? Choose `2` to keep only chat-scoped goals.
2. If Vault is enabled, infer global goals now? Choose `1` to analyze the
   imported history and rebuild the global goal tree.

Start a new Claude Code session (or run `/reload-plugins`) and type `/hc-ui`.
That command opens the goal workspace for the current chat. The global Vault
and its cross-chat inference remain separate, opt-in layers.

See the [hc documentation](./hc/README.md) for persistence, event ingestion,
the inference data boundary, and the separation between chat-scoped and global
state.

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
- `human-compact/` is the one-command npm installer published as
  `human-compact`.

## Development

```bash
python3 -m unittest discover -s tests -v
(cd human-compact && npm test)
claude plugin validate . --strict
uv run --with pyyaml python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py ./compact-focus
claude --plugin-dir ./compact-focus
```

Compact Focus has no third-party runtime dependencies.

## License

MIT. See [LICENSE](./LICENSE).
