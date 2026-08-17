'use strict';

// The wheel is the release artifact, so its contents must be a function of the
// commit alone. setuptools' build_py copies src/ *into* hc/build/lib without
// pruning, so a file deleted from the source tree survives there and gets
// packed -- which is exactly how the pre-rename hc-ui skill shipped inside the
// 0.18.0 wheel on its first build.

const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const test = require('node:test');

const packageRoot = path.resolve(__dirname, '..');
const repoRoot = path.resolve(packageRoot, '..');
const hcRoot = path.join(repoRoot, 'hc');
const vendorRoot = path.join(packageRoot, 'vendor');
const STALE = path.join(
  hcRoot, 'build', 'lib', 'human_compact', 'assets', 'hc-ui-skill', 'SKILL.md');

function uvCommand() {
  const explicit = process.env.HUMAN_COMPACT_BUILD_UV;
  if (explicit && fs.existsSync(explicit)) return explicit;
  for (const candidate of [
    explicit,
    'uv',
    path.join(os.homedir(), '.local', 'bin', 'uv'),
  ]) {
    if (!candidate) continue;
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8' });
    if (!probe.error && probe.status === 0) return candidate;
  }
  return null;
}

function snapshotVendor() {
  const saved = new Map();
  for (const name of fs.readdirSync(vendorRoot)) {
    saved.set(name, fs.readFileSync(path.join(vendorRoot, name)));
  }
  return () => {
    for (const name of fs.readdirSync(vendorRoot)) {
      if (!saved.has(name)) fs.rmSync(path.join(vendorRoot, name), { force: true });
    }
    for (const [name, body] of saved) {
      fs.writeFileSync(path.join(vendorRoot, name), body);
    }
  };
}

test('a stale build tree cannot leak a deleted asset into the wheel', (t) => {
  const uv = uvCommand();
  if (!uv) {
    // Not a silent pass: the assertion below is the point of this file.
    assert.fail('uv is required to build a wheel; install it or set '
      + 'HUMAN_COMPACT_BUILD_UV to its path so this test can run');
  }
  const dirty = spawnSync('git', ['status', '--porcelain', '--', 'hc'],
    { cwd: repoRoot, encoding: 'utf8' });
  assert.equal(dirty.status, 0);
  if (dirty.stdout.trim()) {
    assert.fail(`hc/ is dirty, so build:vendor would refuse:\n${dirty.stdout}`);
  }

  const restoreVendor = snapshotVendor();
  t.after(() => {
    restoreVendor();
    fs.rmSync(path.join(hcRoot, 'build'), { recursive: true, force: true });
  });

  fs.mkdirSync(path.dirname(STALE), { recursive: true });
  fs.writeFileSync(STALE, '---\nname: hc-ui\n---\n\nplanted by a test\n');
  assert.equal(fs.existsSync(STALE), true, 'the stale asset must exist to be a test');

  const built = spawnSync('node', [path.join(packageRoot, 'scripts', 'build-vendor.js')], {
    cwd: packageRoot,
    encoding: 'utf8',
    env: { ...process.env, HUMAN_COMPACT_BUILD_UV: uv },
  });
  assert.equal(built.status, 0, built.stderr || built.stdout);

  const manifest = JSON.parse(
    fs.readFileSync(path.join(vendorRoot, 'manifest.json'), 'utf8'));
  const wheel = path.join(vendorRoot, manifest.wheel);
  const listed = spawnSync('python3', [
    '-c',
    'import sys,zipfile;print("\\n".join(zipfile.ZipFile(sys.argv[1]).namelist()))',
    wheel,
  ], { encoding: 'utf8' });
  assert.equal(listed.status, 0, listed.stderr);
  const names = listed.stdout.split('\n').filter(Boolean);

  assert.equal(names.some((n) => n.includes('hc-ui-skill')), false,
    'the deleted hc-ui skill must not reach the wheel');
  assert.equal(names.includes('human_compact/assets/goals-ui-skill/SKILL.md'), true,
    'the real skill asset must still be there');
  assert.equal(fs.existsSync(STALE), false,
    'the build must have removed the stale tree, not merely ignored it');
});
