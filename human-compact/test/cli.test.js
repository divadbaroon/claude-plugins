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
  promptChoice,
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
    package: 'human-compact',
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

test('promptChoice reprompts until input is exactly 1 or 2', async () => {
  const output = capture();
  const choice = await promptChoice(
    fakeReadline(['yes', '', '2']),
    output.stream,
    'Enable?',
    [['1', 'Yes'], ['2', 'No']],
  );
  assert.equal(choice, '2');
  assert.equal((output.read().match(/Enter 1 or 2/g) || []).length, 2);
});

test('real line reader preserves piped answers across a reprompt', async () => {
  const input = new PassThrough();
  const output = capture();
  input.end('wrong\n2\n');
  const choices = await resolveChoices(parseArgs([]), { input, output: output.stream });
  assert.deepEqual(choices, { globalVault: '2', goals: '2' });
  assert.match(output.read(), /Enter 1 or 2/);
});

test('interactive flow asks about goals only when Vault is enabled', async () => {
  const output = capture();
  assert.deepEqual(
    await resolveChoices(parseArgs([]), { output: output.stream, readline: fakeReadline(['2']) }),
    { globalVault: '2', goals: '2' },
  );
  assert.doesNotMatch(output.read(), /Infer global goals/);

  const second = capture();
  assert.deepEqual(
    await resolveChoices(parseArgs([]), { output: second.stream, readline: fakeReadline(['1', '1']) }),
    { globalVault: '1', goals: '1' },
  );
  assert.match(second.read(), /Infer global goals now/);
  assert.match(second.read(), /sends bounded conversation digests to Anthropic/);

  await assert.rejects(
    resolveChoices(parseArgs(['--goals', '1']), {
      output: capture().stream,
      readline: fakeReadline(['2']),
    }),
    /requires --global-vault 1/,
  );
});

test('noninteractive flow requires every applicable choice', async () => {
  await assert.rejects(
    resolveChoices(parseArgs(['--non-interactive'])),
    /requires --global-vault/,
  );
  await assert.rejects(
    resolveChoices(parseArgs(['--non-interactive', '--global-vault', '1'])),
    /requires --goals/,
  );
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
    assert.match(output.read(), /run \/hc-ui\./);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
