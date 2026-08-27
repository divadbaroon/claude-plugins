# engelbart-cli

Install `/goals-ui` — chat-scoped goal workspaces for Claude Code — with one
command:

```bash
npx engelbart-cli
```

The installer takes no required options and asks no questions. It installs
the `hc` runtime, the Claude Code hooks, and the `/goals-ui` command, then
connects this machine to your Engelbart account.

## Connecting your account

There is no password prompt. The installer prints a short code, opens
`https://berkeley.mathetic.com/engelbart` in your browser, and waits while you
sign in and approve that code on screen. Approving it writes a machine-scoped
token to `~/.human-compact/auth.json`, readable only by you.

Only approve a code your own terminal printed. The installer keeps a second
secret that never leaves your machine, so a pairing link someone else sends you
cannot connect their terminal to your account.

```bash
engelbart auth      # connect this machine (or reconnect it)
engelbart whoami    # show which account this machine is connected to
engelbart env       # print the exports that point Claude Code at your credit
engelbart logout    # disconnect this machine and revoke its token
```

## Using your Claude credit

Approving the code also fetches the Claude key your account was allocated, so
there is nothing to copy out of the browser. The key lands in the same
owner-only file as the token; `engelbart env` prints the two lines a shell
needs to reach it:

```bash
eval "$(engelbart env)"     # this terminal
claude
```

Add that `eval` line to your shell profile to have every new terminal pick it
up. `engelbart env` writes only the exports to stdout, so it is safe to run
through `eval`; everything else it has to say goes to stderr.

Credits can lag a new account. If the key is not ready when you approve the
code, the machine still connects -- run `engelbart auth` again once it is.

Connecting is skipped when there is no terminal to answer in -- a scripted or
CI install never waits on a browser -- and `--no-login` skips it outright. The
install itself does not depend on it: run `engelbart auth` whenever you are
ready. Set `ENGELBART_API_BASE` to point at a deployment other than
`https://berkeley.mathetic.com`.

From then on the hooks record each chat's own prompts and events to a local,
owner-only store under `~/.claude-vault/chat-sessions/<session-id>/` — the
same conversation Claude Code already keeps in `~/.claude/projects/`. Nothing
is analyzed or injected until you run `/goals-ui` in that chat, and nothing
leaves your machine except the model calls your own `claude` CLI makes.

Start a new Claude Code session (or run `/reload-plugins`), then run:

```text
/goals-ui
```

That opens the goal workspace for the current chat. From then on that chat's
goals are inferred with your own authenticated Claude CLI and injected back
into the chat as context — the whole goals document first, then only what
changed since your last message. Subagents and tool batches receive it too.
`/goals-ui disable` turns analysis and injection off again for that chat;
`/goals-ui` turns them back on.

The Python backend is an exact wheel bundled in the npm release. It is
installed into a managed private runtime under `~/.human-compact/`; the npm
package does not fetch code from a mutable Git branch and does not run install
lifecycle scripts.

## Noninteractive installation

There is nothing to answer, so a scripted install is the same command:

```bash
npx engelbart-cli
```

`--non-interactive` is still accepted for compatibility and changes nothing.
`--dry-run` verifies the bundled wheel and prints the plan without installing.
Neither form waits on a browser, so a scripted install finishes unattended and
leaves the account to be connected later with `engelbart auth`.

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
