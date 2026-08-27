'use strict';

const assert = require('assert/strict');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const test = require('node:test');

const {
  CLAUDE_INSTALL_COMMAND,
  CLAUDE_UPDATE_COMMAND,
  claudeProblem,
  ensureClaudeCode,
  ensureUv,
  ensureManagedDirectory,
  ensureLauncherOnPath,
  establishOwnership,
  inspectVendor,
  install,
  parseClaudeVersion,
  requireCompatibleClaude,
  safeChild,
  supportedTarget,
  switchLauncher,
  withClaudeOnPath,
} = require('../lib/installer');

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
  fs.chmodSync(path.join(staging, 'bin', 'python'), 0o700);
  fs.chmodSync(path.join(staging, 'bin', 'hc'), 0o700);
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
  assert.throws(() => supportedTarget('win32', 'x64'), /unsupported platform/);
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
    const setup = calls.find((call) => call.args[0] === 'setup');
    assert.deepEqual(setup.args, [
      'setup', '--global-vault', 'yes', '--goals', 'no',
    ]);
    assert.equal(setup.options.env.HC_EXECUTABLE, path.join(managedRoot, 'bin', 'hc'));
    const manifest = JSON.parse(fs.readFileSync(path.join(managedRoot, 'install.json')));
    assert.equal(manifest.owner, 'engelbart-cli');
    assert.equal(manifest.backendVersion, '0.16.0');

    let rebuilt = false;
    const repeat = installOptions(packageRoot, managedRoot, [], 0);
    repeat.deps.buildRuntime = async () => { rebuilt = true; };
    await install(repeat);
    assert.equal(rebuilt, false);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

// The whole point of the offer is that saying yes is one key, so these state
// what `claude --version` says before and after the fix runs, and read back
// which command the member's agreement actually authorised.
function claudeStage(stages) {
  const ran = [];
  return {
    ran,
    runner(command, args, options) {
      if (command === 'claude' && args[0] === '--version') {
        const answer = stages.shift();
        if (answer instanceof Error) throw answer;
        return { status: 0, stdout: `${answer} (Claude Code)\n`, stderr: '' };
      }
      ran.push({ command, args, options });
      return { status: 0, stdout: '', stderr: '' };
    },
  };
}

