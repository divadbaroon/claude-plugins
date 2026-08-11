#!/usr/bin/env node
// human-compact — launch Claude Code against a FORK of a past session, inside
// a study sandbox, with the compaction hook wired in for exactly this
// invocation (via --settings overlay; the participant's normal sessions and
// settings are never touched).
//
//   human-compact                 pick a session interactively, fork it
//   human-compact <session-id>    fork that session
//   --participant <name>          state dir label (default: timestamp)
//   --hook <path>                 PreCompact hook override (else auto-resolve)
//   --dry-run                     print the command + settings, don't launch
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const pkgRoot = path.resolve(__dirname, '..');
const args = process.argv.slice(2);

function take(flag) {
  const i = args.indexOf(flag);
  if (i === -1) return null;
  const v = args[i + 1];
  args.splice(i, 2);
  return v;
}
const dryRun = args.includes('--dry-run');
if (dryRun) args.splice(args.indexOf('--dry-run'), 1);
const participant = take('--participant');
const hookOverride = take('--hook');
const sessionId = args.find((a) => !a.startsWith('-')) || null;

// Per-run state dir: instrument data (log.jsonl, demoted.jsonl, lens.md,
// threads.json) lands here, never in the participant's real plugin state.
const label = (participant || new Date().toISOString()).replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 64) || 'run';
const stateDir = path.join(os.homedir(), '.human-compact', 'state', label);
fs.mkdirSync(stateDir, { recursive: true });

// PreCompact hook resolution: explicit flag/env, then repo-sibling layout,
// then a skills-dir install. Missing hook = warn and continue (the sandbox,
// statusline, and fork still work; /compact just runs stock).
const candidates = [
  hookOverride,
  process.env.HUMAN_COMPACT_HOOK,
  path.join(pkgRoot, '..', 'compact-focus', 'scripts', 'compact-focus.sh'),
  path.join(os.homedir(), '.claude', 'skills', 'compact-focus', 'scripts', 'compact-focus.sh'),
].filter(Boolean);
const hook = candidates.find((p) => {
  try { return fs.statSync(p).isFile(); } catch { return false; }
});
if (!hook) console.error('human-compact: no compaction hook found — /compact will run stock. Use --hook <path>.');

const sh = (name) => path.join(pkgRoot, 'scripts', name);

const settings = {
  statusLine: { type: 'command', command: sh('statusline.sh') },
  permissions: { deny: ['Edit', 'Write', 'NotebookEdit'] },
  hooks: {
    ...(hook && {
      PreCompact: [
        { matcher: 'manual|auto', hooks: [{ type: 'command', command: hook, timeout: 20 }] },
      ],
    }),
    SessionStart: [
      { matcher: 'resume', hooks: [{ type: 'command', command: sh('session-start.sh'), timeout: 10 }] },
    ],
    PreToolUse: [
      { matcher: 'Bash', hooks: [{ type: 'command', command: sh('sandbox-guard.sh'), timeout: 10 }] },
    ],
    UserPromptSubmit: [
      { hooks: [{ type: 'command', command: sh('graveyard-reminder.sh'), timeout: 10 }] },
    ],
  },
};

const settingsPath = path.join(stateDir, 'settings.json');
fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2));

const claudeArgs = ['--resume'];
if (sessionId) claudeArgs.push(sessionId);
claudeArgs.push('--fork-session', '--settings', settingsPath);

const env = {
  ...process.env,
  COMPACT_FOCUS_STATE_DIR: stateDir,
  HUMAN_COMPACT: '1',
  COMPACT_FOCUS_STUDY: '1',
};

if (dryRun) {
  console.log('command : claude ' + claudeArgs.join(' '));
  console.log('state   : ' + stateDir);
  console.log('hook    : ' + (hook || '(none)'));
  console.log('settings: ' + settingsPath);
  process.exit(0);
}

const res = spawnSync('claude', claudeArgs, { stdio: 'inherit', env });
process.exit(res.status === null ? 1 : res.status);
