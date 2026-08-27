'use strict';

const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const test = require('node:test');

const vaultConfig = require('../lib/vault-config');

function temporaryVault() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'engelbart-vault-'));
}

function configIn(vault) {
  return path.join(vault, 'supabase.json');
}

const PROJECT = { url: 'https://ref.supabase.co', anonKey: 'anon-key' };

function read(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

test('the project a member signed into is the one hc is handed', () => {
  const vault = temporaryVault();
  const result = vaultConfig.write({ ...PROJECT, vaultDir: vault, email: 'm@example.com' });

  assert.equal(result.changed, true);
  assert.deepEqual(read(configIn(vault)), {
    url: 'https://ref.supabase.co',
    anon_key: 'anon-key',
    email: 'm@example.com',
  });
  assert.equal(fs.statSync(configIn(vault)).mode & 0o777, 0o600);
});

// The vault directory may not exist yet on a machine that has only just paired.
test('a vault that does not exist yet is created', () => {
  const vault = path.join(temporaryVault(), 'nested', 'vault');
  assert.equal(vaultConfig.write({ ...PROJECT, vaultDir: vault }).changed, true);
  assert.equal(read(configIn(vault)).url, 'https://ref.supabase.co');
});

test('an account with no address still gets a usable config', () => {
  const vault = temporaryVault();
  vaultConfig.write({ ...PROJECT, vaultDir: vault });
  assert.equal(read(configIn(vault)).email, undefined);
  assert.equal(read(configIn(vault)).anon_key, 'anon-key');
});

// `hc supabase setup` writes a file of placeholders. It is a prompt, not a
// configuration, so there is nothing in it to lose.
test('the setup template is replaced rather than respected', () => {
  const vault = temporaryVault();
  fs.writeFileSync(configIn(vault), JSON.stringify({
    url: 'https://YOUR-PROJECT-REF.supabase.co',
    anon_key: 'PASTE-YOUR-ANON-PUBLIC-KEY-HERE',
    email: 'you@example.com',
  }));

  assert.equal(vaultConfig.write({ ...PROJECT, vaultDir: vault }).changed, true);
  assert.equal(read(configIn(vault)).anon_key, 'anon-key');
});

// A member pointing `hc` at their own project — a second deployment, a local
// stack — keeps it. Overwriting would take their keys with it.
test('a project the member configured themselves is left alone', () => {
  const vault = temporaryVault();
  const theirs = { url: 'https://mine.supabase.co', anon_key: 'mine', email: 'me@example.com' };
  fs.writeFileSync(configIn(vault), JSON.stringify(theirs));

  const result = vaultConfig.write({ ...PROJECT, vaultDir: vault });

  assert.equal(result.changed, false);
  assert.equal(result.reason, 'member-config');
  assert.deepEqual(read(configIn(vault)), theirs);
});

test('a config file that is not readable JSON is never overwritten', () => {
  const vault = temporaryVault();
  fs.writeFileSync(configIn(vault), '{ not json');

  const result = vaultConfig.write({ ...PROJECT, vaultDir: vault });

  assert.equal(result.changed, false);
  assert.equal(result.reason, 'unreadable');
  assert.equal(fs.readFileSync(configIn(vault), 'utf8'), '{ not json');
});

test('a deployment that publishes no project config writes nothing', () => {
  const vault = temporaryVault();
  const result = vaultConfig.write({ url: '', anonKey: '', vaultDir: vault });
  assert.equal(result.reason, 'no-config');
  assert.equal(fs.existsSync(configIn(vault)), false);
});

// The same guard the Claude Code wiring carries, for the same reason.
test('the live vault is out of reach unless the caller says otherwise', () => {
  const live = path.join(vaultConfig.vaultDir({}, os.homedir()), 'supabase.json');
  const before = fs.existsSync(live) ? fs.readFileSync(live, 'utf8') : null;

  const result = vaultConfig.write({ ...PROJECT, homedir: os.homedir() });

  assert.equal(result.changed, false);
  assert.equal(result.reason, 'unsafe-target');
  assert.equal(result.file, live);
  assert.equal(fs.existsSync(live) ? fs.readFileSync(live, 'utf8') : null, before);
});

test('CLAUDE_VAULT_DIR decides where the vault is, as it does for hc', () => {
  const vault = temporaryVault();
  assert.equal(vaultConfig.vaultDir({ CLAUDE_VAULT_DIR: vault }), vault);
  assert.equal(vaultConfig.vaultDir({}, '/home/someone'), path.join('/home/someone', '.claude-vault'));
  assert.equal(vaultConfig.write({ ...PROJECT, env: { CLAUDE_VAULT_DIR: vault } }).changed, true);
  assert.equal(read(configIn(vault)).url, 'https://ref.supabase.co');
});

test('the published config is read from the deployment the token belongs to', async () => {
  const seen = [];
  const project = await vaultConfig.fetchProjectConfig('https://berkeley.example.com', {
    async fetchImpl(url) {
      seen.push(url);
      return { ok: true, status: 200, async json() { return { supabaseUrl: 'https://ref.supabase.co', supabaseAnonKey: 'anon-key', creditsEnabled: true }; } };
    },
  });

  assert.deepEqual(project, { url: 'https://ref.supabase.co', anonKey: 'anon-key' });
  assert.equal(seen[0], 'https://berkeley.example.com/api/engelbart-config');
});

test('a deployment answering without a project is an error, not a half-written file', async () => {
  await assert.rejects(
    () => vaultConfig.fetchProjectConfig('https://berkeley.example.com', {
      async fetchImpl() { return { ok: true, status: 200, async json() { return { creditsEnabled: true }; } }; },
    }),
    /publishes no project config/,
  );
});
