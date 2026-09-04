'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const scripts = path.resolve(__dirname, '..', 'scripts');

function executable(file, body) {
  fs.writeFileSync(file, body, { mode: 0o755 });
}

test('the POSIX download shim is silent around the browser-resume handoff', {
  skip: process.platform === 'win32',
}, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'engelbart-download-shim-'));
  try {
    const mockBin = path.join(root, 'mock-bin');
    const installDir = path.join(root, 'installed');
    fs.mkdirSync(mockBin);
    executable(path.join(mockBin, 'curl'), `#!/bin/sh
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-o' ]; then out="$2"; shift 2; else shift; fi
done
case "$out" in
  *.sha256) printf 'fixture checksum\n' > "$out" ;;
  *) printf '#!/bin/sh\nprintf "CLI-BANNER\\n"\n' > "$out" ;;
esac
`);
    executable(path.join(mockBin, 'sha256sum'), '#!/bin/sh\nexit 0\n');
    const env = {
      ...process.env,
      ENGELBART_INSTALL_DIR: installDir,
      PATH: `${mockBin}${path.delimiter}${process.env.PATH || '/usr/bin:/bin'}`,
    };
    const browser = spawnSync('sh', [path.join(scripts, 'install.sh'),
      '--code', 'ABCD-2345-WXYZ', '--no-open'], { env, encoding: 'utf8' });
    assert.equal(browser.status, 0, browser.stderr);
    assert.equal(browser.stdout, 'CLI-BANNER\n');
    assert.equal(browser.stderr, '');

    const ordinary = spawnSync('sh', [path.join(scripts, 'install.sh'), '--local-only'],
      { env, encoding: 'utf8' });
    assert.equal(ordinary.status, 0, ordinary.stderr);
    assert.equal(ordinary.stdout, 'CLI-BANNER\n');
    assert.match(ordinary.stderr, /Downloading engelbart-/);
    assert.match(ordinary.stderr, /Installed .*engelbart/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the PowerShell download shim routes every progress line through its browser-resume gate', () => {
  const source = fs.readFileSync(path.join(scripts, 'install.ps1'), 'utf8');
  assert.match(source, /\$quietBrowserResume = .*--code.*--no-open/);
  assert.match(source, /function Say\([^)]*\).*if \(-not \$quietBrowserResume\)/);
  assert.equal((source.match(/Write-Host/g) || []).length, 1,
    'only Say may write download-shim progress');
  assert.match(source, /Say "Downloading \$target\.\.\."/);
  assert.match(source, /Say "Installed \$installed"/);
});
