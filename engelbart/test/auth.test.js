'use strict';

const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const test = require('node:test');

const auth = require('../lib/auth');

function temporaryRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'engelbart-auth-'));
}

// Wiring Claude Code means writing a settings file, and the default one is the
// real one. Every test that signs in or out names its own instead, so the
// suite can never rewrite the configuration of whoever is running it. It sits
// beside the managed root rather than inside it, because the installer refuses
// to adopt a root that already has files it did not put there.
function settingsIn(root) {
  return path.join(`${root}-config`, 'settings.json');
}

function collector() {
  const chunks = [];
  return { write(value) { chunks.push(value); }, text() { return chunks.join(''); } };
}

// Each entry answers one POST in order, so a test states the exact exchange
// it expects the installer to have.
function scriptedFetch(responses) {
  const calls = [];
  return {
    calls,
    async fetchImpl(url, options) {
      const body = JSON.parse(options.body);
      calls.push({ url, body, headers: options.headers });
      const next = responses.shift();
      if (!next) throw new Error(`unexpected request: ${body.action}`);
      return {
        ok: next.ok !== false,
        status: next.status || 200,
        async json() { return next.body; },
      };
    },
  };
}

test('the API base defaults to production and refuses plaintext elsewhere', () => {
  assert.equal(auth.apiBase({}), 'https://berkeley.mathetic.com');
  assert.equal(auth.apiBase({ ENGELBART_API_BASE: 'https://preview.example.com/' }), 'https://preview.example.com');
  assert.equal(auth.apiBase({ ENGELBART_API_BASE: 'http://localhost:3000' }), 'http://localhost:3000');
  assert.throws(() => auth.apiBase({ ENGELBART_API_BASE: 'http://example.com' }), /HTTPS/);
  assert.throws(() => auth.apiBase({ ENGELBART_API_BASE: 'not a url' }), /not a URL/);
});

test('a stored token is owner-only on disk', () => {
  const root = temporaryRoot();
  auth.writeCredentials(root, {
    schema: 1, apiBase: 'https://berkeley.mathetic.com', token: 'egb_secret', email: 'm@example.com',
  });
  const file = auth.credentialsPath(root);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(auth.readCredentials(root, {}).email, 'm@example.com');
});

// A token minted against a preview deployment is not a production token, and
// reading it as one would send it to the wrong host.
test('a token issued for another deployment does not count as signed in', () => {
  const root = temporaryRoot();
  auth.writeCredentials(root, {
    schema: 1, apiBase: 'https://preview.example.com', token: 'egb_secret', email: 'm@example.com',
  });
  assert.equal(auth.readCredentials(root, {}), null);
  assert.equal(auth.readCredentials(root, { ENGELBART_API_BASE: 'https://preview.example.com' }).token, 'egb_secret');
});

test('an unreadable or foreign credentials file reads as signed out', () => {
  const root = temporaryRoot();
  assert.equal(auth.readCredentials(root, {}), null);
  fs.writeFileSync(auth.credentialsPath(root), 'not json');
  assert.equal(auth.readCredentials(root, {}), null);
  fs.writeFileSync(auth.credentialsPath(root), JSON.stringify({ schema: 99, token: 'x' }));
  assert.equal(auth.readCredentials(root, {}), null);
});

test('the machine label is the short hostname, bounded', () => {
  assert.equal(auth.machineLabel('laptop.local'), 'laptop');
  assert.equal(auth.machineLabel(`${'x'.repeat(200)}.local`).length, 60);
  assert.equal(auth.machineLabel(''), '');
});

test('login prints the code before opening the browser and stores the token', async () => {
  const root = temporaryRoot();
  const output = collector();
  const opened = [];
  const script = scriptedFetch([
    { body: {
      deviceCode: 'egbd_device-secret',
      userCode: 'WXYZ-1234',
      verificationUrlComplete: 'https://berkeley.mathetic.com/engelbart?code=WXYZ-1234',
      intervalSeconds: 5,
      expiresInSeconds: 600,
    } },
    { body: { status: 'pending' } },
    { body: { status: 'ready', token: 'egb_issued-token', email: 'member@example.com' } },
  ]);

  const result = await auth.login({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    output,
    hostname: 'laptop.local',
    fetchImpl: script.fetchImpl,
    wait: async () => {},
    now: () => 1_700_000_000_000,
    openUrl: (url) => { opened.push(url); return true; },
  });

  assert.equal(result.status, 'ready');
  assert.equal(opened[0], 'https://berkeley.mathetic.com/engelbart?code=WXYZ-1234');
  assert.equal(script.calls[0].body.action, 'start');
  assert.equal(script.calls[0].body.label, 'laptop');
  assert.equal(script.calls[1].body.deviceCode, 'egbd_device-secret');

  const stored = auth.readCredentials(root, {});
  assert.equal(stored.token, 'egb_issued-token');
  assert.equal(stored.email, 'member@example.com');
  assert.equal(stored.apiBase, 'https://berkeley.mathetic.com');

  const printed = output.text();
  assert.match(printed, /WXYZ-1234/);
  assert.match(printed, /Signed in as member@example\.com/);
  // The half that authorizes the exchange is the CLI's alone.
  assert.equal(printed.includes('egbd_device-secret'), false);
  assert.equal(printed.includes('egb_issued-token'), false);
});

