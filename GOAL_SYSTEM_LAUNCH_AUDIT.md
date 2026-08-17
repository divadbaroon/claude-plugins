# Human Vault goal system: current state and launch audit

Audit date: 2026-08-16

Current implementation reviewed: origin/main at 8ada481, Human Vault 0.17.89

Comparison baseline: local main at 220e291

Primary author in scope: David Barron

## Executive verdict

Human Vault is a substantial working system, not a prototype-shaped mock. Its
backend has a durable goal model, real conversation ingestion, provenance,
chat-scoped and global state, bounded model calls, goal-bound Claude sessions,
and live run observation.

It is not ready for an initial public launch at 0.17.89.

The weakest link is not missing feature volume. It is contract integrity:
several controls imply that a state change or action occurred when the backend
does not persist or perform it. The default test environment hides the most
important regression: once Playwright is installed, all four real-browser
tests fail because the chat-scoped workspace is covered by global Vault
onboarding.

The recommended launch promise is:

> Human Vault turns recent Claude Code conversations into a durable,
> inspectable goal map and lets you reopen one goal with its evidence and
> context.

Do not initially promise autonomous execution, a durable approve/revise
workflow, equivalent global and chat workspaces, or cross-platform one-click
launch. Those are either incomplete, platform-limited, or semantically
misleading in the current build.

## Evidence standard

This audit distinguishes three kinds of finding:

- Verified behavior: exercised by a passing or failing test, or directly
  observed against the current package/repository.
- Source-verified behavior: both ends of the relevant code path were traced.
- Risk: an unsafe interleaving or product consequence exists, but this audit
  did not force it to occur in a live user vault.

## What David changed

### Commit census

David has 200 commits in the repository history. The current unreviewed burst
from 220e291 through 8ada481 contains 183 commits over August 13–15:

| Type | Count |
| --- | ---: |
| Feature | 48 |
| Fix | 40 |
| Vendored backend build | 89 |
| Test | 3 |
| Revert | 2 |
| WIP | 1 |

The range changes 48 files with 9,718 insertions and 1,411 deletions. It
contains no documentation commit and no pull request; it was pushed directly
to main. Nearly every source change was followed by a committed wheel update,
which explains the 89 build commits but makes review and bisection noisy.

### Development arc

#### August 9–12: foundations

- Iterated Compact Focus interaction experiments from 0.3.5 through the 0.4
  series.
- Introduced the global goal-aware state layer: inferred goal trees,
  important items, natural-language corrections, evidence inspection, and
  goal-context injection.
- Added the first browser goal UI and then the checked-in design artifact with
  a two-way localStorage bridge.
- Added a one-command installer. The installer and chat-scoped merge were
  briefly reverted, then reconciled by subsequent work.

#### August 13: productization

- Bound real Claude sessions to Vault goals and created a separate execution
  store for agent tasks, activity, files, branch, and completion state.
- Renamed and published the npm package as human-vault.
- Moved opt-in onboarding from the installer into hc ui.
- Replaced sample conversations, simulated progress, fake tasks, fake files,
  and fake artifacts with backend state.
- Prevented an empty vault from importing the design artifact’s demo goals.
- Added conversation-to-goal attribution, inferred descriptions, context
  panels, project directories, install repair, PATH handling, and analysis
  progress.

#### August 14: execution and review

- Added provider retry/backoff and widened extraction to eight concurrent
  conversations.
- Added model-generated proposed plans.
- Added one-click macOS terminal launch, goal-specific prompts and briefings,
  source directories, live task observation, activity summaries, file-change
  counts, session reopening, stalled-run heuristics, and a REVIEW surface.
- Reworked the pane layout repeatedly: prompt, agent, run activity, review,
  approval, and revision controls moved several times.
- Removed the final confirmation modal, making the Run control itself the
  confirmation that immediately starts Claude.

#### August 15: analysis visibility and state retention

