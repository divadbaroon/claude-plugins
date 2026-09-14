# New Project: local Railpack analysis

The production workspace header's **New Project** opens a modal. Enter an
absolute local repository path (or use **Choose folder**, the existing native
`pick_directory` operation), then **Analyze project**. The dialog stays open
while analyzing and after completion. **Done** only dismisses it. This milestone
does not create a project, goal or todo, or run any proposed command.

The browser calls the local runtime's existing JSON `POST /api/op` boundary:

```json
{"op":"analyze_project","path":"/Users/divadbaroon/Projects/berkeley-research"}
```

This is behind existing loopback Host/Origin/media-type validation, chat-scope
checking and shared-workspace refusal. Analysis runs outside the goal state lock.
It never calls Vercel, a model, the preview engine, or a build executor.

## CLI and outputs

Verified against the official Railpack **0.39.0** executable and its help:

```sh
railpack prepare --plan-out /TEMP/hc-railpack-ID/plan.json --info-out /TEMP/hc-railpack-ID/info.json /absolute/project/path
```

`TEMP` is a new system temporary directory, also used as the subprocess cwd.
No shell is used. Only `prepare` runs; generated install/build/start commands
remain strings. The response contains `plan`, `info`, original `rawPlan` and
`rawInfo` JSON strings, `stdout`, `stderr`, `exitCode`, `name` and `path`.
The UI renders text, never HTML from Railpack. The raw view retains JSON and
console output. No analysis is persisted to the selected repository.

Railpack must already be installed on the local machine's PATH. Optionally set
`HC_RAILPACK_BIN` in the **hc server environment** to an executable path. The
browser cannot choose an executable or pass CLI flags. hc does not install
Railpack or project dependencies. An absent CLI is a retryable modal error.

One analysis at a time per hc process, 120-second execution bound, and a 2 MiB
limit per output file prevent an analysis from retaining unlimited data. Failed
or timed-out runs release the lock and remove their temporary files. Output over
the safety limit is reported as a failure, never silently displayed as a full
plan. Railpack can access provider/tool-version metadata and its own cache;
this is not an offline sandbox or a file-system enforcement sandbox. It relies
on the trusted Railpack planning command's read-only project behavior.

## Actual Berkeley result

With the local development-server helper removed, Railpack 0.39.0 detects
Node/npm and resolves Node 24.20.0 (its current LTS default). Its plan contains
`packages:mise`, `install`, `build` and `packages:apt:runtime` steps, with
`npm install` in the proposed install step. It warns **No start command detected**.
The plan's build step has no project build command. It also reports the existing
`node_modules` folder. No suggested command was executed. A before/after source
file hash comparison was unchanged (excluding `.git`, `node_modules`, `.venv`).

## Manual test

Use a local hc runtime built from this checkout, with Railpack on its PATH or
`HC_RAILPACK_BIN` set. Open the workspace, press **New Project**, enter the
Berkeley checkout's absolute path, and press **Analyze project**. Observe the
loading text, then the detection, Railpack console plan and expandable raw JSON.
The modal stays open; **Done** dismisses it. Retry with a missing folder to see
an error without leaving the dialog. No cloud deployment is required.

The isolated development preview for this implementation was started from
`/private/tmp/engelbart-new-project` at `http://127.0.0.1:55293`, with its state
under `/private/tmp/engelbart-railpack-preview` and the verified executable at
`/private/tmp/railpack-inspect/railpack`. This does not replace the installed
Engelbart runtime; a normal packaged release is needed to distribute the change.

## Tests

```sh
PYTHONPATH=hc/src:tests python -m unittest test_project_analysis \
  test_goal_page.GoalPageModuleTests test_goal_page.GoalPageRouteTests -v
```

Tests mock Railpack at `Popen`, assert one `prepare` invocation and no source
writes, cover failure/absence/path validation/timeout/output limits/concurrency,
exercise the real loopback HTTP operation, and run Node DOM/action/service tests
for opening, selection, loading, retained results/raw JSON and retry. No
Playwright, BuildKit or Docker is used. The installed round-trip merge gate is
not run for this unmerged milestone because this task explicitly excludes
Playwright; it remains required before a future merge.

