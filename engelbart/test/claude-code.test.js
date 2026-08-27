'use strict';

const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const test = require('node:test');

const claudeCode = require('../lib/claude-code');

function temporaryRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'engelbart-wiring-'));
}

// The settings file lives beside the managed root, never inside it: the
// installer refuses to adopt a root holding files it did not write.
function settingsIn(root) {
  return path.join(`${root}-config`, 'settings.json');
}

function write(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
  return file;
}

function read(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

const BASE_URL = 'https://proxy.example.com';

function connect(root, settingsFile, extra = {}) {
  return claudeCode.connect({ managedRoot: root, settingsFile, baseUrl: BASE_URL, ...extra });
}

// The whole reason this module is delicate: the file it edits is the member's,
// and it holds work that has nothing to do with us.
test('wiring preserves every setting it did not come to change', () => {
  const root = temporaryRoot();
  const file = write(settingsIn(root), {
    hooks: { PostToolUse: [{ matcher: 'Edit' }] },
    enabledPlugins: { cloudflare: true },
    theme: 'dark',
    env: { EDITOR: 'vim' },
  });

  const result = connect(root, file);

  assert.equal(result.changed, true);
  const settings = read(file);
  assert.deepEqual(settings.hooks, { PostToolUse: [{ matcher: 'Edit' }] });
  assert.deepEqual(settings.enabledPlugins, { cloudflare: true });
  assert.equal(settings.theme, 'dark');
  assert.equal(settings.env.EDITOR, 'vim');
  assert.equal(settings.env.ANTHROPIC_BASE_URL, BASE_URL);
  assert.equal(settings.apiKeyHelper, claudeCode.helperPath(root));
});

test('a settings file that does not exist yet is created, owner-only', () => {
  const root = temporaryRoot();
  const file = settingsIn(root);
  assert.equal(connect(root, file).changed, true);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(read(file).env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS, String(claudeCode.HELPER_TTL_MS));
});

// Signing in twice is the ordinary case — a member re-pairing a machine — and
// it must not accumulate anything.
test('wiring twice leaves the same file as wiring once', () => {
  const root = temporaryRoot();
  const file = write(settingsIn(root), { theme: 'dark' });
  connect(root, file);
  const once = fs.readFileSync(file, 'utf8');
  connect(root, file);
  assert.equal(fs.readFileSync(file, 'utf8'), once);
});

// Silently redirecting Claude Code's credential at our key would take the
// member's other tool offline without ever saying so.
test('an apiKeyHelper belonging to something else is left alone', () => {
  const root = temporaryRoot();
  const file = write(settingsIn(root), { apiKeyHelper: '/opt/other-tool/key', theme: 'dark' });

  const result = connect(root, file);

  assert.equal(result.changed, false);
  assert.equal(result.reason, 'foreign-helper');
  assert.equal(result.helper, '/opt/other-tool/key');
  assert.deepEqual(read(file), { apiKeyHelper: '/opt/other-tool/key', theme: 'dark' });
});

test('a settings file that is not readable JSON is never overwritten', () => {
  const root = temporaryRoot();
  const file = settingsIn(root);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, '{ this is not json');

  const result = connect(root, file);

  assert.equal(result.changed, false);
  assert.equal(result.reason, 'unreadable');
  assert.equal(fs.readFileSync(file, 'utf8'), '{ this is not json');
});

test('an account with no proxy to point at changes nothing', () => {
  const root = temporaryRoot();
  const file = write(settingsIn(root), { theme: 'dark' });
  const result = claudeCode.connect({ managedRoot: root, settingsFile: file, baseUrl: '' });
  assert.equal(result.reason, 'no-base-url');
  assert.deepEqual(read(file), { theme: 'dark' });
});

// This is the guard that exists because its absence rewrote a real machine's
// configuration during a test run.
test('the live settings file is out of reach unless the caller says otherwise', () => {
  const root = temporaryRoot();
  const live = claudeCode.settingsPath(os.homedir(), {});
  const before = fs.existsSync(live) ? fs.readFileSync(live, 'utf8') : null;

  const result = claudeCode.connect({ managedRoot: root, baseUrl: BASE_URL, homedir: os.homedir() });

  assert.equal(result.changed, false);
  assert.equal(result.reason, 'unsafe-target');
  assert.equal(result.file, live);
  assert.equal(fs.existsSync(live) ? fs.readFileSync(live, 'utf8') : null, before);

  const removal = claudeCode.disconnect({ managedRoot: root, baseUrl: BASE_URL, homedir: os.homedir() });
  assert.equal(removal.reason, 'unsafe-target');
  assert.equal(fs.existsSync(live) ? fs.readFileSync(live, 'utf8') : null, before);
});

