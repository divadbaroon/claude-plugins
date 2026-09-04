'use strict';

const assert = require('assert/strict');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const test = require('node:test');

const {
  BROWSER_RESUME_BANNER,
  UsageError,
  importSetup,
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
      command: 'install', code: null, globalVault: '1', goals: '2',
      nonInteractive: true, dryRun: false, localOnly: false, noOpen: false,
      help: false,
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
      command: 'install', code: null, globalVault: '2', goals: '2',
      nonInteractive: false, dryRun: false, localOnly: false, noOpen: false,
      help: false,
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
    assert.match(output.read(), /Dry run complete\. No files or settings were changed\./);
    assert.doesNotMatch(output.read(), /Installed\.|setup-ui/);
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

// Once authentication has yielded a Claude key, install ends on setup -- and
// says so only after any step the shell still needs to reach `hc`.
async function installOutput({ onPath, added, present, linked }, extra) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-path-'));
  try {
    fixturePackage(root);
    const output = capture();
    const options = extra || {};
    await run({
      argv: ['--global-vault', '2'],
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
      interactive: true,
    claudeOnPath: () => true,
      readCredentials: () => null,
      login: async () => ({
        status: 'ready',
        email: 'member@example.com',
        claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      }),
      // Nothing is spawned in a test: the page-opening step is injected,
      // and answers null unless a case says otherwise.
      openSetup: options.openSetup || (async () => null),
      ...options,
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
  // Someone who has just installed has no chat and no project, so the one
  // instruction is the page that asks which of those they are doing.
  assert.match(text, /Next: Run `hc setup-ui` to set up your first project\./);
  assert.doesNotMatch(text, /export PATH/);
  assert.doesNotMatch(text, /Then:/);
  assert.doesNotMatch(text, /hc ui/);
});

test('a launcher that opens the page is named instead of the command', async () => {
  // The page is what they should be looking at; the command is the fallback
  // for when it could not be opened for them.
  let setupEnv;
  const text = await installOutput({ onPath: true, added: false },
    { openSetup: async (options) => {
      setupEnv = options.env;
      return 'http://127.0.0.1:5321/setup';
    } });
  assert.match(text, /Next: Setting up your first project: http:\/\/127\.0\.0\.1:5321\/setup/);
  assert.equal(setupEnv.ANTHROPIC_AUTH_TOKEN, 'sk-issued');
  assert.equal(setupEnv.ANTHROPIC_BASE_URL, 'https://proxy.example.com');
  assert.equal(setupEnv.HC_USE_API_KEY, '1');
  // And the other half of the fork, for someone whose work already exists.
  assert.match(text, /Already have a project\? Open its chat with `claude -r`/);
});

test('/bart is installed before the new auth flow and setup UI run', async () => {
  const order = [];
  await installOutput({ onPath: true, added: false }, {
    install: async () => {
      order.push('install /bart');
      return {
        launcher: '/home/u/.human-compact/bin/hc',
        bartLauncher: '/home/u/.human-compact/bin/bart',
      };
    },
    login: async () => {
      order.push('auth');
      return {
        status: 'ready',
        email: 'member@example.com',
        claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      };
    },
    openSetup: async () => {
      order.push('setup UI');
      return 'http://127.0.0.1:5321/setup';
    },
  });
  assert.deepEqual(order, ['install /bart', 'auth', 'setup UI']);
});

test('setup stays closed until authentication returns a Claude key', async () => {
  let opened = 0;
  const text = await installOutput({ onPath: true, added: false }, {
    login: async () => ({ status: 'ready', email: 'member@example.com', claude: null }),
    openSetup: async () => { opened += 1; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.equal(opened, 0);
  assert.match(text, /Run `npx engelbart-cli auth`/);
  assert.match(text, /Setup starts after that/);
  assert.doesNotMatch(text, /Setting up your first project/);
});

test('setup stays closed when Supabase configuration fails after key issuance', async () => {
  let opened = 0;
  const text = await installOutput({ onPath: true, added: false }, {
    login: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      projectConfigured: false,
    }),
    openSetup: async () => { opened += 1; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.equal(opened, 0);
  assert.match(text, /Supabase sync/);
  assert.doesNotMatch(text, /Setting up your first project/);
});

test('an incomplete browser connection keeps recovery instructions instead of the banner', async () => {
  const text = await installOutput({ onPath: true, added: false }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--no-open', '--global-vault', '2'],
    redeemCode: async () => ({ status: 'ready', email: 'member@example.com', claude: null }),
  });
  assert.notEqual(text, BROWSER_RESUME_BANNER);
  assert.match(text, /Run `npx engelbart-cli auth`/);
  assert.match(text, /Setup starts after that/);
});

test('--no-open authenticates but suppresses the automatic setup launch', async () => {
  let opened = 0;
  const text = await installOutput({ onPath: true, added: false }, {
    argv: ['--no-open', '--global-vault', '2'],
    openSetup: async () => { opened += 1; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.equal(opened, 0);
  assert.match(text, /Next: Run `hc setup-ui` to set up your first project\./);
});

test('a page that would not open leaves a command that can be typed', async () => {
  // Never fatal: a browser that will not open must not read as a failed
  // install, so the reader is left with something to run.
  const text = await installOutput({ onPath: true, added: false },
    { openSetup: async () => null });
  assert.match(text, /Next: Run `hc setup-ui` to set up your first project\./);
});

test('an unreachable stable launcher still opens setup by its absolute runtime path', async () => {
  let launches = 0;
  const text = await installOutput({ onPath: false, added: true }, {
    openSetup: async () => { launches += 1; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.match(text, /Run this once in this terminal/);
  assert.match(text, /new terminals get it from \/home\/u\/\.zshrc/);
  assert.equal(launches, 1);
  assert.match(text, /Next: Setting up your first project: http:\/\/127\.0\.0\.1:5321\/setup/);
});

test('a profile that could not be edited tells the user what to add', async () => {
  const text = await installOutput({ onPath: false, added: false });
  assert.match(text, /Add this to your shell profile/);
  assert.match(text, /export PATH="\$HOME\/\.human-compact\/bin:\$PATH"/);
  assert.ok(text.indexOf('export PATH')
    < text.indexOf('Then: Run the line above'));
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
  assert.match(errors.read(), /npx engelbart-cli auth/);
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

test('a repaired authentication starts onboarding with the issued key', async () => {
  let setup;
  const output = capture();
  const code = await run({
    argv: ['auth'],
    platform: 'linux',
    output: output.stream,
    errorOutput: capture().stream,
    managedRoot: '/managed',
    login: async () => ({
      status: 'ready',
      claude: { apiKey: 'sk-repaired', baseUrl: 'https://proxy.example.com' },
    }),
    openSetup: async (options) => {
      setup = options;
      return 'http://127.0.0.1:4432/setup';
    },
  });
  assert.equal(code, 0);
  assert.equal(setup.launcher, path.resolve('/managed/bin/hc'));
  assert.equal(setup.env.ANTHROPIC_AUTH_TOKEN, 'sk-repaired');
  assert.match(output.read(), /Next: Setting up your first project/);
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
      argv: ['--global-vault', '2'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      output: output.stream,
      errorOutput: errors.stream,
      install: async () => ({ launcher: path.join(root, 'managed', 'bin', 'hc') }),
      ensureLauncherOnPath: () => ({ onPath: true }),
      readCredentials: () => null,
      openSetup: async () => null,
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
    claudeOnPath: () => true,
    login: async () => {
      logins += 1;
      return {
        status: 'ready',
        email: 'member@example.com',
        claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      };
    },
  });
  assert.equal(result.code, 0);
  assert.equal(logins, 1);
  assert.doesNotMatch(result.output, /Run `npx engelbart-cli auth`/);
});

// A scripted install must not sit waiting on a browser that will never open.
test('a scripted install never blocks on a browser', async () => {
  let logins = 0;
  const result = await installWith({
    interactive: false,
    login: async () => { logins += 1; return { status: 'ready' }; },
  });
  assert.equal(logins, 0);
  assert.match(result.output, /Run `npx engelbart-cli auth` to finish connecting your/);
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
    claudeOnPath: () => true,
    login: async () => { logins += 1; return { status: 'ready' }; },
  });
  fs.rmSync(root, { recursive: true, force: true });
  assert.equal(code, 0);
  assert.equal(logins, 0);
  assert.match(output.read(), /Run `npx engelbart-cli auth`/);
});

// The runtime is installed and working by then; only the credits are missing.
test('a failed account connection reports itself without failing the install', async () => {
  const result = await installWith({
    interactive: true,
    claudeOnPath: () => true,
    login: async () => { throw new Error('berkeley.mathetic.com is unreachable'); },
  });
  assert.equal(result.code, 0);
  assert.match(result.errors, /Could not connect an Engelbart account: berkeley\.mathetic\.com is unreachable/);
  assert.match(result.output, /Installed\./);
  assert.match(result.output, /Run `npx engelbart-cli auth`/);
});

test('reinstalling on a connected machine leaves the connection alone', async () => {
  let logins = 0;
  let rewires = 0;
  const result = await installWith({
    interactive: true,
    claudeOnPath: () => true,
    // What a connected machine actually has on disk: a token, and where to
    // spend the credit. Never the key.
    readCredentials: () => ({
      token: 'egb_t',
      email: 'member@example.com',
      claude: { baseUrl: 'https://proxy.example.com' },
    }),
    rewire: async () => {
      rewires += 1;
      return {
        status: 'ready',
        email: 'member@example.com',
        claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
        projectConfigured: true,
      };
    },
    login: async () => { logins += 1; return { status: 'ready' }; },
  });
  // The token it already holds is enough: no browser, no code to approve.
  assert.equal(rewires, 1);
  assert.equal(logins, 0);
  assert.match(result.output, /account {6}member@example\.com/);
  assert.doesNotMatch(result.output, /Run `npx engelbart-cli auth`/);
});

// Setup calls Claude, so it needs a live key -- and the key is not on this
// disk to be read back. A reinstall that could not get a fresh one has to say
// so rather than opening a page whose first screen cannot answer.
test('a reinstall that cannot reach the pool withholds setup instead of guessing', async () => {
  let opened = 0;
  const result = await installWith({
    interactive: true,
    claudeOnPath: () => true,
    readCredentials: () => ({
      token: 'egb_t',
      email: 'member@example.com',
      claude: { baseUrl: 'https://proxy.example.com' },
    }),
    rewire: async () => { throw new Error('berkeley.mathetic.com is unreachable'); },
    openSetup: async () => { opened += 1; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.equal(result.code, 0);
  assert.equal(opened, 0);
  assert.match(result.output, /account {6}member@example\.com/);
  assert.match(result.output, /Run `npx engelbart-cli auth`/);
});

test('a stored Supabase configuration failure still withholds setup on reinstall', async () => {
  let opened = 0;
  const result = await installWith({
    interactive: true,
    claudeOnPath: () => true,
    readCredentials: () => ({
      token: 'egb_t',
      email: 'member@example.com',
      projectConfigured: false,
      claude: { baseUrl: 'https://proxy.example.com' },
    }),
    rewire: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      projectConfigured: false,
    }),
    openSetup: async () => { opened += 1; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.equal(opened, 0);
  assert.match(result.output, /Supabase sync/);
});

// `eval "$(npx engelbart-cli env)"` runs whatever reaches stdout, so anything that is
// not a shell export has to leave by the other pipe.
test('env prints only the exports, and nothing else reaches stdout', async () => {
  const output = capture();
  const errors = capture();
  const code = await run({
    argv: ['env'],
    output: output.stream,
    errorOutput: errors.stream,
    managedRoot: '/nonexistent/managed',
    // Stored credentials carry no key, so `env` has to go and get one. That
    // also makes what it prints current rather than whatever was true at
    // sign-in.
    readCredentials: () => ({ token: 'egb_token', claude: { baseUrl: 'https://proxy.example.com' } }),
    fetchClaudeKey: async () => ({
      apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com', budgetUsd: 25, spendUsd: 4,
    }),
    install: async () => { throw new Error('installer must not run'); },
  });
  assert.equal(code, 0);
  assert.equal(
    output.read(),
    'export ANTHROPIC_BASE_URL="https://proxy.example.com"\n'
      + 'export ANTHROPIC_AUTH_TOKEN="sk-abc"\n',
  );
  assert.equal(errors.read(), '');
});

// The bug this is here for: the deployment said `status: exhausted`, the CLI
// dropped that field, and setup was handed the blocked key anyway -- with
// HC_USE_API_KEY set, which is what stops the provider falling back. Setup
// then failed on a 401 reported to the member as "is the CLI on PATH?".
test('setup is not handed a key the pool has stopped honouring', async () => {
  let passed = null;
  const result = await installWith({
    interactive: true,
    claudeOnPath: () => true,
    readCredentials: () => ({
      token: 'egb_t', email: 'member@example.com',
      claude: { baseUrl: 'https://proxy.example.com' },
    }),
    rewire: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: {
        apiKey: 'sk-dead', baseUrl: 'https://proxy.example.com',
        status: 'exhausted', budgetUsd: 25, spendUsd: 25.96,
      },
      projectConfigured: true,
    }),
    openSetup: async (options) => { passed = options.env; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.equal(result.code, 0);
  // Setup still opens -- on the member's own Claude login, which works.
  assert.ok(passed, 'setup should still open');
  assert.equal(passed.ANTHROPIC_AUTH_TOKEN, undefined);
  assert.equal(passed.HC_USE_API_KEY, undefined);
});

test('setup still gets the key while there is credit left', async () => {
  let passed = null;
  await installWith({
    interactive: true,
    claudeOnPath: () => true,
    readCredentials: () => ({
      token: 'egb_t', email: 'member@example.com',
      claude: { baseUrl: 'https://proxy.example.com' },
    }),
    rewire: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: {
        apiKey: 'sk-live', baseUrl: 'https://proxy.example.com',
        status: 'active', budgetUsd: 25, spendUsd: 4,
      },
      projectConfigured: true,
    }),
    openSetup: async (options) => { passed = options.env; return 'http://127.0.0.1:5321/setup'; },
  });
  assert.equal(passed.ANTHROPIC_AUTH_TOKEN, 'sk-live');
  assert.equal(passed.HC_USE_API_KEY, '1');
});

// Out of credit and no key yet are different problems with different answers,
// and only one of them is fixed by running auth again.
test('env says the credit is spent rather than blaming the sign-in', async () => {
  const output = capture();
  const errors = capture();
  const code = await run({
    argv: ['env'],
    output: output.stream,
    errorOutput: errors.stream,
    managedRoot: '/nonexistent/managed',
    readCredentials: () => ({ token: 'egb_token', claude: { baseUrl: 'https://proxy.example.com' } }),
    fetchClaudeKey: async () => ({
      apiKey: 'sk-dead', baseUrl: 'https://proxy.example.com',
      status: 'exhausted', budgetUsd: 25, spendUsd: 25.96,
    }),
    install: async () => { throw new Error('installer must not run'); },
  });
  assert.equal(code, 1);
  // Nothing a shell would eval: a dead key must not reach stdout.
  assert.equal(output.read(), '');
  assert.match(errors.read(), /credit is used up/);
  assert.doesNotMatch(errors.read(), /sk-dead/);
});

test('env on an unconnected machine fails without printing a broken script', async () => {
  const output = capture();
  const errors = capture();
  const code = await run({
    argv: ['env'],
    output: output.stream,
    errorOutput: errors.stream,
    managedRoot: '/nonexistent/managed',
    readCredentials: () => null,
  });
  assert.equal(code, 1);
  assert.equal(output.read(), '');
  assert.match(errors.read(), /npx engelbart-cli auth/);
});

test('env distinguishes a connected machine whose credit is not ready yet', async () => {
  const output = capture();
  const errors = capture();
  const code = await run({
    argv: ['env'],
    output: output.stream,
    errorOutput: errors.stream,
    managedRoot: '/nonexistent/managed',
    readCredentials: () => ({ token: 'egb_token', claude: null }),
    // The account is paired; the credit behind it is not ready. That is the
    // server's answer to give, now that this machine keeps no key of its own.
    fetchClaudeKey: async () => { throw new Error('that account has no Claude key yet'); },
  });
  assert.equal(code, 1);
  assert.equal(output.read(), '');
  assert.match(errors.read(), /no Claude key yet/);
});

test('a reinstall on an already-connected machine points at the stored key', async () => {
  const output = capture();
  const code = await run({
    argv: ['--dry-run'],
    output: output.stream,
    errorOutput: capture().stream,
    managedRoot: '/nonexistent/managed',
    readCredentials: () => ({
      email: 'm@example.com',
      claude: { apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com' },
    }),
  });
  assert.equal(code, 0);
  // --dry-run never touches the account, so the reused-key line must not fire.
  assert.doesNotMatch(output.read(), /engelbart env/);
});

// Claude Code is a hard requirement, and its installer asks no questions.
test('a missing Claude Code is installed, and the extended env reaches install', async () => {
  const { claudeInstallState, claudeOnPath, installClaudeCode } = require('../lib/cli');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-offer-'));
  try {
    fixturePackage(root);
    let installEnv = null;
    let claudeReady = false;
    const code = await run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      interactive: true,
      env: { PATH: '/usr/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: () => claudeReady
        ? { state: 'compatible', version: '2.1.175' }
        : { state: 'missing' },
      installClaudeCode: async ({ env }) => {
        claudeReady = true;
        return { ...env, PATH: `/home/x/.local/bin:${env.PATH}` };
      },
      install: async (options) => { installEnv = options.deps.env; return { launcher: null }; },
      readCredentials: () => null,
    });
    assert.equal(code, 0);
    assert.equal(installEnv.PATH, '/home/x/.local/bin:/usr/bin');
    assert.equal(typeof claudeOnPath, 'function');
    assert.equal(typeof claudeInstallState, 'function');
    assert.equal(typeof installClaudeCode, 'function');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a runnable but stale Claude Code is updated before the Engelbart install', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-update-'));
  try {
    fixturePackage(root);
    const output = capture();
    let update = null;
    let claudeReady = false;
    let installed = false;
    const code = await run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      interactive: true,
      env: { PATH: '/usr/bin' },
      output: output.stream,
      errorOutput: capture().stream,
      claudeInstallState: () => claudeReady
        ? { state: 'compatible', version: '2.1.175' }
        : { state: 'outdated', version: '2.1.174' },
      updateClaudeCode: async (options) => { update = options; claudeReady = true; return true; },
      install: async () => { installed = true; return { launcher: null }; },
      readCredentials: () => null,
    });
    assert.equal(code, 0);
    assert.equal(installed, true);
    assert.equal(update.version, '2.1.174');
    assert.equal(update.env.PATH, '/usr/bin');
    assert.match(output.read(), /engelbart-cli 0\.16\.0/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the Claude preflight distinguishes compatible, stale, and unknown binaries', () => {
  const { claudeInstallState } = require('../lib/cli');
  const state = (result) => claudeInstallState({ PATH: '/usr/bin' }, () => result);

  assert.deepEqual(state({ status: 0, stdout: '2.1.175 (Claude Code)\n', stderr: '' }), {
    state: 'compatible', version: '2.1.175',
  });
  assert.deepEqual(state({ status: 0, stdout: '2.1.174 (Claude Code)\n', stderr: '' }), {
    state: 'outdated', version: '2.1.174',
  });
  assert.deepEqual(state({ status: 0, stdout: 'other program\n', stderr: '' }), {
    state: 'unrecognized', output: 'other program',
  });
  assert.equal(state({ status: 127, stdout: '', stderr: 'broken launcher' }).state, 'broken');
  assert.equal(state({ status: null, error: { code: 'ETIMEDOUT', message: 'timed out' } }).state, 'broken');
  assert.deepEqual(state({ status: null, error: { code: 'ENOENT' } }), { state: 'missing' });
});

test('a working native Claude install outside the inherited PATH is reused without reinstalling', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-native-path-'));
  try {
    fixturePackage(root);
    const homedir = path.join(root, 'home');
    const localBin = path.join(homedir, '.local', 'bin');
    let installedClaude = false;
    let installEnv = null;
    const output = capture();
    const code = await run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      homedir,
      platform: process.platform === 'win32' ? 'win32' : 'darwin',
      arch: process.arch === 'arm64' ? 'arm64' : 'x64',
      interactive: true,
      env: { PATH: '/usr/bin' },
      output: output.stream,
      errorOutput: capture().stream,
      claudeInstallState: (env) => String(env.PATH).startsWith(localBin)
        ? { state: 'compatible', version: '2.1.175' }
        : { state: 'missing' },
      installClaudeCode: async () => { installedClaude = true; throw new Error('must not reinstall'); },
      install: async (options) => { installEnv = options.deps.env; return { launcher: null }; },
      readCredentials: () => null,
    });
    assert.equal(code, 0);
    assert.equal(installedClaude, false);
    assert.ok(String(installEnv.PATH).startsWith(localBin));
    assert.match(output.read(), process.platform === 'win32'
      ? /Open a new terminal/
      : /export PATH=.*\.local\/bin/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a broken native Claude outside inherited PATH is diagnosed instead of overwritten', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-native-broken-'));
  try {
    fixturePackage(root);
    const homedir = path.join(root, 'home');
    const localBin = path.join(homedir, '.local', 'bin');
    let reinstalled = false;
    await assert.rejects(run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      homedir,
      platform: process.platform === 'win32' ? 'win32' : 'darwin',
      arch: process.arch === 'arm64' ? 'arm64' : 'x64',
      interactive: true,
      env: { PATH: '/usr/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: (env) => String(env.PATH).startsWith(localBin)
        ? { state: 'broken', reason: 'permission denied' }
        : { state: 'missing' },
      installClaudeCode: async () => { reinstalled = true; return {}; },
      install: async () => { throw new Error('must stop before Engelbart install'); },
    }), /permission denied.*claude doctor/i);
    assert.equal(reinstalled, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('an outdated native Claude outside inherited PATH is updated instead of reinstalled', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-native-update-'));
  try {
    fixturePackage(root);
    const homedir = path.join(root, 'home');
    const localBin = path.join(homedir, '.local', 'bin');
    let updated = false;
    let reinstalled = false;
    const code = await run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      homedir,
      platform: process.platform === 'win32' ? 'win32' : 'darwin',
      arch: process.arch === 'arm64' ? 'arm64' : 'x64',
      interactive: true,
      env: { PATH: '/usr/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: (env) => {
        if (!String(env.PATH).startsWith(localBin)) return { state: 'missing' };
        return updated
          ? { state: 'compatible', version: '2.1.175' }
          : { state: 'outdated', version: '2.1.174' };
      },
      updateClaudeCode: async () => { updated = true; return true; },
      installClaudeCode: async () => { reinstalled = true; throw new Error('must not reinstall'); },
      install: async () => ({ launcher: null }),
      readCredentials: () => null,
    });
    assert.equal(code, 0);
    assert.equal(updated, true);
    assert.equal(reinstalled, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('an update that exits zero without changing the selected Claude falls back to the native installer', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-update-noop-'));
  try {
    fixturePackage(root);
    const homedir = path.join(root, 'home');
    const localBin = path.join(homedir, '.local', 'bin');
    let repaired = false;
    let installEnv = null;
    const code = await run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      homedir,
      platform: process.platform === 'win32' ? 'win32' : 'darwin',
      arch: process.arch === 'arm64' ? 'arm64' : 'x64',
      interactive: true,
      env: { PATH: '/old/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: (env) => repaired && String(env.PATH).startsWith(localBin)
        ? { state: 'compatible', version: '2.1.175' }
        : { state: 'outdated', version: '2.1.174' },
      updateClaudeCode: async () => true,
      installClaudeCode: async ({ env }) => {
        repaired = true;
        return { ...env, PATH: `${localBin}${path.delimiter}${env.PATH}` };
      },
      install: async (options) => { installEnv = options.deps.env; return { launcher: null }; },
      readCredentials: () => null,
    });
    assert.equal(code, 0);
    assert.equal(repaired, true);
    assert.ok(String(installEnv.PATH).startsWith(localBin));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a nonzero updater exit is accepted when the version probe proves the update completed', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-update-cleanup-failure-'));
  try {
    fixturePackage(root);
    let updated = false;
    let repaired = false;
    const code = await run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      homedir: path.join(root, 'home'),
      platform: process.platform === 'win32' ? 'win32' : 'darwin',
      arch: process.arch === 'arm64' ? 'arm64' : 'x64',
      interactive: true,
      env: { PATH: '/old/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: () => updated
        ? { state: 'compatible', version: '2.1.175' }
        : { state: 'outdated', version: '2.1.174' },
      updateClaudeCode: async () => { updated = true; return false; },
      installClaudeCode: async () => { repaired = true; throw new Error('must not repair'); },
      install: async () => ({ launcher: null }),
      readCredentials: () => null,
    });
    assert.equal(code, 0);
    assert.equal(repaired, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('an installer process that exits zero must still produce a runnable Claude', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-install-noop-'));
  try {
    fixturePackage(root);
    let installedEngelbart = false;
    await assert.rejects(run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      homedir: path.join(root, 'home'),
      platform: process.platform === 'win32' ? 'win32' : 'darwin',
      arch: process.arch === 'arm64' ? 'arm64' : 'x64',
      interactive: true,
      env: { PATH: '/usr/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: () => ({ state: 'missing' }),
      installClaudeCode: async ({ env }) => env,
      install: async () => { installedEngelbart = true; return { launcher: null }; },
      readCredentials: () => null,
    }), /installer finished.*still not runnable/i);
    assert.equal(installedEngelbart, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a broken command named claude is diagnosed without overwriting it', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-broken-claude-'));
  try {
    fixturePackage(root);
    let reinstalled = false;
    await assert.rejects(run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      homedir: path.join(root, 'home'),
      platform: process.platform === 'win32' ? 'win32' : 'darwin',
      arch: process.arch === 'arm64' ? 'arm64' : 'x64',
      interactive: true,
      env: { PATH: '/broken/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: () => ({ state: 'broken', reason: 'exited with code 127' }),
      installClaudeCode: async () => { reinstalled = true; return {}; },
      install: async () => { throw new Error('must stop before Engelbart install'); },
    }), /found.*could not run.*claude doctor/i);
    assert.equal(reinstalled, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a failed automatic Claude update stops before the Engelbart install', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-update-fail-'));
  try {
    fixturePackage(root);
    let installed = false;
    await assert.rejects(run({
      argv: ['--local-only', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      interactive: true,
      env: { PATH: '/usr/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: () => ({ state: 'outdated', version: '2.1.174' }),
      updateClaudeCode: async () => false,
      installClaudeCode: async () => { throw new Error('automatic repair failed'); },
      install: async () => { installed = true; return { launcher: null }; },
    }), /automatic repair failed/);
    assert.equal(installed, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a session without a TTY still installs missing Claude Code', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-offer-'));
  try {
    fixturePackage(root);
    let probed = false;
    let installed = false;
    let claudeReady = false;
    const code = await run({
      argv: ['--local-only', '--non-interactive', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'darwin',
      arch: 'arm64',
      interactive: false,
      env: { PATH: '/usr/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeInstallState: () => {
        probed = true;
        return claudeReady
          ? { state: 'compatible', version: '2.1.175' }
          : { state: 'missing' };
      },
      installClaudeCode: async ({ env }) => { installed = true; claudeReady = true; return env; },
      install: async () => ({ launcher: null }),
    });
    assert.equal(code, 0);
    assert.equal(probed, true);
    assert.equal(installed, true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('CI never bootstraps Claude Code', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-cli-offer-'));
  try {
    fixturePackage(root);
    let probed = false;
    const code = await run({
      argv: ['--local-only', '--non-interactive', '--no-open'],
      packageRoot: root,
      managedRoot: path.join(root, 'managed'),
      platform: 'linux',
      arch: 'x64',
      env: { CI: '1', PATH: '/usr/bin' },
      output: capture().stream,
      errorOutput: capture().stream,
      claudeOnPath: () => { probed = true; return false; },
      installClaudeCode: async () => { throw new Error('must not be installed'); },
      install: async () => ({ launcher: null }),
    });
    assert.equal(code, 0);
    assert.equal(probed, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the installer runs with no question, says whose it is, and extends PATH', async () => {
  const { installClaudeCode } = require('../lib/cli');
  const ran = [];
  let spawnOptions = null;
  const spawn = (command, args, options) => {
    ran.push([command, ...args]);
    spawnOptions = options;
    return { status: 0 };
  };
  const said = capture();
  const env = { PATH: '/usr/bin' };
  const result = await installClaudeCode({
    env,
    output: said.stream,
    errorOutput: capture().stream,
    platform: 'linux',
    deps: { spawn, homedir: '/home/x' },
  });
  assert.equal(ran.length, 1);
  assert.match(ran[0].join(' '), /curl -fsSL https:\/\/claude\.ai\/install\.sh \| bash/);
  assert.deepEqual(ran[0].slice(0, 3), ['bash', '-o', 'pipefail']);
  assert.equal(spawnOptions.timeout, 600000);
  assert.match(said.read(), /Anthropic's official installer/);
  assert.doesNotMatch(said.read(), /\[Y\/n\]/);
  // localBin is built with the host's path.join (backslashes on Windows), so
  // construct the expectation the same way rather than hardcoding a POSIX path.
  assert.equal(result.PATH, `${path.join('/home/x', '.local', 'bin')}${path.delimiter}/usr/bin`);
});

test('an outdated Claude Code uses its built-in updater', async () => {
  const { updateClaudeCode } = require('../lib/cli');
  const ran = [];
  const said = capture();
  const updated = await updateClaudeCode({
    env: { PATH: '/usr/bin' },
    output: said.stream,
    errorOutput: capture().stream,
    version: '2.1.174',
    deps: { spawn: (command, args) => { ran.push([command, ...args]); return { status: 0 }; } },
  });
  assert.equal(updated, true);
  assert.deepEqual(ran, [['claude', 'update']]);
  assert.match(said.read(), /2\.1\.174 is below the required 2\.1\.175/);
});

test('on Windows the installer is the PowerShell one-liner, not bash+curl', async () => {
  const { installClaudeCode } = require('../lib/cli');
  const ran = [];
  const spawn = (command, args) => { ran.push([command, ...args]); return { status: 0 }; };
  const said = capture();
  const result = await installClaudeCode({
    env: { PATH: 'C:\\Windows' },
    output: said.stream,
    errorOutput: capture().stream,
    platform: 'win32',
    deps: { spawn, homedir: 'C:\\Users\\x' },
  });
  assert.equal(ran[0][0], 'powershell');
  assert.match(ran[0].join(' '), /Invoke-RestMethod 'https:\/\/claude\.ai\/install\.ps1' \| Invoke-Expression/);
  assert.match(ran[0].join(' '), /ErrorActionPreference.*Stop/);
  assert.doesNotMatch(ran[0].join(' '), /bash|curl/);
  assert.match(said.read(), /install\.ps1/);
  assert.ok(result.PATH.startsWith(path.join('C:\\Users\\x', '.local', 'bin')));
});

test('an installer that fails stops immediately and says to install manually', async () => {
  const { installClaudeCode } = require('../lib/cli');
  const errors = capture();
  const env = { PATH: '/usr/bin' };
  await assert.rejects(installClaudeCode({
    env,
    output: capture().stream,
    errorOutput: errors.stream,
    deps: { spawn: () => ({ status: 1 }) },
  }), /could not be installed automatically/);
  assert.match(errors.read(), /did not finish/);
});

test('quiet Claude lifecycle commands capture child chatter but preserve failure detail', async () => {
  const { installClaudeCode, updateClaudeCode } = require('../lib/cli');
  const said = capture();
  let installOptions;
  await installClaudeCode({
    env: { PATH: '/usr/bin' },
    output: said.stream,
    errorOutput: capture().stream,
    quiet: true,
    deps: {
      homedir: '/home/x',
      spawn: (command, args, options) => {
        installOptions = options;
        return { status: 0, stdout: 'installer progress\n', stderr: '' };
      },
    },
  });
  assert.equal(said.read(), '');
  assert.deepEqual(installOptions.stdio, ['ignore', 'pipe', 'pipe']);

  let updateOptions;
  const updated = await updateClaudeCode({
    env: { PATH: '/usr/bin' },
    output: said.stream,
    errorOutput: capture().stream,
    version: '2.1.174',
    quiet: true,
    deps: { spawn: (command, args, options) => {
      updateOptions = options;
      return { status: 0, stdout: 'update progress\n', stderr: '' };
    } },
  });
  assert.equal(updated, true);
  assert.equal(said.read(), '');
  assert.deepEqual(updateOptions.stdio, ['ignore', 'pipe', 'pipe']);

  const errors = capture();
  await assert.rejects(installClaudeCode({
    env: { PATH: '/usr/bin' },
    output: said.stream,
    errorOutput: errors.stream,
    quiet: true,
    deps: { spawn: () => ({ status: 1, stderr: 'network unavailable\n' }) },
  }), /could not be installed automatically/);
  assert.match(errors.read(), /did not finish \(network unavailable\)/);
});

// The binary's users have no npm; every self-reference must name the command
// they actually have. The npm default stays npx.
test('the standalone binary speaks of itself as engelbart, npm as npx', () => {
  const { invocation, setInvocation } = require('../lib/invocation');
  assert.match(usage(), /Usage: npx engelbart-cli/);
  const before = invocation();
  try {
    setInvocation('engelbart');
    assert.match(usage(), /Usage: engelbart \[command\]/);
    assert.doesNotMatch(usage(), /npx/);
  } finally {
    setInvocation(before);
  }
});

// --- web-first onboarding: --code and the pending setup ----------------------

test('--code takes a value, refuses --local-only, and reaches auth too', () => {
  assert.equal(parseArgs(['--code', 'ABCD-2345-WXYZ']).code, 'ABCD-2345-WXYZ');
  assert.equal(parseArgs(['auth', '--code', 'ABCD-2345-WXYZ']).command, 'auth');
  assert.throws(() => parseArgs(['--code']), /requires a setup code/);
  assert.throws(() => parseArgs(['--code', 'X', '--local-only']),
    /--code connects an Engelbart account/);
  assert.match(usage(),
    /^ {2}--code XXXX-XXXX-XXXX connect with the setup code from \/engelbart\/setup$/m);
});

test('a setup code redeems, pulls the web project, and opens the workspace', async () => {
  const redeemed = [];
  let loggedIn = false;
  let localSetupOpened = false;
  const importedWith = [];
  const printed = await installOutput({ onPath: true, added: false }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--global-vault', '2'],
    redeemCode: async (code) => {
      redeemed.push(code);
      return {
        status: 'ready',
        email: 'member@example.com',
        claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
        stored: { token: 'egb_issued', email: 'member@example.com' },
      };
    },
    login: async () => { loggedIn = true; throw new Error('login must not run'); },
    fetchPendingSetup: async (base, token) => {
      assert.equal(token, 'egb_issued');
      return { name: 'nuclear-sim', goals: [] };
    },
    importSetup: (input) => {
      importedWith.push(input.payload);
      return { url: 'http://127.0.0.1:8123/', name: 'nuclear-sim' };
    },
    openSetup: async () => { localSetupOpened = true; return null; },
  });
  assert.deepEqual(redeemed, ['ABCD-2345-WXYZ']);
  assert.equal(loggedIn, false);
  assert.equal(localSetupOpened, false);
  assert.equal(importedWith[0].name, 'nuclear-sim');
  assert.match(printed, /Your project "nuclear-sim" is open in the workspace: http:\/\/127\.0\.0\.1:8123\//);
});

test('a dead setup code falls back to the device flow instead of dying', async () => {
  let loggedIn = false;
  const printed = await installOutput({ onPath: true, added: false }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--global-vault', '2'],
    redeemCode: async () => { throw new Error('That setup code was already used'); },
    login: async () => {
      loggedIn = true;
      return {
        status: 'ready',
        email: 'member@example.com',
        claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      };
    },
    fetchPendingSetup: async () => null,
  });
  assert.equal(loggedIn, true);
  assert.match(printed, /Run `hc setup-ui` to set up your first project\./);
});

test('an account with nothing pending still gets the local setup page', async () => {
  let localSetupOpened = false;
  const printed = await installOutput({ onPath: true, added: false }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--global-vault', '2'],
    redeemCode: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      stored: { token: 'egb_issued', email: 'member@example.com' },
    }),
    fetchPendingSetup: async () => null,
    openSetup: async () => { localSetupOpened = true; return 'http://127.0.0.1:9000/setup'; },
  });
  assert.equal(localSetupOpened, true);
  assert.match(printed, /Setting up your first project: http:\/\/127\.0\.0\.1:9000\/setup/);
});

test('--code with --no-open and nothing pending sends the reader back to the browser', async () => {
  // The web flow's own install line. The site saves the project only at
  // the end, so nothing is pending yet; the reader is mid-setup in a tab,
  // and a setup page or an `hc setup-ui` here would start a second setup.
  let localSetupOpened = false;
  const printed = await installOutput({ onPath: true, added: false }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--no-open', '--global-vault', '2'],
    redeemCode: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      stored: { token: 'egb_issued', email: 'member@example.com' },
    }),
    fetchPendingSetup: async () => null,
    openSetup: async () => { localSetupOpened = true; return 'http://127.0.0.1:9000/setup'; },
  });
  assert.equal(localSetupOpened, false);
  assert.equal(printed, BROWSER_RESUME_BANNER);
});

test('the browser handoff is the only successful output even when PATH changed', async () => {
  const printed = await installOutput({ onPath: false, added: true }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--no-open', '--global-vault', '2'],
    redeemCode: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      stored: { token: 'egb_issued', email: 'member@example.com' },
    }),
    fetchPendingSetup: async () => null,
  });
  assert.equal(printed, BROWSER_RESUME_BANNER);
});

test('the browser handoff also hides a successful Claude bootstrap', async () => {
  let claudeReady = false;
  let quiet = null;
  const printed = await installOutput({ onPath: true, added: false }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--no-open', '--global-vault', '2'],
    env: { PATH: '/usr/bin' },
    claudeInstallState: () => claudeReady
      ? { state: 'compatible', version: '2.1.175' }
      : { state: 'missing' },
    installClaudeCode: async (options) => {
      quiet = options.quiet;
      claudeReady = true;
      return options.env;
    },
    redeemCode: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      stored: { token: 'egb_issued', email: 'member@example.com' },
    }),
    fetchPendingSetup: async () => null,
  });
  assert.equal(quiet, true);
  assert.equal(printed, BROWSER_RESUME_BANNER);
});

test('a pending project still wins over the browser line under --no-open', async () => {
  // Nothing pending is what the new web flow produces; an older flow that
  // saved the project before the install keeps opening it at once.
  const printed = await installOutput({ onPath: true, added: false }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--no-open', '--global-vault', '2'],
    redeemCode: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      stored: { token: 'egb_issued', email: 'member@example.com' },
    }),
    fetchPendingSetup: async () => ({ name: 'nuclear-sim' }),
    importSetup: () => ({ url: 'http://127.0.0.1:8123/', name: 'nuclear-sim' }),
  });
  assert.match(printed, /Your project "nuclear-sim" is open in the workspace/);
  assert.doesNotMatch(printed, /Go back to your browser tab/);
});

test('importSetup pipes the payload on stdin and reads the last URL line', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-import-'));
  try {
    const spawned = [];
    const result = importSetup({
      launcher: '/managed/bin/hc',
      env: { HC_USE_API_KEY: '1' },
      payload: { name: 'nuclear-sim' },
      managedRoot: root,
      errorOutput: capture().stream,
      spawn: (command, args, options) => {
        spawned.push({ command, args, input: options.input });
        return { status: 0, stdout: 'note: hello\nhttp://127.0.0.1:8123/\n' };
      },
    });
    assert.deepEqual(spawned[0].args, ['setup-import', '--stdin']);
    assert.equal(JSON.parse(spawned[0].input).name, 'nuclear-sim');
    assert.deepEqual(result, { url: 'http://127.0.0.1:8123/', name: 'nuclear-sim' });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a payload that cannot be materialized is saved, not lost', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-import-'));
  try {
    const errors = capture();
    const result = importSetup({
      launcher: '/managed/bin/hc',
      env: {},
      payload: { name: 'nuclear-sim' },
      managedRoot: root,
      errorOutput: errors.stream,
      spawn: () => ({ status: 1, stderr: 'a project is already called that' }),
    });
    assert.deepEqual(result, { failed: true });
    const saved = JSON.parse(fs.readFileSync(path.join(root, 'pending-setup.json'), 'utf8'));
    assert.equal(saved.name, 'nuclear-sim');
    assert.match(errors.read(), /a project is already called that/);
    assert.match(errors.read(), /hc setup-import --file /);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the web project still opens when the shell needs a PATH line', async () => {
  // A first install is exactly the machine the web flow exists for, and a
  // first install always still needs the PATH step: the import spawns the
  // launcher by absolute path, so it must not wait for that step.
  const printed = await installOutput({ onPath: false, added: true }, {
    argv: ['--code', 'ABCD-2345-WXYZ', '--global-vault', '2'],
    redeemCode: async () => ({
      status: 'ready',
      email: 'member@example.com',
      claude: { apiKey: 'sk-issued', baseUrl: 'https://proxy.example.com' },
      stored: { token: 'egb_issued', email: 'member@example.com' },
    }),
    fetchPendingSetup: async () => ({ name: 'nuclear-sim' }),
    importSetup: () => ({ url: 'http://127.0.0.1:8123/', name: 'nuclear-sim' }),
  });
  assert.match(printed, /export PATH=/);
  assert.match(printed, /Your project "nuclear-sim" is open in the workspace/);
});
