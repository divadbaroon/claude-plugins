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
      command: 'install', globalVault: '1', goals: '2',
      nonInteractive: true, dryRun: false, noLogin: false, help: false,
    });
  });
  assert.equal(parseArgs(['--global-vault', '2']).goals, '2');
  assert.throws(() => parseArgs(['--global-vault', 'yes']), UsageError);
  assert.throws(() => parseArgs(['--global-vault', '2', '--goals', '1']), /requires/);
  assert.throws(() => parseArgs(['surprise']), /unknown command/);
  assert.throws(() => parseArgs(['--surprise']), /unknown option/);
  assert.throws(() => parseArgs(['auth', 'extra']), /unknown option/);
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
      command: 'install', globalVault: '2', goals: '2',
      nonInteractive: false, dryRun: false, noLogin: false, help: false,
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
    assert.match(output.read(), /Open any Claude Code chat and type \/goals-ui\./);
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
});

// The install ends by telling the user to open /goals-ui, and says so only
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

test('help does not document a prompt the installer never shows', () => {
  // resolveChoices never reads stdin, so "require every applicable choice as a
  // flag" describes an interaction that no longer exists.
  const text = usage();
  assert.doesNotMatch(text, /require every applicable choice as a flag/);
  assert.match(text,
    /^ {2}--non-interactive {5}accepted for compatibility; the installer never prompts$/m);
});

test('the closing line says what is recorded and what waits for /goals-ui', async () => {
  // The hooks record from install; only analysis and injection wait. Claiming
  // "nothing is captured" was false the moment the plugin was on disk.
  const quiet = await withExperimentalAsync(undefined,
    () => installOutput({ onPath: true, added: false }));
  assert.match(quiet,
    /Installed\. Chats are recorded locally; nothing is analyzed or injected until you run \/goals-ui in a chat\./);
  assert.doesNotMatch(quiet, /Nothing is captured or analyzed yet/);
  assert.doesNotMatch(quiet, /Global Vault hooks are wired/);

  const wired = await withExperimentalAsync('1',
    () => installOutput({ onPath: true, added: false }));
  assert.match(wired,
    /Installed\. Chats are recorded locally; nothing is analyzed or injected until you run \/goals-ui in a chat\./);
  assert.match(wired,
    /Global Vault hooks are wired \(HC_EXPERIMENTAL=1\); capture follows your global Vault setting\./);
});

test('a reachable launcher gets no PATH advice', async () => {
  const text = await installOutput({ onPath: true, added: false });
  assert.match(text, /hc {11}ready in this terminal/);
  assert.match(text, /Next: Open any Claude Code chat and type \/goals-ui\./);
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
  assert.match(text, /Then: Open any Claude Code chat and type \/goals-ui\./);
  // The order is what matters, so pin it to the instruction itself: the
  // recording line above also names /goals-ui, and a bare indexOf would find
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
  assert.match(text, /hc {11}ready in this terminal/);
  assert.doesNotMatch(text, /needs one more step/);
});


test('account commands run without touching the installer at all', async () => {
  const output = capture();
  const errors = capture();
  const seen = [];
  const code = await run({
    argv: ['whoami'],
    output: output.stream,
    errorOutput: errors.stream,
    managedRoot: '/nonexistent/managed',
    whoami: async (options) => { seen.push(options.managedRoot); return { signedIn: true, email: 'member@example.com' }; },
    install: async () => { throw new Error('installer must not run'); },
  });
  assert.equal(code, 0);
  assert.equal(seen[0], path.resolve('/nonexistent/managed'));
  assert.match(output.read(), /Connected as member@example\.com\./);
});

test('an unconnected machine exits non-zero and says how to connect', async () => {
  const errors = capture();
  const code = await run({
    argv: ['whoami'],
    output: capture().stream,
    errorOutput: errors.stream,
    managedRoot: '/nonexistent/managed',
    whoami: async () => ({ signedIn: false }),
  });
  assert.equal(code, 1);
  assert.match(errors.read(), /engelbart auth/);
});