## Environment review (second modal page)

**Continue** on a successful plan opens Environment check. Back retains the
plan; Recheck refreshes the inventory; Done dismisses without executing anything.
The local `inspect_project_environment` and `save_project_environment` operations
use the same protected `/api/op` boundary. Save request bodies are excluded from
request telemetry, and responses contain names/status/provenance, never values.

The scanner combines:
- JSON Schema `properties`, `required` and `default` in `env.schema.json` or
  `.env.schema.json`. Conflicting declarations are marked uncertain.
- `.env.example`, `.env.sample`, `.env.template` names, without treating them as
  proof that a variable is required.
- Next/Vite detection from package.json, production dotenv precedence and public
  prefixes. Custom config loading is flagged for review, not executed.
- Lexical JS/TS dot/bracket references and same-line destructuring, Python AST
  `os.environ`/`os.getenv` accesses, and shell variable references.

This is deliberately partial. Executable schemas (Zod/envalid/etc.) are not
executed or treated as authoritative; unknown helpers and unsupported languages
may be missed. Lexical JS can have false positives, so references remain
uncertain. Dynamic computed accesses are reported where recognizable. Examples
are declarations only, not effective values. Nonstandard framework env loading
and interpolation require review. The selected root is authoritative; nested
projects are excluded and listed, as are generated/dependency directories.

Next precedence is `.env.production.local`, `.env.local`, `.env.production`,
`.env`; Vite precedence is `.env.production.local`, `.env.production`,
`.env.local`, `.env`. Saved Engelbart values take priority in this inventory for
future explicit injection. No executor consumes them in this milestone. hc's
own process credentials are deliberately not inherited into an arbitrary
project. Unknown frameworks only report local candidate values, not verified
runtime loading. Presence never implies validity or successful authentication.

Values entered in password fields are saved atomically to
`$HUMAN_COMPACT_HOME/project-environments/<sha256-canonical-path>.json` (default
`~/.human-compact`), outside the selected repository. The directory is mode 0700
and files mode 0600 on POSIX. Storage is local plaintext, not an OS keychain;
Windows filesystem ACLs govern access there. Values are cleared from UI state
after saving or dismissing. Existing values are never returned to the browser.

Bounds: 3,000 eligible files, 512 KiB per file, 12 MiB aggregate scanning, 500
variable names, 40 evidence locations per name; symlink files/directories are
not read. Saved submissions allow 200 detected names, 16 KiB per value, 128 KiB
aggregate. Skipped/unresolved cases are shown without claiming full coverage.

The Desktop Berkeley repository reports three values present in `.env.local`
and two additional uncertain names from the Python import scripts
(`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`). Those script references do not
block reviewing the web app. Source files and credentials remain unchanged.

Additional tests: `test_project_environment` plus the extended Node modal tests
cover evidence merging, precedence, interpolation uncertainty, nested-project
exclusion, source immutability, private atomic storage, project isolation,
telemetry exclusion, the next/back flow and masked inputs. The local preview
continues at http://127.0.0.1:55293; refresh it to load the updated JS.

## Run project (third page)

This milestone supersedes the earlier analysis-only stopping point. Environment
review now continues to **Run project**; nothing executes until the separate
**Run project** button is pressed. Install/build commands can change dependencies
and generated files as normal. Engelbart does not edit source to repair failures.

Railpack results receive an opaque `analysisId`. hc retains the original plan in
private local storage. The browser submits only that ID to `project_run_start`,
and polls `project_run_state`; it cannot replace the command or directory in the
request. The last selected run ID is retained in localStorage for reopening the
modal after reload. Choose another project clears that pointer.

