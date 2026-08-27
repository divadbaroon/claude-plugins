'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const { atomicWrite, establishOwnership, validateManagedRoot } = require('./installer');

const DEFAULT_API_BASE = 'https://berkeley.mathetic.com';
const CREDENTIALS_SCHEMA = 1;
const CREDENTIALS_FILE = 'auth.json';
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
      writeCredentials(managedRoot, {
        schema: CREDENTIALS_SCHEMA,
        apiBase: base,
        token: result.token,
        email: result.email || '',
        label: machineLabel(options.hostname),
        createdAt: new Date(now()).toISOString(),
      });
      output.write(`\nSigned in as ${result.email || 'your Engelbart account'}.\n`);
      return { status: 'ready', email: result.email || '' };
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
  clearCredentials(managedRoot);
  return { revoked, signedOut: true, email: stored.email || '' };
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
  CREDENTIALS_FILE,
  CREDENTIALS_SCHEMA,
  DEFAULT_API_BASE,
  POLL_CEILING_SECONDS,
  apiBase,
  clearCredentials,
  credentialsPath,
  login,
  logout,
  machineLabel,
  openBrowser,
  postJson,
  readCredentials,
  whoami,
  writeCredentials,
};
