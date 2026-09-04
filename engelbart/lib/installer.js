'use strict';

const crypto = require('crypto');
const fs = require('fs');
const https = require('https');
const path = require('path');
const { spawnSync } = require('child_process');

const OWNER = 'engelbart-cli';
// The package was published as human-vault before the product was named, and
// that token is written into the markers of every install it made. It is an
// on-disk contract with those machines, not a brand: read it as ours, and
// write the current name back when we touch it.
const LEGACY_OWNERS = Object.freeze(['human-vault']);
const INSTALL_SCHEMA = 1;

function ownedByUs(owner) {
  return owner === OWNER || LEGACY_OWNERS.indexOf(owner) >= 0;
}

// --- platform shape ---------------------------------------------------------
// A managed venv lays its console scripts out differently on Windows: they
// live in `Scripts` rather than `bin`, and every executable carries a `.exe`.
// These take the *target* platform (what is being installed for), which the
// install flow already knows, so the same code can be exercised for win32 on
// a macOS test host by passing it in.
function isWindows(platform) {
  return (platform || process.platform) === 'win32';
}

function venvBinDir(platform) {
  return isWindows(platform) ? 'Scripts' : 'bin';
}

function exeName(name, platform) {
  return isWindows(platform) ? `${name}.exe` : name;
}

// The stable launcher name the shell actually invokes: a bare symlink on POSIX,
// a `.cmd` shim on Windows (a symlink there would need admin/Developer Mode).
function launcherName(name, platform) {
  return isWindows(platform) ? `${name}.cmd` : name;
}

// A Windows `.cmd` shim that forwards every argument to the runtime console
// script. The quotes tolerate spaces in the managed path; %* preserves the
// caller's own quoting. CRLF because cmd.exe is the interpreter.
function launcherShim(targetExe) {
  return `@echo off\r\n"${targetExe}" %*\r\n`;
}

// Recover the target a shim points at, for verifying an owned prior launcher.
function launcherShimTarget(content) {
  const match = String(content).match(/^"([^"]+)"\s+%\*/m);
  return match ? match[1] : null;
}

