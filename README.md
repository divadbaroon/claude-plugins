# Papert Tools for Claude Code and Codex

This repository is a Claude Code and Codex plugin marketplace. Its published package is
[Compact Focus](./compact-focus): an inline, human-reviewed replacement for
blind context compaction.

## Install

Claude Code:

```bash
claude plugin marketplace add divadbaroon/claude-plugins
claude plugin install compact-focus@papert-tools
```

Codex:

```bash
codex plugin marketplace add divadbaroon/claude-plugins
codex plugin add compact-focus@papert-tools
```

Start a new host session, trust the plugin hooks with `/hooks`, then use the normal `/compact`. The
plugin opens its review inside that terminal; it does not require a second
command, pasted focus directive, browser, or skill invocation.

See the [Compact Focus documentation](./compact-focus/README.md) for the
interaction model, privacy boundary, configuration, recovery commands, and
development workflow.

## Repository scope

- `compact-focus/` is the supported marketplace plugin.
- `human-compact/` is retained as historical prototype code and is not listed
  in the marketplace or represented as a security sandbox.

## Development

```bash
python3 -m unittest discover -s tests -v
claude plugin validate . --strict
uv run --with pyyaml python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py ./compact-focus
claude --plugin-dir ./compact-focus
```

Compact Focus supports Claude Code 2.1.227+ and Codex CLI 0.147.0+ with Python
3.9+ on macOS and Linux. It has no third-party
runtime dependencies.

## License

MIT. See [LICENSE](./LICENSE).
