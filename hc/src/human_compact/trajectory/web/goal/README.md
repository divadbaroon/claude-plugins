# The goal page

What a chat workspace opens on: `hc chat-ui` serves `index.html` at `/` and
the rest of this directory at `/goal/<name>`. Plain ES modules, no build
step; the browser loads them as they are.

    index.html      the shell: fonts, styles.css, app.js
    styles.css      the design's tokens and one class per element
    app.js          selects the production renderer
    bootstrap.js    shared store/actions, first draw, polling and event listeners
    store.js        the state tree and the readers on it (one slice per subgoal)
    actions.js      what the reader can do; the only writer of the store
    services.js     the boundary to everything behind the page (the goals,
                    the account, Bart and the panes are all real)
    dom.js          h() to build a tree, mount() to morph the page toward it
    components/     one render function per region, pure in state and actions

Every change to the store redraws the whole page from state; `mount()`
morphs the tree on screen toward the new one, so inputs the reader is
typing in keep their focus and caret. Rows that can reorder carry a `key`.

`services.js` is the boundary to everything behind the page. Each function
there takes one object of named arguments and returns a promise; replace
the bodies, keep the signatures.

The goals are real. `loadGoal` reads `GET /api/goal-page`: the goal the
address names (`/?goal=<id>`), or when it names none the top-level goal
touched most recently, among those in progress or with something under
them; its subgoals; for each the todo rows, straight from the
chat's `goals.json` through the goals model; and the project the chat is
in, whose name the header shows. This is how a project finished
in the web onboarding arrives: `/bart` claims it, writes its tree and binds
the chat, and the page opens on the direction the reader chose (the ones
they were offered and did not take stay in the tree, with nothing under
them) with its pieces as subgoals (each
piece's notes are seeded from the setup's description and its why, kept
in the tree for `/legacy` and the hooks; this page does not draw notes). The
header's path is the way around: the brand opens every project the vault
knows (`GET /api/projects`, a card each, opening one goes to its workspace
through `open_project`), the project's name opens this project's goals as
cards -- the direction's why, how many pieces are done, a check when all of
them are -- and a goal card opens that goal here, named on the address. The
account menu has the reader's level under a rule, on the bar slider the web
setup asks it with (`GET /api/reader`, `POST /api/goal-page/reader` with one
of `reader.LEVELS`; the rest of the profile is kept as it was). A workspace
with no goal answers empty, and the page asks for one in a line; a goal
with nothing under it yet asks for its first subgoal, since the
conversation and the todos each belong to one. Every
write is one operation on `POST /api/goal-page/op` -- `add_goal`,
`add_todo_row`, `set_todo_text`, `set_todo_done`,
`remove_todo_row`, `build_todos` -- the same operations the workspace at
`/legacy` applies, so the two pages and the chat's hooks write one file.
Each answer carries the goals' revision after the write.

