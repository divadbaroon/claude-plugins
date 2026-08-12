# Papert Tools for Claude Code and Codex

This repository is a Claude Code and Codex plugin marketplace. Its published package is
[Compact Focus](./compact-focus): an inline, human-reviewed replacement for
blind context compaction.

It also contains [`hc`](./hc), the pipx-installed local goal-state service for
Claude Code. `hc` is distributed from this repository rather than the plugin
marketplace because its localhost server and CLI need a Python executable.

## Install chat-scoped goals

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

If an older standalone `uv` makes `pipx install` report a backend-version
conflict, rerun that install command with `--backend pip`.

Start a new Claude Code session and run `/hc-ui`. This quick path is scoped to
the current chat and does not enable the global context layer. To add that
layer later:

```bash
hc backup
hc refresh
hc goals --rebuild
hc ui
```

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
- `hc/` is the pipx package and Claude Code hook/skill installer for goal state.
- `human-compact/` is retained as historical prototype code and is not listed
  in the marketplace or represented as a security sandbox.

## Development

```bash
python3 -m unittest discover -s tests -v
claude plugin validate . --strict
uv run --with pyyaml python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py ./compact-focus
claude --plugin-dir ./compact-focus
```

Compact Focus has no third-party runtime dependencies.

## License

MIT. See [LICENSE](./LICENSE).