test('login works on a machine with no browser to open', async () => {
  const root = temporaryRoot();
  const output = collector();
  const script = scriptedFetch([
    { body: { deviceCode: 'egbd_x', userCode: 'WXYZ-1234', intervalSeconds: 1, expiresInSeconds: 60 } },
    { body: { status: 'ready', token: 'egb_t', email: 'm@example.com' } },
  ]);
  const result = await auth.login({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    output,
    fetchImpl: script.fetchImpl,
    wait: async () => {},
    now: () => 1_700_000_000_000,
    openUrl: () => false,
  });
  assert.equal(result.status, 'ready');
  assert.match(output.text(), /Open that page and approve the code/);
  assert.match(output.text(), /engelbart\?code=WXYZ-1234/);
});

test('a rejected or expired code ends the wait instead of polling on', async () => {
  for (const status of ['denied', 'expired']) {
    const root = temporaryRoot();
    const output = collector();
    const script = scriptedFetch([
      { body: { deviceCode: 'egbd_x', userCode: 'WXYZ-1234', intervalSeconds: 1, expiresInSeconds: 60 } },
      { body: { status } },
    ]);
    const result = await auth.login({
      managedRoot: root,
      settingsFile: settingsIn(root),
      env: {},
      output,
      fetchImpl: script.fetchImpl,
      wait: async () => {},
      now: () => 1_700_000_000_000,
      openUrl: () => true,
    });
    assert.equal(result.status, status);
    assert.equal(auth.readCredentials(root, {}), null);
    assert.equal(script.calls.length, 2);
  }
});

test('a slow_down answer backs the polling off instead of ignoring it', async () => {
  const root = temporaryRoot();
  const waits = [];
  const script = scriptedFetch([
    { body: { deviceCode: 'egbd_x', userCode: 'WXYZ-1234', intervalSeconds: 5, expiresInSeconds: 600 } },
    { body: { status: 'slow_down', intervalSeconds: 10 } },
    { body: { status: 'ready', token: 'egb_t', email: 'm@example.com' } },
  ]);
  await auth.login({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    output: collector(),
    fetchImpl: script.fetchImpl,
    wait: async (seconds) => { waits.push(seconds); },
    now: () => 1_700_000_000_000,
    openUrl: () => true,
  });
  assert.deepEqual(waits, [5, 10]);
});

test('a pairing that is never approved gives up at its own deadline', async () => {
  const root = temporaryRoot();
  const output = collector();
  let clock = 1_700_000_000_000;
  const script = scriptedFetch([
    { body: { deviceCode: 'egbd_x', userCode: 'WXYZ-1234', intervalSeconds: 5, expiresInSeconds: 10 } },
    { body: { status: 'pending' } },
    { body: { status: 'pending' } },
  ]);
  const result = await auth.login({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    output,
    fetchImpl: script.fetchImpl,
    wait: async (seconds) => { clock += seconds * 1000; },
    now: () => clock,
    openUrl: () => true,
  });
  assert.equal(result.status, 'expired');
  assert.match(output.text(), /expired before it was approved/);
});

test('logout revokes the token and removes it locally', async () => {
  const root = temporaryRoot();
  auth.writeCredentials(root, {
    schema: 1, apiBase: 'https://berkeley.mathetic.com', token: 'egb_t', email: 'm@example.com',
  });
  const script = scriptedFetch([{ body: { revoked: true } }]);
  const result = await auth.logout({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    fetchImpl: script.fetchImpl,
  });
  assert.deepEqual(script.calls[0].body, { action: 'revoke', token: 'egb_t' });
  assert.equal(result.revoked, true);
  assert.equal(auth.readCredentials(root, {}), null);
});

// Leaving a usable token on disk because the network was down is the one
// outcome logout must never have.
test('logout clears the local token even when it cannot be revoked', async () => {
  const root = temporaryRoot();
  auth.writeCredentials(root, {
    schema: 1, apiBase: 'https://berkeley.mathetic.com', token: 'egb_t', email: 'm@example.com',
  });
  const result = await auth.logout({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    async fetchImpl() { throw new Error('offline'); },
  });
  assert.equal(result.signedOut, true);
  assert.equal(result.revoked, false);
  assert.equal(auth.readCredentials(root, {}), null);
  assert.equal(fs.existsSync(auth.credentialsPath(root)), false);
});

