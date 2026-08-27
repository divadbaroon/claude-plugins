'use strict';

// Wiring the key into Claude Code itself, so that connecting an account is the
// whole story and no line has to be run in a shell afterwards. Claude Code
// reads `apiKeyHelper` for its credential and `env` for everything else, and
// reloads both on save.

const fs = require('fs');
const os = require('os');
const path = require('path');

const { atomicWrite, establishOwnership, validateManagedRoot } = require('./installer');

const HELPER_FILE = 'engelbart-key';
const SETTINGS_FILE = path.join('.claude', 'settings.json');
// Stamped into what we write so uninstall can tell our entries from a
// hand-edited one and leave anything it did not author alone.
const MARKER = 'engelbart-cli';

// Claude Code re-runs the helper when a request comes back 401, and otherwise
// only once this cache expires. An exhausted LiteLLM budget is never a 401 --
// the proxy answers 400 or 429 -- so that recovery cannot fire for the case
// that actually happens, and this window is the whole time a member spends
// holding a key the pool has already stopped honouring. Claude Code's own
// default is five minutes; fifteen keeps the poll off the proxy's back without
// making the stale window an hour long.
const HELPER_TTL_MS = 900000;

// Claude Code itself reads its settings from $CLAUDE_CONFIG_DIR when that is
// set, so honouring it here is not a test affordance: a member who moved their
// configuration should be wired into the one they actually use.
function settingsPath(homedir = os.homedir(), env = {}) {
  const configDir = env && typeof env.CLAUDE_CONFIG_DIR === 'string' ? env.CLAUDE_CONFIG_DIR.trim() : '';
  if (configDir) return path.join(configDir, 'settings.json');
  return path.join(homedir, SETTINGS_FILE);
}

// The live file holds the member's hooks, plugins and theme, and rewriting it
// by accident is the one failure this module cannot apologise its way out of.
// So reaching it takes saying so: `allowRealHome` is set at the CLI entry
// point and nowhere else, which leaves a forgotten injection in a test writing
// nothing instead of rewriting the configuration of whoever ran the suite.
function resolveTarget(options = {}) {
  const file = options.settingsFile || settingsPath(options.homedir || os.homedir(), options.env || {});
  const real = path.resolve(file) === path.resolve(settingsPath(os.homedir(), {}));
  return { file, real, permitted: !real || options.allowRealHome === true };
}

function helperPath(managedRoot) {
  return path.join(validateManagedRoot(managedRoot), 'bin', HELPER_FILE);
}

