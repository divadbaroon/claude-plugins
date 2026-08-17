'use strict';

// The wheel is the release artifact, so its contents must be a function of the
// commit alone. setuptools' build_py copies src/ *into* hc/build/lib without
// pruning, so a file deleted from the source tree survives there and gets
// packed -- which is exactly how the pre-rename hc-ui skill shipped inside the
// 0.18.0 wheel on its first build. The git check in build-vendor.js cannot see
// it: hc/build/ and *.egg-info are both gitignored.
//
// This test runs the real script against a throwaway clone, never the working
// tree, so an interrupted run cannot leave the committed vendor/ dirty.

const assert = require('assert/strict');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const test = require('node:test');

const packageRoot = path.resolve(__dirname, '..');
const repoRoot = path.resolve(packageRoot, '..');
const vendorRoot = path.join(packageRoot, 'vendor');
const STALE_REL = path.join(
  'hc', 'build', 'lib', 'human_compact', 'assets', 'hc-ui-skill', 'SKILL.md');

// Resolve uv the same way build-vendor.js does, plus the default install path,
// because npm test does not inherit a login shell's PATH on every platform.
function findUv() {
  const candidates = [
    process.env.HUMAN_COMPACT_BUILD_UV,
    'uv',
    path.join(os.homedir(), '.local', 'bin', 'uv'),
  ];
  for (const candidate of candidates) {
    if (!candidate) continue;
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8' });
    if (!probe.error && probe.status === 0) return candidate;
  }
  return null;
}

// A fingerprint of the real vendor/, so the test can prove it never wrote there.
function vendorFingerprint() {
  const hash = crypto.createHash('sha256');
  for (const name of fs.readdirSync(vendorRoot).sort()) {
    hash.update(name);
    hash.update(fs.readFileSync(path.join(vendorRoot, name)));
  }
  return hash.digest('hex');
}

function git(cwd, args) {
  const result = spawnSync('git', args, { cwd, encoding: 'utf8' });
  assert.equal(result.status, 0, `git ${args.join(' ')}: ${result.stderr}`);
  return result.stdout;
}

// engelbart/ + hc/ into a tmpdir with its own git history. build-vendor.js
// derives every path from __dirname, so the copy is self-contained: it reads
// the copy's pyproject.toml, refuses on the copy's git state, and writes the
// copy's vendor/.
function cloneWorkspace(destination) {
  const skip = new Set(['node_modules', 'vendor', 'build', '__pycache__']);
  const filter = (source) => {
    const base = path.basename(source);
    return !skip.has(base) && !base.endsWith('.egg-info');
  };
  fs.mkdirSync(path.join(destination, 'engelbart'), { recursive: true });
  fs.cpSync(packageRoot, path.join(destination, 'engelbart'), {
    recursive: true, filter,
  });
  fs.cpSync(path.join(repoRoot, 'hc'), path.join(destination, 'hc'), {
    recursive: true, filter,
  });
  // The planted tree must be *ignored*, exactly as it is in the real repo --
  // otherwise build-vendor.js would refuse it as an uncommitted change and the
  // test would prove nothing about the leak.
  fs.writeFileSync(path.join(destination, '.gitignore'),
    'node_modules/\nbuild/\n*.egg-info/\n__pycache__/\n');
  git(destination, ['init', '--quiet']);
  git(destination, ['add', '-A']);
  git(destination, [
    '-c', 'user.email=test@example.invalid', '-c', 'user.name=vendor test',
    'commit', '--quiet', '-m', 'fixture',
  ]);
  assert.equal(git(destination, ['status', '--porcelain', '--', 'hc']).trim(), '',
    'the fixture clone must start clean, or build-vendor would refuse it');
}

test('a stale build tree cannot leak a deleted asset into the wheel', (t) => {
  const uv = findUv();
  if (!uv) {
    // Visibly skipped, never silently passed. CI has no uv (see
    // .github/workflows/test.yml npm-installer), and installing a Python
    // toolchain there to guard a release-engineering invariant is a worse
    // trade than running this locally, where releases are actually cut.
    t.skip('uv not installed');
    return;
  }

  const before = vendorFingerprint();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-vendor-hygiene-'));
  t.after(() => {
    fs.rmSync(workspace, { recursive: true, force: true });
    assert.equal(vendorFingerprint(), before,
      'the real vendor/ must be byte-identical after this test');
  });

  cloneWorkspace(workspace);

  const stale = path.join(workspace, STALE_REL);
  fs.mkdirSync(path.dirname(stale), { recursive: true });
  fs.writeFileSync(stale, '---\nname: hc-ui\n---\n\nplanted by a test\n');
  assert.equal(fs.existsSync(stale), true, 'the stale asset must exist to be a test');

  const built = spawnSync(
    'node', [path.join(workspace, 'engelbart', 'scripts', 'build-vendor.js')],
    {
      cwd: path.join(workspace, 'engelbart'),
      encoding: 'utf8',
      env: { ...process.env, HUMAN_COMPACT_BUILD_UV: uv },
    });
  assert.equal(built.status, 0, built.stderr || built.stdout);

  const clonedVendor = path.join(workspace, 'engelbart', 'vendor');
  const manifest = JSON.parse(
    fs.readFileSync(path.join(clonedVendor, 'manifest.json'), 'utf8'));
  const listed = spawnSync('python3', [
    '-c',
    'import sys,zipfile;print("\\n".join(zipfile.ZipFile(sys.argv[1]).namelist()))',
    path.join(clonedVendor, manifest.wheel),
  ], { encoding: 'utf8' });
  assert.equal(listed.status, 0, listed.stderr);
  const names = listed.stdout.split('\n').filter(Boolean);

  assert.equal(names.some((n) => n.includes('hc-ui-skill')), false,
    'the deleted hc-ui skill must not reach the wheel');
  assert.equal(names.includes('human_compact/assets/goals-ui-skill/SKILL.md'), true,
    'the real skill asset must still be there');
  assert.equal(fs.existsSync(stale), false,
    'the build must have removed the stale tree, not merely ignored it');

  // The whole point of the fixture: the script wrote to the clone, not here.
  assert.equal(vendorFingerprint(), before,
    'build-vendor.js must not have touched the real vendor/');
});