The native adapter executes the shell commands in Railpack's `install` and
`build` stages, in order, then `deploy.startCommand`. It excludes the known
`mkdir -p /app/node_modules/.cache` scaffolding, copy/PATH directives and
`packages:*` container provisioning. It does not execute Linux apt/image/mise
provisioning on the host or provision Railpack's selected Node version; it uses
installed local tools. Unknown application stages and commands containing
container-specific paths fail as unsupported. No alternate dev command is
substituted. This is native application-stage execution, not execution of the
entire container build graph. Supporting other provider layouts requires an
explicit adapter; this milestone targets the demonstrated Node/Next plan.

`preview.PlanProc` extends the existing `preview.Proc`, uses its process-tree
termination and shared per-directory registry, and adds separate 256 KiB tails
for stdout and stderr. There is no disk copy of unredacted stream output.
Supplied values are redacted before capture is exposed. The exact commands run
via the existing shell mechanism (`bash -c`, fallback `/bin/sh`), without a login
shell or detector/model/recovery calls. Known-value redaction cannot guarantee
masking arbitrary encodings/transforms performed by user code; project scripts
are trusted local code and are not sandboxed.

Finite stages have a 600-second limit. Start gets 75 seconds to announce a local
URL and pass an HTTP GET while alive. Only loopback URLs and redirects are
probed; proxies are disabled. Successful startup requires a successful HTTP
response and continued process liveness. No browser automation is used. The
process group remains owned after the launch request/modal closes; the worker
retains failure evidence if it subsequently exits. A silent server with no URL
announcement currently times out instead of guessing a port.

Stages and terminal evidence are atomically persisted under
`$HUMAN_COMPACT_HOME/project-runs` with private files. HTTP calls return quickly
while a local worker handles execution. Failures include stage, unchanged
command, exit code, reason and bounded redacted streams. Retry repeats the same
plan from install; it does not roll back prior dependency/build changes. An hc
restart loses live ownership: persisted nonterminal runs become interrupted
failures and are not blindly reattached by PID or automatically restarted.

The executor loads production dotenv files with the Environment check's
precedence, overlays saved project values, and carries Railpack stage variables
forward. OS tool-path/home/temp variables are retained; hc's unrelated API
credentials are not. Dynamic dotenv interpolation is refused rather than
injected incorrectly. Secrets are passed through the process environment, never
command interpolation or repository writes. Public variables can naturally be
embedded by the framework in its browser build. No credential-validity test is
performed independently of the app's execution.

Tests in `test_project_run` execute disposable local command/server fixtures:
sequential install/build, failure stopping later stages, exact retry, persistent
HTTP startup, early exit, timeout, output capture, secret redaction, HTTP request
lifetime and forbidden model/recovery entrypoints. Node tests cover the Run
page's pending/progress/success/failure/retry states. No real Berkeley install or
build was run as part of implementation.

## Component discovery and startup-order review

Analyze now first calls local `discover_project_components`. A sole candidate at
the selected root continues as before. Nested or multiple candidates open a
component review page. Each native candidate can enter the existing Railpack →
environment → run flow, with Back to components retaining the discovery result.
This milestone does **not** add coordinated multi-component execution or claim
that one healthy component establishes whole-application health.

Discovery visits up to 2,000 directories, depth six, with bounded file reads and
a 4 MiB content budget. Dependencies/generated directories and symlink folders
are excluded. Manifests identify candidates, not proof of successful execution.
The maximum displayed inventory is 200 components; truncation is reported.

`project_components.order` computes topological groups from declared dependencies
and reports missing prerequisites and cycles. Compose `depends_on` preserves
`service_started`, `service_healthy`, and `service_completed_successfully` as
separate conditions. Optional (`required: false`) dependencies remain advisory.
Independent positions in this declared graph are not proof of real independence.

PyYAML SafeLoader is used for declarative Compose parsing only; config never
executes. YAML tags/anchors/aliases, large documents and excessive token counts
are refused. Files are analyzed individually, not merged as Compose overlays;
extends/profiles and unsupported constructs require review. Container services
are labeled as such and cannot be run through the native-only runner.