test('login is the same command whichever name it is called by', async () => {
  for (const name of ['auth', 'login']) {
    let called = 0;
    const code = await run({
      argv: [name],
      output: capture().stream,
      errorOutput: capture().stream,
      managedRoot: '/nonexistent/managed',
      login: async () => { called += 1; return { status: 'ready', email: 'm@example.com' }; },
    });
    assert.equal(code, 0);
    assert.equal(called, 1);
  }
  const refused = await run({
    argv: ['auth'],
    output: capture().stream,
    errorOutput: capture().stream,
    managedRoot: '/nonexistent/managed',
    login: async () => ({ status: 'denied' }),
  });
  assert.equal(refused, 1);
});

test('logout reports whether the token was actually revoked', async () => {
  const output = capture();
  await run({
    argv: ['logout'],
    output: output.stream,
    errorOutput: capture().stream,
    managedRoot: '/nonexistent/managed',
    logout: async () => ({ signedOut: true, revoked: false }),
  });
  assert.match(output.read(), /Disconnected on this machine/);
  assert.match(output.read(), /disconnect it there/);

  const clean = capture();
  await run({
    argv: ['logout'],
    output: clean.stream,
    errorOutput: capture().stream,
    managedRoot: '/nonexistent/managed',
    logout: async () => ({ signedOut: true, revoked: true }),
  });
  assert.match(clean.read(), /Disconnected\. That token is revoked\./);
});

function installWith(extra) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-test-'));
  try {
    fixturePackage(root);
    const output = capture();
    const errors = capture();
    return run({
      argv: ['--non-interactive', '--global-vault', '2'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      output: output.stream,
      errorOutput: errors.stream,
      install: async () => ({ launcher: path.join(root, 'managed', 'bin', 'hc') }),
      ensureLauncherOnPath: () => ({ onPath: true }),
      readCredentials: () => null,
      ...extra,
    }).then((code) => ({ code, output: output.read(), errors: errors.read() }));
  } finally {
    setTimeout(() => fs.rmSync(root, { recursive: true, force: true }), 0).unref?.();
  }
}

test('a person at the terminal is offered the account connection once', async () => {
  let logins = 0;
  const result = await installWith({
    interactive: true,
    login: async () => { logins += 1; return { status: 'ready', email: 'member@example.com' }; },
  });
  assert.equal(result.code, 0);
  assert.equal(logins, 1);
  assert.doesNotMatch(result.output, /Run `engelbart auth`/);
});

// A scripted install must not sit waiting on a browser that will never open.
test('a scripted install never blocks on a browser', async () => {
  let logins = 0;
  const result = await installWith({
    interactive: false,
    login: async () => { logins += 1; return { status: 'ready' }; },
  });
  assert.equal(logins, 0);
  assert.match(result.output, /Run `engelbart auth` to connect your Engelbart account/);
});

test('--no-login installs without asking about an account', async () => {
  let logins = 0;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-test-'));
  fixturePackage(root);
  const output = capture();
  const code = await run({
    argv: ['--no-login', '--non-interactive', '--global-vault', '2'],
    packageRoot: root,
    managedRoot: path.join(root, 'managed'),
    platform: 'darwin',
    arch: 'arm64',
    output: output.stream,
    errorOutput: capture().stream,
    install: async () => ({ launcher: path.join(root, 'managed', 'bin', 'hc') }),
    ensureLauncherOnPath: () => ({ onPath: true }),
    interactive: true,
    login: async () => { logins += 1; return { status: 'ready' }; },
  });
  fs.rmSync(root, { recursive: true, force: true });
  assert.equal(code, 0);
  assert.equal(logins, 0);
  assert.match(output.read(), /Run `engelbart auth`/);
});

// The runtime is installed and working by then; only the credits are missing.
test('a failed account connection reports itself without failing the install', async () => {
  const result = await installWith({
    interactive: true,
    login: async () => { throw new Error('berkeley.mathetic.com is unreachable'); },
  });
  assert.equal(result.code, 0);
  assert.match(result.errors, /Could not connect an Engelbart account: berkeley\.mathetic\.com is unreachable/);
  assert.match(result.output, /Installed\./);
  assert.match(result.output, /Run `engelbart auth`/);
});

test('reinstalling on a connected machine leaves the connection alone', async () => {
  let logins = 0;
  const result = await installWith({
    interactive: true,
    readCredentials: () => ({ token: 'egb_t', email: 'member@example.com' }),
    login: async () => { logins += 1; return { status: 'ready' }; },
  });
  assert.equal(logins, 0);
  assert.match(result.output, /account {6}member@example\.com/);
  assert.doesNotMatch(result.output, /Run `engelbart auth`/);
});
