# Agents behind the goal page

The reader sees Plan, Bart, Live preview and Terminal. The agents have no separate UI.

```text
interaction → local event log → cheap wake-up policy
                                  ├─ minor event → done
                                  └─ meaningful transition → Overseer
                                      ├─ Chat ↔ local environment discovery
                                      ├─ Brainstorm (human preference/options)
                                      ├─ Path → minimal structured plan change
                                      ├─ Build → Verifier → bounded repair
                                      └─ none
verified result → shared context updated → Overseer reassesses the project
```

`policy.py` decides whether an event wakes routing. `overseer.py` makes semantic
project decisions through the configured model with bounded project direction,
current goal, subgoals/todos, selected work, current run, reader profile, durable
facts, recent results, Bart turns, interactions and the triggering result. Explicit
Bart intent is enforced: ordinary messages go to Chat, options to Brainstorm,
planning to Path. Environment uncertainty is inspected locally. Build requests,
build completion and the repair limit have deterministic guards. A routing outage
uses a conservative fallback and is recorded on the telemetry span.

`path.py` returns and applies `keep_plan`, `add_subgoal`, `revise_subgoal`,
`reorder_subgoal`, `replace_subgoal`, `add_todos`, `revise_todos`, and
`remove_obsolete_todo`. It validates a complete patch before one locked save,
rejects stale snapshots and protects running/completed work. The existing goal
change feed refreshes Plan; Bart supplies the explanation.

`acceptance.py` derives a minimal observable criterion when absent and persists
it before the Build agent starts. The same contract enters the build prompt and
run record. `verifier.py` checks row statuses, process error/exit and nested preview
health first, then `artifacts.py` inspects real artifacts. Browser checks use
Playwright on the loopback preview and can assert visible content, accessible
controls and bounded click/fill behavior. File checks read through LocalRuntime.
Prose criteria use model judgment grounded in inspected artifacts; missing
inspection evidence fails. HTTP errors, including 404, are not healthy previews.

A failed verdict carries its actual evidence into the repair instruction, including
fresh-session repairs. Repair runs retain the selected row and original set of
rows to reverify. Two repairs are allowed, then Bart and Terminal explain the
failure. Successful verification updates shared context before the Overseer
chooses none, explanation, preference discussion or a Path revision. It does not
unconditionally replan or start the next build.

`POST /api/goal-page/interaction` accepts only minor interaction names. It records
bounded local data and cannot invoke routing. The page records project/goal opens,
subgoal selection, tabs, preview open/close/focus/controls, opened preview artifacts,
proposal acceptance/rejection, starting a new todo and chat saves. Cross-origin
preview internals remain opaque; entering the frame is recorded without reading
its contents. Build cancellation is also recorded by the build lifecycle.

Shared context supports all nine kinds: `new_fact`, `user_preference`,
`project_constraint`, `decision`, `discovered_dependency`, `todo_status`,
`run_result`, `artifact`, `verification_result`. Updates use stable subject keys to
supersede obsolete information; current facts are rendered first. Session locks
serialize context/event writes. System Bart messages have stable IDs, survive
stale browser saves and appear through the existing pane poll on already-open
pages.

Agent spans feed the existing `human_compact.telemetry` debugger contract. No new
debugger UI is introduced. `bart.message` contains `overseer.route` and
`chat.reply → model.chat`. The asynchronous `todo.build` operation is carried into
the build reader thread, verification, repairs and final reassessment. The local
`agent_trace.jsonl` remains a diagnostic record; debugger traces use the shared
telemetry sink and IDs.

Local files beside the chat's goals:

- `agent_events.jsonl`: rotating interaction/result log (2 MB / 2,000-line tail).
- `agent_context.json`: at most 200 current context updates.
- `agent_state.json`: repair counts per subgoal.
- Existing build records: selected rows, verification rows, acceptance and repair instruction.

`Runtime` is the interface and `LocalRuntime` is its only implementation.
`HC_AGENT_RUNTIME` accepts `local` only. `HC_AGENTS=0` retains legacy Bart/build
behavior. `HC_CHAT_PROVIDER` chooses the model provider.

Browser verification requires Python Playwright and Chromium or an installed
Chrome (`HC_BROWSER_EXECUTABLE` can name it). Missing browser tooling fails the
artifact check; it never turns page health into a semantic pass. The headless
Build lifecycle is covered end to end; the optional legacy connected-session
queue still relies on its hook completion path and is not an asynchronous
headless trace. Cross-process server restarts cannot resume an in-memory trace.

Before merging, run the native installed cross-repository round trip and the
macOS, Ubuntu and Windows workflow gates specified in the repository AGENTS.md.