test('logout on an unconnected machine is not an error', async () => {
  const root = temporaryRoot();
  const result = await auth.logout({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    fetchImpl() { throw new Error('must not be called'); },
  });
  assert.deepEqual(result, { revoked: false, signedOut: false });
});

test('whoami asks the deployment rather than trusting the local file', async () => {
  const root = temporaryRoot();
  auth.writeCredentials(root, {
    schema: 1, apiBase: 'https://berkeley.mathetic.com', token: 'egb_t', email: 'stale@example.com',
  });
  const script = scriptedFetch([{ body: { email: 'member@example.com' } }]);
  const result = await auth.whoami({ managedRoot: root, env: {}, fetchImpl: script.fetchImpl });
  assert.equal(script.calls[0].headers.Authorization, 'Bearer egb_t');
  assert.deepEqual(result, { signedIn: true, email: 'member@example.com' });

  const revoked = await auth.whoami({
    managedRoot: root,
    env: {},
    fetchImpl: scriptedFetch([{ ok: false, status: 401, body: { error: 'This Engelbart CLI is not signed in' } }]).fetchImpl,
  });
  assert.equal(revoked.signedIn, false);
  assert.match(revoked.reason, /not signed in/);
});

test('whoami on an unconnected machine reports it without a request', async () => {
  const result = await auth.whoami({
    managedRoot: temporaryRoot(),
    env: {},
    fetchImpl() { throw new Error('must not be called'); },
  });
  assert.deepEqual(result, { signedIn: false });
});

// The credential calls are not device calls: one is a POST with no body and
// the other a GET, so they need a stub that does not assume JSON went out.
function credentialFetch(responses) {
  const calls = [];
  return {
    calls,
    async fetchImpl(url, options = {}) {
      calls.push({ url, method: options.method || 'GET', headers: options.headers });
      const next = responses.shift();
      if (!next) throw new Error(`unexpected request: ${options.method || 'GET'} ${url}`);
      return {
        ok: next.ok !== false,
        status: next.status || 200,
        async json() { return next.body; },
      };
    },
  };
}

