'use strict';

const os = require('os');
const path = require('path');
const {
  ensureLauncherOnPath,
  install,
  inspectVendor,
  supportedTarget,
} = require('./installer');
const auth = require('./auth');

const COMMANDS = Object.freeze(['install', 'auth', 'login', 'logout', 'whoami', 'env']);

class UsageError extends Error {}
class InputCancelled extends Error {}

function usage() {
  return `Usage: npx engelbart-cli [command] [options]

Commands:
  install               install the runtime and connect this machine (default)
  auth, login           connect this machine to your Engelbart account
  logout                disconnect this machine and revoke its token
  whoami                show which account this machine is connected to
  env                   print the shell exports that point Claude Code at your
                        credit; use as: eval "$(npx engelbart-cli env)"

Options:
  --local-only          install without connecting an Engelbart account
  --non-interactive     install locally without opening a browser
  --no-open             install without opening the setup page
  --dry-run             verify the bundled release and show the plan only
  -h, --help            show this help

Set ENGELBART_API_BASE to point at a deployment other than
${auth.DEFAULT_API_BASE}.

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
    command: 'install',
    globalVault: null,
    goals: null,
    nonInteractive: false,
    dryRun: false,
    localOnly: false,
    noOpen: false,
    help: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    // A bare first word is the command; everything else must be a flag, so a
    // typo still fails loudly instead of installing something unasked.
    if (index === 0 && !arg.startsWith('-')) {
      if (!COMMANDS.includes(arg)) throw new UsageError(`unknown command: ${arg}`);
      result.command = arg === 'login' ? 'auth' : arg;
    }
    else if (arg === '-h' || arg === '--help') result.help = true;
    else if (arg === '--non-interactive') result.nonInteractive = true;
    else if (arg === '--dry-run') result.dryRun = true;
    else if (arg === '--no-open') result.noOpen = true;
    // `--no-login` was this flag's name before the rename; both spellings
    // mean the same thing, so a script written against either still works.
    else if (arg === '--local-only' || arg === '--no-login') result.localOnly = true;
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

// Connecting an account means opening a browser and waiting for a person, so
// it happens only where there is a person: never in CI, never down a pipe.
function canPrompt(deps = {}) {
  if (deps.interactive !== undefined) return deps.interactive;
  const env = deps.env || process.env;
  if (env.CI) return false;
  return Boolean(process.stdin.isTTY && process.stdout.isTTY);
}

function accountClaude(account) {
  if (!account || account.status !== 'ready') return null;
  return account.claude || (account.stored && account.stored.claude) || null;
}

function setupEnvironment(account, env) {
  const claude = accountClaude(account);
  if (!claude || !claude.apiKey || account.projectConfigured === false) return null;
  return {
    ...env,
    ANTHROPIC_BASE_URL: claude.baseUrl,
    ANTHROPIC_AUTH_TOKEN: claude.apiKey,
    // Provider subprocesses normally strip shell API keys in favour of the
    // reader's Claude subscription. Setup is the exception: this process was
    // opened by the device flow specifically to use the key it just issued.
    HC_USE_API_KEY: '1',
  };
}

async function runAccountCommand(command, options, authDeps, deps, errorOutput) {
  const output = authDeps.output;
  if (command === 'auth') {
    const result = await (deps.login || auth.login)(authDeps);
    if (result.status !== 'ready') return 1;
    const setupEnv = setupEnvironment(result, authDeps.env);
    if (!setupEnv) {
      output.write('\nSetup is waiting for both Claude credits and Supabase sync. '
        + 'Run `npx engelbart-cli auth` again after the missing connection is ready.\n');
      return 0;
    }
    const launcher = deps.launcher || path.join(authDeps.managedRoot, 'bin', 'hc');
    const opened = options.noOpen
      ? null
      : await (deps.openSetup || openSetup)({
        launcher, env: setupEnv, output, spawn: deps.spawn,
      });
    output.write(opened
      ? `\nNext: Setting up your first project: ${opened}\n`
      : '\nNext: Run `hc setup-ui` to set up your first project.\n');
    return 0;
  }
  if (command === 'logout') {
    const result = await (deps.logout || auth.logout)(authDeps);
    if (!result.signedOut) {
      output.write('This machine is not connected to an Engelbart account.\n');
      return 0;
    }
    // A token that could not be revoked is still gone from this disk, but the
    // member is the only one who can close it everywhere.
    output.write(result.revoked
      ? 'Disconnected. That token is revoked.\n'
      : 'Disconnected on this machine. The token could not be revoked; sign in at '
        + '/engelbart and disconnect it there if this machine is not yours.\n');
    return 0;
  }
  // Only the exports reach stdout, so `eval` gets a shell script and nothing
  // a shell would choke on. Everything explanatory goes to stderr.
  if (command === 'env') {
    const stored = (deps.readCredentials || auth.readCredentials)(authDeps.managedRoot, authDeps.env);
    const lines = auth.claudeEnv(stored);
    if (!lines) {
      errorOutput.write(stored
        ? 'This account has no Claude key yet. Run `npx engelbart-cli auth` again once your credit is ready.\n'
        : 'Not connected. Run `npx engelbart-cli auth` to connect this machine.\n');
      return 1;
    }
    output.write(lines);
    return 0;
  }
  const result = await (deps.whoami || auth.whoami)(authDeps);
  if (result.signedIn) {
    output.write(`Connected as ${result.email}.\n`);
    return 0;
  }
  errorOutput.write(result.reason
    ? `Not connected: ${result.reason}\nRun \`npx engelbart-cli auth\` to connect this machine.\n`
    : 'Not connected. Run `npx engelbart-cli auth` to connect this machine.\n');
  return 1;
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
    const env = deps.env || process.env;
    const homedir = deps.homedir || os.homedir();
    const managedRoot = path.resolve(
      deps.managedRoot
        || env.HUMAN_COMPACT_HOME
        || path.join(homedir, '.human-compact'),
    );
    const authDeps = {
      managedRoot,
      env,
      homedir,
      output,
      // The one place that knows it is running on a member's real machine, and
      // so the one place permitted to write their Claude Code settings.
      allowRealHome: true,
      fetchImpl: deps.fetchImpl,
      openUrl: deps.openUrl,
      wait: deps.wait,
      now: deps.now,
      hostname: deps.hostname,
    };
    if (options.command !== 'install') {
      return await runAccountCommand(options.command, options, authDeps, deps, errorOutput);
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

    let reach = null;

    let launcherPath = '';
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
      launcherPath = launcher || '';
      if (launcher) {
        reach = (deps.ensureLauncherOnPath || ensureLauncherOnPath)({
          launcherDir: path.dirname(launcher),
          env,
          homedir,
        });
      }
    }
    // One status block, then one instruction. Anything the user must do to
    // make that instruction work belongs above it, not after it.
    if (reach && reach.onPath) {
      output.write(`  hc + bart    ready in this terminal\n`);
    } else if (reach) {
      output.write(`  hc + bart    need one more step (below)\n`);
    }
    // The install stands on its own. An account adds the hosted Claude
    // credits to it, so failing to connect one is reported, never fatal.
    let account = null;
    if (!options.dryRun && !options.localOnly && !options.nonInteractive) {
      const stored = (deps.readCredentials || auth.readCredentials)(managedRoot, authDeps.env);
      if (stored) {
        account = {
          status: 'ready',
          email: stored.email || '',
          reused: true,
          stored,
          projectConfigured: stored.projectConfigured,
        };
        output.write(`  account      ${stored.email || 'connected'}\n`);
      } else if (canPrompt(deps)) {
        try {
          account = await (deps.login || auth.login)(authDeps);
        } catch (error) {
          errorOutput.write(`\nCould not connect an Engelbart account: ${error.message}\n`);
        }
      }
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
    const setupEnv = setupEnvironment(account, env);
    const accountReady = Boolean(setupEnv);
    // The one instruction. Someone who has just installed has no chat and no
    // project, so "open a chat and type /bart" is an instruction with a blank
    // screen at the end of it -- the setup page is what asks them which of
    // those two things they are actually doing. It is only offered where it
    // can be followed: a launcher that is not yet on PATH cannot be run, and
    // a step the reader must do first belongs above the instruction, not
    // after it.
    // Authentication is the gate, not a parallel branch: setup calls Claude,
    // so opening it before the issued key exists produces a first screen that
    // cannot answer. Pass that freshly-issued key to the detached setup server
    // as well as wiring Claude Code, so a pre-existing foreign apiKeyHelper
    // cannot make onboarding silently use the wrong account.
    const opened = (accountReady && !needsPathStep && launcherPath && !options.noOpen)
      ? await (deps.openSetup || openSetup)({ launcher: launcherPath, env: setupEnv,
                                              output, spawn: deps.spawn })
      : null;
    if (!options.dryRun && !accountReady) {
      output.write('\nNext: Run `npx engelbart-cli auth` to finish connecting your '
        + 'Engelbart account, Claude credits, and Supabase sync. Setup starts '
        + 'after that.\n');
    } else {
      const next = opened
        ? `Setting up your first project: ${opened}`
        : needsPathStep
        ? 'Run the line above, then `hc setup-ui` to set up your first project.'
        : 'Run `hc setup-ui` to set up your first project.';
      output.write(`\n${needsPathStep ? 'Then' : 'Next'}: ${next}\n`);
    }
    if (opened) {
      output.write('Already have a project? Open its chat with `claude -r`'
        + ' and type /bart.\n');
    }
    if (accountReady && account.reused && auth.claudeEnv(account.stored)) {
      // A fresh pairing prints this itself; a reused one has to be told, or a
      // second install looks like it forgot the key it is already holding.
      output.write('\nRun this once here so `claude` uses your credit:\n\n    source '
        + `${auth.envPath(managedRoot)}\n`);
    }
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

/* Open the setup page for someone who has just installed.
 *
 * The launcher does the work -- minting a workspace, starting a server and
 * printing the URL -- so this only has to run it and report what it said.
 * Never fatal: a browser that will not open, or a launcher that will not
 * run, leaves the reader with a command to type rather than a failed
 * install, so anything that goes wrong here answers null and the caller
 * prints the instruction instead.
 */
async function openSetup({ launcher, env, output, spawn }) {
  const run = spawn || require('child_process').spawnSync;
  try {
    const done = run(launcher, ['setup-ui'], {
      env,
      encoding: 'utf8',
      timeout: 20000,
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    if (!done || done.status !== 0) return null;
    const said = String(done.stdout || '').trim().split('\n');
    const url = said.reverse().find((line) => line.startsWith('http://127.0.0.1:'));
    return url || null;
  } catch (error) {
    if (output && process.env.HC_DEBUG) {
      output.write(`  setup        not opened (${error.message})\n`);
    }
    return null;
  }
}

module.exports = {
  COMMANDS,
  InputCancelled,
  UsageError,
  canPrompt,
  numericChoice,
  openSetup,
  parseArgs,
  resolveChoices,
  run,
  runAccountCommand,
  usage,
};
