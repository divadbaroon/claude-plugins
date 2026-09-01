#!/usr/bin/env node
// Thin, cross-platform Claude Code hook adapter. Chat-scoped capture is always
// active once installed; unlike the Vault hook it does not require CLAUDE_VAULT.
//
// This is Node rather than a shell script because a Claude Code plugin ships one
// hooks.json command for every OS, Windows does not honor shebangs, and Node is
// the one interpreter available wherever Claude Code runs. It reads the event
// JSON on stdin, locates the `hc` runtime, and pipes the event to `hc chat-hook`.
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { isFile, resolveHc, haveHc } = require('./hc-runtime.cjs');

function readStdin() {
  try {
    return fs.readFileSync(0, 'utf8');
  } catch {
    return '';
  }
}

function repairHint() {
  const home = os.homedir();
  const localEngelbart = path.join(home, '.local', 'bin',
    process.platform === 'win32' ? 'engelbart.exe' : 'engelbart');
  if (isFile(localEngelbart)) {
    return process.platform === 'win32'
      ? `${localEngelbart} install`
      : '~/.local/bin/engelbart install';
  }
  return process.platform === 'win32'
    ? 'irm https://berkeley.mathetic.com/engelbart/install.ps1 | iex'
    : 'curl -fsSL https://berkeley.mathetic.com/engelbart/install.sh | sh';
}

function main() {
  // Internal Claude inference also loads installed hooks; without this guard a
  // goal-analysis subprocess would create another chat and recurse forever.
  if (process.env.HC_CHAT_INFERENCE === '1') return 0;

  const input = readStdin();
  const isExpansion = /"hook_event_name"\s*:\s*"UserPromptExpansion"/.test(input);
  const isSessionStart = /"hook_event_name"\s*:\s*"SessionStart"/.test(input);
  const passthrough = process.argv.slice(2);

  const hc = resolveHc();
  if (!haveHc(hc)) {
    const repair = repairHint();
    if (isExpansion) {
      process.stdout.write(JSON.stringify({
        decision: 'block',
        reason: `bart could not open: its runtime is unavailable; run: ${repair}; then retry /bart`,
      }) + '\n');
    } else if (isSessionStart) {
      process.stdout.write(JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'SessionStart',
          additionalContext: `The vault plugin is installed but its runtime is not. Goals, /bart and goal-bound sessions stay inactive until you run: ${repair}`,
        },
      }) + '\n');
    }
    return 0;
  }

  const result = spawnSync(hc.cmd, ['chat-hook', ...passthrough], {
    input,
    encoding: 'utf8',
    shell: hc.shell,
    stdio: ['pipe', 'pipe', 'ignore'],
  });
  const output = (result.stdout || '').trim();
  if (output) {
    process.stdout.write(output + '\n');
  } else if (isExpansion) {
    const status = result.status == null ? 'unknown' : result.status;
    process.stdout.write(JSON.stringify({
      decision: 'block',
      reason: `bart could not open: hc chat-hook exited without a response (status ${status})`,
    }) + '\n');
  }
  return 0;
}

process.exit(main());