Literal package proxy endpoints and script `cd` references supply functional
relationship hints. Python app.run port literals can match a proxy endpoint.
Neither signal creates an automatic startup dependency. Script bodies and
README instructions are never converted into launch rules. Documentation paths
are listed for review. No ordering is inferred from names like backend/frontend.

The HypoCompass-shaped fixture discovers system/frontend and system/backend,
recognizes the frontend's proxy to port 8090 and its backend launch-script
reference, and reports no declared startup order. The Desktop Berkeley repository
returns both the root and berkley candidates, with no invented dependency.

A separate preview at http://127.0.0.1:55294 runs this version to leave the earlier
runtime undisturbed. `test_project_components` covers discovery, proxy hints,
Compose conditions/topological groups, cycles/unresolved prerequisites, symlinks,
unsupported YAML and unchanged source. Node tests cover component selection and
returning to the inventory. PyYAML is an added packaged runtime dependency;
this remains an unpushed worktree change.

## Bounded Setup fallback

The earlier deterministic milestones above remain the first path. After **Run
project**, a command failure or native-plan rejection now enters `setup_planning`
in the same modal. Nothing calls a model while simply analyzing, reviewing
commands, or polling a successful deterministic run.

`project_setup.py` uses the configured hc provider's tool-free `generate_plain`
interface. It supplies the failed stage/command/exit code, bounded redacted log
excerpts, environment names/statuses, and at most two setup documents. One
additional request may read three named manifest/README excerpts. Each excerpt
is at most 3,000 bytes; each prompt at most 24,000 characters and each response
24,000 characters. No env files or full repository trees enter the prompt.
Known local values and credential-looking assignments are redacted. Arbitrary
unlabeled secrets in project documentation remain a limitation of text-based
redaction; do not treat it as a general secret detector.

The result is either `needs_input`, `unsupported`, or a JSON plan with bounded
preparation argv arrays, services, relative working directories, service
prerequisites, HTTP health URLs, and an entry service. hc validates paths,
symlinks, command forms, existing npm scripts, dependency cycles, loopback URLs,
and non-secret port/legacy Node options before executing. The model cannot run
tools or directly edit source. Existing project scripts still execute project
code: this is a constrained setup adapter, not a malicious-code sandbox.

The selected repository root is retained separately from the analyzed component
path. A selected backend can therefore refer to a sibling frontend inside the
same selected repository. Older retained analyses are limited to their original
cwd; analyze again to establish the wider boundary.

`project_run.recover` supervises validated commands using the existing
`preview.PlanProc`, extended with service ownership keys and direct argv execution.
Multiple services can share a working directory. Each prerequisite must pass HTTP
health before its dependents launch, and all services must be alive and healthy
before the entry URL is reported. An occupied health port is refused. Preparation
has a 600-second limit; service startup has a 75-second limit. No containers,
Playwright, shell repair expressions, or model-supplied source changes execute.

Up to five repair attempts after the initial launch are allowed per retained analysis. Approval resumes the same attempt and does not reset the budget. There
are at most two bounded provider calls per attempt (the second only for requested
excerpts). Identical failed repairs stop. Invalid output and unavailable providers
stop with useful errors. Attempts and original/repair failure evidence persist
outside the repo; browser reload joins live state. Runtime restart never blindly
reattaches saved PIDs. Reset cancels pending setup and stops every owned service;
a model response arriving after Reset cannot launch commands. Runtime exits after
successful startup do not automatically invoke another recovery.

The modal shows recovery progress, attempt summaries, retained detailed logs,
manual-input/unsupported states and an Environment check action. Environment values
from the originally selected component and each command's cwd reach subprocesses
via environment dictionaries, with known values masked in logs. Required missing
values still block execution.

