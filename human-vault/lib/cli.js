'use strict';

const os = require('os');
const path = require('path');
const {
  ensureLauncherOnPath,
  install,
  inspectVendor,
  supportedTarget,
} = require('./installer');

class UsageError extends Error {}
class InputCancelled extends Error {}


function usage() {
  return `Usage: npx human-vault [options]

Options:
  --global-vault <1|2>  1 enables global Vault; 2 installs /hc-ui only
  --goals <1|2>         1 builds global goals now; 2 skips (Vault only)
  --non-interactive     require every applicable choice as a flag
  --dry-run             verify the bundled release and show the plan only
  -h, --help            show this help
`;
}

function numericChoice(flag, value) {
  if (value !== '1' && value !== '2') {
    throw new UsageError(`${flag} must be 1 or 2`);
  }
  return value;
}

function parseArgs(argv) {
  const result = {
    globalVault: null,
    goals: null,
    nonInteractive: false,
    dryRun: false,
    help: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '-h' || arg === '--help') result.help = true;
    else if (arg === '--non-interactive') result.nonInteractive = true;
    else if (arg === '--dry-run') result.dryRun = true;
    else if (arg === '--global-vault' || arg === '--goals') {
      if (index + 1 >= argv.length) throw new UsageError(`${arg} requires 1 or 2`);
      const value = numericChoice(arg, argv[index += 1]);
      if (arg === '--global-vault') result.globalVault = value;
      else result.goals = value;
    } else {
      throw new UsageError(`unknown option: ${arg}`);
    }
  }
  if (result.globalVault === '2' && result.goals === '1') {
    throw new UsageError('--goals 1 requires --global-vault 1');
  }
  if (result.globalVault === '2' && result.goals === null) result.goals = '2';
  return result;
}


async function resolveChoices(options) {
  // Onboarding moved into the goal UI, where the same two questions are asked
  // with their consequences visible. The installer only installs; it never
  // enables capture or sends anything on the user's behalf. Explicit flags are
  // still honoured for scripted installs.
  const globalVault = options.globalVault === null ? '2' : options.globalVault;
  const goals = options.goals === null ? '2' : options.goals;
  if (globalVault === '2' && goals === '1') {
    throw new UsageError('--goals 1 requires --global-vault 1');
  }
  return { globalVault, goals };
}

async function run(deps = {}) {
  const argv = deps.argv || process.argv.slice(2);
  const output = deps.output || process.stdout;
  const errorOutput = deps.errorOutput || process.stderr;
  try {
    const options = parseArgs(argv);
    if (options.help) {
      output.write(usage());
      return 0;
    }
    const choices = await resolveChoices(options, {
      input: deps.input,
      output,
    });
    const packageRoot = deps.packageRoot || path.resolve(__dirname, '..');
    const packageJson = require(path.join(packageRoot, 'package.json'));
    const platform = deps.platform || process.platform;
    const arch = deps.arch || process.arch;
    const target = supportedTarget(platform, arch, deps.processReport);
    const vendor = inspectVendor(packageRoot, packageJson.version);
    const managedRoot = path.resolve(
      deps.managedRoot
        || process.env.HUMAN_COMPACT_HOME
        || path.join(os.homedir(), '.human-compact'),
    );

    let reach = null;
    output.write(`\nhuman-vault ${packageJson.version}\n\n`);
    if (options.dryRun) {
      output.write(`Verified bundled backend ${vendor.version} (${vendor.sha256.slice(0, 12)}).\n`);
      output.write(`Target: ${target.name}; managed runtime: ${managedRoot}\n`);
      output.write(`Plan: global Vault ${choices.globalVault === '1' ? 'enabled' : 'disabled'}; global goals ${choices.goals === '1' ? 'build now' : 'skip'}.\n`);
    } else {
      const installed = await (deps.install || install)({
        packageRoot,
        packageVersion: packageJson.version,
        managedRoot,
        choices,
        platform,
        arch,
        processReport: deps.processReport,
        output,
        errorOutput,
        deps: deps.installerDeps || {},
      });
      // Only claim `hc ui` works once the shell can actually find `hc`.
      const launcher = installed && installed.launcher;
      if (launcher) {
        reach = (deps.ensureLauncherOnPath || ensureLauncherOnPath)({
          launcherDir: path.dirname(launcher),
          env: process.env,
          homedir: os.homedir(),
        });
      }
    }
    // One status block, then one instruction. Anything the user must do to
    // make that instruction work belongs above it, not after it.
    if (reach && reach.onPath) {
      output.write(`  hc           ready in this terminal\n`);
    } else if (reach) {
      output.write(`  hc           needs one more step (below)\n`);
    }
    output.write('\nInstalled. Nothing is captured or analyzed yet.\n');
    if (reach && !reach.onPath) {
      output.write(reach.added
        ? `\nRun this once in this terminal (new terminals get it from ${reach.profile}):\n\n    ${reach.line}\n`
        : reach.present
        ? `\nThis terminal predates ${reach.profile}. Run this once here:\n\n    ${reach.line}\n`
        : `\nAdd this to your shell profile, then run it here:\n\n    ${reach.line}\n`);
    }
    const needsPathStep = !!(reach && !reach.onPath);
    output.write(`\n${needsPathStep ? 'Then set up' : 'Next \u2014 set up'} your Vault and build your goals:\n\n    hc ui\n`);
    output.write('\nIt walks you through the rest.\n');
    return 0;
  } catch (error) {
    if (error instanceof InputCancelled) {
      errorOutput.write(`human-compact: ${error.message}\n`);
      return 130;
    }
    if (error instanceof UsageError) {
      errorOutput.write(`human-compact: ${error.message}\n\n${usage()}`);
      return 2;
    }
    throw error;
  }
}

module.exports = {
  InputCancelled,
  UsageError,
  numericChoice,
  parseArgs,
  resolveChoices,
  run,
  usage,
};