// POSIX rename atomically replaces an existing file; on Windows rename fails
// if the destination exists, so the destination is cleared first. That gives
// up the crash-window atomicity Windows never actually offered here. Keyed on
// the *real* platform: this is about the filesystem doing the rename, not the
// target being installed for.
function atomicReplace(temporary, file) {
  if (process.platform === 'win32') {
    try { fs.rmSync(file, { force: true }); } catch { /* fall through to rename */ }
  }
  fs.renameSync(temporary, file);
}
const MIN_CLAUDE_VERSION = Object.freeze([2, 1, 175]);
const MIN_CLAUDE_VERSION_TEXT = MIN_CLAUDE_VERSION.join('.');
const UV_VERSION = '0.11.32';
const UV_RELEASE = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}`;
const MAX_UV_ARCHIVE_BYTES = 128 * 1024 * 1024;

const UV_ASSETS = Object.freeze({
  'darwin-arm64': {
    target: 'aarch64-apple-darwin',
    sha256: 'ed336d0ba49db8ef89b2b41fffa372ce63bd032f22a56f001c265891aec32829',
  },
  'darwin-x64': {
    target: 'x86_64-apple-darwin',
    sha256: '77f5ca26c0de20e992a3677a174fe1121ee25c36f9b1434a863f75bf077a05eb',
  },
  'linux-arm64-gnu': {
    target: 'aarch64-unknown-linux-gnu',
    sha256: '4d4fa08d95b06642e5800df6a22bd71455f23f988269e18da2847971d8c0bf31',
  },
  'linux-x64-gnu': {
    target: 'x86_64-unknown-linux-gnu',
    sha256: 'aab924fd522efd06f1c5f3b93a243864fc453132c94b2dc49f1371b528a4b967',
  },
  'linux-arm64-musl': {
    target: 'aarch64-unknown-linux-musl',
    sha256: 'd70cdae687feb6aad9a09fe8d686df8c8efaf69a1007fa581379a2025adc10a5',
  },
  'linux-x64-musl': {
    target: 'x86_64-unknown-linux-musl',
    sha256: '1fd052f196108d87e61fc3d98fe06b4ec758c9a1eb1466a6fd1a436fe45885f2',
  },
  'win32-x64': {
    target: 'x86_64-pc-windows-msvc',
    sha256: 'acfde570451cfdb8689fa159a138ee805ba4e241c466432750302c86254b0984',
  },
  'win32-arm64': {
    target: 'aarch64-pc-windows-msvc',
    sha256: 'a7427ea0440bb826b6716d1837ff3d173b8e7d496cb09ee8f456b4e023a2fdcd',
  },
});

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    throw new Error(`cannot read ${file}: ${error.message}`);
  }
}

function sha256File(file) {
  const hash = crypto.createHash('sha256');
  hash.update(fs.readFileSync(file));
  return hash.digest('hex');
}

function inspectVendor(packageRoot, expectedVersion) {
  const vendorRoot = path.join(packageRoot, 'vendor');
  const manifest = readJson(path.join(vendorRoot, 'manifest.json'));
  if (manifest.schema !== 1 || manifest.package !== 'engelbart-cli') {
    throw new Error('bundled backend manifest has an unsupported schema or package');
  }
  if (manifest.version !== expectedVersion) {
    throw new Error(`npm/backend version mismatch: ${expectedVersion} != ${manifest.version}`);
  }
  if (!/^[a-f0-9]{40}$|^[a-f0-9]{64}$/.test(manifest.sourceRevision || '')) {
    throw new Error('bundled backend manifest has an invalid source revision');
  }
  if (typeof manifest.wheel !== 'string' || path.basename(manifest.wheel) !== manifest.wheel
      || !/^human_compact-[A-Za-z0-9_.+-]+-py3-none-any\.whl$/.test(manifest.wheel)) {
    throw new Error('bundled backend manifest names an invalid wheel');
  }
  if (!/^[a-f0-9]{64}$/.test(manifest.sha256 || '')) {
    throw new Error('bundled backend manifest has an invalid SHA-256');
  }
  const expectedWheelPrefix = `human_compact-${manifest.version.replace(/-/g, '_')}-`;
  if (!manifest.wheel.startsWith(expectedWheelPrefix)) {
    throw new Error('bundled backend wheel filename does not match its version');
  }
  const wheelPath = path.join(vendorRoot, manifest.wheel);
  if (!fs.statSync(wheelPath, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`bundled backend wheel is missing: ${manifest.wheel}`);
  }
  const actual = sha256File(wheelPath);
  if (!crypto.timingSafeEqual(Buffer.from(actual), Buffer.from(manifest.sha256))) {
    throw new Error('bundled backend wheel failed its SHA-256 check');
  }
  return { ...manifest, wheelPath };
}

function isMusl(reportProvider) {
  try {
    const report = reportProvider
      ? reportProvider()
      : process.report?.getReport?.();
    return !report?.header?.glibcVersionRuntime;
  } catch {
    return false;
  }
}

function supportedTarget(platform, arch, reportProvider) {
  // Bun and uv both ship native Windows ARM64 binaries. Keep the architecture
  // explicit rather than silently selecting the slower x64 emulation layer.
  const archs = ['arm64', 'x64'];
  if (!['darwin', 'linux', 'win32'].includes(platform) || !archs.includes(arch)) {
    throw new Error(`unsupported platform: ${platform}-${arch}; human-compact supports macOS and Linux on arm64/x64, and Windows on x64`);
  }
  const libc = platform === 'linux' ? (isMusl(reportProvider) ? 'musl' : 'gnu') : null;
  const key = [platform, arch, libc].filter(Boolean).join('-');
  const asset = UV_ASSETS[key];
  if (!asset) throw new Error(`no pinned uv bootstrap for ${key}`);
  // uv ships Windows as a .zip, every other target as .tar.gz.
  const extension = platform === 'win32' ? 'zip' : 'tar.gz';
  return {
    ...asset,
    key,
    name: key,
    platform,
    extension,
    file: `uv-${asset.target}.${extension}`,
    url: `${UV_RELEASE}/uv-${asset.target}.${extension}`,
  };
}

function safeChild(root, child) {
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(child);
  if (resolved === resolvedRoot || !resolved.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new Error(`refusing unsafe managed path: ${resolved}`);
  }
  return resolved;
}

function removeManaged(root, child) {
  fs.rmSync(safeChild(root, child), { recursive: true, force: true });
}

function atomicWrite(file, contents, mode = 0o600) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.tmp-${process.pid}-${crypto.randomBytes(5).toString('hex')}`;
  fs.writeFileSync(temporary, contents, { mode });
  atomicReplace(temporary, file);
}

function validateManagedRoot(root) {
  const resolved = path.resolve(root);
  const parsed = path.parse(resolved);
  if (resolved === parsed.root) throw new Error('managed runtime directory cannot be a filesystem root');
  return resolved;
}

function ensureManagedDirectory(root, relative) {
  const resolvedRoot = path.resolve(root);
  let current = resolvedRoot;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    current = safeChild(resolvedRoot, path.join(current, part));
    const existing = lstatIfPresent(current);
    if (existing) {
      if (existing.isSymbolicLink() || !existing.isDirectory()) {
        throw new Error(`managed directory component is not a real directory: ${current}`);
      }
    } else {
      fs.mkdirSync(current, { mode: 0o700 });
    }
  }
  return current;
}

