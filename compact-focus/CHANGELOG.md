# Changelog

## 0.21.0 — 2026-08-11

- Replace same-terminal file-descriptor theft with a companion terminal review
  launched automatically by the ordinary `/compact`. Claude Code and Codex
  keep rendering their hook status in the original terminal while the reviewer
  gets exclusive input and output in the companion surface.
- Remove every `SIGSTOP`/`SIGCONT` path and the obsolete terminal-lease probe.
  Compact Focus can no longer suspend the host, strand zsh in job-control mode,
  or leave an orphaned curses process competing with the chat renderer.
- Fail closed when the review window is closed, mismatched, unavailable, or
  timed out; compaction proceeds only after an explicit approval result.
- Restore the full Codex contract beside the first post-compaction prompt.
  Codex 0.147.0 defers `SessionStart` until that prompt; `UserPromptSubmit`
  supplies the user-authored precommit without duplicating the full contract,
  and remains the full-contract fallback if the lifecycle event never arrives.

## 0.20.7 — 2026-08-11

- Correct the friend-facing Claude Code install flow: source review happens at
  installation when prompted, while `/hooks` is a read-only verification
  screen.
- Add a minimal external-beta path and an explicit, privacy-preserving feedback
  route.

## 0.20.6 — 2026-08-11

- Label the host-only lifecycle subcommand explicitly instead of leaking
  argparse's internal suppression sentinel into `compact-focus --help`.

## 0.20.5 — 2026-08-11

- Move the opt-in Codex proposal worker default from deprecated
  `gpt-5.4-mini` to its CLI-recommended replacement, `gpt-5.6-luna`.

## 0.20.4 — 2026-08-11

- Replace full-ledger prompt reinforcement with a contradiction-free cue that
  contains only the exact human precommit and explicit provenance. This avoids
  retrieving an older assistant denial over the user's adjudication.
- Reinforce for the first three continuation prompts by default instead of
  consuming the cue on an arbitrary first prompt.

## 0.20.3 — 2026-08-11

- Reinforce the approved contract once beside the first post-compaction user
  prompt. SessionStart delivery remains the primary restoration path; prompt
  reinforcement prevents large third-party startup contexts from burying the
  user's catastrophic constraint.
- Keep the catastrophic precommit flagged for human inspection whenever it is
  not carried exactly; high lexical overlap is not treated as proof that a
  rewording preserved the user's meaning.

## 0.20.2 — 2026-08-11

- Reconcile Claude's provisional hook-payload audit against the new
  `isCompactSummary` transcript record in a bounded detached process, because
  Claude appends the authoritative carried summary only after synchronous
  compaction hooks return.

## 0.20.1 — 2026-08-11

- Restore the exact approved contract through `SessionStart` on both hosts;
  Claude's native summarizer still receives it as a best-effort constraint but
  is no longer trusted as the sole carrier.
- Audit the catastrophic precommit independently from ledger items and prefer
  the new `isCompactSummary` transcript record over hook diagnostics or an
  older compaction boundary.
- Degrade safely to monochrome on terminals with restricted color-pair support
  and create or tighten every state lock with user-only permissions.

## 0.20.0 — 2026-08-11

- Add a native Codex plugin manifest and lifecycle-hook port around the built-in
  `/compact`, with the same precommit, inline ledger, cancellation, recovery,
  and natural feedback workflow as Claude Code.
- Parse Codex rollout evidence after the latest compaction boundary, including
  assistant messages, semantic command/file/tool outcomes, direct context-window
  telemetry, mid-turn continuation episodes, and explicit reasoning exclusion.
- Restore the approved contract automatically through Codex `SessionStart`,
  then carry its structured human decisions into later Codex reviews.
- Record the platform boundary explicitly: Codex does not accept dynamic
  `PreCompact` context and remote summary text is encrypted, so Compact Focus
  does not claim direct summarizer control or post-summary adherence auditing.
- Make background Codex proposal analysis opt-in, ephemeral, hook-free,
  read-only, schema-bounded, and isolated from the project directory.
- Replace the host-specific terminal lease and async hook dependency with a
  Claude/Codex host lease and a self-detaching background dispatcher.

## 0.19.0 — 2026-08-11

- Replace the skill-driven, copy/paste `/compact focus` flow with one bare
  `/compact`, a same-terminal review, and automatic continuation.
- Add an unanchored precommit, editable document ledger, independent
  type/status/retention axes, class rules, provenance expansion, rival
  representations, and per-prompt context costs.
- Reconstruct assistant conclusions, tool calls/results, file snapshots, and
  relevant attachments with stable source IDs and exact coverage validation.
- Add recoverable demotion through SQLite/FTS and natural omission versus
  misconstrual feedback.
- Add a conservative post-compaction lexical adherence audit and feed explicit
  review/loss corrections into later proposals.
- Add deterministic cold proposals, cancellable background enrichment,
  stale-analysis rebasing, process/terminal watchdogs, atomic state, and
  per-session locks.
- Remove the obsolete shell orchestration, external terminal/browser launch,
  skill workflows, global current-session pointer, and tracked bytecode.
- Add an installable Python CLI, marketplace metadata, license, CI, tests, and
  operational/privacy documentation.
- Store sensitive state with user-only permissions and collapse cumulative file
  snapshots to actual deltas.