- Made every conversation row report analyzed, queued, or analyzing.
- Added an eight-wide progress banner and a separate goal-tree synthesis
  state.
- Added live goal/run indicators, then reverted the tree dot.
- Fixed pane/selection retention and noticing a newly launched run without a
  manual reload.

### Review of the change set

The strongest work is in the backend boundaries:

- Chat goal writes use a cross-process lock and compare-and-swap revision.
- Raw transcripts are not rewritten by inference.
- Human goal state is kept separate from an agent’s task state.
- User prompt detachments survive later inference.
- File contents are excluded from run activity and review summaries.
- Localhost servers validate Host, Origin, content type, request size, and
  server identity.
- Provider calls are bounded, retry transient failures, and do not silently
  fall back to another provider.
- Empty-vault sample-data contamination was explicitly fixed.

The weakest work is in integration and release discipline:

- The frontend is a generated HTML artifact altered at runtime through exact
  string replacements in bridge.js.
- Unit tests assert many replacement strings, while CI does not install the
  browser harness that exercises the resulting page.
- Some frontend controls have no durable backend representation.
- Global and chat scope are presented through one artifact even when an
  operation is valid in only one scope.
- Package identity, setup instructions, platform claims, and release metadata
  are inconsistent with the implementation.

## System model

Human Vault currently contains two related but distinct goal products.

### Global Vault

The global Vault is optional. It can:

1. Capture and backfill Claude Code conversations into local Vault storage.
2. Analyze up to the most recent 30 days with Ollama or the authenticated
   Claude CLI.
3. Infer and incrementally update a cross-conversation goal tree.
4. Show conversations, goal context, provenance, and attached sources.
5. Start and observe a Claude session bound to one global goal.

The global browser workspace is opened with hc ui.

### Chat-scoped goals

Chat-scoped goals are installed by default and do not require global capture.
Each Claude session has:

- an independent goal tree;
- a prompt store and many-to-many prompt links;
- an event stream containing user prompts, visible assistant plans, tool
  activity, task events, compact summaries, and completion evidence;
- incremental inference with human fields preserved;
- cached goal context reinjected into that chat.

The intended entry point is /hc-ui inside Claude Code, implemented by hc
chat-ui and a session-scoped localhost server. Agent execution is not available
for chat-scoped goals.

## Current feature inventory

### Installation and updates

| Feature | Current behavior | Surface |
| --- | --- | --- |
| One-command install | npx human-vault installs a bundled, checksummed Python wheel | npm |
| Managed runtime | Versioned runtimes and stable hc launcher under ~/.human-compact | npm |
| Python discovery | Reuses a compatible Python or downloads pinned uv assets and provisions one | npm |
| Claude integration | Installs hooks and the /hc-ui skill | npm |
| Safe default | A flagless install captures and analyzes nothing | npm |
| Scripted onboarding | Numeric flags can still enable global capture and analysis | npm |
| Reinstall repair | Reuses a verified runtime and preserves existing capture state by default | npm |
| PATH repair | Adds or explains the managed launcher path | npm |
| No install scripts | The npm package does not rely on lifecycle scripts or mutable Git code | npm |

### Conversation persistence and analysis

| Feature | Current behavior | Surface |
| --- | --- | --- |
| Global backfill | Imports existing Claude Code transcripts after explicit opt-in | Global UI, CLI |
| Future capture | Hooks preserve sessions, transcripts, compaction snapshots, summaries, and end events | Backend |
| Local analysis | Ollama option keeps analysis on device | Global UI, CLI |
| Claude analysis | Uses the user’s authenticated Claude CLI with tools/session persistence disabled | Global UI, CLI |
| Parallel extraction | Up to eight conversations are extracted concurrently | Backend, progress UI |
| Retry control | Transient provider errors use shared-gate backoff; permanent errors fail directly | Backend |
| Incremental update | New conversations are classified into an existing tree | Backend |
| Full rebuild | Re-infers the complete global tree from cached extractions | CLI |
| Machine-session filtering | Vault’s own inference and goal-launch sessions are excluded from source history | Backend |
| Analysis progress | Shows total, analyzed, active conversations, and synthesis phase | Global UI |
| Conversation list | Real conversations, largest first, with goal attribution and short preview | Global UI |
| Full thread | Loaded on demand when a conversation is opened | Global UI |

