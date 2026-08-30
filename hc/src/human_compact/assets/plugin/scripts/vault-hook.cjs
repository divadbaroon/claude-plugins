#!/usr/bin/env node
// Cross-platform Vault lifecycle hook. Inert unless the global Vault is enabled;
// its whole job is to hand the event to the installed runtime, which does the
// snapshotting and queueing. Never blocks, never writes into the session, and
// exits 0 on every failure path so a hook can never break a Claude Code turn.
//
// The old bash version carried a jq/coreutils fallback for installs whose Python
// CLI had been removed; that path cannot run on Windows and is obsolete, so this
// port keeps only the supported route: locate hc and run `hc global-hook`.
'use strict';

const fs = require('node:fs');
const { spawnSync } = require('node:child_process');

const { resolveHc, haveHc } = require('./hc-runtime.cjs');

function readStdin() {
  try {
    return fs.readFileSync(0, 'utf8');
  } catch {
    return '';
  }
}

function main() {
  const input = readStdin();
  const hc = resolveHc();
  if (!haveHc(hc)) return 0;   // no runtime: silently do nothing
  const result = spawnSync(hc.cmd, ['global-hook'], {
    input,
    encoding: 'utf8',
    shell: hc.shell,
    // This hook is only wired in the experimental config; carry that to the
    // runtime here rather than as a POSIX `VAR=1 cmd` prefix, which no Windows
    // shell understands.
    env: { ...process.env, HC_EXPERIMENTAL: '1' },
    stdio: ['pipe', 'pipe', 'ignore'],
  });
  const output = (result.stdout || '').trim();
  if (output) process.stdout.write(output + '\n');
  return 0;
}

process.exit(main());
