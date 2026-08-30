'use strict';

const assert = require('assert/strict');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const test = require('node:test');

const {
  INSTALL_SCHEMA,
  OWNER,
  ensureUv,
  ensureManagedDirectory,
  ensureLauncherOnPath,
  establishOwnership,
  inspectVendor,
  install,
  parseClaudeVersion,
  requireCompatibleClaude,
  runtimeExecutables,
  safeChild,
  spawnableLauncher,
  supportedTarget,
  switchLauncher,
  launcherShim,
  launcherShimTarget,
  venvBinDir,
  exeName,
  launcherName,
} = require('../lib/installer');

// POSIX symlinks, mode bits, and the shell-PATH branch (which uses the host
// path.delimiter and resolve semantics) cannot be reproduced on a Windows
// filesystem; those tests are host-gated. win32 behavior has its own tests.
const WINDOWS_HOST = process.platform === 'win32';

function capture() {
  let value = '';
  return {
    stream: { write(chunk) { value += chunk; } },
    read() { return value; },
  };
}

function writeVendor(packageRoot, body = 'wheel-a') {
  const vendor = path.join(packageRoot, 'vendor');
  fs.mkdirSync(vendor, { recursive: true });
  const wheel = 'human_compact-0.16.0-py3-none-any.whl';
  fs.writeFileSync(path.join(vendor, wheel), body);
  fs.writeFileSync(path.join(vendor, 'manifest.json'), JSON.stringify({
    schema: 1,
    package: 'engelbart-cli',
    version: '0.16.0',
    wheel,
    sha256: crypto.createHash('sha256').update(body).digest('hex'),
    sourceRevision: '1'.repeat(40),
  }));
}

