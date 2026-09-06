# The agents behind the goal page

The page keeps four surfaces -- Plan, Bart, Live preview, Terminal. Behind
them, six roles, each a module here with one entry point, and an
orchestrator that hands a turn to the right one.

## The rule

Every interaction is written to a local event log; the Overseer is asked
only on meaningful transitions. Concretely:

```
page / build ──► orchestrator.emit(event) ──► agent_events.jsonl   (always)
                        │
                        └─ policy.is_meaningful(event)?
                              no  ─► done (a row edited, a draft saved, a span)
                              yes ─► overseer.route(event, state) ─► one decision
                                        ├─ chat        ─► agents/chat.py       (one small model call)
                                        ├─ brainstorm  ─► agents/brainstorm.py (the existing brainstorm.ask)
                                        ├─ replan      ─► agents/path.py       (brainstorm.ask, todos card forced)
                                        ├─ build       ─► runtime.build / runtime.reopen  (build.start / build.reopen)
                                        ├─ verify      ─► agents/verifier.py   (no model)
                                        └─ none
```

The meaningful transitions (`policy.MEANINGFUL`): a message to Bart, rows
handed to the build, a plan asked for, a build finishing, failing or
stopping to ask, a verdict from the Verifier, and the Chat agent declaring
an uncertainty. Everything else -- `todo.added`, `todo.text_edited`,
`todo.done_toggled`, `notes.edited`, `chat.saved`, every span -- is recorded
and goes no further.

## A Bart message

```
POST /api/goal-page/bart
  ui._bart_context (under the lock: the piece, its goal, the digest)
  ui._bart_answer  → orchestrator.for_chat(...).bart_message(held, transcript)
      emit bart.message (user)
      overseer.route → policy.bart_intent(text)
          words ask for options  → brainstorm  → brainstorm.reply → replies
          words ask for a plan   → replan      → path.plan        → proposals
          otherwise              → chat        → chat.ask         → prose (+ a row or two)
              chat says needs.kind = human_preference
                  → emit chat.needs_human → overseer → brainstorm, with that question
              chat says needs.kind = environment
                  → emit chat.needs_discovery → overseer → chat again,
                    after runtime.discover(question); the reader is never asked
      emit chat.replied | brainstorm.replied | path.planned (agent)
```

## A build

```
POST /api/goal-page/op {op: build_todos}
  ui._goal_page_write → orchestrator.build_requested(goal, rows)
      emit todo.build_requested (user) → overseer → build → runtime.build → build.start
      emit build.started
  ... the headless claude works; build.Run._finish writes the run record ...
  build._after_finish → orchestrator.build_finished(goal, ended, rows)
      idle    → emit build.completed → overseer → verify → verifier.verify
                    pass → emit verify.passed → overseer → none
                             + context: verification_result, todo_status per row
                    fail → emit verify.failed → overseer
                             attempts < 2 → build → runtime.reopen(row, "Verification failed: ...")
                             attempts = 2 → chat  → a plain message into the piece's
                                            conversation and the Terminal; no model
      failed  → emit build.failed   → overseer → none (+ context: run_result)
      waiting → emit build.question → overseer → none (the answer resumes the run)
```

The Terminal pane shows the Verifier's lines (`verifying the build`,
`verified: ...`, `verification failed: ...`, `repair 1 of 2: ...`) through
the build's own activity feed.

## Files, per chat (beside goals.json, via chat_state.paths)

- `agent_events.jsonl` -- the log. One LocalEvent a line:
  `{id, projectId, timestamp, type, source, payload, subgoalId, todoId, runId}`.
  Rotated past 2 MB to its last 2000 lines.
- `agent_trace.jsonl` -- spans: `bart.message > overseer.route > chat.reply`,
  `todo.build > overseer.route > build.agent`, `build.finished > verifier.agent`,
  and inside the runtime `file.read`, `file.write`, `command.exec`, `preview.start`,
  `model.call`. Same shape as an OTel span; no dependency.
- `agent_context.json` -- shared context updates: `new_fact`, `user_preference`,
  `project_constraint`, `decision`, `discovered_dependency`, `todo_status`,
  `run_result`, `artifact`, `verification_result`. `context.render` puts the
  durable ones under "What is already known" in every agent's prompt.
- `agent_state.json` -- repair attempts per subgoal.

## The runtime

`runtime.Runtime` is the interface: `describe`, `read_file`, `write_file`,
`run`, `discover`, `preview_state`, `start_preview`, `build`, `reopen`.
`LocalRuntime` is this machine. `runtime.make()` reads `HC_AGENT_RUNTIME`
(default `local`); `daytona` is named and refuses with a clear message until
the class lands. The agents and the page never touch the project except
through the runtime the orchestrator holds.

## Switches

- `HC_AGENTS=0` -- the old path: every Bart message is the brainstorm, a
  finished build is not verified, nothing is logged.
- `HC_AGENT_RUNTIME` -- `local` (default) or, later, `daytona`.
- `HC_CHAT_PROVIDER` -- as before, the provider every model call uses.