### Goal model

| Feature | Current behavior | Surface |
| --- | --- | --- |
| Uniform tree | Goals, subgoals, and promoted legacy todos use the same node model | Both UIs, CLI |
| Bounded depth | Tree depth is capped at four | Backend |
| Status | active, in progress, completed, abandoned | Backend; three-state UI projection |
| Priority | urgent, high, normal | Both UIs |
| Description | Inferred from a goal’s own user evidence; manually editable | Both UIs, CLI |
| Notes | Human-authored Markdown, preserved across inference | Both UIs |
| Sources | Local directories, GitHub references, and documents | Global UI, backend |
| Opening line | Per-goal override for the first message of a launched session | Backend API only |
| Prompt links | Many-to-many human-prompt relationships | Both UIs |
| Automatic links | Evidence-cited user prompts are linked and labelled automatic | Both scopes |
| Detachment | A user-detached prompt is not reattached by later inference | Both UIs |
| User authority | Manual title, structure, status, priority, notes, description, sources, and links survive inference | Backend |
| Non-destructive delete | A browser-deleted node becomes abandoned rather than erased | Both UIs |
| Three-way reconcile | Browser local edits merge with a changed remote revision | Both UIs |
| Context injection | Selected goal context is supplied to Claude rather than the whole tree | Hooks, hc work |

### Global browser workspace

- Goals and Conversations pages.
- Active, in-progress, done, and all filters.
- Add, rename, nest, reorder, complete, abandon, and prioritize goals.
- Split, tree-only, and inspector-only layouts.
- Light/dark theme and keyboard navigation.
- Description and notes editing.
- Context panels for hierarchy, user wording, decisions, built work, blockers,
  sources, and related prompts.
- Prompt picker across available user prompts with search, dates, source
  conversation, and attach/detach.
- Local folder, GitHub repository, and document attachment.
- Agent pane with proposed plan, recommended prompt, notes, and Run.
- Review pane with run state, elapsed time, task/subgoal progress, activity,
  file paths and edit counts, branch/commits, checks, attention state,
  suggested inspection commands, and session reopening.
- Analysis progress banner and per-conversation queue state.

### Chat browser workspace

The intended chat workspace supports the goal tree, descriptions, notes,
statuses, priorities, and prompt links for one Claude session. Global
conversations, setup, analysis, briefings, and agent runs are deliberately
unavailable.

At 0.17.89 this surface is not usable on a fresh page: the global onboarding
overlay appears in chat scope even though chat scope rejects the corresponding
global operations. This is verified by all four Playwright tests failing.

### Goal-bound Claude sessions

| Feature | Current behavior |
| --- | --- |
| Resolution | hc work accepts a goal ID or an unambiguous title fragment |
| Context | Builds a bounded goal-only briefing with hierarchy, evidence-derived context, notes, sources, and prior work |
| Working directory | Derived from the goal’s cited conversation evidence, with parent/child fallback |
| Additional directories | Existing local source paths become Claude --add-dir arguments |
| References | GitHub/document sources are cited in the prompt |
| Binding | Environment or one-time claim binds exactly one new Claude session |
| Observation | Existing hooks record TaskCreate, TaskUpdate, TaskList, TaskGet, writes, branch, commits, activity, waiting, and completion |
| Separation | Agent task completion never automatically completes the human goal |
| Resume | Opens the recorded Terminal window or resumes the Claude session |
| Platform | Browser one-click launch and window raising are macOS-only |

### Security and durability

