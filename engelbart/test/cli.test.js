'use strict';

const assert = require('assert/strict');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const test = require('node:test');

const {
  UsageError,
  connectEngelbartAccount,
  parseArgs,
  resolveChoices,
  run,
  usage,
} = require('../lib/cli');
const { PassThrough } = require('stream');

// The gate reads the environment at parse time, so each test states it.
function withExperimental(value, body) {
  const previous = process.env.HC_EXPERIMENTAL;
  if (value === undefined) delete process.env.HC_EXPERIMENTAL;
  else process.env.HC_EXPERIMENTAL = value;
  try {
    return body();
  } finally {
    if (previous === undefined) delete process.env.HC_EXPERIMENTAL;
    else process.env.HC_EXPERIMENTAL = previous;
  }
}

// `run` reads the flag after its awaits, so an async body needs the variable
// held for the whole call, not just until the promise is returned.
async function withExperimentalAsync(value, body) {
  const previous = process.env.HC_EXPERIMENTAL;
  if (value === undefined) delete process.env.HC_EXPERIMENTAL;
  else process.env.HC_EXPERIMENTAL = value;
  try {
    return await body();
  } finally {
    if (previous === undefined) delete process.env.HC_EXPERIMENTAL;
    else process.env.HC_EXPERIMENTAL = previous;
  }
}

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
    package: 'engelbart-cli',
    version: '0.16.0',
    wheel,
    sha256: crypto.createHash('sha256').update(body).digest('hex'),
    sourceRevision: '1'.repeat(40),
  }));
}

test('parseArgs accepts only numeric choices and enforces goal dependency', () => {
  withExperimental('1', () => {
    assert.deepEqual(parseArgs(['--non-interactive', '--global-vault', '1', '--goals', '2']), {
      globalVault: '1', goals: '2', localOnly: false,
      nonInteractive: true, dryRun: false, help: false,
    });
  });
  assert.equal(parseArgs(['--global-vault', '2']).goals, '2');
  assert.throws(() => parseArgs(['--global-vault', 'yes']), UsageError);
  assert.throws(() => parseArgs(['--global-vault', '2', '--goals', '1']), /requires/);
  assert.throws(() => parseArgs(['surprise']), /unknown option/);
});

test('turning global Vault on is refused without HC_EXPERIMENTAL=1', () => {
  withExperimental(undefined, () => {
    for (const argv of [['--global-vault', '1'], ['--global-vault', '1', '--goals', '1']]) {
      assert.throws(() => parseArgs(argv), UsageError);
      assert.throws(() => parseArgs(argv),
        /--global-vault and --goals are experimental in this release; set HC_EXPERIMENTAL=1/);
    }
    // The inert choice keeps working, so scripted installs do not break.
    assert.deepEqual(parseArgs(['--global-vault', '2', '--goals', '2']), {
      globalVault: '2', goals: '2', localOnly: false,
      nonInteractive: false, dryRun: false, help: false,
    });
  });
  withExperimental('0', () => {
    assert.throws(() => parseArgs(['--goals', '1', '--global-vault', '1']), UsageError);
  });
  withExperimental('1', () => {
    assert.equal(parseArgs(['--global-vault', '1', '--goals', '1']).goals, '1');
  });
});

test('help documents the launch surface, not the experimental flags', () => {
  const text = usage();
  assert.doesNotMatch(text, /^ *--global-vault <1\|2>/m);
  assert.doesNotMatch(text, /^ *--goals <1\|2>/m);
  assert.match(text,
    /Global Vault features are experimental; set HC_EXPERIMENTAL=1 to use --global-vault\/--goals\./);
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
    assert.match(output.read(), /Open any Claude Code chat and type \/bart\./);
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
  assert.deepEqual(
    await resolveChoices(withExperimental('1',
      () => parseArgs(['--global-vault', '1', '--goals', '1']))),
    { globalVault: '1', goals: '1' });
  // The contradiction is caught while parsing, before anything is installed.
  assert.throws(() => parseArgs(['--global-vault', '2', '--goals', '1']),
    /requires --global-vault 1/);
  assert.equal(parseArgs(['--local-only']).localOnly, true);
});

