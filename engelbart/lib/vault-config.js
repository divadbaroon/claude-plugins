'use strict';

// Handing `hc` the project it should talk to, so connecting an account is the
// only thing a member does. `hc supabase setup` otherwise asks them to find a
// project URL and paste an anon key by hand — a step that has nothing to do
// with them, and one wrong character in which fails much later and unclearly.

const fs = require('fs');
const os = require('os');
const path = require('path');

const { atomicWrite } = require('./installer');

const CONFIG_FILE = 'supabase.json';
const CONFIG_ENDPOINT = '/api/engelbart-config';
// What `hc supabase setup` writes when it has nothing to write. A file still
// holding these is a prompt, not a configuration, so replacing it loses
// nothing — anything else is the member's and is left alone.
const PLACEHOLDERS = Object.freeze([
  'https://YOUR-PROJECT-REF.supabase.co',
  'PASTE-YOUR-ANON-PUBLIC-KEY-HERE',
  'you@example.com',
]);

// The same resolution `hc` performs (chat_state._state_location): the vault is
// CLAUDE_VAULT_DIR or ~/.claude-vault, and both halves have to land on one
// file or the CLI writes a config the sync button never reads.
function vaultDir(env = process.env, homedir = os.homedir()) {
  const configured = env && typeof env.CLAUDE_VAULT_DIR === 'string' ? env.CLAUDE_VAULT_DIR.trim() : '';
  return configured || path.join(homedir, '.claude-vault');
}

function configPath(options = {}) {
  return path.join(options.vaultDir || vaultDir(options.env, options.homedir), CONFIG_FILE);
}

// The member's vault is real state on a real machine. Reaching it takes saying
// so, for the same reason writing their Claude Code settings does.
function resolveTarget(options = {}) {
  const file = options.configFile || configPath(options);
  const real = path.resolve(file) === path.resolve(path.join(vaultDir({}, os.homedir()), CONFIG_FILE));
  return { file, real, permitted: !real || options.allowRealHome === true };
}

async function fetchProjectConfig(base, options = {}) {
  const fetchImpl = options.fetchImpl || global.fetch;
  if (typeof fetchImpl !== 'function') {
    throw new Error('this Node build has no fetch; Node 18 or newer is required');
  }
  const response = await fetchImpl(`${base}${CONFIG_ENDPOINT}`, { headers: { Accept: 'application/json' } });
  let value = {};
  try {
    value = await response.json();
  } catch (error) {
    value = {};
  }
  if (!response.ok) throw new Error(value.error || `${base} answered ${response.status}`);
  if (!value.supabaseUrl || !value.supabaseAnonKey) throw new Error('that deployment publishes no project config');
  return { url: String(value.supabaseUrl), anonKey: String(value.supabaseAnonKey) };
}

function readConfig(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch (error) {
    return null;
  }
}

// A file holding anything real is the member's own — a second project, a local
// stack — and overwriting it would take their keys with it.
function isOurs(existing) {
  if (!existing) return true;
  return Object.keys(existing).every((key) => {
    const value = existing[key];
    return !value || PLACEHOLDERS.includes(String(value));
  });
}

function write(options = {}) {
  const { file, permitted } = resolveTarget(options);
  if (!permitted) return { changed: false, reason: 'unsafe-target', file };
  if (!options.url || !options.anonKey) return { changed: false, reason: 'no-config', file };

  const existing = fs.existsSync(file) ? readConfig(file) : null;
  if (existing === null && fs.existsSync(file)) return { changed: false, reason: 'unreadable', file };
  if (!isOurs(existing)) return { changed: false, reason: 'member-config', file };

  // The address is carried too: `hc supabase login` offers it as the default,
  // so the member types a password rather than both halves of a sign-in they
  // already completed in the browser.
  const next = { url: String(options.url), anon_key: String(options.anonKey) };
  if (options.email) next.email = String(options.email);

  fs.mkdirSync(path.dirname(file), { recursive: true });
  atomicWrite(file, `${JSON.stringify(next, null, 2)}\n`, 0o600);
  return { changed: true, file, url: next.url };
}

module.exports = {
  CONFIG_ENDPOINT,
  CONFIG_FILE,
  PLACEHOLDERS,
  configPath,
  fetchProjectConfig,
  isOurs,
  readConfig,
  resolveTarget,
  vaultDir,
  write,
};