test('the Claude key is provisioned then read back', async () => {
  const key = { apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com', budgetUsd: 25, spendUsd: 4 };
  const scripted = credentialFetch([{ body: { ready: true } }, { body: key }]);
  const result = await auth.fetchClaudeKey('https://berkeley.mathetic.com', 'egb_token', {
    fetchImpl: scripted.fetchImpl,
  });

  assert.deepEqual(result, { apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com', budgetUsd: 25, spendUsd: 4 });
  assert.equal(scripted.calls.length, 2);
  assert.equal(scripted.calls[0].method, 'POST');
  assert.equal(scripted.calls[1].method, 'GET');
  for (const call of scripted.calls) {
    assert.equal(call.url, 'https://berkeley.mathetic.com/api/engelbart-credentials');
    assert.equal(call.headers.Authorization, 'Bearer egb_token');
  }
});

test('a credential endpoint that refuses says why', async () => {
  const scripted = credentialFetch([{ body: {} }, { ok: false, status: 409, body: { error: 'Credits are not ready' } }]);
  await assert.rejects(
    () => auth.fetchClaudeKey('https://berkeley.mathetic.com', 'egb_token', { fetchImpl: scripted.fetchImpl }),
    /Credits are not ready/,
  );
});

test('signing in stores the Claude key beside the token', async () => {
  const root = temporaryRoot();
  const output = collector();
  const scripted = scriptedFetch([
    { body: { deviceCode: 'egb_dev', userCode: 'ABCD-2345', expiresInSeconds: 600, intervalSeconds: 1 } },
    { body: { status: 'ready', token: 'egb_token', email: 'm@example.com' } },
  ]);

  const result = await auth.login({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    output,
    fetchImpl: scripted.fetchImpl,
    openUrl: false,
    wait: async () => {},
    now: () => 0,
    hostname: 'laptop',
    fetchClaudeKey: async () => ({
      apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com', budgetUsd: 25, spendUsd: 4,
    }),
  });

  assert.equal(result.status, 'ready');
  const stored = auth.readCredentials(root, {});
  assert.equal(stored.token, 'egb_token');
  assert.equal(stored.claude.apiKey, 'sk-abc');
  assert.match(output.text(), /\$21\.00 of \$25\.00 left/);

  // Claude Code is the reason the key exists, so signing in wires it directly
  // and the member is told to run `claude`, not a shell line.
  assert.match(output.text(), /Claude Code is set up to use it/);
  const settings = JSON.parse(fs.readFileSync(settingsIn(root), 'utf8'));
  assert.equal(settings.apiKeyHelper, path.join(root, 'bin', 'engelbart-key'));
  assert.equal(settings.env.ANTHROPIC_BASE_URL, 'https://proxy.example.com');

  // The exports file is still written: it is what a shell, a CI runner, or
  // anything that is not Claude Code reads.
  const envFile = auth.envPath(root);
  assert.equal(fs.readFileSync(envFile, 'utf8'),
    '# Written by `engelbart auth`. Sourced by your shell; not meant to be edited.\n'
      + 'export ANTHROPIC_BASE_URL="https://proxy.example.com"\n'
      + 'export ANTHROPIC_AUTH_TOKEN="sk-abc"\n');
  assert.equal(fs.statSync(envFile).mode & 0o777, 0o600);
});

// Redirecting Claude Code's credential at something the member did not choose
// is a worse failure than one extra line of instruction, so a helper we did
// not write sends signing in back to the shell exports.
test('an apiKeyHelper we did not write sends the member to the exports instead', async () => {
  const root = temporaryRoot();
  const output = collector();
  const settingsFile = settingsIn(root);
  fs.mkdirSync(path.dirname(settingsFile), { recursive: true });
  fs.writeFileSync(settingsFile, JSON.stringify({ apiKeyHelper: '/opt/other-tool/key', theme: 'dark' }));
  const scripted = scriptedFetch([
    { body: { deviceCode: 'egb_dev', userCode: 'ABCD-2345', expiresInSeconds: 600, intervalSeconds: 1 } },
    { body: { status: 'ready', token: 'egb_token', email: 'm@example.com' } },
  ]);

  await auth.login({
    managedRoot: root,
    settingsFile,
    env: {},
    output,
    fetchImpl: scripted.fetchImpl,
    openUrl: false,
    wait: async () => {},
    now: () => 0,
    hostname: 'laptop',
    fetchClaudeKey: async () => ({
      apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com', budgetUsd: 25, spendUsd: 0,
    }),
  });

  assert.match(output.text(), /Leaving your existing apiKeyHelper alone \(\/opt\/other-tool\/key\)/);
  assert.match(output.text(), new RegExp(`source ${auth.envPath(root).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
  const settings = JSON.parse(fs.readFileSync(settingsFile, 'utf8'));
  assert.equal(settings.apiKeyHelper, '/opt/other-tool/key');
  assert.equal(settings.env, undefined);
});

// A sourced profile would otherwise keep pointing `claude` at a key this
// machine no longer holds.
test('disconnecting takes the shell exports with it', () => {
  const root = temporaryRoot();
  const stored = auth.writeCredentials(root, {
    schema: 1,
    apiBase: 'https://berkeley.mathetic.com',
    token: 'egb_secret',
    email: 'm@example.com',
    claude: { apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com' },
  });
  auth.writeEnvFile(root, stored);
  assert.ok(fs.existsSync(auth.envPath(root)));

  auth.clearCredentials(root);
  assert.equal(fs.existsSync(auth.envPath(root)), false);
  assert.equal(fs.existsSync(auth.credentialsPath(root)), false);
});

test('a connection with no Claude key writes no exports to source', () => {
  const root = temporaryRoot();
  assert.equal(auth.writeEnvFile(root, { token: 'egb_secret' }), '');
  assert.equal(fs.existsSync(auth.envPath(root)), false);
});

// Credits can lag the account. Losing the key must not lose the pairing too,
// or the member has to approve a second code to get back to the same place.
test('a missing Claude key still leaves the machine connected', async () => {
  const root = temporaryRoot();
  const output = collector();
  const scripted = scriptedFetch([
    { body: { deviceCode: 'egb_dev', userCode: 'ABCD-2345', expiresInSeconds: 600, intervalSeconds: 1 } },
    { body: { status: 'ready', token: 'egb_token', email: 'm@example.com' } },
  ]);

  const result = await auth.login({
    managedRoot: root,
    settingsFile: settingsIn(root),
    env: {},
    output,
    fetchImpl: scripted.fetchImpl,
    openUrl: false,
    wait: async () => {},
    now: () => 0,
    hostname: 'laptop',
    fetchClaudeKey: async () => { throw new Error('Credits are not ready'); },
  });

  assert.equal(result.status, 'ready');
  assert.equal(auth.readCredentials(root, {}).token, 'egb_token');
  assert.equal(auth.readCredentials(root, {}).claude, null);
  assert.match(output.text(), /Could not read this account's Claude key: Credits are not ready/);
});

test('the shell exports are exactly two lines, and absent without a key', () => {
  assert.equal(auth.claudeEnv(null), '');
  assert.equal(auth.claudeEnv({ token: 'egb_token' }), '');
  assert.equal(
    auth.claudeEnv({ claude: { apiKey: 'sk-abc', baseUrl: 'https://proxy.example.com' } }),
    'export ANTHROPIC_BASE_URL="https://proxy.example.com"\n'
      + 'export ANTHROPIC_AUTH_TOKEN="sk-abc"\n',
  );
});