test('a configuration directory of its own is not the live one', () => {
  const root = temporaryRoot();
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), 'engelbart-config-'));
  const target = claudeCode.resolveTarget({ env: { CLAUDE_CONFIG_DIR: configDir } });

  assert.equal(target.file, path.join(configDir, 'settings.json'));
  assert.equal(target.real, false);
  assert.equal(target.permitted, true);

  const result = claudeCode.connect({ managedRoot: root, baseUrl: BASE_URL, env: { CLAUDE_CONFIG_DIR: configDir } });
  assert.equal(result.changed, true);
  assert.equal(read(path.join(configDir, 'settings.json')).env.ANTHROPIC_BASE_URL, BASE_URL);
});

test('disconnecting removes what signing in added and nothing else', () => {
  const root = temporaryRoot();
  const file = write(settingsIn(root), { theme: 'dark', env: { EDITOR: 'vim' } });
  // The order `engelbart auth` uses: the helper is written, then pointed at.
  claudeCode.writeHelper(root, path.join(root, 'auth.json'));
  connect(root, file);
  assert.ok(fs.existsSync(claudeCode.helperPath(root)));

  const result = claudeCode.disconnect({ managedRoot: root, settingsFile: file, baseUrl: BASE_URL });

  assert.equal(result.changed, true);
  assert.deepEqual(read(file), { theme: 'dark', env: { EDITOR: 'vim' } });
  assert.equal(fs.existsSync(claudeCode.helperPath(root)), false);
});

// A member who repointed these at something of their own keeps it: we only
// take back the exact values we wrote.
test('a base URL the member changed by hand survives disconnecting', () => {
  const root = temporaryRoot();
  const file = write(settingsIn(root), {});
  connect(root, file);
  const settings = read(file);
  settings.env.ANTHROPIC_BASE_URL = 'https://something-of-mine.example.com';
  write(file, settings);

  claudeCode.disconnect({ managedRoot: root, settingsFile: file, baseUrl: BASE_URL });

  assert.equal(read(file).env.ANTHROPIC_BASE_URL, 'https://something-of-mine.example.com');
  assert.equal(read(file).apiKeyHelper, undefined);
});

test('disconnecting a machine that was never wired reports so and writes nothing', () => {
  const root = temporaryRoot();
  const file = write(settingsIn(root), { theme: 'dark' });
  const before = fs.readFileSync(file, 'utf8');

  const result = claudeCode.disconnect({ managedRoot: root, settingsFile: file, baseUrl: BASE_URL });

  assert.equal(result.changed, false);
  assert.equal(result.reason, 'not-ours');
  assert.equal(fs.readFileSync(file, 'utf8'), before);
});

test('disconnecting with no settings file at all is not an error', () => {
  const root = temporaryRoot();
  const result = claudeCode.disconnect({ managedRoot: root, settingsFile: settingsIn(root), baseUrl: BASE_URL });
  assert.equal(result.changed, false);
  assert.equal(result.reason, 'absent');
});

