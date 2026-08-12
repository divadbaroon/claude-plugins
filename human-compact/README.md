# human-compact

Install chat-scoped goal workspaces for Claude Code with one command:

```bash
npx human-compact
```

The installer asks two numeric questions:

1. whether to enable the optional global Vault; and
2. if Vault is enabled, whether to analyze history and build global goals now.

It always installs the Claude Code integration for chat-scoped goals. Start a
new Claude Code session (or run `/reload-plugins`), then run:

```text
/hc-ui
```

The Python backend is an exact wheel bundled in the npm release. It is
installed into a managed private runtime under `~/.human-compact/`; the npm
package does not fetch code from a mutable Git branch and does not run install
lifecycle scripts.

## Noninteractive installation

Use numeric flags in automation:

```bash
# Chat-scoped /hc-ui only
npx human-compact --non-interactive --global-vault 2

# Global Vault plus global goal inference
npx human-compact --non-interactive --global-vault 1 --goals 1

# Global Vault without running goal inference now
npx human-compact --non-interactive --global-vault 1 --goals 2
```

Values other than `1` and `2` are rejected. `--goals 1` is invalid when the
global Vault is disabled.

## Requirements and state

- macOS or Linux
- Node.js 18+
- Claude Code

Installer metadata lives at `~/.human-compact/install.json`; versioned Python
runtimes live under `~/.human-compact/runtimes/`; and the stable backend
launcher is `~/.human-compact/bin/hc`. Re-running the same npm version repairs
the Claude integration and reuses a verified runtime.

The installer uses an existing compatible Python when available. Otherwise it
downloads pinned `uv` 0.11.32 release assets, verifies their published SHA-256,
and provisions a managed Python automatically. Set `HUMAN_COMPACT_PYTHON` to
an explicit Python executable when automatic interpreter discovery is
unsuitable.

## Maintainer release step

After the matching `hc/` source is committed, populate the immutable wheel and
its checksum manifest:

```bash
npm run build:vendor
npm test
npm publish --dry-run
```

The published tarball must contain `vendor/manifest.json` and exactly the wheel
named by that manifest.