- State directories and artifacts are owner-only.
- Symlinked Vault/session roots are rejected.
- Writes use atomic replacement.
- Chat writes share a cross-process lock and revision check.
- Global and chat state live in separate namespaces.
- Local servers bind to 127.0.0.1.
- Host validation blocks DNS rebinding.
- Origin validation and JSON content-type requirements block cross-site writes.
- Request bodies are capped at 2 MB.
- A server registry verifies process identity before replacing a stale server.
- Chat servers expire after inactivity.
- No telemetry is present.

## Built but not exposed in the primary frontend

### Complete backend/CLI capabilities with no primary browser access

| Capability | Existing implementation | Current access | Launch disposition |
| --- | --- | --- | --- |
| Important items | important.json, mark_important, goal attachment, context rendering | hc mark and hc goals | Integrate as a lightweight Inbox or omit from launch messaging |
| Natural-language correction | Move, merge, rename, status, demote, add subgoal, mark/attach important | hc goals correction flow | Expose through a correction command or structured evidence drawer |
| Evidence inspection | Raw evidence IDs and cited turns | hc goals and hc lens | Add “Why this goal?” in the browser |
| Lens testing/correction | Derived context lens, correction store, A/B test flow | hc lens | Keep advanced/experimental |
| Evidence graph | graph_build output plus Sigma browser | hc trajectory --browser or --serve-only | Legacy; do not market |
| Opening override | set_opening operation and goal.opening | API/backend | Connect the visible prompt editor to it or remove editability |
| Capture disable | enable_capture with enabled=false | API/CLI | Add Settings with disable and data-retention consequences |
| Analysis failure details | failed directory, analysis.log, hc status | CLI/files | Add visible error, retry, and diagnostics |
| Direct run claim | start_agent_run and cancel_agent_run | API/tests | Remove if obsolete or use for an explicit preflight flow |
| Full briefing payload | /api/briefing returns the exact assembled context | API | Offer inspect/copy/export |
| Abandoned status | Distinct durable state | Backend/CLI | Stop collapsing it into completed in the browser |

### Frontend affordances whose backend contract is incomplete

These are more dangerous than hidden features because the screen asserts an
action that is not durable.

1. Recommended prompt edits are ignored by Run.
   The browser records the edited textarea locally, then posts only goal_id.
   The backend reconstructs its own short opening from goal.opening or the
   title. No prompt text crosses the launch request.

2. Decisions, blockers, built work, and “in my words” look editable but are
   derived briefing fields.
   Browser import persists description and notes, but not these context
   textareas. A reload replaces edits with derived sections.

3. Request revisions has no server operation or review-decision store.
   It changes the artifact object in browser state and does not send feedback
   to the running/resumable Claude session.

4. Approve is only partially durable.
   It can mark the goal complete through tree import, but artifact approval
   itself is not stored. Reloading reconstructs the review card from the run
   and presents it as pending again.

5. Proposed plans can be stale.
   /api/plan caches one file per goal indefinitely. Title, description, notes,
   hierarchy, prompt links, or sources do not invalidate it.

6. Opening the Agent pane can trigger a provider call.
   The browser automatically GETs /api/plan when the pane opens. That GET may
   invoke Ollama or Claude and write a cache file, so a nominal read has cost,
   privacy, and mutation side effects.

### Legacy or orphaned implementation

| Component | State |
| --- | --- |
| trajectory/web/goals.html | Earlier custom goal UI; no current server route serves it |
| trajectory/serve.py plus web/index.html | Legacy evidence graph server, reachable only through hidden/advanced CLI flags |
| graph_build.py | Still runs opportunistically; failures are swallowed because the graph is optional |
| toggle_todo and add_todo API names | Legacy vocabulary over the promoted child-goal model; current bridge uses tree import |
| rename/status/priority/notes/description/add-goal operations | Valid direct API alternatives, but the current artifact usually sends a revisioned whole-tree import |
| start_agent_run and cancel_agent_run | Tested claim workflow superseded in the current browser by launch_agent_run |

## Launch blockers

### P0: scope and interaction truth

