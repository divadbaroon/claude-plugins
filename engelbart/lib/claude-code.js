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
function helperSource(credentialsFile) {
  return `#!/usr/bin/env node
'use strict';
// Written by \`engelbart auth\`. Claude Code runs this for its credential.
// Edit ${MARKER} state with \`engelbart auth\` / \`engelbart logout\`, not here.
const fs = require('fs');
const FILE = ${JSON.stringify(credentialsFile)};

function stored() {
  try {
    return JSON.parse(fs.readFileSync(FILE, 'utf8'));
  } catch (error) {
    return null;
  }
}

async function main() {
  const value = stored();
  if (!value || !value.claude || !value.claude.apiKey) process.exit(1);
  // Asking the server first is what lets a rotated or revoked key take effect
  // on the next Claude Code session instead of the next \`engelbart auth\`. The
  // stored key is the answer when there is no network, which is the common
  // case in a room full of laptops on conference wifi.
  try {
    const response = await fetch(value.apiBase + '/api/engelbart-credentials', {
      headers: { Accept: 'application/json', Authorization: 'Bearer ' + value.token },
      signal: AbortSignal.timeout(5000),
    });
    if (response.ok) {
      const fresh = await response.json();
      if (fresh && fresh.apiKey) {
        process.stdout.write(String(fresh.apiKey));
        return;
      }
    }
  } catch (error) { /* fall through to the stored key */ }
  process.stdout.write(String(value.claude.apiKey));
}

main();
`;
}

function writeHelper(managedRoot, credentialsFile) {
  const root = validateManagedRoot(managedRoot);
  establishOwnership(root);
  const bin = path.join(root, 'bin');
  fs.mkdirSync(bin, { recursive: true });
  const file = path.join(bin, HELPER_FILE);
  atomicWrite(file, helperSource(credentialsFile), 0o700);
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

const HELPER_TTL_MS = 3600000;

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
