# Engelbart TODO and Bart runtime explorer

An interactive, code-derived walkthrough of two scenarios: building nested
TODOs and adding more work; sending a message to Bart. Uses the request-list,
flow and inspector pattern from Berkeley onboarding's `/engelbart/setup/test`.

Audit target: installed Engelbart **0.20.4**, wheel SHA-256
`67f0f5010964b7d2ae0760b15aa4f494b4f5b76290c76519f78ea59e732df015`.
Inspected September 13, 2026. Defaults: `HC_AGENTS` enabled, headless builds,
automatic full/quick selection, interface model Opus, build model Sonnet.
Model aliases can resolve to account-specific names; configuration overrides
and previously retained quick sessions can change behavior. This is not a
claim about every currently running workspace or a deployed build.

## Reproduce

Use the Python interpreter in the installed `hc` runtime, or run with
`PYTHONPATH=hc/src` against an explicitly chosen source checkout:

```sh
python docs/runtime-explorer/probe.py > docs/runtime-explorer/evidence.json
python3 docs/runtime-explorer/render.py --output "$HOME/Desktop/Codex Readings/engelbart-runtime-explorer"
python3 -m http.server 8768 --bind 127.0.0.1 --directory "$HOME/Desktop/Codex Readings/engelbart-runtime-explorer"
```

Open `http://127.0.0.1:8768/`. The exported `index.html` also opens directly;
walkthrough controls, prompts, and source excerpts are embedded and simulate
behavior without dispatching builds. To ask questions in Codex, replace the
static server with the question service below. The static export still reads
normally, but cannot submit or load added questions without that service.

`explorer.html` is a template; `render.py` substitutes the probe's JSON.
Raw `evidence.json` is ignored because it includes full source files; the
export carries only cited excerpts, original line positions and module hashes.
`validation.json` records reproducible pure-function results and provenance.

## Evidence boundary

- The probe imports real prompt and row-selection functions, uses synthetic
  project/goal/row records in a temporary directory, and fails if they try to
  launch a process. It verifies nesting, automatic lane selection, Notes
  inclusion/omission, and the cold-start estimate.
- Flow, concurrency, provider, and verification behavior comes from source
  inspection. No real agent response or successful live verification is
  represented as observed. The response-path selector illustrates branches;
  it does not predict routing of the sample words.
- The captured full and quick prompts omit optional sections for which the
  fixture has no records (model-derived acceptance, run profile, resources,
  attachments and reader profile). A real successful launch with agents enabled ensures saved acceptance before spawning; this fixture deliberately skips that model-derived step. Other sections appear when their records exist.
- Process labels and IDs are illustrative. Model output and future timing
  cannot be derived from source code. Passing generated acceptance is only as
  strong as those criteria's coverage.
- The main source checkout contained unrelated uncommitted changes. Work is
  isolated on `docs/todo-bart-runtime-explorer`; installed files supply the
  line-numbered evidence. No production runtime was changed.

## Important distinctions exposed

1. Five selected stored rows form two parent protocol units in one initial
   full-context Claude session.
2. The UI blocks more builds on a busy subgoal; the backend supports joining
   rows through process termination and `--resume`. Different subgoals can
   run concurrently against the same filesystem.
3. Saving Notes does not update a running builder. Automatic quick builds
   omit the goal tree and its Notes. Full builds include a fresh snapshot.
4. A clean process exit marks remaining building rows done even without DONE
   markers. Verification/repair follows separately.
5. Bart uses fresh, tool-disabled structured CLI calls, usually two per turn.
   Router and responder get different context. Path can directly mutate the
   plan; ordinary proposal cards require adoption.

## Validation performed

- Isolated prompt/selection probe assertions passed against the installed runtime.
- Browser exercised 60 scenario-step × inspector-view combinations.
- Targeted checks passed for full/quick Notes inclusion, later-row presence,
  different-subgoal comparison reset, and the paused-builder routing bypass.
- All four Bart paths rendered; source excerpts loaded; browser console had
  no errors. Layout had no horizontal overflow at 360, 390, 736, or 1,024 px.
- Final generated JavaScript passed `node --check`.
- TODO and Bart explanations received independent source audits. No live
  end-to-end build or model-quality test was run.

Official capability references, checked September 13:

- https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/computer-use

Claude Code supports streaming input; this Build launcher chooses closed stdin.
Built-in computer-use requires an interactive session and does not run under
`-p`. Configured browser automation tools are a different capability.

## Consolidated conversation guide

The opening overview now connects **36 questions** to the two original traces
and two incident replays. `guide.js` holds the curated answers, relations,
incident timelines, and local fast/full simulator; `guide.css` styles this layer.
Both are embedded by `render.py`, so the export remains one portable HTML file.
`policy-guide.js` adds the role comparison, eight Path operations, complete
Overseer table, 40 fixed-behavior entries in ten orchestrator groups, and the
queue/Notes scenarios. These are appended to the same searchable question index.

- The question index searches question wording, answer text, and categories.
  Only the selected answer is expanded. Related questions link across topics.
- URL fragments preserve questions, incident steps, and original trace views,
  for example `#question=preview-coupling`, `#incident=bart&step=3`, and
  `#trace=todos&step=2&view=payload`.
- The fast/full widget uses patterns extracted from the installed Python
  function. Unicode word boundaries and character counting are preserved.
  It never changes the user's settings or dispatches work.
- The two incident replays are curated observations from the user's actual
  seven-row build and later Bart message. They are separate from the synthetic
  five-row original scenario. Screenshots are reconstructed from the supplied
  content; private screenshots and full transcripts are not embedded.