1. Fix chat-scope boot.
   Chat pages must seed setup as complete and must never render or call global
   capture/analysis onboarding. Add browser tests for both scopes to required
   CI.

2. Apply a strict rule: every editable control either persists across reload
   or is read-only.
   Add explicit human-override fields for decisions, built work, blockers, and
   user framing, or render the derived briefing as non-editable provenance.

3. Make the launch prompt exact.
   POST the reviewed text and persist it as the goal opening, or remove the
   editor and label the shown material as context preview. The current hybrid
   is deceptive.

4. Remove approve/request-revisions until their decisions are durable.
   A launch version can ship REVIEW as a read-only run report. Reintroduce
   decisions only with a backend review record and explicit effects.

### P0: state safety

5. Serialize global goal writes across the browser and analyzer.
   Chat scope has a cross-process lock; global scope does not. The HTTP
   process lock cannot coordinate with the detached worker. A worker can load
   an old tree, a user can save an edit, and the worker can atomically replace
   it with its old copy. Use one global cross-process lock plus revisioned
   retry/merge for every writer.

6. Persist “Not now.”
   start_analysis with provider=none records nothing. setup_state claims that
   decline is a settled state but derives done only from a configured provider
   or existing goals. The choice survives only in one browser’s localStorage.

7. Expose failure as a terminal state.
   The backend counts failed conversations and writes analysis.log, but
   /api/setup omits failures and the UI has no failure/retry state. A detached
   analysis can stop and leave the user with zero goals and no explanation.

8. Invalidate proposed plans.
   Cache keys must include the semantic goal revision and provider/model, or
   plan generation must be explicit and refreshable.

### P0: release integrity

9. Stop runtime-patching an opaque bundle without an end-to-end gate.
   bridge.js performs exact string substitutions and throws when the artifact
   shape changes. For the launch branch, pin the artifact hash and make
   Playwright mandatory. After launch, move the frontend into source-owned
   components with typed API contracts.

10. Correct platform and package claims.
    The package declares macOS and Linux, while browser Run intentionally
    raises “one-click launch currently supports macOS only” on Linux. Gate the
    control and show a copyable hc work fallback, or implement a Linux
    launcher.

11. Repair all public documentation and metadata.
    The root README and human-vault README still say human-compact and claim
    that a flagless installer asks two numeric questions. The root development
    command points to a directory that no longer exists. package.json points
    its repository directory and homepage to human-compact. Error prefixes
    still say human-compact.

12. Cut a real release.
    Human Vault is already public on npm at 0.17.89, but there is no Human
    Vault GitHub tag/release or changelog; the repository’s latest release is
    Compact Focus 0.22.3. Freeze a release candidate instead of publishing
    directly from a two-day main-branch stream.

## Proposed initial-launch scope

### Include

- Flagless, inert installer.
- Explicit global capture opt-in and clear local storage location.
- Explicit provider choice with a visible data boundary.
- User-selected analysis window.
- Real analysis progress, failure, retry, and cancellation.
- Conversations list and full thread.
- Inferred goal tree with title, hierarchy, description, status, priority,
  notes, and prompt provenance.
- Evidence drawer explaining why each goal exists.
- Durable manual correction of structure and prompt links.
- Attached sources with a visible distinction between inferred working
  directory and user-authorized extra directories.
- Inspectable/copyable goal briefing.
- “Open this goal in Claude” with an exact preflight: working directory,
  additional readable directories, provider, and opening message.
- Read-only run activity/review if the agent surface remains enabled.

### Hide behind Experimental

- Chat-scoped /hc-ui until its browser suite passes and its relationship to
  the global tree is explained.
- One-click terminal launch and resume.
- Model-generated plan proposals.
- Live waiting/stalled inference.

### Defer

- Approve/revise workflow.
- Automatic promotion of agent completion into human goal completion.
- Evidence graph.
- Lens A/B testing.
- Important-item Inbox unless it is part of the launch’s core information
  architecture.