The page hears about everyone else's writes through `GET
/api/goal-page/events`, a stream of server-sent events. The server stats
the goal files every half second and, when one has moved, sends the new
revision; the page reads the goal again unless it already displays that revision, and lays the answer under whatever the reader is in the middle of.
A row the builder holds (`queued`, `building`, `asking`) says so and is
left alone until it comes back. Local files are the truth; the chat's
autosync sends them to the account four seconds after the last edit.

The account shows the same pattern. `loadAccount` asks `/api/supabase` who
this machine is connected as (the account the installer wrote to
`auth.json`). `signOut` and `startSignIn` post to `/api/account/sign-out`
and `/api/account/sign-in`, where the server runs the Engelbart CLI itself:
`engelbart logout` revokes the machine token, unwires the Claude Code
helper and removes `auth.json`; `engelbart auth` prints a code, opens the
page that approves it, and waits, while the page asks `GET
/api/account/sign-in` after it until it has finished. The CLI is the one
the installer put at `~/.local/bin/engelbart`, or whatever `ENGELBART_CLI`
names.

Bart uses the existing agent routing, told which subgoal the conversation is about. `POST /api/goal-page/bart` takes the subgoal's whole
conversation, reads and digests the tree under the state lock, asks the
model outside it on the reader's own account (`claude` in safe mode, as
setup and the brainstorm do), and answers with what to draw: prose as
text, each row it proposed as a proposal the reader adds with one click
(through `add_todo_row`, so nothing is written until they do). A question
or a choice is said as text with its options, and answered by typing. The
conversation is kept beside the goals, per subgoal, in the page's own
shape (`chat_state.save_bart_chat`, `bart.json` in the tree's session):
`POST /api/goal-page/chat` merges nonempty saves by stable message id after every change, and the
payload's slices carry it back, so a reload draws what was on screen.

The preview and Terminal read the real preview engine and build activity through
`GET /api/goal-page/panes`. Preview operations use `/api/goal-page/preview`.

The workspace this page replaced still answers at `/legacy`.

## What answers Bart

A message to Bart is routed before a model sees it (`trajectory/agents/`,
its README has the flow). An ordinary message is answered by the Chat
agent -- prose, at most a row or two proposed. A message that asks for
options in so many words goes to the brainstorm, and one that asks for the
project to be planned goes to the Path agent, which can revise subgoals and
todos. Responses use the same Bart conversation and todo-proposal contract. When the Chat agent finds the message turns on a
preference only the reader can settle, the brainstorm puts that question
to them; when it turns on a fact of the project, the project directory is
read and nobody is asked. A build the page starts is verified when it
ends, and a verdict that fails sends the row back out with the reason,
twice, before Bart says so in the conversation. Every interaction is
written to the chat's `agent_events.jsonl`; only the transitions above
reach the Overseer.


## Alternate workspace at `/test`

`/test` and `/test?goal=<id>` serve `test/index.html` on the same server and
session. `/` retains its production shell, stylesheet and renderer. Both use
`bootstrap.js`, `store.js`, `actions.js`, and `services.js`; there is no separate
disk state, API namespace, model caller, or execution system.

The workspace half of `Engelbart Workspace (standalone).html` supplies the visual
layout. `test/page.js` composes existing components, puts editable todos and Build
all in the Plan rail, and gives the conversation the main Bart pane. The bundled
Latin Source Code Pro font comes from the supplied design. At phone widths the
scrollable rail stacks above the main pane.

| Design interaction | State | Shared action → service/API |
| --- | --- | --- |
| Plan subgoal | `subgoals`, `activeId` | `selectSubgoal` → pane read + interaction event |
| Add subgoal | `subgoalDraft` | `commitAddSubgoal` → `addSubgoal` → `add_goal` |
| Todo edit/toggle/remove | active slice `todos` | `editTodo` / `toggleTodo` / `removeTodo` → existing todo operations |
| Add todo | active slice `newTodo` | `commitNewTodo` → `addTodo` → `add_todo_row` |
| Bart composer | active slice `draft`, `chat` | `sendMessage` → `sendBartMessage` / `saveChat` → Bart/chat APIs |
| Todo proposal Add/Skip | active slice `chat` | `acceptProposal` / `rejectProposal` → existing todo/chat APIs and events |
| Build all | active slice `todos` | `buildAll` → `startBuild` → `build_todos` |
| Live Preview | `panes.preview` | existing preview actions → `previewOp` → preview API |
| Terminal | `panes.build.lines`, `.run`, preview output | existing pane polling |
| Header and account | project, goals, account, reader | existing project/goal navigation, auth and expertise actions |

Small shared contract extensions:

- Optional `panes.build.phase = {status, todoIds, at, reason}` is a read-only
  projection of existing recorded lifecycle events. It distinguishes checking,
  fixing and needs-user from the todo row's storage status. No agent code changes.
  Subsequent successful user edits remove affected rows from old terminal phases.
- Nonempty chat saves merge message ids under the existing session lock, keeping
  other pages' unseen turns and settled proposals. An explicit empty save retains
  the existing clear behavior. Polling imports ordinary turns as well as system
  messages; page-specific UUIDs avoid collisions between simultaneous pages.
- Goal change notifications compare with the revision currently displayed, so a
  remote add/edit/remove sequence returning to an earlier revision is visible.

The design's simulated debugger, model counters, timers, percentages, demo
preview and terminal text are not included. Existing agent explanations, choices,
and questions render as Bart prose; todo proposals retain Add/Skip. The current
contract has no structured choice buttons, deferred Apply/Keep-plan card, retry/skip
failure card, or preview-check badge strip. Those visual widgets are not fabricated.
Actual plan changes still arrive through the goal event stream; evidence and
execution activity remain available in Terminal and the existing debugger.

Validation: `tests/test_goal_page_test_ui.py` reuses the goal-page Playwright and
isolated loopback server fixtures. It covers both routes, shared edits and
conversations, existing Build invocation, plan updates, account/expertise,
preview discovery/start/iframe/stop, activity/events, lifecycle labels, responsive
layout, query selection, and stale-save protection. Model replies and the external
build executable are replaced at their existing boundaries; HTTP, files, event
capture and preview processes are real. Cross-repository installed round-trip and
platform workflow gates remain required before a merge.

### Lifecycle communication

Both renderers use the shared `todoPhase`/`TODO_LABELS` mapping of the existing
`panes.build.phase` event projection. Build starts show Building…, verification
shows Checking…, automatic repair shows Fixing…, confirmed human dependencies
show Needs you, and successful verification shows Done with a checkmark.
Unsuccessful execution shows Failed. No time-based progress or layout is added.
The composer remains usable while background work runs.

A build-protocol question is classified through the existing Chat boundary before
it can become `chat.needs_human`. Environment questions use local discovery and
resume the same build. Human answers use the existing Bart endpoint and build
answer operation; optional model `resolution` distinguishes resume/cancel/wait.
The `human.answered` event clears the dependency. Saved run row ids and criteria
are retained when resuming after a server restart.

`agents/communication.py` publishes result-grounded prose for meaningful failure,
repair, completion, or a human question, with stable per-build message identities.
It adds no announcement model calls. Repeated identical repair problems remain
quiet; ordinary starts, checking, and polling do not append conversation entries.
Technical results fall back to a concise todo-level summary; detailed verifier
evidence is bounded and written to existing Terminal activity. Applied plan
changes continue through the existing Plan state and Bart reply contracts.

`tests/test_goal_lifecycle.py` covers classification, resume, cold-run metadata,
repair deduplication, evidence, and the real loopback page/composer. Model and
runtime execution boundaries are controlled fixtures, not live-provider tests.

### Project resources and inactive lifecycle truth

`project.resources` is the project store's durable resource list. Claim preparation
keeps downloaded PDFs and extracted text, or verified tabular data, beneath the
project's ignored `.engelbart-resources/<id>/` directory. Paper readiness requires
both readable PDF and extracted text. Dataset readiness requires opening actual
CSV/TSV, Parquet, JSON/JSONL, or safely extracted ZIP contents. Automatic downloads
and total ZIP extraction are capped at 50 MiB by default (`HC_RESOURCE_MAX_BYTES`);
papers additionally cap at 20 MiB. Restricted or ambiguous sources need user action.
No downloaded file is executed. Public network addresses and redirects are checked,
and local paper serving accepts only a ready persisted resource ID, never a path.

Production `/` and `/test` share `components/resources.js`: the contextual Paper
tab, subordinate Resources list, and temporary dataset detail content. Production
`breakdown.js`, `tabs.js`, and `page.js` compose it through existing actions/services
and shared `project.resources`, `resourceId`, `resourceUrl`, and tab state. The
alternate renderer no longer implements a separate resource UI. Details expose
bounded schema/path/status/source and fallback provenance, never raw samples. A
small narrow-screen rule stacks the rail above the center and wraps existing tabs. Native PDF rendering requires no PDF viewer dependency in the browser.
PDF text and Parquet support in the local runtime require pypdf and pyarrow; the
installer resolves wheel-declared dependencies. Release vendoring still follows
the existing committed-source workflow.

Onboarding `available` is an access preflight, not local `ready`. The claim carries
the verified direct target and explicit fallback provenance. Preparation currently
runs at local claim, when a project directory exists, and reaches terminal states
before returning. No extra cloud acquisition job, resource cache, or agent exists.
The existing bounded project context supplies artifact references and a short
untrusted paper excerpt, with dataset schema summaries but no raw dataset samples.

Both the initial goal response and existing pane poll include `phases`, keyed by
subgoal ID, from the same lifecycle event projection used for active work. The
alternate Plan completion check calls `todoPhase(todo, state, subgoalId)` for every
row, including inactive subgoals. Checking, fixing, needs-user, and failed work
cannot appear successful merely because the builder wrote raw `done` rows. Manual
completion on legacy rows with no lifecycle history retains its existing behavior.
`tests/test_inactive_lifecycle.py` exercises the exact switch-away/repair/recheck/
pass sequence in the real loopback browser.


### Resource recovery

`prepare()` reuses only `cached_ready()` resources. Validation checks safe paths,
present PDF/text or listed inspected primary files, plausible headers, and sizes.
Fresh preparations store a size plus first/last 4 KiB fingerprint per artifact.
This catches common truncation/replacement without hashing or reparsing a large
file at startup; it does not detect arbitrary interior-only edits that preserve
size and both edges. Legacy records have no fingerprint and use basic format checks.

Supplying a failed, needs-user, interrupted, missing, or detectably corrupt resource
again retries acquisition in place with the fresh source and preserved provenance.
Acquiring and terminal state are persisted; no background retry loop is added.
Newly supplied unresolved/discovered manifests still do not trigger acquisition.
Signed `downloadUrl` fields and token-bearing URL queries are removed from persisted
source and nested access/provenance evidence. ZIP retry discards the previous
managed extraction so stale files cannot validate a new broken archive.

`web_setup.materialize()` recognizes a repeated onboarding ID from existing durable
resource provenance, allowing the same handoff to retry resources without replacing
project/goals. A different onboarding that merely shares its name remains a collision.

An explicitly labeled `synthetic_fallback` may carry at most 8 KiB of `source.inlineCsv`.
Preparation writes it through the same file/schema inspector before marking Ready.
Its original/source/reason/structure provenance reaches bounded agent context and the
shared details view. There is no synthetic execution or additional resource system.