- The broad `pkill` command is observed. Attribution of the preview shutdown
  to that command is explicitly labelled an inference because no PID-specific
  termination record was captured. The raw Bart classifier response is not
  available in the curated evidence; the matching application return branch
  is shown without inventing its output.
- Every recovery or routing change described under proposed fixes remains a
  proposal. This documentation update does not patch the installed runtime.

The incident provenance is local audit evidence, not a new live reproduction:
`supabase-preview/agent_events.jsonl` lines 377–398, 410–411 and the builder
transcript `f8817919-08e7-46f5-9749-774ca9a0bb15.jsonl` lines 1032 and 1175.
The exported page includes only the relevant fields and public source excerpts.

After refreshing the probe, validate the JavaScript and simulator:

```sh
python3 docs/runtime-explorer/validate.py
```

This checks 15 examples against captured results from the real Python selector,
including size boundaries, nested-row counts, wording changes, risky work,
Unicode word boundaries and code-point length, plus two override cases.

Browser validation for the consolidated guide is recorded in
[`browser-validation.json`](browser-validation.json): all 30 question links,
all 12 incident steps, search, source disclosure, fast/full controls, original
prompt comparisons, pending-answer routing, deep-link reload, and responsive
checks at 390 and 1,024 pixels. No browser console errors or warnings were found.

## Routing and queue follow-up

The six appended answers cover Path, Chat versus Brainstorm, all Overseer
options, the orchestrator inventory, queue behavior/stashing, and a new batch
on the same subgoal with changed Notes. The existing Notes answer now also
states that backend joining does not refresh Notes.

The routing control displays captured results from the installed
`OVERSEER.route`, not a JavaScript reimplementation: 17 scenarios × eight
hypothetical outputs (six actions, invalid output, and an exception), all
checked with a stub model. It distinguishes model invocation, the guarded
action, and the orchestrator's extra smalltalk/question/escalation handling.
The JSON is labelled hypothetical input with a real captured code decision.

The queue probe checks real `start`, `_join`, `joined_message`, and `deliver`
behavior in temporary state. A fake live process captures `redirect`; it never
terminates or starts Claude. Nine checks establish conversation/lane retention,
new-row inclusion, Notes omission on join, no queue entry for joins, queued to
building transition, draining, enqueue-time prompt snapshots, and empty repeat
delivery. This is not a live hook delivery or Claude resume/completion test.

The scenario selector covers the standard busy UI, backend join, later full,
later quick, and alternate connected-session queue. Captured message examples
show the context difference. Current headless submissions do not populate the
legacy `later.json` queue. The app does not automatically stash per-TODO code
changes; code stays in the shared working directory.

Follow-up browser checks cover all six appended links, all ten inventory
disclosures, all five queue scenarios, nine routing boundary combinations,
all eight hypothetical model-output choices, captured message disclosure,
question deep links, and phone layout. Earlier 30-question validation is
retained as the baseline; it was not rerun in full for this additive update.

## Ask from a highlight

Select a passage and choose **Ask Codex** (or use Alt+Shift+A after selecting).
Write the question, then **Save and ask Codex**. It appears beneath its parent
in the question index, with the selected quote retained. You can highlight an
added answer to ask a further subquestion. **Add question** works without a
selection; **Export added Q&A** downloads the saved questions and answers.

Run this service instead of `http.server` on the same port:

```sh
python3 docs/runtime-explorer/serve.py \
  --directory "$HOME/Desktop/Codex Readings/engelbart-runtime-explorer" \
  --thread YOUR_EXISTING_CODEX_THREAD_UUID \
  --transcript /absolute/path/to/rollout-for-that-thread.jsonl
```

Keep the Codex desktop app open. `--thread` defaults to `CODEX_THREAD_ID` when
launched from Codex. The transcript must be that thread's existing local
JSONL file. On this Mac the service prefers the app's bundled CLI, which
supports `codex queue --thread … --message …`; `--codex` permits an explicit
compatible binary. This uses the existing conversation and its configured
model/tools. It does not launch a substitute LLM or builder session.

Questions wait behind the current Codex turn. The service sends one page
question at a time, then waits for its completed answer before sending the
next. The UI shows delivery status; it does not imply an immediate response.
Queued questions are normal conversation inputs, so intervening chat messages
can affect what Codex works on. Each question requests a unique answer marker.
Only correlated completed answers from the configured transcript are saved;
commentary and unrelated final answers are excluded. The marker is removed
from the displayed answer. This depends on the installed CLI and rollout
event format, not a stable public integration API.

Added Q&A live in `<output>/.thoughts/questions.sqlite3` (override with
`--data-dir`). Regenerating `index.html` does not erase them. The store binds
to one thread/transcript, survives service restarts, and deduplicates request
IDs. Back up that directory or use Export. The portable HTML alone does not
contain your added Q&A. No database, transcript, or credentials are served as
static files or included in the repository.

Delivery failures retain the question and expose retry. A timeout or restart
during delivery is marked uncertain and is never automatically resent. Check
the Codex conversation before retrying, since it may already be queued.
The server binds only to loopback and checks Host, Origin, JSON content type,
and a random session token for mutations. Prompt text is passed as one argv
value, never interpolated into shell code. Model prose is escaped before its
small Markdown subset is rendered; executable links are rejected.

Backend boundary checks:

```sh
python3 -m unittest discover -s docs/runtime-explorer -p test_thoughts.py -v
python3 docs/runtime-explorer/validate.py
```

The live highlight round trip was verified: a browser-selected passage became
an input in the existing Codex conversation, whose real completed answer was
captured under the correct parent and displayed after regeneration/reload.
This establishes one successful local round trip; it does not establish
long-term queue reliability or compatibility with all future CLI versions.
