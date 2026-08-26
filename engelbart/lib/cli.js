'use strict';

const os = require('os');
const path = require('path');
const {
  ensureLauncherOnPath,
  install,
  inspectVendor,
  runCommand,
  supportedTarget,
} = require('./installer');

class UsageError extends Error {}
class InputCancelled extends Error {}

function usage() {
  return `Usage: npx engelbart-cli [options]

Options:
  --local-only          install without connecting an Engelbart account
  --non-interactive     install locally without opening a browser
  --dry-run             verify the bundled release and show the plan only
  -h, --help            show this help

Global Vault features are experimental; set HC_EXPERIMENTAL=1 to use --global-vault/--goals.
`;
}

function experimentalEnabled() {
  return process.env.HC_EXPERIMENTAL === '1';
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
    localOnly: false,
    nonInteractive: false,
    dryRun: false,
    help: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '-h' || arg === '--help') result.help = true;
    else if (arg === '--local-only') result.localOnly = true;
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
  // The flags still parse, so scripted installs keep their inert '2'; only
  // turning the global layer on is withheld from this release.
  if ((result.globalVault === '1' || result.goals === '1') && !experimentalEnabled()) {
    throw new UsageError(
      '--global-vault and --goals are experimental in this release; set HC_EXPERIMENTAL=1');
  }
  if (result.globalVault === '2' && result.goals === null) result.goals = '2';
  return result;
}

async function connectEngelbartAccount(options) {
  const env = options.env || process.env;
  const runner = options.runner || runCommand;
  const login = runner(options.bartLauncher, ['auth'], {
    env,
    stdio: 'inherit',
  });
  if (login.status !== 0) {
    throw new Error(
      'Engelbart account sign-in did not finish; the local install is usable, '
      + 'or rerun with `bart auth`',
    );
  }
  options.output.write('  account      connected; Claude credits configured\n');
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
    const env = deps.env || process.env;
    const homedir = deps.homedir || os.homedir();
    const platform = deps.platform || process.platform;
    const arch = deps.arch || process.arch;
    const target = supportedTarget(platform, arch, deps.processReport);
    const vendor = inspectVendor(packageRoot, packageJson.version);
    const managedRoot = path.resolve(
      deps.managedRoot
        || env.HUMAN_COMPACT_HOME
        || path.join(homedir, '.human-compact'),
    );

    let reach = null;
    output.write(`\nengelbart-cli ${packageJson.version}\n\n`);
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
      // Only promise `hc` in this terminal once the shell can actually find it.
      const launcher = installed && installed.launcher;
      if (launcher) {
        reach = (deps.ensureLauncherOnPath || ensureLauncherOnPath)({
          launcherDir: path.dirname(launcher),
          env,
          homedir,
        });
        if (options.localOnly || options.nonInteractive) {
          output.write('  account      local-only (browser sign-in skipped)\n');
        } else {
          await (deps.connectAccount || connectEngelbartAccount)({
            bartLauncher: installed.bartLauncher || path.join(path.dirname(launcher), 'bart'),
            env,
            homedir,
            output,
          });
        }
      }
    }
    // One status block, then one instruction. Anything the user must do to
    // make that instruction work belongs above it, not after it.
    if (reach && reach.onPath) {
      output.write(`  hc + bart    ready in this terminal\n`);
    } else if (reach) {
      output.write(`  hc + bart    need one more step (below)\n`);
    }
    // The chat hooks record from the moment they are installed -- that is what
    // lets /bart, run mid-chat, see the chat from its beginning. Only
    // analysis and injection wait for it, so those are what this line promises.
    output.write(experimentalEnabled()
      ? '\nInstalled. Chats are recorded locally; nothing is analyzed or '
        + 'injected until you run /bart in a chat.\n'
        + 'Global Vault hooks are wired (HC_EXPERIMENTAL=1); capture follows '
        + 'your global Vault setting.\n'
      : '\nInstalled. Chats are recorded locally; nothing is analyzed or '
        + 'injected until you run /bart in a chat.\n');
    if (reach && !reach.onPath) {
      output.write(reach.added
        ? `\nRun this once in this terminal (new terminals get it from ${reach.profile}):\n\n    ${reach.line}\n`
        : reach.present
        ? `\nThis terminal predates ${reach.profile}. Run this once here:\n\n    ${reach.line}\n`
        : `\nAdd this to your shell profile, then run it here:\n\n    ${reach.line}\n`);
    }
    const needsPathStep = !!(reach && !reach.onPath);
    const next = 'Open any Claude Code chat and type /bart.';
    output.write(`\n${needsPathStep ? 'Then' : 'Next'}: ${next}\n`);
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
  connectEngelbartAccount,
  numericChoice,
  parseArgs,
  resolveChoices,
  run,
  usage,
};