// `npx engelbart-cli` leaves no installed copy of the package behind, so the
// helper cannot shell back into the CLI — it has to stand on its own.
test('the credential helper is executable, self-contained, and owner-only', () => {
  const root = temporaryRoot();
  const credentials = path.join(root, 'auth.json');
  const helper = claudeCode.writeHelper(root, credentials);

  assert.equal(fs.statSync(helper).mode & 0o777, 0o700);
  const source = fs.readFileSync(helper, 'utf8');
  assert.match(source, /^#!\/usr\/bin\/env node/);
  assert.ok(source.includes(JSON.stringify(credentials)));
  assert.equal(/require\('\.\.?\//.test(source), false);
});

// ---------------------------------------------------------------------------
// What the helper does when the pool runs dry.
//
// These run the generated script for real, against a real socket. The bug they
// exist for could not be caught by reading the source: the old helper did ask
// the server first -- it just treated every answer it did not like, including
// "your credit is gone", as if the network were down, and printed the dead key
// anyway.
// ---------------------------------------------------------------------------

const http = require('http');
const { execFile } = require('child_process');

function stubServer(reply) {
  const server = http.createServer((req, res) => {
    const answer = reply(req);
    res.writeHead(answer.status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(answer.body === undefined ? {} : answer.body));
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({
      base: `http://127.0.0.1:${server.address().port}`,
      close: () => new Promise((done) => server.close(done)),
    }));
  });
}

// A wired machine: credentials on disk, helper written, settings pointing at it.
function wiredMachine(apiBase) {
  const root = temporaryRoot();
  const settingsFile = settingsIn(root);
  write(settingsFile, { theme: 'dark' });
  // The helper goes in first: the installer will not adopt a managed root that
  // already holds files it did not write.
  const credentials = path.join(root, 'auth.json');
  const helper = claudeCode.writeHelper(root, credentials, settingsFile);
  fs.writeFileSync(credentials, JSON.stringify({
    schema: 1,
    apiBase,
    token: 'egb_token',
    email: 'm@example.com',
    claude: { apiKey: 'sk-stored', baseUrl: BASE_URL, budgetUsd: 25, spendUsd: 25 },
  }));
  connect(root, settingsFile);
  return { root, settingsFile, helper };
}

function runHelper(helper) {
  return new Promise((resolve) => {
    execFile(helper, [], { timeout: 15000 }, (error, stdout, stderr) => {
      resolve({ code: error ? error.code || 1 : 0, stdout, stderr });
    });
  });
}

test('a server that says the credit is spent does not get the stored key printed anyway', async () => {
  const stub = await stubServer(() => ({
    status: 402,
    body: { error: 'your Engelbart Claude credit is used up' },
  }));
  try {
    const machine = wiredMachine(stub.base);
    const result = await runHelper(machine.helper);

    // The whole failure this change exists for: printing `sk-stored` here is
    // what buys a session that dies on its first request.
    assert.equal(result.stdout, '');
    assert.equal(result.code, 1);
    assert.match(result.stderr, /used up/);

    // And it takes itself out, so Claude Code falls back to the member's own
    // login instead of being left with no usable credential at all.
    const settings = read(machine.settingsFile);
    assert.equal(settings.apiKeyHelper, undefined);
    assert.equal(settings.env, undefined);
    assert.equal(settings.theme, 'dark');
  } finally {
    await stub.close();
  }
});

test('a 200 that reports an exhausted status is a refusal too', async () => {
  const stub = await stubServer(() => ({
    status: 200,
    body: { apiKey: 'sk-fresh', baseUrl: BASE_URL, status: 'exhausted', budgetUsd: 25, spendUsd: 25 },
  }));
  try {
    const machine = wiredMachine(stub.base);
    const result = await runHelper(machine.helper);

    // A key that exists but cannot be spent is worse than no key: it looks
    // like success right up until the first request.
    assert.equal(result.stdout, '');
    assert.equal(result.code, 1);
    assert.equal(read(machine.settingsFile).apiKeyHelper, undefined);
  } finally {
    await stub.close();
  }
});

// The other half of the trade. Failing closed on a refusal is only safe if a
// bad minute on conference wifi is never mistaken for one.
test('an unreachable server still gets the stored key, and changes nothing', async () => {
  const machine = wiredMachine('http://127.0.0.1:1');
  const result = await runHelper(machine.helper);

  assert.equal(result.stdout, 'sk-stored');
  assert.equal(result.code, 0);
  assert.equal(read(machine.settingsFile).apiKeyHelper, machine.helper);
});

test('a deployment having a bad minute is not a refusal', async () => {
  const stub = await stubServer(() => ({ status: 503, body: { error: 'upstream unavailable' } }));
  try {
    const machine = wiredMachine(stub.base);
    const result = await runHelper(machine.helper);

    assert.equal(result.stdout, 'sk-stored');
    assert.equal(result.code, 0);
    assert.equal(read(machine.settingsFile).apiKeyHelper, machine.helper);
  } finally {
    await stub.close();
  }
});

test('a healthy server still rotates the key in front of the stored one', async () => {
  const stub = await stubServer(() => ({
    status: 200,
    body: { apiKey: 'sk-fresh', baseUrl: BASE_URL, status: 'active', budgetUsd: 25, spendUsd: 1 },
  }));
  try {
    const machine = wiredMachine(stub.base);
    const result = await runHelper(machine.helper);

    assert.equal(result.stdout, 'sk-fresh');
    assert.equal(result.code, 0);
    assert.equal(read(machine.settingsFile).apiKeyHelper, machine.helper);
  } finally {
    await stub.close();
  }
});

// A member who pointed Claude Code at their own helper after signing in keeps
// it: unwiring is only ever allowed to remove the exact value we wrote.
test('refusing never removes a helper we did not write', async () => {
  const stub = await stubServer(() => ({ status: 403, body: { error: 'this key is paused' } }));
  try {
    const machine = wiredMachine(stub.base);
    write(machine.settingsFile, { apiKeyHelper: '/opt/other-tool/key', theme: 'dark' });
    const result = await runHelper(machine.helper);

    assert.equal(result.stdout, '');
    assert.equal(result.code, 1);
    assert.equal(read(machine.settingsFile).apiKeyHelper, '/opt/other-tool/key');
  } finally {
    await stub.close();
  }
});

// Claude Code re-runs the helper on a 401 and otherwise waits out this cache.
// An exhausted LiteLLM budget answers 400 or 429, never 401, so this window is
// the only bound on how long a spent key stays in front of a member.
test('the cache window is short enough to bound a spent key', () => {
  assert.ok(claudeCode.HELPER_TTL_MS <= 900000);
});
