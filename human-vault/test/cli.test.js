'use strict';

const assert = require('assert/strict');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const test = require('node:test');

const {
  UsageError,
  parseArgs,
  resolveChoices,
  run,
} = require('../lib/cli');
const { PassThrough } = require('stream');

function capture() {
  let value = '';
  return {
    stream: { write(chunk) { value += chunk; } },
    read() { return value; },
  };
}

function fakeReadline(answers) {
  return {
    async question() {
      if (!answers.length) throw new Error('EOF');
      return answers.shift();
    },
  };
}

function fixturePackage(root) {
  fs.mkdirSync(path.join(root, 'vendor'), { recursive: true });
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ version: '0.16.0' }));
  const wheel = 'human_compact-0.16.0-py3-none-any.whl';
  const body = Buffer.from('fixture wheel');
  fs.writeFileSync(path.join(root, 'vendor', wheel), body);
  fs.writeFileSync(path.join(root, 'vendor', 'manifest.json'), JSON.stringify({
    schema: 1,
    package: 'human-vault',
    version: '0.16.0',
    wheel,
    sha256: crypto.createHash('sha256').update(body).digest('hex'),
    sourceRevision: '1'.repeat(40),
  }));
}

test('parseArgs accepts only numeric choices and enforces goal dependency', () => {
  assert.deepEqual(parseArgs(['--non-interactive', '--global-vault', '1', '--goals', '2']), {
    globalVault: '1', goals: '2', nonInteractive: true, dryRun: false, help: false,
  });
  assert.equal(parseArgs(['--global-vault', '2']).goals, '2');
  assert.throws(() => parseArgs(['--global-vault', 'yes']), UsageError);
  assert.throws(() => parseArgs(['--global-vault', '2', '--goals', '1']), /requires/);
  assert.throws(() => parseArgs(['surprise']), /unknown option/);
});





test('dry-run verifies the package and never invokes installer', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-test-'));
  try {
    fixturePackage(root);
    const output = capture();
    let invoked = false;
    const code = await run({
      argv: ['--dry-run', '--non-interactive', '--global-vault', '2'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      output: output.stream,
      errorOutput: capture().stream,
      install: async () => { invoked = true; },
    });
    assert.equal(code, 0);
    assert.equal(invoked, false);
    assert.match(output.read(), /Verified bundled backend 0\.16\.0/);
    assert.match(output.read(), /hc ui/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the installer asks nothing and enables nothing by default', async () => {
  // Onboarding belongs to the UI now: a fresh install captures no history and
  // sends nothing until the user chooses it there.
  const chosen = await resolveChoices(parseArgs([]));
  assert.deepEqual(chosen, { globalVault: '2', goals: '2' });
});

test('explicit flags are still honoured for scripted installs', async () => {
  assert.deepEqual(await resolveChoices(parseArgs(['--global-vault', '1', '--goals', '1'])),
    { globalVault: '1', goals: '1' });
  // The contradiction is caught while parsing, before anything is installed.
  assert.throws(() => parseArgs(['--global-vault', '2', '--goals', '1']),
    /requires --global-vault 1/);
});

// The install ends by telling the user to run `hc ui`. If their shell cannot
// find `hc`, that instruction is wrong and the install has not landed.
async function installOutput({ onPath, added, present, linked }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-path-'));
  try {
    fixturePackage(root);
    const output = capture();
    await run({
      argv: ['--non-interactive', '--global-vault', '2'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      output: output.stream,
      errorOutput: capture().stream,
      install: async () => ({ launcher: '/home/u/.human-compact/bin/hc' }),
      ensureLauncherOnPath: () => ({
        onPath, added, present, linked, profile: '/home/u/.zshrc',
        line: 'export PATH="$HOME/.human-compact/bin:$PATH"',
      }),
    });
    return output.read();
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test('a reachable launcher gets no PATH advice', async () => {
  const text = await installOutput({ onPath: true, added: false });
  assert.match(text, /hc {11}ready in this terminal/);
  assert.match(text, /Next — set up your Vault/);
  assert.doesNotMatch(text, /export PATH/);
  assert.doesNotMatch(text, /Then set up/);
});

test('an unreachable launcher says what to run now, before the next step', async () => {
  const text = await installOutput({ onPath: false, added: true });
  assert.match(text, /Run this once in this terminal/);
  assert.match(text, /new terminals get it from \/home\/u\/\.zshrc/);
  // The order is the point: an instruction the user cannot yet follow must
  // not come before the one that makes it work.
  assert.ok(text.indexOf('export PATH') < text.indexOf('hc ui'));
});

test('a profile that could not be edited tells the user what to add', async () => {
  const text = await installOutput({ onPath: false, added: false });
  assert.match(text, /Add this to your shell profile/);
  assert.match(text, /export PATH="\$HOME\/\.human-compact\/bin:\$PATH"/);
  assert.ok(text.indexOf('export PATH') < text.indexOf('hc ui'));
});

test('a profile that is already correct says the shell is stale, not the config', async () => {
  const text = await installOutput({ onPath: false, added: false, present: true });
  assert.match(text, /This terminal predates \/home\/u\/\.zshrc/);
  assert.doesNotMatch(text, /Add this to your shell profile/);
});

test('a linked launcher says it works right now', async () => {
  const text = await installOutput({ onPath: true, linked: '/home/u/.local/bin/hc' });
  assert.match(text, /hc {11}ready in this terminal/);
  assert.doesNotMatch(text, /needs one more step/);
});
