// Shared runtime resolution for the cross-platform Claude Code hooks.
// Locates a spawnable `hc` the same way on macOS, Linux, and Windows, so the
// hook scripts carry no per-OS branching of their own.
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function isFile(p) {
  try {
    return !!p && fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

// On Windows the stable bin\hc.cmd shim is not directly spawnable, so resolve
// the runtime's real hc.exe from the owned install manifest -- the record the
// installer writes. POSIX uses the spawnable bin/hc symlink directly.
function runtimeHcFromManifest(managedRoot) {
  try {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(managedRoot, 'install.json'), 'utf8'));
    if (!manifest || typeof manifest.runtime !== 'string') return '';
    return process.platform === 'win32'
      ? path.join(manifest.runtime, 'Scripts', 'hc.exe')
      : path.join(manifest.runtime, 'bin', 'hc');
  } catch {
    return '';
  }
}

// Returns { cmd, shell }: a spawnable hc plus whether it needs a shell (only the
// Windows PATH fallback to hc.cmd does).
function resolveHc() {
  const fromEnv = process.env.HC_EXECUTABLE;
  if (isFile(fromEnv)) return { cmd: fromEnv, shell: false };
  const managedRoot = path.join(os.homedir(), '.human-compact');
  const fromManifest = runtimeHcFromManifest(managedRoot);
  if (isFile(fromManifest)) return { cmd: fromManifest, shell: false };
  if (process.platform !== 'win32') {
    const launcher = path.join(managedRoot, 'bin', 'hc');
    if (isFile(launcher)) return { cmd: launcher, shell: false };
    return { cmd: 'hc', shell: false };            // last resort: PATH lookup
  }
  return { cmd: 'hc', shell: true };  // Windows: hc.cmd via the shell's PATHEXT
}

function haveHc(resolved) {
  return resolved.shell || isFile(resolved.cmd);
}

module.exports = { isFile, runtimeHcFromManifest, resolveHc, haveHc };