function establishOwnership(root) {
  const markerPath = path.join(root, '.owner.json');
  const existing = lstatIfPresent(root);
  if (existing?.isSymbolicLink()) {
    throw new Error(`managed runtime directory cannot be a symlink: ${root}`);
  }
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const markerStat = lstatIfPresent(markerPath);
  if (markerStat) {
    if (markerStat.isSymbolicLink() || !markerStat.isFile()) {
      throw new Error(`managed ownership marker is not a regular file: ${markerPath}`);
    }
    const marker = readJson(markerPath);
    if (!ownedByUs(marker.owner) || marker.schema !== INSTALL_SCHEMA) {
      throw new Error(`${root} is not owned by human-compact`);
    }
    if (marker.owner !== OWNER) {
      atomicWrite(markerPath, `${JSON.stringify({ owner: OWNER, schema: INSTALL_SCHEMA }, null, 2)}\n`);
    }
  } else {
    const legacyEntries = new Set(['state', '.DS_Store']);
    const unowned = fs.readdirSync(root).filter((name) => !legacyEntries.has(name));
    if (unowned.length) {
      throw new Error(`${root} is non-empty and has no human-compact ownership marker`);
    }
    atomicWrite(markerPath, `${JSON.stringify({ owner: OWNER, schema: INSTALL_SCHEMA }, null, 2)}\n`);
  }
  fs.chmodSync(root, 0o700);
}

function pidAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === 'EPERM';
  }
}

function acquireLock(root, now = () => Date.now()) {
  const lock = path.join(root, 'install.lock');
  const attempt = () => {
    const existing = lstatIfPresent(lock);
    if (existing && (existing.isSymbolicLink() || !existing.isDirectory())) {
      throw new Error(`managed install lock is not a real directory: ${lock}`);
    }
    try {
      fs.mkdirSync(lock, { mode: 0o700 });
      atomicWrite(path.join(lock, 'owner.json'), `${JSON.stringify({
        owner: OWNER,
        pid: process.pid,
        createdAt: new Date(now()).toISOString(),
      }, null, 2)}\n`);
      return;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
    let owner = null;
    try { owner = readJson(path.join(lock, 'owner.json')); } catch {}
    let created = owner?.createdAt ? Date.parse(owner.createdAt) : NaN;
    if (!Number.isFinite(created)) {
      try { created = fs.statSync(lock).mtimeMs; } catch { created = now(); }
    }
    const age = now() - created;
    if (owner?.owner === OWNER && !pidAlive(owner.pid) && age > 30 * 60 * 1000) {
      removeManaged(root, lock);
      fs.mkdirSync(lock, { mode: 0o700 });
      atomicWrite(path.join(lock, 'owner.json'), `${JSON.stringify({
        owner: OWNER,
        pid: process.pid,
        createdAt: new Date(now()).toISOString(),
      }, null, 2)}\n`);
      return;
    }
    throw new Error('another human-compact installation is active');
  };
  attempt();
  return () => removeManaged(root, lock);
}

function runCommand(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env || process.env,
    encoding: 'utf8',
    stdio: options.stdio || ['ignore', 'pipe', 'pipe'],
  });
  if (result.error) throw new Error(`could not run ${command}: ${result.error.message}`);
  return result;
}

function checkedCommand(runner, command, args, options, description) {
  const result = runner(command, args, options);
  if (result.status !== 0) {
    const detail = String(result.stderr || result.stdout || '').trim();
    throw new Error(`${description} failed${detail ? `: ${detail}` : ''}`);
  }
  return result;
}

function parseClaudeVersion(output) {
  const match = String(output || '').match(
    /^\s*(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\s+\(Claude Code\)\s*$/,
  );
  if (!match) return null;
  const version = match.slice(1).map(Number);
  return version.every(Number.isSafeInteger) ? version : null;
}

function compareVersions(left, right) {
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] < right[index] ? -1 : 1;
  }
  return 0;
}

function requireCompatibleClaude(output) {
  const raw = String(output || '').trim();
  const version = parseClaudeVersion(raw);
  if (!version) {
    throw new Error(
      `unsupported Claude Code version output ${JSON.stringify(raw || '(empty)')}; `
      + `human-compact requires Claude Code ${MIN_CLAUDE_VERSION_TEXT} or newer`,
    );
  }
  const installed = version.join('.');
  if (compareVersions(version, MIN_CLAUDE_VERSION) < 0) {
    throw new Error(
      `Claude Code ${installed} is too old; `
      + `human-compact requires Claude Code ${MIN_CLAUDE_VERSION_TEXT} or newer`,
    );
  }
  return installed;
}

