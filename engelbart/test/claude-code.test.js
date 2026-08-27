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