test('a default install connects the account and local-only never does', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-account-'));
  try {
    fixturePackage(root);
    const connected = [];
    const common = {
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      output: capture().stream,
      errorOutput: capture().stream,
      install: async () => ({ launcher: '/managed/hc', bartLauncher: '/managed/bart' }),
      ensureLauncherOnPath: () => ({ onPath: true }),
      connectAccount: async (options) => { connected.push(options.bartLauncher); },
    };
    assert.equal(await run({ ...common, argv: [] }), 0);
    assert.deepEqual(connected, ['/managed/bart']);
    assert.equal(await run({ ...common, argv: ['--local-only'] }), 0);
    assert.deepEqual(connected, ['/managed/bart']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// The install ends by telling the user to open /bart, and says so only
// after any step their shell still needs to reach `hc`.
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
      install: async () => ({
        launcher: '/home/u/.human-compact/bin/hc',
        bartLauncher: '/home/u/.human-compact/bin/bart',
      }),
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

test('help does not document a prompt the installer never shows', () => {
  const text = usage();
  assert.match(text, /^ {2}--local-only {10}install without connecting an Engelbart account$/m);
  assert.match(text, /^ {2}--non-interactive {5}install locally without opening a browser$/m);
});

test('account connection delegates the complete flow to bart auth', async () => {
  const calls = [];
  const output = capture();
  await connectEngelbartAccount({
    bartLauncher: '/managed/bart',
    env: { TEST_ENV: 'yes' },
    output: output.stream,
    runner(command, args, options) {
      calls.push({ command, args, options });
      return { status: 0 };
    },
  });
  assert.equal(calls[0].command, '/managed/bart');
  assert.deepEqual(calls[0].args, ['auth']);
  assert.equal(calls[0].options.env.TEST_ENV, 'yes');
  assert.equal(calls[0].options.stdio, 'inherit');
  assert.match(output.read(), /credits configured/);
});

test('the closing line says what is recorded and what waits for /bart', async () => {
  // The hooks record from install; only analysis and injection wait. Claiming
  // "nothing is captured" was false the moment the plugin was on disk.
  const quiet = await withExperimentalAsync(undefined,
    () => installOutput({ onPath: true, added: false }));
  assert.match(quiet,
    /Installed\. Chats are recorded locally; nothing is analyzed or injected until you run \/bart in a chat\./);
  assert.doesNotMatch(quiet, /Nothing is captured or analyzed yet/);
  assert.doesNotMatch(quiet, /Global Vault hooks are wired/);

  const wired = await withExperimentalAsync('1',
    () => installOutput({ onPath: true, added: false }));
  assert.match(wired,
    /Installed\. Chats are recorded locally; nothing is analyzed or injected until you run \/bart in a chat\./);
  assert.match(wired,
    /Global Vault hooks are wired \(HC_EXPERIMENTAL=1\); capture follows your global Vault setting\./);
});

test('a reachable launcher gets no PATH advice', async () => {
  const text = await installOutput({ onPath: true, added: false });
  assert.match(text, /hc \+ bart {4}ready in this terminal/);
  assert.match(text, /Next: Open any Claude Code chat and type \/bart\./);
  assert.doesNotMatch(text, /export PATH/);
  assert.doesNotMatch(text, /Then:/);
  assert.doesNotMatch(text, /hc ui/);
});

test('an unreachable launcher says what to run now, before the next step', async () => {
  const text = await installOutput({ onPath: false, added: true });
  assert.match(text, /Run this once in this terminal/);
  assert.match(text, /new terminals get it from \/home\/u\/\.zshrc/);
  // The order is the point: an instruction the user cannot yet follow must
  // not come before the one that makes it work.
  assert.match(text, /Then: Open any Claude Code chat and type \/bart\./);
  // The order is what matters, so pin it to the instruction itself: the
  // recording line above also names /bart, and a bare indexOf would find
  // that one and pass no matter where the instruction ended up.
  assert.ok(text.indexOf('export PATH')
    < text.indexOf('Then: Open any Claude Code chat'));
});

test('a profile that could not be edited tells the user what to add', async () => {
  const text = await installOutput({ onPath: false, added: false });
  assert.match(text, /Add this to your shell profile/);
  assert.match(text, /export PATH="\$HOME\/\.human-compact\/bin:\$PATH"/);
  assert.ok(text.indexOf('export PATH')
    < text.indexOf('Then: Open any Claude Code chat'));
});

test('a profile that is already correct says the shell is stale, not the config', async () => {
  const text = await installOutput({ onPath: false, added: false, present: true });
  assert.match(text, /This terminal predates \/home\/u\/\.zshrc/);
  assert.doesNotMatch(text, /Add this to your shell profile/);
});

test('a linked launcher says it works right now', async () => {
  const text = await installOutput({ onPath: true, linked: '/home/u/.local/bin/hc' });
  assert.match(text, /hc \+ bart {4}ready in this terminal/);
  assert.doesNotMatch(text, /needs one more step/);
});