function compatiblePython(runner, command) {
  const result = runner(command, [
    '-c',
    'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  return result.status === 0;
}

function pythonCandidates(env) {
  const candidates = [env.HUMAN_COMPACT_PYTHON, 'python3', 'python'].filter(Boolean);
  return [...new Set(candidates)];
}

function runtimeExecutables(runtime, platform = process.platform) {
  const bin = venvBinDir(platform);
  return {
    python: path.join(runtime, bin, exeName('python', platform)),
    hc: path.join(runtime, bin, exeName('hc', platform)),
    bart: path.join(runtime, bin, exeName('bart', platform)),
  };
}

// A launcher path that child_process can spawn directly. On POSIX the stable
// bin/<name> symlink is spawnable and upgrade-stable, so it is the answer. On
// Windows the stable launcher is a bin\<name>.cmd shim, which Node cannot spawn
// without a shell (and won't at all since the .bat/.cmd hardening), so resolve
// the runtime's real .exe from the owned install manifest instead.
function spawnableLauncher(managedRoot, name = 'hc', platform = process.platform) {
  const root = validateManagedRoot(managedRoot);
  if (!isWindows(platform)) return path.join(root, 'bin', name);
  const install = loadOwnedInstall(root);
  if (!install || typeof install.runtime !== 'string') {
    throw new Error('no owned install manifest to resolve a Windows launcher from');
  }
  const runtime = safeChild(root, install.runtime);
  const exe = runtimeExecutables(runtime, platform)[name];
  if (!exe) throw new Error(`unknown launcher: ${name}`);
  return exe;
}

function validateRuntime(runner, runtime, version, platform = process.platform) {
  const { python, hc, bart } = runtimeExecutables(runtime, platform);
  if (!fs.statSync(python, { throwIfNoEntry: false })?.isFile()
      || !fs.statSync(hc, { throwIfNoEntry: false })?.isFile()
      || !fs.statSync(bart, { throwIfNoEntry: false })?.isFile()) return false;
  try {
    const result = runner(python, [
      '-c',
      // The Python distribution is named for the module it ships, not for the
      // npm package that carries it. These are deliberately different names.
      `import importlib.metadata; raise SystemExit(0 if importlib.metadata.version("human-compact") == ${JSON.stringify(version)} else 1)`,
    ], { stdio: ['ignore', 'pipe', 'pipe'] });
    return result.status === 0;
  } catch {
    return false;
  }
}

function createRuntimeWithPython(runner, python, staging, wheelPath, version, env, platform = process.platform) {
  let result = runner(python, ['-m', 'venv', staging], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  if (result.status !== 0) return false;
  const runtimePython = runtimeExecutables(staging, platform).python;
  result = runner(runtimePython, [
    '-m', 'pip', 'install', '--disable-pip-version-check', '--no-index', '--no-deps', wheelPath,
  ], { env, stdio: ['ignore', 'pipe', 'pipe'] });
  return result.status === 0 && validateRuntime(runner, staging, version, platform);
}

function downloadHttps(url, destination, redirects = 0) {
  if (redirects > 5) return Promise.reject(new Error('too many redirects downloading uv'));
  return new Promise((resolve, reject) => {
    const request = https.get(url, {
      headers: { 'User-Agent': 'human-compact-installer' },
      timeout: 30_000,
    }, (response) => {
      if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
        const location = response.headers.location;
        response.resume();
        if (!location) return reject(new Error('uv download redirect had no location'));
        const redirect = new URL(location, url);
        if (redirect.protocol !== 'https:') return reject(new Error('refusing non-HTTPS uv redirect'));
        return downloadHttps(redirect.href, destination, redirects + 1).then(resolve, reject);
      }
      if (response.statusCode !== 200) {
        response.resume();
        return reject(new Error(`uv download returned HTTP ${response.statusCode}`));
      }
      const declaredLength = Number(response.headers['content-length']);
      if (Number.isFinite(declaredLength) && declaredLength > MAX_UV_ARCHIVE_BYTES) {
        response.resume();
        return reject(new Error('uv download exceeded the installer size limit'));
      }
      const file = fs.createWriteStream(destination, { flags: 'wx', mode: 0o600 });
      let received = 0;
      response.on('data', (chunk) => {
        received += chunk.length;
        if (received > MAX_UV_ARCHIVE_BYTES) {
          response.destroy(new Error('uv download exceeded the installer size limit'));
        }
      });
      response.pipe(file);
      file.on('finish', () => file.close(resolve));
      file.on('error', reject);
      response.on('error', reject);
      return undefined;
    });
    request.on('timeout', () => request.destroy(new Error('uv download timed out')));
    request.on('error', reject);
  });
}

function findFile(root, basename, depth = 0) {
  if (depth > 3) return null;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const candidate = path.join(root, entry.name);
    if (entry.isFile() && entry.name === basename) return candidate;
    if (entry.isDirectory()) {
      const nested = findFile(candidate, basename, depth + 1);
      if (nested) return nested;
    }
  }
  return null;
}

async function ensureUv(options) {
  const { root, target, runner, download = downloadHttps } = options;
  const platform = options.platform || target.platform || process.platform;
  const windows = isWindows(platform);
  const uvName = exeName('uv', platform);
  ensureManagedDirectory(root, path.join('tools', 'uv', UV_VERSION));
  ensureManagedDirectory(root, 'tmp');
  const toolRoot = path.join(root, 'tools', 'uv', UV_VERSION, target.key);
  const executable = path.join(toolRoot, uvName);
  const toolStat = lstatIfPresent(toolRoot);
  if (toolStat && (toolStat.isSymbolicLink() || !toolStat.isDirectory())) {
    throw new Error(`managed uv path is not a real directory: ${toolRoot}`);
  }
  if (fs.statSync(executable, { throwIfNoEntry: false })?.isFile()) {
    try {
      const manifest = readJson(path.join(toolRoot, 'manifest.json'));
      // On Windows executability is the .exe extension, not a mode bit.
      const runnable = windows || (fs.statSync(executable).mode & 0o111) !== 0;
      if (ownedByUs(manifest.owner) && manifest.version === UV_VERSION
          && manifest.target === target.key
          && manifest.archiveSha256 === target.sha256
          && runnable
          && sha256File(executable) === manifest.binarySha256) return executable;
    } catch {}
  }
  if (fs.existsSync(toolRoot)) removeManaged(root, toolRoot);
  const temporary = path.join(root, 'tmp', `uv-${process.pid}-${crypto.randomBytes(5).toString('hex')}`);
  const archive = path.join(temporary, target.file);
  const extracted = path.join(temporary, 'extract');
  fs.mkdirSync(extracted, { recursive: true, mode: 0o700 });
  try {
    await download(target.url, archive);
    if (sha256File(archive) !== target.sha256) throw new Error('downloaded uv archive failed its pinned SHA-256 check');
    // bsdtar (the `tar` on Windows and macOS) reads a .zip; the -z flag is
    // gzip-specific, so drop it for the Windows zip and let tar autodetect.
    const tarFlags = target.extension === 'zip'
      ? ['-xf', archive, '-C', extracted]
      : ['-xzf', archive, '-C', extracted];
    checkedCommand(runner, 'tar', tarFlags, {
      stdio: ['ignore', 'pipe', 'pipe'],
    }, 'uv archive extraction');
    const source = findFile(extracted, uvName);
    if (!source) throw new Error('uv archive did not contain the uv executable');
    const stagedTool = `${toolRoot}.tmp-${process.pid}`;
    removeManaged(root, stagedTool);
    fs.mkdirSync(stagedTool, { recursive: true, mode: 0o700 });
    fs.copyFileSync(source, path.join(stagedTool, uvName));
    fs.chmodSync(path.join(stagedTool, uvName), 0o700);
    atomicWrite(path.join(stagedTool, 'manifest.json'), `${JSON.stringify({
      owner: OWNER,
      version: UV_VERSION,
      target: target.key,
      archiveSha256: target.sha256,
      binarySha256: sha256File(path.join(stagedTool, uvName)),
    }, null, 2)}\n`);
    atomicReplace(stagedTool, toolRoot);
    return executable;
  } finally {
    removeManaged(root, temporary);
  }
}

async function buildRuntime(options) {
  const { root, staging, vendor, target, runner, env, output, download } = options;
  const platform = options.platform || target.platform || process.platform;
  for (const candidate of pythonCandidates(env)) {
    let compatible = false;
    try { compatible = compatiblePython(runner, candidate); } catch {}
    if (!compatible) continue;
    output.write(`  building the runtime with ${candidate}\u2026\n`);
    try {
      if (createRuntimeWithPython(runner, candidate, staging, vendor.wheelPath, vendor.version, env, platform)) return;
    } catch {}
    removeManaged(root, staging);
  }
  output.write(`  no usable python venv; bootstrapping uv ${UV_VERSION}\u2026\n`);
  const uv = await ensureUv({ root, target, runner, download, platform });
  ensureManagedDirectory(root, path.join('cache', 'uv'));
  ensureManagedDirectory(root, 'python');
  const uvEnv = {
    ...env,
    UV_CACHE_DIR: path.join(root, 'cache', 'uv'),
    UV_PYTHON_INSTALL_DIR: path.join(root, 'python'),
    UV_PYTHON_DOWNLOADS: 'automatic',
    UV_NO_PROGRESS: '1',
  };
  checkedCommand(runner, uv, ['venv', '--python', '3.12', staging], {
    env: uvEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
  }, 'managed Python creation');
  checkedCommand(runner, uv, [
    'pip', 'install', '--python', runtimeExecutables(staging, platform).python,
    '--no-index', '--no-deps', vendor.wheelPath,
  ], { env: uvEnv, stdio: ['ignore', 'pipe', 'pipe'] }, 'backend installation');
  if (!validateRuntime(runner, staging, vendor.version, platform)) {
    throw new Error('installed backend failed its version check');
  }
}

function loadOwnedInstall(root) {
  const file = path.join(root, 'install.json');
  const stat = lstatIfPresent(file);
  if (!stat) return null;
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`${file} is not a regular owned install manifest`);
  }
  const manifest = readJson(file);
  if (!ownedByUs(manifest.owner) || manifest.schema !== INSTALL_SCHEMA) {
    throw new Error(`${file} is not an owned human-compact install manifest`);
  }
  return manifest;
}

