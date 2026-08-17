#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const packageRoot = path.resolve(__dirname, '..');
const repoRoot = path.resolve(packageRoot, '..');
const hcRoot = path.join(repoRoot, 'hc');
const vendorRoot = path.join(packageRoot, 'vendor');
const packageJson = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'));
const pyproject = fs.readFileSync(path.join(hcRoot, 'pyproject.toml'), 'utf8');
const match = pyproject.match(/^version\s*=\s*"([^"]+)"\s*$/m);
if (!match) throw new Error('could not read hc project version');
if (match[1] !== packageJson.version) {
  throw new Error(`npm/backend version mismatch: ${packageJson.version} != ${match[1]}`);
}

const gitStatus = spawnSync('git', ['status', '--porcelain', '--', 'hc'], {
  cwd: repoRoot,
  encoding: 'utf8',
});
if (gitStatus.status !== 0) throw new Error('could not inspect hc source state');
if (gitStatus.stdout.trim()) {
  throw new Error('refusing to vendor an uncommitted hc source tree');
}
const revision = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: repoRoot, encoding: 'utf8' });
if (revision.status !== 0) throw new Error('could not resolve source revision');

// setuptools' build_py copies src/ into hc/build/lib without pruning, so a
// file deleted from the source tree survives there and is packed into the next
// wheel. The git check above cannot see it -- both paths are gitignored. Start
// from a clean tree so the wheel is a function of the commit alone.
for (const stale of [
  path.join(hcRoot, 'build'),
  ...fs.existsSync(path.join(hcRoot, 'src'))
    ? fs.readdirSync(path.join(hcRoot, 'src'))
      .filter((name) => name.endsWith('.egg-info'))
      .map((name) => path.join(hcRoot, 'src', name))
    : [],
]) {
  fs.rmSync(stale, { recursive: true, force: true });
}

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'human-compact-wheel-'));
try {
  const uv = process.env.HUMAN_COMPACT_BUILD_UV || 'uv';
  const build = spawnSync(uv, ['build', '--wheel', '--out-dir', temporary, hcRoot], {
    cwd: repoRoot,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (build.error || build.status !== 0) {
    throw new Error(`wheel build failed: ${build.error?.message || build.stderr.trim()}`);
  }
  const wheels = fs.readdirSync(temporary).filter((name) => name.endsWith('.whl'));
  if (wheels.length !== 1) throw new Error(`expected one wheel, found ${wheels.length}`);
  const expectedPrefix = `human_compact-${packageJson.version.replace(/-/g, '_')}-`;
  if (!wheels[0].startsWith(expectedPrefix) || !wheels[0].endsWith('-py3-none-any.whl')) {
    throw new Error(`wheel filename does not match release: ${wheels[0]}`);
  }
  const source = path.join(temporary, wheels[0]);
  const sha256 = crypto.createHash('sha256').update(fs.readFileSync(source)).digest('hex');

  fs.mkdirSync(vendorRoot, { recursive: true });
  for (const name of fs.readdirSync(vendorRoot)) {
    if (name.endsWith('.whl') || name === 'manifest.json') {
      fs.rmSync(path.join(vendorRoot, name), { force: true });
    }
  }
  fs.copyFileSync(source, path.join(vendorRoot, wheels[0]));
  fs.writeFileSync(path.join(vendorRoot, 'manifest.json'), `${JSON.stringify({
    schema: 1,
    package: 'engelbart-cli',
    version: packageJson.version,
    wheel: wheels[0],
    sha256,
    sourceRevision: revision.stdout.trim(),
  }, null, 2)}\n`);
  process.stdout.write(`Vendored ${wheels[0]} (${sha256})\n`);
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}
