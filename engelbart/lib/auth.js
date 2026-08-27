'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const { atomicWrite, establishOwnership, validateManagedRoot } = require('./installer');
const claudeCode = require('./claude-code');
const vaultConfig = require('./vault-config');

const DEFAULT_API_BASE = 'https://berkeley.mathetic.com';
const CREDENTIALS_SCHEMA = 1;
const CREDENTIALS_FILE = 'auth.json';
const CREDENTIALS_ENDPOINT = '/api/engelbart-credentials';
const ENV_FILE = 'env.sh';
const POLL_CEILING_SECONDS = 30;

// A token minted against a development deployment must not be replayed at
// berkeley.mathetic.com, so the base it was issued for travels with it.
function apiBase(env = process.env) {
  const raw = String(env.ENGELBART_API_BASE || DEFAULT_API_BASE).trim().replace(/\/+$/, '');
  let url;
  try {
    url = new URL(raw);
  } catch (error) {
    throw new Error(`ENGELBART_API_BASE is not a URL: ${raw}`);
  }
  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
  if (url.protocol !== 'https:' && !local) {
    throw new Error('ENGELBART_API_BASE must be an HTTPS URL');
  }
  return url.origin;
}

function credentialsPath(managedRoot) {
  return path.join(validateManagedRoot(managedRoot), CREDENTIALS_FILE);
}

function readCredentials(managedRoot, env = process.env) {
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(credentialsPath(managedRoot), 'utf8'));
  } catch (error) {
    return null;
  }
  if (!parsed || parsed.schema !== CREDENTIALS_SCHEMA || typeof parsed.token !== 'string') {
    return null;
  }
  // Silently ignoring a base mismatch would send a dev token to production.
  if (parsed.apiBase !== apiBase(env)) return null;
  return parsed;
}

function writeCredentials(managedRoot, value) {
  const root = validateManagedRoot(managedRoot);
  establishOwnership(root);
  atomicWrite(path.join(root, CREDENTIALS_FILE), `${JSON.stringify(value, null, 2)}\n`, 0o600);
  return value;
}

function clearCredentials(managedRoot) {
  try {
    fs.rmSync(credentialsPath(managedRoot), { force: true });
    // The exports outlive the token otherwise, and a sourced profile would keep
    // pointing `claude` at a key this machine is no longer entitled to.
    fs.rmSync(envPath(managedRoot), { force: true });
    return true;
  } catch (error) {
    return false;
  }
}

function machineLabel(hostname = os.hostname()) {
  return String(hostname || '').split('.')[0].slice(0, 60);
}