- Linux one-click launch until implemented.

## Proposed feature changes after blockers

### P1: evidence and correction

1. Add a “Why this goal?” drawer.
   Show the cited prompt, conversation, date, inferred relationship, and
   whether each link is automatic or human-authored.

2. Expose the backend correction vocabulary.
   Move, merge, split/demote, rename, attach evidence, and abandon should be
   first-class actions with an undo log. Natural language can be a shortcut,
   not the only path.

3. Separate inferred state from asserted state.
   Each description/decision/blocker should carry provenance and an authority
   label: inferred, human-edited, or observed from an agent run.

4. Distinguish completed from abandoned.
   The browser currently maps both to done. This destroys the difference
   between successful closure and removal from scope.

### P1: setup and trust

5. Add Settings and Health.
   Show capture state, provider/model, analysis window, last successful run,
   failures, pending queue, storage path/size, disable capture, delete local
   data, retry, and export a redacted support bundle.

6. Make temporal coverage explicit.
   Discovery is hard-coded to 30 days in the browser paths. Let the user
   choose 7, 30, 90, or all available history, then show “Goals inferred from
   N conversations between date A and date B.”

7. Separate workspace from permission.
   Evidence-derived cwd is currently written into the same sources list as a
   user-attached directory, even though local sources become --add-dir.
   Store inferred workspace and explicitly authorized additional directories
   as different fields.

### P1: execution

8. Replace Run with a preflight.
   Show the exact opening message, briefing summary, cwd, extra directories,
   and command. Offer Copy command everywhere; offer Start now only where the
   platform path is tested.

9. Make plan generation explicit.
   Use a button with provider/cost disclosure and a Refresh action. Cache by
   goal revision, provider, model, and prompt-schema version.

10. Add a safe diff viewer.
    Keep file contents out of telemetry and activity logs, but let the local
    browser render git diff for the selected run after explicit request.

### P2: integration

11. Decide how chat goals relate to global goals.
    The minimum coherent choices are isolation with clear naming, or an
    explicit “Promote to Vault” action. Silent dual trees create ambiguous
    authority.

12. Integrate important items only if they have a retrieval loop.
    A durable Inbox with assign/dismiss/snooze can justify the existing store.
    A second unreviewed list cannot.

13. Rebuild the frontend around stable API objects.
    Remove localStorage as a shadow database, replace whole-tree imports with
    revisioned operations, and keep browser-only state limited to layout.

## Current limits and silent caps

| Limit | Current value |
| --- | ---: |
| Global discovery window in browser paths | 30 days |
| Conversation list | 200 |
| Full thread | 400 turns |
| Maximum rendered characters per full-thread turn | 6,000 |
| Goal tree depth | 4 |
| Goal title | 120 characters |
| Description | 600 characters |
| Notes | 4,000 characters |
| Opening override | 400 characters |
| Sources per goal after normalization | 20 |
| Parallel global extraction | 8 conversations |
| HTTP JSON body | 2 MB |
| Activity log | 60 entries in backend; smaller recent slices in UI |

These limits are reasonable as safeguards, but a launch UI should state the
history window and any truncation that can change what a user believes the
system considered.

## Test and release evidence

### Tests

- Python default environment: 560 tests reported OK with 4 Playwright tests
  skipped.
- npm: 32 of 32 tests passed.
- Python with Playwright 1.62 and installed Google Chrome: 560 tests ran,
  556 passed and 4 failed.
- All four failures are real-browser chat workspace tests. The observed page
  shows global onboarding over the chat-scoped tree, so the empty state,
  prompt picker, reconciliation flow, and completion layout cannot be reached.
- Minor ResourceWarning output remains around HTTP errors and server
  replacement subprocess streams.

CI currently installs neither Playwright nor a browser harness, so the four
failures are skipped on every matrix entry.

### Public release state

