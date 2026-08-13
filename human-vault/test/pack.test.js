'use strict';

const assert = require('assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const test = require('node:test');

const packageRoot = path.resolve(__dirname, '..');

function checked(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', ...options });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return result;
}

test('packed npm artifact contains and executes the verified wheel', () => {
  const metadata = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json')));
  assert.deepEqual(Object.keys(metadata.bin), ['human-vault']);
  assert.equal(metadata.dependencies, undefined);
  assert.equal(metadata.devDependencies, undefined);
  for (const lifecycle of ['preinstall', 'install', 'postinstall']) {
    assert.equal(metadata.scripts[lifecycle], undefined);
  }
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'hc-pack-test-'));
  try {
    const packed = checked('npm', [
      'pack', '--ignore-scripts', '--json', '--pack-destination', fixture,
    ], { cwd: packageRoot });
    const [{ filename, files, name, version }] = JSON.parse(packed.stdout);
    assert.equal(name, 'human-vault');
    assert.equal(version, '0.16.0');
    const paths = files.map((entry) => entry.path);
    assert(paths.includes('vendor/manifest.json'));
    assert.equal(paths.filter((entry) => entry.endsWith('.whl')).length, 1);
    assert.equal(paths.some((entry) => entry.startsWith('test/')), false);

    const prefix = path.join(fixture, 'prefix');
    checked('npm', [
      'install', '--ignore-scripts', '--no-audit', '--no-fund',
      '--prefix', prefix, path.join(fixture, filename),
    ]);
    const binary = path.join(prefix, 'node_modules', '.bin', 'human-vault');
    const managed = path.join(fixture, 'must-not-exist');
    const invocation = checked(binary, [
      '--dry-run', '--non-interactive', '--global-vault', '2',
    ], {
      env: { ...process.env, HUMAN_COMPACT_HOME: managed },
    });
    assert.match(invocation.stdout, /Verified bundled backend 0\.16\.0/);
    assert.match(invocation.stdout, /run \/hc-ui\./);
    assert.equal(fs.existsSync(managed), false);
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});