// `npx engelbart-cli` leaves no installed copy of this package behind, so the
// helper cannot shell back into the CLI. It reads the credentials file
// directly and depends on nothing but the Node the installer already required.
//
// The settings path is baked in for one reason: when the server says this
// account has no spendable key, the helper has to be able to take itself back
// out. While an `apiKeyHelper` is set, Claude Code does not fall back to the
// member's own claude.ai login -- so a helper that keeps printing a dead key
// does not degrade the session, it ends it.
function helperSource(credentialsFile, settingsFile, helperFile, baseUrl) {
  return `#!/usr/bin/env node
'use strict';
// Written by \`engelbart auth\`. Claude Code runs this for its credential.
//
// To put Claude Code back on your own account, run this file with
// --disconnect. That undoes exactly what \`engelbart auth\` wrote and nothing
// else. It is spelled out here because the installer puts only \`hc\` on PATH:
// there is no \`engelbart\` command to reach for, and while apiKeyHelper is set
// even \`/login\` cannot get past it.
const fs = require('fs');
const FILE = ${JSON.stringify(credentialsFile)};
const SETTINGS = ${JSON.stringify(settingsFile || '')};
const HELPER = ${JSON.stringify(helperFile || '')};
const BASE_URL = ${JSON.stringify(baseUrl || '')};
const TTL = ${JSON.stringify(String(HELPER_TTL_MS))};

// A refusal is an answer; a timeout is not. Only these say "this account has
// no key to give you" -- 5xx and 429 are the deployment having a bad minute,
// and a member on conference wifi must not lose a working key over one.
const REFUSED = new Set([401, 402, 403, 404, 409]);
const SPENT = new Set(['exhausted', 'revoked', 'blocked']);

function stored() {
  try {
    return JSON.parse(fs.readFileSync(FILE, 'utf8'));
  } catch (error) {
    return null;
  }
}

// The raw bytes come back too, because they are what makes the write safe:
// this file is the member's, it holds their hooks and permissions and theme,
// and it is being read and written by the Claude Code session that spawned us.
function settings() {
  try {
    const raw = fs.readFileSync(SETTINGS, 'utf8');
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? { raw, parsed }
      : null;
  } catch (error) {
    return null;
  }
}

// Removes only what \`engelbart auth\` wrote, matched by value, so a member who
// pointed any of this somewhere else keeps it. Claude Code reloads settings on
// save, so the running session picks its own login back up without a restart.
//
// Removing it is not tidiness. While \`apiKeyHelper\` is set, its output takes
// precedence over a saved claude.ai login and \`/login\` cannot override it, so
// this is the only thing that gives a member their own account back -- exiting
// non-zero and leaving the setting in place strands them.
//
// The read and the write are separated by a whole process, and Claude Code is
// running the entire time. So the bytes we parsed are checked again at the
// last moment, and anything that moved underneath us means someone else's
// change is in flight: a spent key is not worth overwriting a permission the
// member just granted, and the next run will refuse again anyway.
function unwireOnce(baseUrl) {
  if (!SETTINGS || !HELPER) return false;
  const before = settings();
  if (!before || before.parsed.apiKeyHelper !== HELPER) return false;
  const next = { ...before.parsed };
  delete next.apiKeyHelper;
  if (next.env && typeof next.env === 'object') {
    const env = { ...next.env };
    const gateway = baseUrl || BASE_URL;
    if (gateway && env.ANTHROPIC_BASE_URL === gateway) delete env.ANTHROPIC_BASE_URL;
    if (env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS === TTL) delete env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS;
    if (Object.keys(env).length) next.env = env;
    else delete next.env;
  }
  let temporary = '';
  try {
    temporary = SETTINGS + '.engelbart-' + process.pid;
    fs.writeFileSync(temporary, JSON.stringify(next, null, 2) + '\\n', { mode: 0o600 });
    const after = settings();
    if (!after || after.raw !== before.raw) {
      fs.rmSync(temporary, { force: true });
      return false;
    }
    fs.renameSync(temporary, SETTINGS);
    return true;
  } catch (error) {
    try {
      if (temporary) fs.rmSync(temporary, { force: true });
    } catch (cleanup) { /* nothing left to remove */ }
    return false;
  }
}

// Losing the race is only a deferral when this runs unattended -- the next
// refusal tries again. When a member ran --disconnect it is the whole point of
// the command, so it gets a few attempts before giving up.
function unwire(baseUrl, attempts = 1) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (unwireOnce(baseUrl)) return true;
  }
  return false;
}

// Already gone counts as success: a member running this twice, or running it
// after \`engelbart logout\`, asked for a state that is already true.
function disconnected() {
  const current = settings();
  return !current || current.parsed.apiKeyHelper !== HELPER;
}

// Never the silent path: a member whose session just changed accounts is owed
// the reason, and stderr is the only channel a credential helper has. Claude
// Code reads stdout for the key, so nothing here can go there.
function refuse(value, reason) {
  const unwired = unwire(value && value.claude && value.claude.baseUrl);
  process.stderr.write('Engelbart: ' + reason + '\\n');
  // The second line is never decoration. Un-wired, the member is already back
  // on their own account and only needs to know why. Still wired -- because
  // their settings file moved under us, or is not ours to edit -- they are
  // stuck until the setting goes, and \`/login\` cannot get them out of it.
  //
  // So what is printed is a path, not a command name. There is no \`engelbart\`
  // on PATH to run, npx needs a network, and this file is the one thing that
  // is certainly here and certainly executable: Claude Code just ran it.
  process.stderr.write(unwired
    ? 'Claude Code is back on your own account. Reconnect with \`npx engelbart-cli auth\`.\\n'
    : 'To put Claude Code back on your own account, run:\\n\\n    '
      + HELPER + ' --disconnect\\n');
  process.exit(1);
}

// The way back, and deliberately not dependent on anything: no network, no
// npx, no PATH, no credentials file. Whatever else has gone wrong, the member
// still has this file and it still knows which three values to take out.
function disconnect() {
  if (disconnected()) {
    process.stdout.write('Claude Code is already on your own account.\\n');
    return 0;
  }
  const value = stored();
  if (unwire(value && value.claude && value.claude.baseUrl, 3)) {
    process.stdout.write('Claude Code is back on your own account.\\n');
    process.stdout.write('Reconnect with \`npx engelbart-cli auth\`.\\n');
    return 0;
  }
  process.stderr.write('Could not edit ' + SETTINGS + '.\\n');
  process.stderr.write('Remove "apiKeyHelper" from it by hand to undo this.\\n');
  return 1;
}

async function main() {
  // Checked before anything else reads a file or opens a socket: this is the
  // command a stuck member runs, so it must not be able to fail for any of the
  // reasons that got them stuck.
  if (process.argv.slice(2).some((arg) => arg === '--disconnect')) {
    process.exit(disconnect());
  }
  const value = stored();
  if (!value || !value.claude || !value.claude.apiKey) process.exit(1);
  // Asking the server first is what lets a rotated, revoked or spent key take
  // effect on the next Claude Code session instead of the next
  // \`engelbart auth\`. The stored key is the answer when there is no network,
  // which is the common case in a room full of laptops on conference wifi --
  // but it is not the answer when the server answered and the answer was no.
  let response = null;
  try {
    response = await fetch(value.apiBase + '/api/engelbart-credentials', {
      headers: { Accept: 'application/json', Authorization: 'Bearer ' + value.token },
      signal: AbortSignal.timeout(5000),
    });
  } catch (error) {
    // Unreachable, not refused. Fall through to the stored key.
  }
  if (response && REFUSED.has(response.status)) {
    let detail = '';
    try {
      const body = await response.json();
      detail = body && body.error ? String(body.error) : '';
    } catch (error) { /* the status is enough */ }
    return refuse(value, detail || 'this account has no spendable Claude credit right now.');
  }
  if (response && response.ok) {
    let fresh = null;
    try {
      fresh = await response.json();
    } catch (error) { /* fall through to the stored key */ }
    // A key that exists but cannot be spent is worse than no key at all: it
    // buys a session that fails on its first request with a proxy error
    // Claude Code cannot explain.
    if (fresh && SPENT.has(String(fresh.status || ''))) {
      return refuse(value, 'your Engelbart Claude credit is used up.');
    }
    if (fresh && fresh.apiKey) {
      process.stdout.write(String(fresh.apiKey));
      return;
    }
  }
  process.stdout.write(String(value.claude.apiKey));
}

main();
`;
}