Tests: `test_project_setup`, updated `test_project_run` / `test_project_analysis`,
and `project_analysis_ui.mjs`. Provider responses are mocked; subprocess execution
and two-service HTTP health use real disposable local fixtures. No real user's
project is installed or modified by these tests. Packaging/three-OS installed
round-trip gates remain required before merge; this prototype is not deployed.

## Pre-execution Run Order Agent

Multiple discovered native components no longer require choosing one component.
`project_order_start` starts a local persisted assessment; `project_order_state`
joins/polls it. hc discovers the repository again, runs Railpack sequentially for
its candidates, then invokes a **separate Run Order Agent prompt** before any
install or service is launched. A single component still uses deterministic
Railpack analysis directly, including a sole application nested below the root.
Explicit container declarations are reported as unsupported by this native
runtime, rather than inferred away or sent to the agent.

Assessment is bounded to six components, one tool-free model response and one
optional evidence round. Context includes component manifests/relationships,
compact Railpack command summaries, and up to four initial setup excerpts. The
agent may request three additional bounded README/manifest/source excerpts,
including offsets into a longer file. Excerpts are 3,000 bytes, requested offsets
are bounded at 48,000 bytes, prompts at 36,000 characters, and output at 24,000
characters. Environment files are not readable by the agent; known values from
the candidate components are masked. Unlabeled hardcoded secrets remain a text
redaction limitation, as described above.

The agent returns selected/excluded components, evidence references, ordering
rationale, preparation argv arrays and persistent service definitions. Each
command has its own cwd; service identity does not imply a directory. Optional
`environmentCwd` selects the component whose configuration should be loaded when
the command is launched from another directory. Service dependencies specify
HTTP readiness. Ambiguous unrelated applications produce `needs_input`, not a
plan to start every candidate. The modal displays the question and lets the user
choose a narrower project or explicitly retry assessment.

The complete relative launch plan is persisted as `orderPlan` alongside the
existing retained run record. Reload resumes the assessment or displays its saved
result; concurrent requests join the active assessment. If hc restarts during an
assessment, it reports interruption and requires explicit re-analysis. Completed
plans are never re-generated by polling. **Continue** opens component configuration
review, then the existing **Run project** action validates and executes the plan.
Assessment itself executes no project commands.

Execution uses the same hc validator/supervisor as setup repair. The assessed
plan does not consume either of the two repair attempts. If it fails, the
separate repair agent receives the accepted plan plus failure evidence. A
successful assessed launch never calls the repair agent. Root, cwd, argv,
dependency, health, and cancellation checks remain in force.

`test_project_order` covers all-component analysis, no execution during assessment,
duplicate joins, persisted results/interruption, ambiguous and invalid output,
bounded evidence requests, source/secret boundaries, local HTTP routing, and
explicit container refusal. Its HypoCompass-shaped integration fixture launches
`npm run backend` and `npm start` from the **same frontend directory**; the backend
script enters its sibling directory and uses that backend's local configuration.
Both are real supervised processes with HTTP health checks; assessment/model
responses are mocked. The modal tests cover the combined plan and both cwd labels.

## Live agent diagnostics

Run Order and Setup Repair now persist a bounded `agentTrace` for each request.
The local modal shows the agent name, provider/model, elapsed time, 90-second
request limit, prompt character count, tool setting, exact redacted prompt sent,
and bounded redacted response/error. Prompts are saved before inference begins;
completion and timeout timestamps survive reload. Each follow-up evidence turn
has its own prompt and response. Expand/collapse state survives polling redraws.
Railpack component status and elapsed time are also visible while analysis runs.
This reports observable activity, not internal model reasoning or token streaming.

Setup inference opts into `generate_plain(..., planning=True)`, which uses explicit
low effort on Claude while leaving ordinary plain calls unchanged. The CLI timeout
remains 90 seconds. A timeout alone cannot distinguish CLI startup delays from
provider latency; low effort does not guarantee that every request will succeed.
Older failed attempts do not have recorded prompts and require a fresh assessment
to capture diagnostics. Model responses in tests are mocked; production model
latency has not been certified by those tests.
