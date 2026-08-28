'use strict';

/* Entry point for the compiled (bun build --compile) standalone binary.
 *
 * npm installs never run this file -- they use bin/engelbart.js, which reads
 * package.json and vendor/ off the disk they arrived on. Here there is no
 * disk layout: scripts/build-binary.sh stages the vendored wheel and its
 * manifest under compile/assets/, Bun embeds them into the executable, and
 * this file's job is to hand lib/cli.js the same shape it gets from npm.
 *
 * pip and uv are external processes and cannot read Bun's virtual
 * filesystem, so the embedded wheel is written out to a real temporary
 * directory shaped like a package root. inspectVendor() then re-hashes the
 * extracted copy against the embedded manifest, so a corrupt extraction
 * fails exactly the way a corrupt npm package would.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

// Parsed at bundle time by Bun's JSON loader; nothing is read at runtime.
const packageJson = require('../package.json');
const manifest = require('./assets/manifest.json');
// Embedded by Bun's file loader; resolves to a path inside the binary.
const wheelAsset = require('./assets/backend.whl');

const { run } = require('../lib/cli');
const { setInvocation } = require('../lib/invocation');

// This build is the standalone binary: its users have no npm, so every
// "run this again" instruction must name the command they actually have.
setInvocation('engelbart');

function assetPath(asset) {
  // Bundlers differ on whether a file-loader require yields the path or a
  // module object holding it; accept both so a Bun upgrade cannot silently
  // hand a broken path to fs.
  const value = (asset && typeof asset === 'object') ? asset.default : asset;
  if (typeof value !== 'string' || !value) {
    throw new Error('embedded wheel asset did not resolve to a path');
  }
  return value;
}

function materializePackageRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'engelbart-'));
  const vendorDir = path.join(root, 'vendor');
  fs.mkdirSync(vendorDir, { mode: 0o700 });
  fs.writeFileSync(
    path.join(vendorDir, 'manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  fs.writeFileSync(
    path.join(vendorDir, manifest.wheel),
    fs.readFileSync(assetPath(wheelAsset)),
  );
  return root;
}

async function main() {
  const packageRoot = materializePackageRoot();
  try {
    return await run({ packageRoot, packageJson });
  } finally {
    fs.rmSync(packageRoot, { recursive: true, force: true });
  }
}

main().then(
  (code) => { process.exitCode = code; },
  (error) => {
    process.stderr.write(`human-compact: ${error.message}\n`);
    process.exitCode = 1;
  },
);