function writeHelper(managedRoot, credentialsFile, settingsFile, baseUrl) {
  const root = validateManagedRoot(managedRoot);
  establishOwnership(root);
  const bin = path.join(root, 'bin');
  fs.mkdirSync(bin, { recursive: true });
  const file = path.join(bin, HELPER_FILE);
  // The gateway URL is baked in as well as stored, so `--disconnect` still
  // knows which ANTHROPIC_BASE_URL was ours after the credentials file is gone.
  atomicWrite(file, helperSource(credentialsFile, settingsFile, file, baseUrl), 0o700);
  return file;
}

function readSettings(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch (error) {
    return null;
  }
}

// Ours is identified by value, not by a marker key: Claude Code validates this
// file, and inventing a foreign key to own would be a worse trade than simply
// refusing to touch anything whose value is not the one we wrote.
function ownsHelper(settings, helper) {
  return !settings.apiKeyHelper || settings.apiKeyHelper === helper;
}

// Never clobbers. An apiKeyHelper that points somewhere else belongs to another
// tool or a hand edit, and silently redirecting Claude Code's credential is a
// worse failure than printing one line and letting the member decide.
function connect(options = {}) {
  const managedRoot = validateManagedRoot(options.managedRoot);
  const { file, permitted } = resolveTarget(options);
  if (!permitted) return { changed: false, reason: 'unsafe-target', file };
  const helper = helperPath(managedRoot);
  const baseUrl = String(options.baseUrl || '');
  if (!baseUrl) return { changed: false, reason: 'no-base-url', file };

  const existing = fs.existsSync(file) ? readSettings(file) : {};
  if (existing === null) return { changed: false, reason: 'unreadable', file };
  if (!ownsHelper(existing, helper)) {
    return { changed: false, reason: 'foreign-helper', file, helper: existing.apiKeyHelper };
  }

  const env = { ...(existing.env && typeof existing.env === 'object' ? existing.env : {}) };
  env.ANTHROPIC_BASE_URL = baseUrl;
  env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS = String(HELPER_TTL_MS);
  const next = { ...existing, apiKeyHelper: helper, env };

  fs.mkdirSync(path.dirname(file), { recursive: true });
  atomicWrite(file, `${JSON.stringify(next, null, 2)}\n`, 0o600);
  return { changed: true, file, helper, baseUrl };
}

// Removes only what still matches what we wrote, so a member who pointed these
// at something of their own keeps it.
function disconnect(options = {}) {
  const { file, permitted } = resolveTarget(options);
  if (!permitted) return { changed: false, reason: 'unsafe-target', file };
  let helper = '';
  try {
    helper = helperPath(options.managedRoot);
  } catch (error) {
    return { changed: false, reason: 'no-managed-root', file };
  }
  if (!fs.existsSync(file)) return { changed: false, reason: 'absent', file };
  const existing = readSettings(file);
  if (existing === null) return { changed: false, reason: 'unreadable', file };

  const next = { ...existing };
  let changed = false;
  if (next.apiKeyHelper === helper) {
    delete next.apiKeyHelper;
    changed = true;
  }
  if (next.env && typeof next.env === 'object') {
    const env = { ...next.env };
    if (env.ANTHROPIC_BASE_URL === options.baseUrl) { delete env.ANTHROPIC_BASE_URL; changed = true; }
    if (env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS === String(HELPER_TTL_MS)) {
      delete env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS;
      changed = true;
    }
    if (Object.keys(env).length) next.env = env;
    else delete next.env;
  }
  if (!changed) return { changed: false, reason: 'not-ours', file };
  atomicWrite(file, `${JSON.stringify(next, null, 2)}\n`, 0o600);
  try {
    fs.rmSync(helper, { force: true });
  } catch (error) { /* the helper is already gone */ }
  return { changed: true, file };
}

module.exports = {
  HELPER_FILE,
  HELPER_TTL_MS,
  MARKER,
  connect,
  disconnect,
  helperPath,
  helperSource,
  ownsHelper,
  readSettings,
  resolveTarget,
  settingsPath,
  writeHelper,
};