function fakeRuntime(staging) {
  fs.mkdirSync(path.join(staging, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(staging, 'bin', 'python'), '#!/bin/sh\n');
  fs.writeFileSync(path.join(staging, 'bin', 'hc'), '#!/bin/sh\n');
  fs.writeFileSync(path.join(staging, 'bin', 'bart'), '#!/bin/sh\n');
  fs.chmodSync(path.join(staging, 'bin', 'python'), 0o700);
  fs.chmodSync(path.join(staging, 'bin', 'hc'), 0o700);
  fs.chmodSync(path.join(staging, 'bin', 'bart'), 0o700);
}

function installOptions(packageRoot, managedRoot, calls, setupStatus = 0) {
  return {
    packageRoot,
    packageVersion: '0.16.0',
    managedRoot,
    choices: { globalVault: '1', goals: '2' },
    platform: 'darwin',
    arch: 'arm64',
    output: capture().stream,
    errorOutput: capture().stream,
    deps: {
      now: () => Date.UTC(2026, 7, 12),
      async buildRuntime({ staging }) { fakeRuntime(staging); },
      runCommand(command, args, options) {
        calls.push({ command, args, options });
        if (args[0] === 'setup') return { status: setupStatus };
        return { status: 0, stdout: '2.1.175 (Claude Code)\n', stderr: '' };
      },
    },
  };
}

test('supportedTarget pins Darwin, glibc, and musl archives', () => {
  assert.equal(supportedTarget('darwin', 'arm64').sha256.length, 64);
  assert.equal(
    supportedTarget('linux', 'x64', () => ({ header: { glibcVersionRuntime: '2.39' } })).key,
    'linux-x64-gnu',
  );
  assert.equal(
    supportedTarget('linux', 'arm64', () => ({ header: {} })).key,
    'linux-arm64-musl',
  );
  // Windows x64 is a supported target: a pinned .zip uv archive, no arm64.
  const win = supportedTarget('win32', 'x64');
  assert.equal(win.key, 'win32-x64');
  assert.equal(win.platform, 'win32');
  assert.equal(win.extension, 'zip');
  assert.equal(win.sha256.length, 64);
  assert.ok(win.file.endsWith('.zip'));
  assert.throws(() => supportedTarget('win32', 'arm64'), /unsupported platform/);
});

test('inspectVendor rejects npm/backend skew and tampering', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-vendor-test-'));
  try {
    writeVendor(root);
    assert.equal(inspectVendor(root, '0.16.0').version, '0.16.0');
    assert.throws(() => inspectVendor(root, '0.17.0'), /version mismatch/);
    fs.appendFileSync(path.join(root, 'vendor', 'human_compact-0.16.0-py3-none-any.whl'), 'tamper');
    assert.throws(() => inspectVendor(root, '0.16.0'), /SHA-256/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('Claude Code version parsing is strict, suffix-aware, and fail-closed', () => {
  assert.deepEqual(parseClaudeVersion('2.1.175 (Claude Code)\n'), [2, 1, 175]);
  assert.deepEqual(parseClaudeVersion('2.2.0-beta.1+build.7 (Claude Code)'), [2, 2, 0]);
  assert.equal(parseClaudeVersion('claude 2.1.175'), null);
  assert.equal(requireCompatibleClaude('2.1.175 (Claude Code)'), '2.1.175');
  assert.throws(
    () => requireCompatibleClaude('2.1.174 (Claude Code)'),
    /Claude Code 2\.1\.174 is too old.*2\.1\.175 or newer/,
  );
  assert.throws(
    () => requireCompatibleClaude('development build'),
    /unsupported Claude Code version output.*2\.1\.175 or newer/,
  );
});

test('safeChild refuses root and sibling deletion targets', () => {
  const root = path.join(os.tmpdir(), 'managed-root');
  assert.equal(safeChild(root, path.join(root, 'runtimes')), path.join(root, 'runtimes'));
  assert.throws(() => safeChild(root, root), /unsafe/);
  assert.throws(() => safeChild(root, `${root}-sibling`), /unsafe/);
});

test('an install made under the old package name is adopted, not refused', () => {
  // The package was published as human-vault before it was named. That name
  // is written into the marker of every install it made, including the ones
  // on the machines of everybody who installed it -- so it is an on-disk
  // contract, not a brand. Refusing those directories would turn a rename
  // into a broken upgrade for exactly the earliest users.
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-rename-test-'));
  try {
    const root = path.join(fixture, 'managed');
    fs.mkdirSync(root, { recursive: true });
    fs.writeFileSync(path.join(root, '.owner.json'),
      `${JSON.stringify({ owner: 'human-vault', schema: 1 }, null, 2)}\n`);
    fs.mkdirSync(path.join(root, 'runtimes'), { recursive: true });

    assert.doesNotThrow(() => establishOwnership(root));
    assert.equal(
      JSON.parse(fs.readFileSync(path.join(root, '.owner.json'))).owner,
      'engelbart-cli',
      'adopting it should also bring the marker up to the current name');
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('ownership accepts legacy state but rejects unrelated unowned content', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-marker-test-'));
  try {
    const legacy = path.join(fixture, 'legacy');
    fs.mkdirSync(path.join(legacy, 'state'), { recursive: true });
    establishOwnership(legacy);
    assert.equal(JSON.parse(fs.readFileSync(path.join(legacy, '.owner.json'))).owner, 'engelbart-cli');

    const unrelated = path.join(fixture, 'unrelated');
    fs.mkdirSync(unrelated);
    fs.writeFileSync(path.join(unrelated, 'notes.txt'), 'mine');
    assert.throws(() => establishOwnership(unrelated), /non-empty/);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('managed directory checks reject symlinked components before cleanup', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-symlink-test-'));
  try {
    const root = path.join(fixture, 'managed');
    const outside = path.join(fixture, 'outside');
    fs.mkdirSync(root);
    fs.mkdirSync(outside);
    fs.symlinkSync(outside, path.join(root, 'runtimes'));
    assert.throws(
      () => ensureManagedDirectory(root, path.join('runtimes', 'release')),
      /not a real directory/,
    );
    assert.equal(fs.existsSync(outside), true);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('pinned uv bootstrap verifies the archive and repairs a corrupt managed binary', async () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-uv-test-'));
  try {
    const root = path.join(fixture, 'managed');
    fs.mkdirSync(root);
    const payload = path.join(fixture, 'payload', 'uv-test-target');
    fs.mkdirSync(payload, { recursive: true });
    fs.writeFileSync(path.join(payload, 'uv'), '#!/bin/sh\nexit 0\n', { mode: 0o700 });
    const archive = path.join(fixture, 'uv-test-target.tar.gz');
    const packed = spawnSync('tar', [
      '-czf', archive, '-C', path.join(fixture, 'payload'), 'uv-test-target',
    ], { encoding: 'utf8' });
    assert.equal(packed.status, 0, packed.stderr);
    const target = {
      key: 'test-target',
      file: path.basename(archive),
      url: 'https://example.invalid/uv.tar.gz',
      sha256: crypto.createHash('sha256').update(fs.readFileSync(archive)).digest('hex'),
    };
    let downloads = 0;
    const options = {
      root,
      target,
      platform: 'linux',
      runner(command, args, spawnOptions) {
        return spawnSync(command, args, { encoding: 'utf8', ...spawnOptions });
      },
      async download(url, destination) {
        assert.equal(url, target.url);
        downloads += 1;
        fs.copyFileSync(archive, destination);
      },
    };
    const executable = await ensureUv(options);
    assert.equal(downloads, 1);
    assert.equal(fs.readFileSync(executable, 'utf8'), '#!/bin/sh\nexit 0\n');
    fs.appendFileSync(executable, 'corrupt');
    assert.equal(await ensureUv(options), executable);
    assert.equal(downloads, 2);
    assert.equal(fs.readFileSync(executable, 'utf8'), '#!/bin/sh\nexit 0\n');
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('installer creates stable launcher, manifest, and exact setup argv', async () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-install-test-'));
  try {
    const packageRoot = path.join(fixture, 'package');
    const managedRoot = path.join(fixture, 'managed');
    writeVendor(packageRoot);
    const calls = [];
    const result = await install(installOptions(packageRoot, managedRoot, calls));
    assert.equal(fs.lstatSync(result.launcher).isSymbolicLink(), true);
    assert.equal(
      fs.realpathSync(result.launcher),
      fs.realpathSync(path.join(result.runtime, 'bin', 'hc')),
    );
    assert.equal(fs.lstatSync(result.bartLauncher).isSymbolicLink(), true);
    assert.equal(
      fs.realpathSync(result.bartLauncher),
      fs.realpathSync(path.join(result.runtime, 'bin', 'bart')),
    );
    const setup = calls.find((call) => call.args[0] === 'setup');
    assert.deepEqual(setup.args, [
      'setup', '--global-vault', 'yes', '--goals', 'no',
    ]);
    assert.equal(setup.options.env.HC_EXECUTABLE, path.join(managedRoot, 'bin', 'hc'));
    const manifest = JSON.parse(fs.readFileSync(path.join(managedRoot, 'install.json')));
    assert.equal(manifest.owner, 'engelbart-cli');
    assert.equal(manifest.backendVersion, '0.16.0');
    assert.equal(manifest.bartLauncher, path.join(managedRoot, 'bin', 'bart'));

    let rebuilt = false;
    const repeat = installOptions(packageRoot, managedRoot, [], 0);
    repeat.deps.buildRuntime = async () => { rebuilt = true; };
    await install(repeat);
    assert.equal(rebuilt, false);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('installer requires Claude Code before creating managed state', async () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-claude-test-'));
  try {
    const packageRoot = path.join(fixture, 'package');
    const managedRoot = path.join(fixture, 'managed');
    writeVendor(packageRoot);
    const options = installOptions(packageRoot, managedRoot, []);
    options.deps.runCommand = () => ({ status: 127, stdout: '', stderr: 'missing' });
    await assert.rejects(install(options), /Claude Code is required/);
    assert.equal(fs.existsSync(managedRoot), false);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('installer rejects incompatible Claude Code before creating managed state', async () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-claude-version-test-'));
  try {
    const packageRoot = path.join(fixture, 'package');
    const managedRoot = path.join(fixture, 'managed');
    writeVendor(packageRoot);
    const options = installOptions(packageRoot, managedRoot, []);
    options.deps.runCommand = () => ({
      status: 0, stdout: '2.1.150 (Claude Code)\n', stderr: '',
    });
    await assert.rejects(install(options), /2\.1\.150 is too old.*2\.1\.175 or newer/);
    assert.equal(fs.existsSync(managedRoot), false);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('failed setup keeps a usable base install and rerun repairs it', async () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-rollback-test-'));
  try {
    const packageRoot = path.join(fixture, 'package');
    const managedRoot = path.join(fixture, 'managed');
    writeVendor(packageRoot);
    await assert.rejects(
      install(installOptions(packageRoot, managedRoot, [], 7)),
      /exit code 7/,
    );
    const launcher = path.join(managedRoot, 'bin', 'hc');
    assert.equal(fs.lstatSync(launcher).isSymbolicLink(), true);
    assert.equal(fs.existsSync(fs.realpathSync(launcher)), true);
    let manifest = JSON.parse(fs.readFileSync(path.join(managedRoot, 'install.json')));
    assert.equal(manifest.owner, 'engelbart-cli');
    assert.equal(manifest.setupStatus, 'failed');
    assert.equal(manifest.setupExitCode, 7);

    let rebuilt = false;
    const retry = installOptions(packageRoot, managedRoot, [], 0);
    retry.deps.buildRuntime = async () => { rebuilt = true; };
    await install(retry);
    assert.equal(rebuilt, false);
    manifest = JSON.parse(fs.readFileSync(path.join(managedRoot, 'install.json')));
    assert.equal(manifest.setupStatus, 'complete');
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('switchLauncher refuses an unmanaged existing launcher', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-owner-test-'));
  try {
    fs.mkdirSync(path.join(fixture, 'bin'), { recursive: true });
    fs.writeFileSync(path.join(fixture, 'bin', 'hc'), 'mine');
    assert.throws(
      () => switchLauncher(fixture, '/managed/hc', null, null, 'linux'),
      /unmanaged launcher/,
    );
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('the runtime check asks for the python distribution, not the npm package', () => {
  // These names are deliberately different: the npm package is engelbart-cli,
  // the wheel it carries is human-compact. Asking for the wrong one made a
  // published install fail its version check on the user's first command.
  const source = fs.readFileSync(path.join(__dirname, '..', 'lib', 'installer.js'), 'utf8');
  const lookup = source.match(/importlib\.metadata\.version\("([^"]+)"\)/);
  assert(lookup, 'the runtime version check should still exist');
  assert.equal(lookup[1], 'human-compact');

  const pyproject = fs.readFileSync(
    path.join(__dirname, '..', '..', 'hc', 'pyproject.toml'), 'utf8');
  const distribution = pyproject.match(/^name\s*=\s*"([^"]+)"/m);
  assert.equal(lookup[1], distribution[1],
    'the check must name whatever pyproject actually builds');
});

// A one-command install that ends by telling the user to run a command their
// shell cannot find is not installed. This is the check that was missing.
test('PATH: an already-reachable launcher is left alone', { skip: WINDOWS_HOST }, () => {
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/usr/bin:/home/u/.human-compact/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: { readFileSync() { throw new Error('must not read'); },
                  appendFileSync() { throw new Error('must not write'); } },
  });
  assert.equal(got.onPath, true);
  assert.equal(got.added, false);
});

test('PATH: a launcher the shell cannot find is added to the zsh profile', { skip: WINDOWS_HOST }, () => {
  const writes = [];
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/usr/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: {
      readFileSync() { const e = new Error('nope'); e.code = 'ENOENT'; throw e; },
      appendFileSync(file, text) { writes.push([file, text]); },
    },
  });
  assert.equal(got.onPath, false);
  assert.equal(got.added, true);
  assert.equal(got.profile, '/home/u/.zshrc');
  assert.equal(got.line, 'export PATH="$HOME/.human-compact/bin:$PATH"');
  assert.equal(writes.length, 1);
  assert.match(writes[0][1], /engelbart-cli \(runtime on PATH\)/);
});

test('PATH: ZDOTDIR is honoured over the home directory', { skip: WINDOWS_HOST }, () => {
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/usr/bin', SHELL: '/bin/zsh', ZDOTDIR: '/home/u/cfg' },
    homedir: '/home/u',
    fileSystem: { readFileSync: () => '', appendFileSync() {} },
  });
  assert.equal(got.profile, '/home/u/cfg/.zshrc');
});

test('PATH: the profile is never given the same line twice', () => {
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/usr/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: {
      readFileSync: () => 'export PATH="$HOME/.human-compact/bin:$PATH"\n',
      appendFileSync() { throw new Error('must not append twice'); },
    },
  });
  assert.equal(got.added, false);
  assert.equal(got.onPath, false);
});

test('PATH: an unrecognised shell is instructed, not edited', { skip: WINDOWS_HOST }, () => {
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/usr/bin', SHELL: '/usr/bin/fish' },
    homedir: '/home/u',
    fileSystem: { appendFileSync() { throw new Error('must not write'); } },
  });
  assert.equal(got.profile, null);
  assert.equal(got.added, false);
  assert.equal(got.line, 'export PATH="$HOME/.human-compact/bin:$PATH"');
});

test('PATH: both launchers are linked into a directory the shell already searches', { skip: WINDOWS_HOST }, () => {
  const links = [];
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/usr/bin:/home/u/.local/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: {
      lstatSync() { const e = new Error('nope'); e.code = 'ENOENT'; throw e; },
      symlinkSync(from, to) { links.push([from, to]); },
      appendFileSync() { throw new Error('must not edit a profile'); },
    },
  });
  assert.equal(got.onPath, true);
  assert.deepEqual(got.linked, [
    '/home/u/.local/bin/hc', '/home/u/.local/bin/bart',
  ]);
  assert.deepEqual(links, [
    ['/home/u/.human-compact/bin/hc', '/home/u/.local/bin/hc'],
    ['/home/u/.human-compact/bin/bart', '/home/u/.local/bin/bart'],
  ]);
});

test('PATH: an hc that is not ours is never overwritten', { skip: WINDOWS_HOST }, () => {
  const appended = [];
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/home/u/.local/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: {
      lstatSync: () => ({ isSymbolicLink: () => false }),
      symlinkSync() { throw new Error('must not clobber'); },
      readFileSync: () => '',
      appendFileSync(file, text) { appended.push(file); },
    },
  });
  assert.equal(got.linked, null);
  assert.equal(got.added, true);          // falls back to the profile
  assert.deepEqual(appended, ['/home/u/.zshrc']);
});

test('PATH: re-running the installer reuses its own link', { skip: WINDOWS_HOST }, () => {
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/home/u/.local/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: {
      lstatSync: () => ({ isSymbolicLink: () => true }),
      readlinkSync: (link) => path.join('/home/u/.human-compact/bin', path.basename(link)),
      symlinkSync() { throw new Error('must not relink'); },
    },
  });
  assert.equal(got.onPath, true);
  assert.deepEqual(got.linked, [
    '/home/u/.local/bin/hc', '/home/u/.local/bin/bart',
  ]);
});

test('PATH: a directory on PATH but not a known bin dir is left alone', { skip: WINDOWS_HOST }, () => {
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/usr/bin:/opt/homebrew/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: {
      symlinkSync() { throw new Error('must not write outside the home bins'); },
      readFileSync: () => '',
      appendFileSync() {},
    },
  });
  assert.equal(got.linked, null);
  assert.equal(got.added, true);
});

// --- Windows port: platform is injected, so these run on any host. ----------

test('platform helpers: venv layout differs on Windows', () => {
  assert.equal(venvBinDir('linux'), 'bin');
  assert.equal(venvBinDir('win32'), 'Scripts');
  assert.equal(exeName('hc', 'darwin'), 'hc');
  assert.equal(exeName('hc', 'win32'), 'hc.exe');
  assert.equal(launcherName('hc', 'linux'), 'hc');
  assert.equal(launcherName('hc', 'win32'), 'hc.cmd');
});

test('runtimeExecutables: Windows uses Scripts\\*.exe', () => {
  const win = runtimeExecutables('C:\\rt', 'win32');
  assert.ok(win.hc.endsWith(path.join('Scripts', 'hc.exe')));
  assert.ok(win.bart.endsWith(path.join('Scripts', 'bart.exe')));
  assert.ok(win.python.endsWith(path.join('Scripts', 'python.exe')));
  const posix = runtimeExecutables('/rt', 'linux');
  assert.ok(posix.hc.endsWith(path.join('bin', 'hc')));
});

test('launcher shim: forwards args to the runtime exe and round-trips', () => {
  const target = 'C:\\Users\\u\\.human-compact\\runtimes\\r\\Scripts\\hc.exe';
  const shim = launcherShim(target);
  assert.match(shim, /^@echo off/);
  assert.match(shim, /%\*/);
  assert.equal(launcherShimTarget(shim), target);
  assert.equal(launcherShimTarget('not a shim'), null);
});

test('PATH (win32): a launcher not on PATH is instructed with setx, never a profile edit', () => {
  // Drive-less paths keep this host-independent: real Windows drive-letter
  // matching (its ';' delimiter, case-folding) is exercised by windows-port CI,
  // since the POSIX host's ':' delimiter would split a "C:\..." entry apart.
  const got = ensureLauncherOnPath({
    launcherDir: '\\human-compact\\bin',
    env: { PATH: '\\Windows' },
    homedir: '\\Users\\U',
    platform: 'win32',
    fileSystem: { appendFileSync() { throw new Error('Windows has no shell profile to edit'); } },
  });
  assert.equal(got.onPath, false);
  assert.equal(got.profile, null);
  assert.equal(got.line, 'setx PATH "%PATH%;\\human-compact\\bin"');
});

test('PATH (win32): the launcher dir already on PATH needs no instruction', () => {
  const got = ensureLauncherOnPath({
    launcherDir: '\\human-compact\\bin',
    env: { PATH: '\\human-compact\\bin' },
    homedir: '\\Users\\U',
    platform: 'win32',
  });
  assert.equal(got.onPath, true);
  assert.equal(got.line, null);
});

test('switchLauncher (win32): a fresh install writes hc.cmd and bart.cmd shims', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-win-launcher-'));
  try {
    const rtBin = path.join('C:', 'rt', 'Scripts');
    const hc = path.join(rtBin, 'hc.exe');
    const bart = path.join(rtBin, 'bart.exe');
    const result = switchLauncher(fixture, hc, null, bart, 'win32');
    assert.ok(result.launcher.endsWith(path.join('bin', 'hc.cmd')));
    assert.ok(result.bartLauncher.endsWith(path.join('bin', 'bart.cmd')));
    const hcShim = fs.readFileSync(result.launcher, 'utf8');
    assert.equal(launcherShimTarget(hcShim), hc);
    const bartShim = fs.readFileSync(result.bartLauncher, 'utf8');
    assert.equal(launcherShimTarget(bartShim), bart);
    // No POSIX symlink was created.
    assert.equal(fs.lstatSync(result.launcher).isFile(), true);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('switchLauncher (win32): refuses to overwrite an unmanaged .cmd', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-win-owner-'));
  try {
    fs.mkdirSync(path.join(fixture, 'bin'), { recursive: true });
    fs.writeFileSync(path.join(fixture, 'bin', 'hc.cmd'), '@echo not ours');
    assert.throws(
      () => switchLauncher(fixture, path.join('C:', 'rt', 'Scripts', 'hc.exe'), null, null, 'win32'),
      /unmanaged launcher/,
    );
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('spawnableLauncher: POSIX returns the stable symlink, no manifest read', { skip: WINDOWS_HOST }, () => {
  // A non-existent root would make a manifest read throw; POSIX must not read one.
  assert.equal(
    spawnableLauncher('/nope/.human-compact', 'hc', 'linux'),
    path.join('/nope/.human-compact', 'bin', 'hc'),
  );
});

test('spawnableLauncher (win32): resolves the runtime .exe from the owned manifest', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-spawnable-'));
  try {
    const runtime = path.join(fixture, 'runtimes', 'r');
    fs.mkdirSync(runtime, { recursive: true });
    fs.writeFileSync(path.join(fixture, 'install.json'), `${JSON.stringify({
      owner: OWNER, schema: INSTALL_SCHEMA, runtime,
    }, null, 2)}\n`);
    const exe = spawnableLauncher(fixture, 'hc', 'win32');
    assert.equal(exe, path.join(runtime, 'Scripts', 'hc.exe'));
    // No manifest on disk -> a clear error, never a bad spawn target.
    const bare = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-spawnable-bare-'));
    try {
      assert.throws(() => spawnableLauncher(bare, 'hc', 'win32'), /manifest/);
    } finally {
      fs.rmSync(bare, { recursive: true, force: true });
    }
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('switchLauncher (win32): an owned upgrade re-points the shim to the new runtime', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-win-upgrade-'));
  try {
    // First install at runtime "old".
    const oldRt = path.join(fixture, 'runtimes', 'old');
    const oldHc = path.join(oldRt, 'Scripts', 'hc.exe');
    const oldBart = path.join(oldRt, 'Scripts', 'bart.exe');
    const first = switchLauncher(fixture, oldHc, null, oldBart, 'win32');
    const manifest = {
      runtime: oldRt,
      launcher: first.launcher,
      bartLauncher: first.bartLauncher,
    };
    // Upgrade to runtime "new"; the manifest proves the old shim is ours.
    const newRt = path.join(fixture, 'runtimes', 'new');
    const newHc = path.join(newRt, 'Scripts', 'hc.exe');
    const newBart = path.join(newRt, 'Scripts', 'bart.exe');
    const second = switchLauncher(fixture, newHc, manifest, newBart, 'win32');
    assert.equal(launcherShimTarget(fs.readFileSync(second.launcher, 'utf8')), newHc);
    assert.equal(launcherShimTarget(fs.readFileSync(second.bartLauncher, 'utf8')), newBart);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});