- npm latest: human-vault 0.17.89, published 2026-08-15.
- npm has no human-compact package.
- GitHub repository: public, default branch main, blank description.
- Latest GitHub release: Compact Focus 0.22.3 from 2026-08-11.
- Human Vault has no dedicated tag, GitHub release, or changelog.
- There are no open issues and no PR covering the 183-commit burst.

## Launch acceptance criteria

Do not label a build launch-ready until all of the following are true:

1. Fresh global and chat installs complete their intended first-run flow in
   real Chrome on macOS and Linux.
2. Required CI runs the browser tests; zero tests are skipped for missing
   harness dependencies.
3. Every editable visible field survives reload and a concurrent analyzer
   update.
4. A deterministic concurrency test proves that a global UI write and worker
   update merge without lost human state.
5. The exact launch preflight equals the command, prompt, cwd, and additional
   directories actually used.
6. Review buttons either have durable, tested effects or are absent.
7. “Not now,” provider choice, capture disable, and analysis failure survive a
   new browser profile.
8. No GET request invokes an external model or writes user state.
9. Linux never shows a control that can only return a macOS-only error.
10. A clean install, reinstall, upgrade, and rollback are tested from the
    published tarball.
11. Root README, package README, package metadata, CLI help, marketplace
    description, changelog, and release notes describe the same product.
12. The UI names its temporal evidence boundary and provider data boundary.

## CLI inventory

| Command | Role | Audience |
| --- | --- | --- |
| hc ui | Global Vault browser workspace and onboarding | Primary |
| hc chat-ui | One-chat goal workspace | Primary through /hc-ui |
| hc goals | Render/rebuild/describe/correct global goals and inspect evidence | Advanced |
| hc work | List, preview, or start work bound to one global goal | Primary/advanced |
| hc mark | Preserve and optionally attach an important item | Advanced |
| hc status | Queue, failure, worker, lens, and freshness diagnostics | Support |
| hc refresh | Process pending global conversations | Support |
| hc analyze | Extraction plus goal rebuild | Internal/support |
| hc lens | Inspect, correct, and test the derived context lens | Legacy/advanced |
| hc trajectory | Full analysis and optional legacy graph browser | Legacy alias |
| hc backup | Enable/import global Vault history | Legacy onboarding |
| hc install | Install Claude integration without global capture | Installer |
| hc setup | Scripted noninteractive onboarding | Installer |
| hc chat-serve | Session-scoped browser server | Internal |
| hc chat-hook | Ingest and inject one chat’s state | Internal |
| hc chat-refresh | Run chat inference | Internal |
| hc global-hook | Capture and observe global events | Internal |
| hc worker | Drain global analysis queue | Internal |

## HTTP API inventory

### Reads

| Route | Purpose |
| --- | --- |
| GET / | Serve the patched goal artifact |
| GET /bridge.js | Serve the runtime integration bridge |
| GET /api/state | Goal, prompt, run, scope, provider, analyzer, and revision state |
| GET /api/briefing?goal=ID | Exact goal context, opening, sources, and launch directory |
| GET /api/briefings | Briefing sections for every global goal |
| GET /api/plan?goal=ID | Generate or return a cached proposed plan |
| GET /api/conversation?id=ID | Full global conversation thread |
| GET /api/setup | Global onboarding and analysis progress |
| GET /api/review?goal=ID | Goal run review records |
| GET /api/health | Scope, version, and server identity |

### Writes

| Route | Purpose |
| --- | --- |
| POST /api/import | Revisioned nested-tree import; missing nodes become abandoned |
| POST /api/op | Capture, analysis, goal fields, prompt links, agent launch/resume, and legacy direct operations |

## The decision that precedes more feature work

Is the first product one-chat goal continuity or a cross-chat Vault that
recovers and reopens long-lived work?

The code currently treats both as first-class while the package name,
installer copy, slash command, onboarding, and agent boundary point in
different directions. Choose one launch contract. Every feature not needed to
complete that loop should be hidden, not merely left half-connected.