function lstatIfPresent(file) {
  try {
    return fs.lstatSync(file);
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

function switchLauncher(root, targetHc, previousInstall, targetBart = null, platform = process.platform) {
  const windows = isWindows(platform);
  const bin = ensureManagedDirectory(root, 'bin');
  fs.mkdirSync(bin, { recursive: true, mode: 0o700 });
  const targetDir = path.dirname(targetHc);
  const bartTarget = targetBart || path.join(targetDir, exeName('bart', platform));
  const launcher = path.join(bin, launcherName('hc', platform));
  const bartLauncher = path.join(bin, launcherName('bart', platform));
  // Logical launcher name -> the runtime executable it must resolve to, plus the
  // install-manifest field that records its stable path.
  const specs = [
    { name: 'hc', stable: launcher, target: targetHc, field: 'launcher' },
    { name: 'bart', stable: bartLauncher, target: bartTarget, field: 'bartLauncher' },
  ];

  // Write / read / atomically place a launcher, in the shape the platform uses:
  // a symlink on POSIX, a `.cmd` shim on Windows.
  const writeLauncherAt = (file, target) => {
    if (windows) fs.writeFileSync(file, launcherShim(target), { mode: 0o700 });
    else fs.symlinkSync(target, file);
  };
  const placeLauncher = (temporary, stable) => {
    // renameSync already replaces a POSIX symlink atomically; Windows must clear
    // the destination first (atomicReplace does).
    if (windows) atomicReplace(temporary, stable);
    else fs.renameSync(temporary, stable);
  };

  const previousTargets = new Map();
  for (const spec of specs) {
    const { stable, target, field } = spec;
    const launcherStat = lstatIfPresent(stable);
    if (!launcherStat) {
      previousTargets.set(stable, null);
      continue;
    }
    if (!previousInstall) throw new Error(`refusing to overwrite unmanaged launcher: ${stable}`);
    if (previousInstall[field] !== stable
        || typeof previousInstall.runtime !== 'string') {
      throw new Error('owned install manifest does not match its stable launcher');
    }
    const previousRuntime = safeChild(root, previousInstall.runtime);
    const expectedTarget = path.join(previousRuntime, venvBinDir(platform), exeName(spec.name, platform));
    let previousTarget;
    if (windows) {
      if (!launcherStat.isFile()) throw new Error(`owned launcher is not a shim: ${stable}`);
      previousTarget = launcherShimTarget(fs.readFileSync(stable, 'utf8'));
      if (!previousTarget) throw new Error(`owned launcher is not a recognizable shim: ${stable}`);
    } else {
      if (!launcherStat.isSymbolicLink()) throw new Error(`owned launcher is not a symlink: ${stable}`);
      previousTarget = fs.readlinkSync(stable);
    }
    if (!path.isAbsolute(previousTarget)
        || path.resolve(previousTarget) !== path.resolve(expectedTarget)) {
      throw new Error('owned stable launcher target does not match the install manifest');
    }
    previousTargets.set(stable, previousTarget);
    void target;
  }

  const temporaries = new Map();
  const switched = [];
  const restore = (stable) => {
    const previous = previousTargets.get(stable);
    if (previous === null) {
      fs.rmSync(stable, { force: true });
      return;
    }
    const rollback = path.join(
      bin,
      `.${path.basename(stable)}.rollback-${process.pid}-${crypto.randomBytes(5).toString('hex')}`,
    );
    writeLauncherAt(rollback, previous);
    placeLauncher(rollback, stable);
  };
  try {
    for (const spec of specs) {
      const temporary = path.join(
        bin,
        `.${path.basename(spec.stable)}.tmp-${process.pid}-${crypto.randomBytes(5).toString('hex')}`,
      );
      writeLauncherAt(temporary, spec.target);
      temporaries.set(spec.stable, temporary);
    }
    for (const spec of specs) {
      placeLauncher(temporaries.get(spec.stable), spec.stable);
      switched.push(spec.stable);
    }
  } catch (error) {
    for (const temporary of temporaries.values()) fs.rmSync(temporary, { force: true });
    for (const stable of switched.reverse()) restore(stable);
    throw error;
  }
  return {
    launcher,
    bartLauncher,
    rollback() {
      restore(bartLauncher);
      restore(launcher);
    },
  };
}

async function install(options) {
  const root = validateManagedRoot(options.managedRoot);
  const deps = options.deps || {};
  const runner = deps.runCommand || runCommand;
  const output = options.output || process.stdout;
  const errorOutput = options.errorOutput || process.stderr;
  const env = deps.env || process.env;
  const target = supportedTarget(options.platform, options.arch, options.processReport);
  const platform = target.platform;
  const vendor = inspectVendor(options.packageRoot, options.packageVersion);
  let claude;
  try {
    claude = runner('claude', ['--version'], {
      env,
      timeout: 20000,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    throw new Error('Claude Code is required and was not found: '
      + `${error.message}\nInstall it with: curl -fsSL https://claude.ai/install.sh | bash`);
  }
  if (claude.status !== 0) {
    throw new Error('Claude Code is required; install it and ensure `claude` is on PATH.\n'
      + 'Install it with: curl -fsSL https://claude.ai/install.sh | bash');
  }
  const claudeVersionOutput = String(claude.stdout || '').trim()
    || String(claude.stderr || '').trim();
  requireCompatibleClaude(claudeVersionOutput);
  establishOwnership(root);
  const releaseLock = acquireLock(root, deps.now);
  let createdRuntime = false;
  let staging = null;
  try {
    const previousInstall = loadOwnedInstall(root);
    const runtimeName = `${vendor.version}-${vendor.sha256.slice(0, 12)}`;
    ensureManagedDirectory(root, 'runtimes');
    const runtime = path.join(root, 'runtimes', runtimeName);
    const runtimeStat = lstatIfPresent(runtime);
    if (runtimeStat && (runtimeStat.isSymbolicLink() || !runtimeStat.isDirectory())) {
      throw new Error(`managed runtime path is not a real directory: ${runtime}`);
    }
    if (!validateRuntime(runner, runtime, vendor.version, platform)) {
      if (fs.existsSync(runtime)) removeManaged(root, runtime);
      // Python venv console scripts contain absolute shebangs. Build at the
      // immutable final path; it remains unreachable until the launcher swap.
      staging = runtime;
      fs.mkdirSync(path.dirname(runtime), { recursive: true, mode: 0o700 });
      await (deps.buildRuntime || buildRuntime)({
        root,
        staging,
        vendor,
        target,
        runner,
        env,
        output,
        download: deps.download,
        platform,
      });
      if (!validateRuntime(runner, staging, vendor.version, platform)) throw new Error('new runtime failed validation');
      staging = null;
      createdRuntime = true;
    } else {
    }

    let switched;
    const executables = runtimeExecutables(runtime, platform);
    try {
      switched = switchLauncher(root, executables.hc, previousInstall, executables.bart, platform);
    } catch (error) {
      if (createdRuntime) removeManaged(root, runtime);
      throw error;
    }
    const setupArgs = [
      'setup',
      '--global-vault', options.choices.globalVault === '1' ? 'yes' : 'keep',
      '--goals', options.choices.goals === '1' ? 'yes' : 'no',
    ];
    // The stable launcher is a `.cmd` shim on Windows, which spawnSync cannot
    // execute without a shell. Drive setup through the runtime's real .exe;
    // HC_EXECUTABLE still names the shim the user actually runs on POSIX, and
    // the real exe on Windows so the backend can re-invoke itself directly.
    const spawnLauncher = isWindows(platform) ? executables.hc : switched.launcher;
    const setupEnv = { ...env, HC_EXECUTABLE: spawnLauncher };
    const baseManifest = {
      owner: OWNER,
      schema: INSTALL_SCHEMA,
      npmVersion: options.packageVersion,
      backendVersion: vendor.version,
      wheel: vendor.wheel,
      wheelSha256: vendor.sha256,
      runtime,
      launcher: switched.launcher,
      bartLauncher: switched.bartLauncher,
      globalVault: options.choices.globalVault === '1',
      goalsRequested: options.choices.goals === '1',
      setupStatus: 'pending',
      installedAt: new Date((deps.now || Date.now)()).toISOString(),
    };
    try {
      atomicWrite(path.join(root, 'install.json'), `${JSON.stringify(baseManifest, null, 2)}\n`);
    } catch (error) {
      switched.rollback();
      if (createdRuntime) removeManaged(root, runtime);
      throw error;
    }

    let setup;
    try {
      setup = runner(spawnLauncher, setupArgs, {
        env: setupEnv,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      if (setup.error || setup.status !== 0) {
        // Only now is its output worth showing: it says what went wrong.
        const detail = `${setup.stdout || ''}${setup.stderr || ''}`.trim();
        if (detail) errorOutput.write(`${detail}\n`);
        throw new Error(`hc setup failed${setup.error ? `: ${setup.error.message}` : ` with exit code ${setup.status}`}`);
      }
      atomicWrite(path.join(root, 'install.json'), `${JSON.stringify({
        ...baseManifest,
        goalsBuilt: options.choices.goals === '1',
        setupStatus: 'complete',
      }, null, 2)}\n`);
    } catch (error) {
      // The base install is usable even when optional Vault import/inference
      // fails. Keep its stable launcher and record a repairable setup state.
      atomicWrite(path.join(root, 'install.json'), `${JSON.stringify({
        ...baseManifest,
        setupStatus: 'failed',
        setupExitCode: Number.isInteger(setup?.status) ? setup.status : null,
      }, null, 2)}\n`);
      throw error;
    }
    output.write(`  runtime      ${vendor.version}\n  Claude Code  plugin and /bart installed\n`);
    return {
      runtime,
      launcher: switched.launcher,
      bartLauncher: switched.bartLauncher,
    };
  } finally {
    if (staging) removeManaged(root, staging);
    releaseLock();
  }
}

// The launcher is installed to a directory the tool owns; nothing has ever put
// that directory on PATH. Telling the user to run `hc ui` when their shell
// cannot find `hc` makes a one-command install fail at its last step.
function shellProfileFor(env, homedir) {
  const shell = path.basename(String(env.SHELL || ''));
  if (shell === 'zsh') {
    return path.join(env.ZDOTDIR || homedir, '.zshrc');
  }
  if (shell === 'bash') {
    return path.join(homedir, '.bashrc');
  }
  return null;                       // fish and friends: instruct, do not edit
}

function ensureLauncherOnPath({ launcherDir, env, homedir, fileSystem, platform }) {
  const files = fileSystem || fs;
  const entries = String(env.PATH || '').split(path.delimiter).filter(Boolean);
  if (isWindows(platform)) {
    // Windows PATH matching is case-insensitive, and there is no symlink-into-
    // an-existing-bin shortcut: the launcher dir is the tool's own directory.
    const wanted = path.resolve(launcherDir).toLowerCase();
    if (entries.some((entry) => path.resolve(entry).toLowerCase() === wanted)) {
      return { onPath: true, profile: null, added: false, linked: null, line: null };
    }
    // A child cannot change its parent shell's PATH; setx persists it for new
    // terminals, which is the most a one-shot installer can safely do without
    // touching the user's registry ourselves.
    const line = `setx PATH "%PATH%;${launcherDir}"`;
    return { onPath: false, profile: null, added: false, linked: null, line };
  }
  if (entries.some((entry) => path.resolve(entry) === path.resolve(launcherDir))) {
    return { onPath: true, profile: null, added: false, linked: null, line: null };
  }
  // A child process cannot change its parent shell's PATH, so editing a
  // profile always costs the user a new terminal. Linking into a directory
  // the shell already searches costs them nothing: `hc` works immediately.
  const launcherNames = ['hc', 'bart'];
  for (const entry of entries) {
    const dir = path.resolve(entry);
    if (dir !== path.resolve(homedir, '.local', 'bin')
        && dir !== path.resolve(homedir, 'bin')) continue;
    const states = [];
    let usable = true;
    for (const name of launcherNames) {
      const source = path.join(launcherDir, name);
      const link = path.join(dir, name);
      try {
        const existing = files.lstatSync(link);
        if (!existing.isSymbolicLink()
            || path.resolve(files.readlinkSync(link)) !== path.resolve(source)) {
          usable = false;
          break;
        }
        states.push({ source, link, missing: false });
      } catch (error) {
        if (error.code !== 'ENOENT') {
          usable = false;
          break;
        }
        states.push({ source, link, missing: true });
      }
    }
    if (!usable) continue;
    const created = [];
    try {
      for (const state of states.filter((item) => item.missing)) {
        files.symlinkSync(state.source, state.link);
        created.push(state.link);
      }
      return {
        onPath: true,
        profile: null,
        added: false,
        linked: states.map((state) => state.link),
        line: null,
      };
    } catch (error) {
      for (const link of created) {
        try { files.rmSync(link, { force: true }); } catch {}
      }
      // Unwritable or racing another install: fall through to the profile.
    }
  }
  const relative = launcherDir.startsWith(homedir + path.sep)
    ? '$HOME' + launcherDir.slice(homedir.length)
    : launcherDir;
  const line = `export PATH="${relative}:$PATH"`;
  const profile = shellProfileFor(env, homedir);
  if (!profile) return { onPath: false, profile: null, added: false, linked: null, line };
  let existing = '';
  try {
    existing = files.readFileSync(profile, 'utf8');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  if (existing.includes(launcherDir) || existing.includes(relative)) {
    return { onPath: false, profile, added: false, present: true, linked: null, line };
  }
  files.appendFileSync(profile, `\n# engelbart-cli (runtime on PATH)\n${line}\n`);
  return { onPath: false, profile, added: true, linked: null, line };
}

module.exports = {
  INSTALL_SCHEMA,
  ensureLauncherOnPath,
  isWindows,
  venvBinDir,
  exeName,
  launcherName,
  launcherShim,
  launcherShimTarget,
  atomicReplace,
  shellProfileFor,
  MIN_CLAUDE_VERSION,
  MIN_CLAUDE_VERSION_TEXT,
  OWNER,
  UV_ASSETS,
  UV_VERSION,
  acquireLock,
  atomicWrite,
  buildRuntime,
  compatiblePython,
  downloadHttps,
  ensureUv,
  ensureManagedDirectory,
  establishOwnership,
  inspectVendor,
  install,
  isMusl,
  loadOwnedInstall,
  pythonCandidates,
  parseClaudeVersion,
  requireCompatibleClaude,
  removeManaged,
  runCommand,
  runtimeExecutables,
  safeChild,
  spawnableLauncher,
  sha256File,
  supportedTarget,
  switchLauncher,
  validateManagedRoot,
  validateRuntime,
};
