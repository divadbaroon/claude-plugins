'use strict';

const os = require('os');
const path = require('path');
const readline = require('readline');
const { install, inspectVendor, supportedTarget } = require('./installer');

class UsageError extends Error {}
class InputCancelled extends Error {}

function createAnswerReader(input, output) {
  const interface_ = readline.createInterface({
    input,
    output,
    terminal: Boolean(input.isTTY && output.isTTY),
    crlfDelay: Infinity,
  });
  const iterator = interface_[Symbol.asyncIterator]();
  return {
    async question(prompt) {
      output.write(prompt);
      const answer = await iterator.next();
      if (answer.done) throw new Error('end of input');
      return answer.value;
    },
    close() { interface_.close(); },
  };
}

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

async function promptChoice(rl, output, question, options) {
  output.write(`${question}\n`);
  for (const [value, label] of options) output.write(`  ${value}. ${label}\n`);
  for (;;) {
    let answer;
    try {
      answer = (await rl.question('Choose [1/2]: ')).trim();
    } catch (error) {
      throw new InputCancelled('input closed before installation choices were complete', { cause: error });
    }
    if (answer === '1' || answer === '2') return answer;
    output.write('Enter 1 or 2.\n');
  }
}

async function resolveChoices(options, io = {}) {
  const input = io.input || process.stdin;
  const output = io.output || process.stdout;
  let { globalVault, goals } = options;
  if (options.nonInteractive) {
    if (globalVault === null) {
      throw new UsageError('--non-interactive requires --global-vault 1 or 2');
    }
    if (globalVault === '1' && goals === null) {
      throw new UsageError('--non-interactive with Vault enabled requires --goals 1 or 2');
    }
    return { globalVault, goals: goals || '2' };
  }

  const rl = io.readline || createAnswerReader(input, output);
  const ownsReadline = !io.readline;
  try {
    if (globalVault === null) {
      globalVault = await promptChoice(
        rl,
        output,
        'Enable the global Vault?',
        [['1', 'Yes'], ['2', 'No']],
      );
    }
    if (globalVault === '1' && goals === null) {
      goals = await promptChoice(
        rl,
        output,
        'Infer global goals now?',
        [
          ['1', 'Yes — analyze history now (Claude mode sends bounded conversation digests to Anthropic)'],
          ['2', 'No'],
        ],
      );
    }
    if (globalVault === '2' && goals === '1') {
      throw new UsageError('--goals 1 requires --global-vault 1');
    }
    return { globalVault, goals: goals || '2' };
  } finally {
    if (ownsReadline) rl.close();
  }
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
      readline: deps.readline,
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

    output.write('\nhuman-compact · Claude Code goal workspaces\n\n');
    if (options.dryRun) {
      output.write(`Verified bundled backend ${vendor.version} (${vendor.sha256.slice(0, 12)}).\n`);
      output.write(`Target: ${target.name}; managed runtime: ${managedRoot}\n`);
      output.write(`Plan: global Vault ${choices.globalVault === '1' ? 'enabled' : 'disabled'}; global goals ${choices.goals === '1' ? 'build now' : 'skip'}.\n`);
    } else {
      await (deps.install || install)({
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
    }
    output.write('\nDone. Start a new Claude Code session (or run /reload-plugins), then run /hc-ui.\n');
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
  createAnswerReader,
  numericChoice,
  parseArgs,
  promptChoice,
  resolveChoices,
  run,
  usage,
};
