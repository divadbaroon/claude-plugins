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
all interaction, prompts and source excerpts are embedded. No dependencies,
API requests, model calls, account actions, or builds are made by the page.

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