async function postJson(base, body, options = {}) {
  const fetchImpl = options.fetchImpl || global.fetch;
  if (typeof fetchImpl !== 'function') {
    throw new Error('this Node build has no fetch; Node 18 or newer is required');
  }
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (options.token) headers.Authorization = `Bearer ${options.token}`;
  let response;
  try {
    response = await fetchImpl(`${base}/api/engelbart-device`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new Error(`could not reach ${base}: ${error.message}`);
  }
  let value = {};
  try {
    value = await response.json();
  } catch (error) {
    value = {};
  }
  if (!response.ok) {
    throw new Error(value.error || `${base} answered ${response.status}`);
  }
  return value;
}

// The token is not what the member came for -- the Claude key it can fetch is.
// Reading it here is what lets `claude` run without copying two export lines
// out of a browser by hand.
async function fetchClaudeKey(base, token, options = {}) {
  const fetchImpl = options.fetchImpl || global.fetch;
  if (typeof fetchImpl !== 'function') {
    throw new Error('this Node build has no fetch; Node 18 or newer is required');
  }
  const headers = { Accept: 'application/json', Authorization: `Bearer ${token}` };
  const url = `${base}${CREDENTIALS_ENDPOINT}`;
  // POST provisions the key and is idempotent, so pairing a second machine
  // reuses the first machine's key rather than minting a rival one.
  await fetchImpl(url, { method: 'POST', headers });
  const response = await fetchImpl(url, { headers });
  let value = {};
  try {
    value = await response.json();
  } catch (error) {
    value = {};
  }
  if (!response.ok) throw new Error(value.error || `${base} answered ${response.status}`);
  if (!value.apiKey || !value.baseUrl) throw new Error('that account has no Claude key yet');
  return {
    apiKey: String(value.apiKey),
    baseUrl: String(value.baseUrl),
    budgetUsd: Number(value.budgetUsd) || 0,
    spendUsd: Number(value.spendUsd) || 0,
  };
}

// Two lines, in the order a shell wants them, so `eval` is the whole install
// step rather than a paragraph of instructions.
function claudeEnv(stored) {
  if (!stored || !stored.claude || !stored.claude.apiKey) return '';
  return `export ANTHROPIC_BASE_URL="${stored.claude.baseUrl}"\n`
    + `export ANTHROPIC_AUTH_TOKEN="${stored.claude.apiKey}"\n`;
}

function envPath(managedRoot) {
  return path.join(validateManagedRoot(managedRoot), ENV_FILE);
}

// `npx engelbart-cli` leaves no `engelbart` on PATH -- the installer puts only
// `hc` there -- so telling the member to run `engelbart env` names a command
// they do not have. A file next to the token is always reachable, costs no
// network, and is cheap enough to source from a shell profile.
function writeEnvFile(managedRoot, stored) {
  const lines = claudeEnv(stored);
  if (!lines) {
    try {
      fs.rmSync(envPath(managedRoot), { force: true });
    } catch (error) { /* nothing to remove */ }
    return '';
  }
  const root = validateManagedRoot(managedRoot);
  establishOwnership(root);
  const body = '# Written by `engelbart auth`. Sourced by your shell; not meant to be edited.\n'
    + `${lines}`;
  atomicWrite(path.join(root, ENV_FILE), body, 0o600);
  return envPath(managedRoot);
}

// Best effort by design: a machine with no browser still has the URL and the
// code printed above this call, which is the whole fallback path.
function openBrowser(url, options = {}) {
  const platform = options.platform || process.platform;
  const spawnImpl = options.spawnImpl || spawn;
  const command = platform === 'darwin' ? 'open' : 'xdg-open';
  try {
    const child = spawnImpl(command, [url], { stdio: 'ignore', detached: true });
    if (child && typeof child.unref === 'function') child.unref();
    if (child && typeof child.on === 'function') child.on('error', () => {});
    return true;
  } catch (error) {
    return false;
  }
}

// Deliberately not unref'd: between two polls there is nothing else holding
// the loop open, and an unref'd timer would let the process exit mid-wait.
function sleep(seconds) {
  return new Promise((resolve) => { setTimeout(resolve, seconds * 1000); });
}

// Failing to wire Claude Code is never fatal: the exports file still works,
// and a member whose settings we will not touch is better served by being told
// than by having their configuration rewritten.
function wireClaudeCode(managedRoot, stored, options = {}) {
  if (!stored || !stored.claude || !stored.claude.baseUrl) return { changed: false, reason: 'no-key' };
  try {
    claudeCode.writeHelper(managedRoot, credentialsPath(managedRoot));
    return claudeCode.connect({
      managedRoot,
      homedir: options.homedir,
      settingsFile: options.settingsFile,
      env: options.env,
      allowRealHome: options.allowRealHome,
      baseUrl: stored.claude.baseUrl,
    });
  } catch (error) {
    return { changed: false, reason: error.message };
  }
}

// Never fatal, and never an overwrite: a member who pointed `hc` at their own
// project keeps it, and one who has not is spared the setup step entirely.
async function shareProjectConfig(base, stored, options = {}) {
  const project = await vaultConfig.fetchProjectConfig(base, options);
  return vaultConfig.write({
    ...project,
    email: stored && stored.email,
    env: options.env,
    homedir: options.homedir,
    vaultDir: options.vaultDir,
    configFile: options.vaultConfigFile,
    allowRealHome: options.allowRealHome,
  });
}

async function login(options = {}) {
  const env = options.env || process.env;
  const base = apiBase(env);
  const output = options.output || process.stdout;
  const wait = options.wait || sleep;
  const now = options.now || Date.now;
  const managedRoot = options.managedRoot;

  const session = await postJson(base, {
    action: 'start',
    label: machineLabel(options.hostname),
  }, options);
  const url = session.verificationUrlComplete || `${base}/engelbart?code=${session.userCode}`;

  // The code is printed before the browser opens: on a machine that cannot
  // open one, everything needed to finish is already on screen.
  output.write('\nConnect this machine to your Engelbart account.\n\n');
  output.write(`  code   ${session.userCode}\n`);
  output.write(`  page   ${url}\n\n`);
  const opened = options.openUrl === false
    ? false
    : (options.openUrl || openBrowser)(url, options);
  output.write(opened
    ? 'Opening that page. Approve the code above to finish.\n'
    : 'Open that page and approve the code above to finish.\n');

  const deadline = now() + (Number(session.expiresInSeconds) || 600) * 1000;
  let interval = Math.max(1, Number(session.intervalSeconds) || 5);

  while (now() < deadline) {
    await wait(interval);
    const result = await postJson(base, { action: 'poll', deviceCode: session.deviceCode }, options);
    if (result.status === 'ready') {
      // The key is fetched before the token is written, but a failure to get
      // one never discards the pairing: the account is connected either way,
      // and `engelbart auth` can be run again once credits are ready.
      let claude = null;
      let keyError = '';
      try {
        claude = await (options.fetchClaudeKey || fetchClaudeKey)(base, result.token, options);
      } catch (error) {
        keyError = error.message;
      }
      const stored = writeCredentials(managedRoot, {
        schema: CREDENTIALS_SCHEMA,
        apiBase: base,
        token: result.token,
        email: result.email || '',
        label: machineLabel(options.hostname),
        createdAt: new Date(now()).toISOString(),
        claude,
      });
      output.write(`\nSigned in as ${result.email || 'your Engelbart account'}.\n`);
      if (claude) {
        const left = Math.max(0, claude.budgetUsd - claude.spendUsd);
        output.write(`Claude credit: $${left.toFixed(2)} of $${claude.budgetUsd.toFixed(2)} left.\n`);
        const written = writeEnvFile(managedRoot, stored);
        // Claude Code is the reason the key exists, so it gets told directly
        // rather than being left to inherit a variable the member has to
        // remember to export.
        // `hc` needs the same project this account lives in. Handing it over
        // now is the difference between a member typing a password and a
        // member hunting for an anon key, and a deployment that will not
        // answer is not a reason to fail a sign-in that already succeeded.
        try {
          await (options.shareProjectConfig || shareProjectConfig)(base, stored, options);
        } catch (error) { /* `hc supabase setup` still works by hand */ }

        const wired = (options.wireClaudeCode || wireClaudeCode)(managedRoot, stored, options);
        if (wired.changed) {
          output.write('\nClaude Code is set up to use it. Just run `claude`.\n');
        } else if (wired.reason === 'foreign-helper') {
          output.write(`\nLeaving your existing apiKeyHelper alone (${wired.helper}).\n`);
          output.write(`To use this credit instead, run: source ${written}\n`);
        } else {
          output.write(`\nRun this once here so \`claude\` uses it:\n\n    source ${written}\n`);
        }
      } else {
        output.write(`Could not read this account's Claude key: ${keyError}\n`);
      }
      return { status: 'ready', email: result.email || '', claude, stored };
    }
    if (result.status === 'denied') {
      output.write('\nThat code was rejected in the browser. Nothing was connected.\n');
      return { status: 'denied' };
    }
    if (result.status === 'expired') {
      output.write('\nThat code expired before it was approved.\n');
      return { status: 'expired' };
    }
    if (result.status === 'slow_down') {
      interval = Math.min(POLL_CEILING_SECONDS, Number(result.intervalSeconds) || interval * 2);
    }
  }
  output.write('\nThat code expired before it was approved.\n');
  return { status: 'expired' };
}

async function logout(options = {}) {
  const env = options.env || process.env;
  const managedRoot = options.managedRoot;
  const stored = readCredentials(managedRoot, env);
  if (!stored) return { revoked: false, signedOut: false };
  // The local file goes either way: a server that cannot be reached must not
  // leave a usable token sitting on disk.
  let revoked = false;
  try {
    const result = await postJson(apiBase(env), { action: 'revoke', token: stored.token }, options);
    revoked = Boolean(result.revoked);
  } catch (error) {
    revoked = false;
  }
  const unwired = claudeCode.disconnect({
    managedRoot,
    homedir: options.homedir,
    settingsFile: options.settingsFile,
    env: options.env,
    allowRealHome: options.allowRealHome,
    baseUrl: stored.claude && stored.claude.baseUrl,
  });
  clearCredentials(managedRoot);
  return { revoked, signedOut: true, email: stored.email || '', unwired: unwired.changed };
}

async function whoami(options = {}) {
  const env = options.env || process.env;
  const stored = readCredentials(options.managedRoot, env);
  if (!stored) return { signedIn: false };
  try {
    const result = await postJson(apiBase(env), { action: 'whoami' }, {
      ...options,
      token: stored.token,
    });
    return { signedIn: true, email: result.email || stored.email || '' };
  } catch (error) {
    return { signedIn: false, reason: error.message, email: stored.email || '' };
  }
}

module.exports = {
  CREDENTIALS_ENDPOINT,
  CREDENTIALS_FILE,
  CREDENTIALS_SCHEMA,
  DEFAULT_API_BASE,
  POLL_CEILING_SECONDS,
  apiBase,
  claudeEnv,
  clearCredentials,
  ENV_FILE,
  credentialsPath,
  envPath,
  fetchClaudeKey,
  login,
  logout,
  machineLabel,
  openBrowser,
  postJson,
  readCredentials,
  shareProjectConfig,
  whoami,
  wireClaudeCode,
  writeCredentials,
  writeEnvFile,
};
