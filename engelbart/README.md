# engelbart-cli

Install `/bart` — chat-scoped goal workspaces for Claude Code — with one
command:

```bash
npx engelbart-cli
```

The installer takes no required options. It installs the `hc` runtime, the
Claude Code hooks, and the `/bart` command, then opens the browser to connect
the machine to an existing Engelbart account. Use `--local-only` to skip the
account connection and keep the installation local.

From then on the hooks record each chat's own prompts and events to a local,
owner-only store under `~/.claude-vault/chat-sessions/<session-id>/` — the
same conversation Claude Code already keeps in `~/.claude/projects/`. Nothing
is analyzed or injected until you run `/bart` in that chat, and nothing
leaves your machine except the model calls your own `claude` CLI makes.

Start a new Claude Code session (or run `/reload-plugins`), then run:

```text
/bart
```

That opens the goal workspace for the current chat. From then on that chat's
goals are inferred with your own authenticated Claude CLI and injected back
into the chat as context — the whole goals document first, then only what
changed since your last message. Subagents and tool batches receive it too.
`/bart disable` turns analysis and injection off again for that chat;
`/bart` turns them back on.

The Python backend is an exact wheel bundled in the npm release. It is
installed into a managed private runtime under `~/.human-compact/`; the npm
package does not fetch code from a mutable Git branch and does not run install
lifecycle scripts.

## Noninteractive installation

Scripted or deliberately local installation skips browser authentication:

```bash
npx engelbart-cli --local-only
```

`--non-interactive` is still accepted for compatibility and also skips browser
authentication.
`--dry-run` verifies the bundled wheel and prints the plan without installing.

## Experimental (HC_EXPERIMENTAL=1)

The global Vault — cross-chat conversation capture — and the global goal
inference built on it are experimental in this release. `--global-vault 1` and
`--goals 1` are refused unless `HC_EXPERIMENTAL=1` is set, and the global
capture hooks are installed only when the flag is set at install time:

```bash
# Global Vault plus global goal inference
HC_EXPERIMENTAL=1 npx engelbart-cli --non-interactive --global-vault 1 --goals 1

# Global Vault without running goal inference now
HC_EXPERIMENTAL=1 npx engelbart-cli --non-interactive --global-vault 1 --goals 2
```

Values other than `1` and `2` are rejected, and `--goals 1` is invalid when the
global Vault is disabled.

If you already had the global Vault enabled, a plain reinstall leaves it
enabled on disk but stops capturing, because the default hook set no longer
calls the Vault hook. Reinstall with `HC_EXPERIMENTAL=1` to wire it again.
See [`STASHED.md`](../STASHED.md) for the full inventory.

## Requirements and state

- macOS or Linux
- Node.js 18+
- Claude Code 2.1.175+

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