test('a missing Claude Code is one keypress away from being installed', async () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-claude-offer-'));
  try {
    // The installer puts it here, and finding it here is what lets the rest of
    // the install use it without the member opening a new terminal first.
    const homedir = path.join(fixture, 'home');
    fs.mkdirSync(path.join(homedir, '.local', 'bin'), { recursive: true });
    fs.writeFileSync(path.join(homedir, '.local', 'bin', 'claude'), '#!/bin/sh\n');
    const stage = claudeStage([new Error('spawnSync claude ENOENT'), '2.1.175']);
    const asked = [];
    const env = await ensureClaudeCode({
      runner: stage.runner,
      env: { PATH: '/usr/bin' },
      homedir,
      confirm: (problem) => { asked.push(problem); return true; },
    });
    assert.equal(asked.length, 1);
    assert.equal(asked[0].kind, 'missing');
    assert.equal(asked[0].fix, CLAUDE_INSTALL_COMMAND);
    assert.deepEqual(stage.ran.map((call) => call.args[1]),
      [`set -o pipefail; ${CLAUDE_INSTALL_COMMAND}`]);
    assert.equal(stage.ran[0].command, 'bash');
    // Inherited, because the installer's own progress and errors are the
    // member's to read.
    assert.equal(stage.ran[0].options.stdio, 'inherit');
    assert.equal(env.PATH, `${path.join(homedir, '.local', 'bin')}${path.delimiter}/usr/bin`);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test('an outdated Claude Code is offered the update, not the installer', async () => {
  const stage = claudeStage(['2.1.150', '2.1.175']);
  const asked = [];
  await ensureClaudeCode({
    runner: stage.runner,
    env: { PATH: '/usr/bin' },
    homedir: path.join(os.tmpdir(), 'hc-absent-home'),
    confirm: (problem) => { asked.push(problem); return true; },
  });
  assert.equal(asked[0].kind, 'old');
  assert.equal(asked[0].installed, '2.1.150');
  assert.deepEqual(stage.ran.map((call) => call.args[1]),
    [`set -o pipefail; ${CLAUDE_UPDATE_COMMAND}`]);
});

test('declining the offer names the command instead of running one', async () => {
  const stage = claudeStage([new Error('spawnSync claude ENOENT')]);
  await assert.rejects(
    ensureClaudeCode({
      runner: stage.runner,
      env: {},
      homedir: os.tmpdir(),
      confirm: () => false,
    }),
    (error) => error.message.includes(CLAUDE_INSTALL_COMMAND),
  );
  assert.deepEqual(stage.ran, []);
});

test('with nobody to ask, the offer is a message and never a command', async () => {
  const stage = claudeStage([new Error('spawnSync claude ENOENT')]);
  await assert.rejects(
    ensureClaudeCode({ runner: stage.runner, env: {}, homedir: os.tmpdir(), confirm: null }),
    (error) => error.message.includes(CLAUDE_INSTALL_COMMAND),
  );
  assert.deepEqual(stage.ran, []);
});

// Something else on this machine answering to `claude` is not ours to replace.
test('an unrecognisable claude is never offered a fix', () => {
  const problem = claudeProblem({ present: true, output: 'development build' });
  assert.equal(problem.kind, 'unusable');
  assert.equal(problem.fix, null);
});

test('a fix that did not take says so instead of offering itself again', async () => {
  const stage = claudeStage([new Error('ENOENT'), '2.1.150']);
  await assert.rejects(
    ensureClaudeCode({
      runner: stage.runner,
      env: {},
      homedir: os.tmpdir(),
      confirm: () => true,
    }),
    /2\.1\.150 is too old.*after running/s,
  );
});

test('the installed path is carried forward only when it holds a claude', () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-claude-path-'));
  try {
    const env = { PATH: '/usr/bin' };
    assert.equal(withClaudeOnPath(env, fixture), env);
    const bin = path.join(fixture, '.local', 'bin');
    fs.mkdirSync(bin, { recursive: true });
    fs.writeFileSync(path.join(bin, 'claude'), '');
    assert.equal(withClaudeOnPath(env, fixture).PATH, `${bin}${path.delimiter}/usr/bin`);
    // Already there: prepending it a second time would be a lie about which
    // copy the rest of the install is going to run.
    assert.equal(withClaudeOnPath({ PATH: bin }, fixture).PATH, bin);
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
      () => switchLauncher(fixture, '/managed/hc', null),
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
test('PATH: an already-reachable launcher is left alone', () => {
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

test('PATH: a launcher the shell cannot find is added to the zsh profile', () => {
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

test('PATH: ZDOTDIR is honoured over the home directory', () => {
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

test('PATH: an unrecognised shell is instructed, not edited', () => {
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

test('PATH: the launcher is linked into a directory the shell already searches', () => {
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
  assert.equal(got.linked, '/home/u/.local/bin/hc');
  assert.deepEqual(links, [['/home/u/.human-compact/bin/hc', '/home/u/.local/bin/hc']]);
});

test('PATH: an hc that is not ours is never overwritten', () => {
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

test('PATH: re-running the installer reuses its own link', () => {
  const got = ensureLauncherOnPath({
    launcherDir: '/home/u/.human-compact/bin',
    env: { PATH: '/home/u/.local/bin', SHELL: '/bin/zsh' },
    homedir: '/home/u',
    fileSystem: {
      lstatSync: () => ({ isSymbolicLink: () => true }),
      readlinkSync: () => '/home/u/.human-compact/bin/hc',
      symlinkSync() { throw new Error('must not relink'); },
    },
  });
  assert.equal(got.onPath, true);
  assert.equal(got.linked, '/home/u/.local/bin/hc');
});

test('PATH: a directory on PATH but not a known bin dir is left alone', () => {
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
