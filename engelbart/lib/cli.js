'use strict';

const os = require('os');
const path = require('path');
const {
  ensureLauncherOnPath,
  install,
  inspectVendor,
  parseClaudeVersion,
  requireCompatibleClaude,
  spawnableLauncher,
  supportedTarget,
  MIN_CLAUDE_VERSION_TEXT,
} = require('./installer');
const auth = require('./auth');
const { invocation } = require('./invocation');

const COMMANDS = Object.freeze(['install', 'auth', 'login', 'logout', 'whoami', 'env']);

class UsageError extends Error {}
class InputCancelled extends Error {}

function usage() {
  return `Usage: ${invocation()} [command] [options]

Commands:
  install               install the runtime and connect this machine (default)
  auth, login           connect this machine to your Engelbart account
  logout                disconnect this machine and revoke its token
  whoami                show which account this machine is connected to
  env                   print the shell exports that point Claude Code at your
                        credit; use as: eval "$(${invocation()} env)"

Options:
  --code XXXX-XXXX-XXXX connect with the setup code from /engelbart/setup
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
    code: null,
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
    else if (arg === '--code') {
      if (index + 1 >= argv.length) throw new UsageError('--code requires a setup code');
      result.code = String(argv[index += 1]).trim();
    }
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
  if (result.code && result.localOnly) {
    throw new UsageError('--code connects an Engelbart account; drop --local-only');
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
  // A key the pool has stopped honouring is not a credential, it is a 401 with
  // a delay. Forcing it on setup is worse than passing nothing: HC_USE_API_KEY
  // makes the provider keep it instead of falling back, so setup fails on a
  // dead key while the member's own working Claude login sits right there. Out
  // of credit has to mean "setup runs on your account", the same fallback the
  // credential helper takes when it unwires itself. The ambient shell may
  // still carry that same dead key -- `engelbart env` exported it -- so a
  // plain copy of env is not clean enough: strip the wiring too.
  if (auth.spent(claude)) {
    const clean = { ...env };
    delete clean.ANTHROPIC_AUTH_TOKEN;
    delete clean.ANTHROPIC_BASE_URL;
    delete clean.HC_USE_API_KEY;
    return clean;
  }
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
    // A setup code stands in for the whole device flow: no browser, no poll.
    const result = options.code
      ? await (deps.redeemCode || auth.redeemCode)(options.code, authDeps)
      : await (deps.login || auth.login)(authDeps);
    if (result.status !== 'ready') return 1;
    const setupEnv = setupEnvironment(result, authDeps.env);
    if (!setupEnv) {
      output.write('\nSetup is waiting for both Claude credits and Supabase sync. '
        + `Run \`${invocation()} auth\` again after the missing connection is ready.\n`);
      return 0;
    }
    // On Windows the stable bin\hc.cmd shim is not directly spawnable; resolve
    // the runtime's real .exe. On POSIX this is the bin/hc symlink as before.
    const launcher = deps.launcher
      || spawnableLauncher(authDeps.managedRoot, 'hc', deps.platform || process.platform);
    const opened = options.noOpen
      ? null
      : await (deps.openSetup || openSetup)({
        launcher, env: setupEnv, output, spawn: deps.spawn,
      });
    if (options.code && options.noOpen) {
      // Same situation as the installer's: a code plus --no-open is the
      // browser flow, and the browser is where the setup continues.
      output.write(`\nConnected as ${result.email || 'your Engelbart account'}. `
        + 'Go back to your browser tab to keep setting up your project.\n');
      return 0;
    }
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
    if (!stored) {
      errorOutput.write(`Not connected. Run \`${invocation()} auth\` to connect this machine.\n`);
      return 1;
    }
    // Fetched, not read back: this machine does not keep the key. That also
    // makes these exports current rather than whatever was true at sign-in.
    let claude = null;
    try {
      claude = await (deps.fetchClaudeKey || auth.fetchClaudeKey)(
        auth.apiBase(authDeps.env),
        stored.token,
        authDeps,
      );
    } catch (error) {
      errorOutput.write(`Could not fetch this account's Claude key: ${error.message}\n`);
      return 1;
    }
    const lines = auth.claudeEnv(claude);
    if (!lines) {
      // Two different situations, and telling a member out of credit that they
      // have "no key yet" sends them to re-run auth, which cannot help.
      errorOutput.write(auth.spent(claude)
        ? 'Your Engelbart Claude credit is used up, so there is nothing to export. '
          + '`claude` will use your own account until it is topped up.\n'
        : `This account has no Claude key yet. Run \`${invocation()} auth\` `
          + 'again once your credit is ready.\n');
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
    ? `Not connected: ${result.reason}\nRun \`${invocation()} auth\` to connect this machine.\n`
    : `Not connected. Run \`${invocation()} auth\` to connect this machine.\n`);
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
    let env = deps.env || process.env;
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
    // Injected by the compiled (bun --compile) entry, where __dirname points
    // into the binary's virtual filesystem and a runtime require cannot work.
    const packageJson = deps.packageJson || require(path.join(packageRoot, 'package.json'));
    const platform = deps.platform || process.platform;
    const arch = deps.arch || process.arch;
    const target = supportedTarget(platform, arch, deps.processReport);
    const vendor = inspectVendor(packageRoot, packageJson.version);

    // Claude Code is a hard requirement. A pipe or session runner may remove
    // the TTY even though this is a real install, so only CI and dry runs
    // suppress the bootstrap. The compatibility check is deliberately the
    // same gate install() applies: a runnable but too-old `claude` needs an
    // update, not a late failure after the rest of the command has started.
    if (!options.dryRun && !env.CI) {
      // `claudeOnPath` was the original test seam. Keep it as a compatibility
      // shim for callers that model only present/missing; production always
      // uses the full version probe below.
      const probe = deps.claudeInstallState
        || (deps.claudeOnPath
          ? (currentEnv, spawn) => (deps.claudeOnPath(currentEnv, spawn)
            ? { state: 'compatible' }
            : { state: 'missing' })
          : claudeInstallState);
      const inspect = (candidateEnv) => probe(candidateEnv, deps.spawn);
      let claude = inspect(env);

      // Anthropic's native installer always owns ~/.local/bin/claude. A shell
      // opened before that directory was added to PATH can therefore report
      // "missing" (or hit an older broken launcher) while a working install is
      // already on disk. Prefer the known native launcher before downloading
      // anything. This also repairs the current child process; Anthropic's
      // installer owns the persistent shell integration for later terminals.
      if (claude.state !== 'compatible') {
        const nativeEnv = nativeClaudeEnvironment(env, homedir, platform);
        if (nativeEnv !== env) {
          const native = inspect(nativeEnv);
          // A compatible native install is always preferable to a stale or
          // broken launcher earlier on PATH. When PATH has no Claude at all,
          // preserve every non-missing native state so an old install gets
          // updated and a broken one gets diagnosed rather than overwritten.
          if (native.state === 'compatible'
              || (claude.state === 'missing' && native.state !== 'missing')) {
            output.write(native.state === 'compatible'
              ? `\nFound the working native Claude Code ${native.version} outside this terminal's PATH; using it without reinstalling.\n`
              : '\nFound Claude Code in the standard native location outside this terminal\'s PATH; checking that installation before making changes.\n');
            output.write(platform === 'win32'
              ? 'Open a new terminal before launching Claude directly so it picks up the native installer PATH.\n'
              : 'To launch Claude directly from this terminal afterward, run:\n\n    export PATH="$HOME/.local/bin:$PATH"\n\n');
            env = nativeEnv;
            authDeps.env = env;
            claude = native;
          }
        }
      }

      if (claude.state === 'missing') {
        env = await (deps.installClaudeCode || installClaudeCode)({
          env, output, errorOutput, platform, deps,
        });
        authDeps.env = env;
        claude = inspect(env);
      } else if (claude.state === 'outdated') {
        const updateFinished = await (deps.updateClaudeCode || updateClaudeCode)({
          env, output, errorOutput, version: claude.version, deps,
        });
        claude = inspect(env);
        if (claude.state === 'compatible' && !updateFinished) {
          output.write(`Claude Code now reports ${claude.version}; continuing despite the updater's nonzero exit.\n`);
        }
        if (claude.state !== 'compatible') {
          // `claude update` can exit zero for a package-manager install while
          // leaving the selected executable untouched. The official native
          // installer is Anthropic's documented escape hatch for those stale
          // and permission-broken installs. Its PATH is re-probed below; an
          // exit code alone is never accepted as proof of repair.
          env = await (deps.installClaudeCode || installClaudeCode)({
            env, output, errorOutput, platform, deps, repair: true,
          });
          authDeps.env = env;
          claude = inspect(env);
        }
      }
      if (claude.state !== 'compatible') {
        throw claudeStateError(claude, platform);
      }
    }

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
        deps: { env, ...(deps.installerDeps || {}) },
      });
      // Only promise `hc` in this terminal once the shell can actually find it.
      const launcher = installed && installed.launcher;
      // PATH guidance points at the stable launcher's dir; spawning setup uses a
      // directly-spawnable target (the runtime .exe on Windows, the symlink on POSIX).
      launcherPath = launcher
        ? spawnableLauncher(managedRoot, 'hc', platform)
        : '';
      if (launcher) {
        reach = (deps.ensureLauncherOnPath || ensureLauncherOnPath)({
          launcherDir: path.dirname(launcher),
          env,
          homedir,
          platform,
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
    // A setup code is the one way in that needs no browser and no prompt,
    // which is why it lifts the --non-interactive gate.
    let account = null;
    if (!options.dryRun && !options.localOnly
        && (options.code || !options.nonInteractive)) {
      const stored = (deps.readCredentials || auth.readCredentials)(managedRoot, authDeps.env);
      if (options.code) {
        // An explicit code wins over stored credentials: rebinding this
        // machine to the account that issued the code is what the member
        // asked for by pasting it. Said out loud when it changes accounts.
        try {
          account = await (deps.redeemCode || auth.redeemCode)(options.code, authDeps);
        } catch (error) {
          errorOutput.write(`\nCould not redeem that setup code: ${error.message}\n`);
        }
        if (account && stored && stored.email && account.email
            && stored.email !== account.email) {
          output.write(`  note         this machine was connected to ${stored.email}; `
            + `it is now connected to ${account.email}\n`);
        }
      }
      if (!account && stored) {
        output.write(`  account      ${stored.email || 'connected'}\n`);
        // The key is not on this disk any more, so a reinstall cannot read one
        // back out of the stored record -- it has to ask for a fresh one. The
        // token this machine already holds is enough for that, so this costs
        // the member nothing: no browser, no code to approve.
        try {
          account = await (deps.rewire || auth.rewire)(auth.apiBase(authDeps.env), authDeps);
        } catch (error) {
          account = null;
        }
        if (account) account.reused = true;
        else {
          account = {
            status: 'ready',
            email: stored.email || '',
            reused: true,
            stored,
            projectConfigured: stored.projectConfigured,
          };
        }
      } else if (!account && canPrompt(deps)) {
        try {
          account = await (deps.login || auth.login)(authDeps);
        } catch (error) {
          errorOutput.write(`\nCould not connect an Engelbart account: ${error.message}\n`);
        }
      }
    }
    if (options.dryRun) {
      output.write('\nDry run complete. No files or settings were changed.\n');
      return 0;
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
      output.write(platform === 'win32'
        // Windows PATH is a per-user registry value, not a profile file: setx
        // writes it for new terminals; the caller still needs it in this one.
        ? `\nAdd the launcher to your PATH (takes effect in new terminals):\n\n    ${reach.line}\n`
        : reach.added
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
    // those two things they are actually doing. setup-ui is launched through
    // the runtime executable's absolute path, so it must not wait for a new
    // terminal just because the stable `hc` command is not yet on PATH.
    // Authentication is the gate, not a parallel branch: setup calls Claude,
    // so opening it before the issued key exists produces a first screen that
    // cannot answer. Pass that freshly-issued key to the detached setup server
    // as well as wiring Claude Code, so a pre-existing foreign apiKeyHelper
    // cannot make onboarding silently use the wrong account.
    // A project approved on the web comes first: the account may hold the
    // setup this member already finished in the browser, and asking them to
    // describe the work a second time would waste the conversation they had.
    // The claim is single-use on the server, so a payload that cannot be
    // materialized is written to disk rather than lost. Unlike the setup
    // page below, this does not wait for the PATH step: the launcher is
    // spawned by its absolute path, and a first install -- the machine the
    // web flow exists for -- always still needs that step.
    let imported = null;
    if (accountReady && launcherPath
        && account && account.stored && account.stored.token) {
      const payload = await (deps.fetchPendingSetup || auth.fetchPendingSetup)(
        auth.apiBase(authDeps.env), account.stored.token, authDeps);
      if (payload) {
        imported = (deps.importSetup || importSetup)({
          launcher: launcherPath,
          env: setupEnv,
          payload,
          managedRoot,
          errorOutput,
          spawn: deps.spawn,
        });
      }
    }
    const opened = (!imported && accountReady && launcherPath
                    && !options.noOpen)
      ? await (deps.openSetup || openSetup)({ launcher: launcherPath, env: setupEnv,
                                              output, spawn: deps.spawn })
      : null;
    if (!options.dryRun && !accountReady) {
      output.write(`\nNext: Run \`${invocation()} auth\` to finish connecting your `
        + 'Engelbart account, Claude credits, and Supabase sync. Setup starts '
        + 'after that.\n');
    } else if (imported && imported.url) {
      output.write(`\nNext: Your project${imported.name ? ` "${imported.name}"` : ''}`
        + ` is open in the workspace: ${imported.url}\n`);
    } else if (imported) {
      // The recovery instructions are already on stderr; repeating them as
      // a "Next" would bury the command they name.
    } else if (options.code && options.noOpen && account) {
      // The web flow's own install line: the reader is mid-setup in a
      // browser tab, the site saves their project only at the end, and the
      // chat's first /bart claims it. Pointing them at a setup page or a
      // command here would start a second setup beside the one they are in.
      output.write(`\nConnected as ${account.email || 'your Engelbart account'}. `
        + 'Go back to your browser tab to keep setting up your project.\n');
    } else {
      const next = opened
        ? `Setting up your first project: ${opened}`
        : needsPathStep
        ? 'Run the line above, then `hc setup-ui` to set up your first project.'
        : 'Run `hc setup-ui` to set up your first project.';
      output.write(`\n${needsPathStep && !opened ? 'Then' : 'Next'}: ${next}\n`);
    }
    if (opened || (imported && imported.url)) {
      output.write('Already have a project? Open its chat with `claude -r`'
        + ' and type /bart.\n');
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

const CLAUDE_INSTALL_URL = 'https://claude.ai/install.sh';
const CLAUDE_INSTALL_URL_PS1 = 'https://claude.ai/install.ps1';

function nativeClaudeEnvironment(env, homedir, platform = process.platform) {
  const localBin = path.join(homedir || os.homedir(), '.local', 'bin');
  const delimiter = platform === 'win32' ? ';' : ':';
  const entries = String(env.PATH || '').split(delimiter).filter(Boolean);
  const normalize = (entry) => {
    const resolved = path.resolve(entry);
    return platform === 'win32' ? resolved.toLowerCase() : resolved;
  };
  if (entries.length && normalize(entries[0]) === normalize(localBin)) return env;
  const withoutNative = entries.filter((entry) => normalize(entry) !== normalize(localBin));
  return { ...env, PATH: [localBin, ...withoutNative].join(delimiter) };
}

function boundedDetail(value) {
  return String(value || '').replace(/\s+/g, ' ').trim().slice(0, 240);
}

function claudeStateError(claude, platform = process.platform) {
  const doctor = platform === 'win32' ? 'where claude, then claude doctor' : 'which -a claude, then claude doctor';
  if (claude.state === 'broken') {
    return new Error('A command named `claude` was found but could not run'
      + `${claude.reason ? ` (${claude.reason})` : ''}. Engelbart left it untouched rather than overwriting an installed program. `
      + `Run ${doctor}, fix the reported launcher or permission problem, and retry.`);
  }
  if (claude.state === 'unrecognized') {
    return new Error('The `claude` command on PATH did not identify itself as Claude Code'
      + `${claude.output ? ` (${JSON.stringify(claude.output)})` : ''}. `
      + `Run ${doctor} and remove or update the conflicting command.`);
  }
  if (claude.state === 'outdated') {
    return new Error(`Claude Code ${claude.version || '(unknown)'} is still below the required ${MIN_CLAUDE_VERSION_TEXT} after both update paths. `
      + `Run ${doctor}, update the installation it selects, and retry.`);
  }
  return new Error('Anthropic\'s Claude Code installer finished, but `claude --version` is still not runnable. '
    + `Run ${doctor}, make sure ~/.local/bin is reachable, and retry.`);
}

/* Is there any command named `claude` on this PATH? Only ENOENT means absent;
 * a nonzero exit or failed launch still means an installed command needs
 * diagnosis rather than replacement. The full state gate decides what follows.
 */
function claudeOnPath(env, spawn) {
  return claudeInstallState(env, spawn).state !== 'missing';
}

/*
 * Separate a missing binary from a stale Claude Code installation before the
 * main installer mutates anything. Version output we cannot positively
 * identify is not sent `update`: a command named `claude` could be another
 * program, and the existing fail-closed preflight remains the safe outcome.
 */
function claudeInstallState(env, spawn) {
  const run = spawn || require('child_process').spawnSync;
  try {
    const done = run('claude', ['--version'], {
      env,
      encoding: 'utf8',
      timeout: 20000,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (!done) return { state: 'broken', reason: 'the version probe returned no result' };
    if (done.error) {
      if (done.error.code === 'ENOENT') return { state: 'missing' };
      return { state: 'broken', reason: boundedDetail(done.error.message || done.error.code) || 'the version probe failed' };
    }
    if (done.status !== 0) {
      const detail = boundedDetail(done.stderr || done.stdout);
      const ended = done.signal ? `ended with ${done.signal}` : `exited with code ${done.status}`;
      return { state: 'broken', reason: detail ? `${ended}: ${detail}` : ended };
    }
    const output = String(done.stdout || '').trim() || String(done.stderr || '').trim();
    const parsed = parseClaudeVersion(output);
    if (!parsed) return { state: 'unrecognized', output };
    try {
      return { state: 'compatible', version: requireCompatibleClaude(output) };
    } catch (error) {
      return { state: 'outdated', version: parsed.join('.') };
    }
  } catch (error) {
    if (error && error.code === 'ENOENT') return { state: 'missing' };
    return { state: 'broken', reason: boundedDetail(error && error.message) || 'the version probe failed' };
  }
}

/* Run Anthropic's official Claude Code installer, no questions asked: the
 * one command promises a working install, and Claude Code is part of what
 * working means. CI and dry runs bypass this bootstrap; interactive and
 * explicitly non-interactive member installs both use it. The line below says
 * what is happening and whose installer is doing it before anything runs.
 *
 * The installer wires new shells but not this process, so on success the
 * returned env reaches its ~/.local/bin directly; install()'s own preflight
 * then judges the result. A failed download is fatal here: continuing would
 * let the later Engelbart success copy contradict the state of the machine.
 */
async function installClaudeCode({ env, output, errorOutput, platform = process.platform, deps = {}, repair = false }) {
  const windows = platform === 'win32';
  const url = windows ? CLAUDE_INSTALL_URL_PS1 : CLAUDE_INSTALL_URL;
  output.write(repair
    ? '\nThe selected Claude Code did not update -- repairing it '
      + `with Anthropic's official native installer (${url})...\n\n`
    : '\nClaude Code is required and was not found -- installing it '
    + `with Anthropic's official installer (${url})...\n\n`);
  const run = deps.spawn || require('child_process').spawnSync;
  // Windows ships no bash; its official installer is a PowerShell one-liner.
  // POSIX needs pipefail: without it a failed curl feeds an empty program to
  // bash, whose zero exit status falsely reports a successful installation.
  const [command, args] = windows
    ? ['powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
      `$ErrorActionPreference = 'Stop'; Invoke-RestMethod '${url}' | Invoke-Expression`]]
    : ['bash', ['-o', 'pipefail', '-c', `curl -fsSL ${url} | bash`]];
  const done = run(command, args, { env, stdio: 'inherit', timeout: 600000 });
  if (!done || done.error || done.status !== 0) {
    const detail = done && done.error ? ` (${boundedDetail(done.error.message)})` : '';
    errorOutput.write(`\nThe Claude Code installer did not finish${detail}.\n`);
    throw new Error('Claude Code could not be installed automatically. Run Anthropic\'s installer manually, then retry Engelbart.');
  }
  // The installer wires new shells, not this process. Point PATH at the dir it
  // drops `claude` into so install()'s preflight can find it right away; the
  // native installer uses ~/.local/bin on every platform, Windows included.
  const localBin = path.join(deps.homedir || os.homedir(), '.local', 'bin');
  return { ...env, PATH: `${localBin}${path.delimiter}${env.PATH || ''}` };
}

// Claude Code owns its installation mechanism. Using its documented update
// command keeps an existing npm, native, or managed installation in place
// instead of replacing it with a different installation type.
async function updateClaudeCode({ env, output, errorOutput, version, deps = {} }) {
  output.write(`\nClaude Code ${version} is below the required ${MIN_CLAUDE_VERSION_TEXT} -- updating it...\n\n`);
  const run = deps.spawn || require('child_process').spawnSync;
  try {
    const done = run('claude', ['update'], { env, stdio: 'inherit', timeout: 120000 });
    if (done && !done.error && done.status === 0) return true;
  } catch (error) {
    // The caller emits one actionable error below; avoid exposing platform-
    // specific spawn internals as though they were a member-facing remedy.
  }
  return false;
}

/* Materialize a project the member approved on the web.
 *
 * The launcher does the work -- `hc setup-import` reads the payload on
 * stdin, creates the project and its goal tree through the same commit path
 * the local setup page uses, starts the workspace on it, and prints its URL
 * as the last line, the same contract `setup-ui` keeps. The claim that
 * fetched this payload was single-use, so a payload that cannot be
 * materialized (a duplicate project name, a broken runtime) is written to
 * disk with the command that retries it rather than lost.
 */
function importSetup({ launcher, env, payload, managedRoot, errorOutput, spawn }) {
  const run = spawn || require('child_process').spawnSync;
  let done;
  try {
    done = run(launcher, ['setup-import', '--stdin'], {
      env,
      encoding: 'utf8',
      timeout: 60000,
      input: JSON.stringify(payload),
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch (error) {
    done = { status: -1, stderr: error.message };
  }
  if (done && done.status === 0) {
    const said = String(done.stdout || '').trim().split('\n');
    const url = said.reverse().find((line) => line.startsWith('http://127.0.0.1:'));
    if (url) return { url, name: String((payload && payload.name) || '') };
  }
  const file = path.join(managedRoot, 'pending-setup.json');
  try {
    require('fs').writeFileSync(file, `${JSON.stringify(payload, null, 2)}\n`, { mode: 0o600 });
    const detail = String((done && (done.stderr || done.stdout)) || '').trim();
    if (detail) errorOutput.write(`\n${detail}\n`);
    errorOutput.write(`\nCould not open the project you set up on the web. It is saved at\n`
      + `${file};\nfix the issue above, then run:\n\n    hc setup-import --file ${file}\n`);
  } catch (error) {
    errorOutput.write(`\nCould not import or save your web setup: ${error.message}\n`);
  }
  return { failed: true };
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
  claudeInstallState,
  claudeOnPath,
  claudeStateError,
  importSetup,
  installClaudeCode,
  numericChoice,
  nativeClaudeEnvironment,
  openSetup,
  parseArgs,
  resolveChoices,
  run,
  runAccountCommand,
  updateClaudeCode,
  usage,
};
