# Papert Tools for Claude Code

This repository is a Claude Code plugin marketplace. Its published package is
[Compact Focus](./compact-focus): an inline, human-reviewed replacement for
blind context compaction.

## Install

```bash
claude plugin marketplace add divadbaroon/claude-plugins
claude plugin install compact-focus@papert-tools
```

Start a new Claude Code session, then use the normal `/compact` command. The
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
claude --plugin-dir ./compact-focus
```

Compact Focus supports Python 3.9+ on macOS and Linux. It has no third-party
runtime dependencies.

## License

MIT. See [LICENSE](./LICENSE).
