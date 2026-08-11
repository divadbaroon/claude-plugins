# Compact Focus

Compact Focus turns the native `/compact` in Claude Code and Codex into a
human-reviewed transaction. One command opens an inline editor before
compaction: state what must not be misconstrued, inspect the source-grounded
ledger, change its meaning or retention, and press Enter. There is no generated
command to copy and no second confirmation dialog.

Version 0.20.7 is tested against Claude Code 2.1.227 and Codex CLI 0.147.0 on
macOS. Linux uses the same POSIX terminal path. Native Windows is not yet
supported.

## Transaction model

```text
normal turns ── optional bounded background proposal ──┐
                                                       v
one bare /compact ── unanchored precommit ── inline editable ledger
                                                       |
                         q blocks <── human decision ──> Enter approves
                                                       |
                ┌──────────────────────────────────────┴─────────────────────┐
                v                                                            v
       Claude compactor receives                                  Codex compacts natively
       the approved contract
                |                                                            |
                └──────────────────────┬─────────────────────────────────────┘
                                       v
                    SessionStart restores the contract
                                       |
                                       v
                  first prompts reinforce the precommit
                                       |
                                       v
                        recovery + feedback-conditioned reviews
```

The foreground path never waits for a proposal model. If background analysis
is absent, stale, or running, the editor opens an immediate conservative view.
The validator partitions every reconstructed source exactly once and blocks
approval when a source is missing, duplicated, invented, or attached to an
unresolved contested item.

The host capabilities are not identical:

| Capability | Claude Code | Codex |
| --- | --- | --- |
| Intercept one native `/compact` | Yes | Yes |
| Inline precommit and full ledger editor | Yes | Yes |
| Cancel before compaction | Yes | Yes |
| Feed the approved contract into the native summarizer | Yes, best-effort and audited | No |
| Restore the contract into the immediate continuation | `SessionStart` + bounded precommit reinforcement | `SessionStart` + bounded precommit reinforcement |
| Inspect and lexically audit the generated summary | Yes | No; remote summary text is encrypted |
| Recover demoted evidence and learn from explicit feedback | Yes | Yes |

Claude's native summarizer receives the contract but can still violate it.
Compact Focus audits the plaintext result and independently restores the full
contract after compaction, then reinforces only the contradiction-free
precommit beside the first three continuation prompts. Codex preserves the same
downstream contract without
pretending to control or inspect a summarizer interface that Codex does not
expose.

## Install for Claude Code

Requirements: Claude Code with plugin hooks, Python 3.9+, and a macOS or Linux
terminal.

```bash
claude plugin marketplace add divadbaroon/claude-plugins
claude plugin install compact-focus@papert-tools
```

For deterministic foreground review with no background model call:

```bash
claude plugin install compact-focus@papert-tools --config background_analysis=false
```

Review and accept Claude Code's plugin-source warning during installation,
then start a new Claude Code session and use the ordinary `/compact`. The
`/hooks` menu can verify that Compact Focus loaded, but it is read-only and is
not a separate trust step.

Update with:

```bash
claude plugin marketplace update papert-tools
claude plugin update compact-focus@papert-tools
```

## Install for Codex

Requirements: Codex CLI 0.147.0+ with lifecycle hooks enabled, Python 3.9+,
and a macOS or Linux terminal.

```bash
codex plugin marketplace add divadbaroon/claude-plugins
codex plugin add compact-focus@papert-tools
```

Start a new Codex session, open `/hooks`, and trust the Compact Focus hook
definition. Then use the ordinary `/compact`. Codex hashes trusted hook
definitions, so review them again after an update.

```bash
codex plugin marketplace upgrade papert-tools
codex plugin add compact-focus@papert-tools
```

Codex background model analysis is off by default. The deterministic editor is
the complete foreground workflow.

## Friend beta

Installation is user-scoped by default, so one install applies to new local
sessions across projects. For a meaningful test, use a conversation containing
several decisions, a reversed assumption, and an unresolved question. Run the
ordinary `/compact`, edit any misconstrual in the inline ledger, and approve
with Enter or cancel with `q`.

Report friction through the
[Compact Focus beta feedback form](https://github.com/divadbaroon/claude-plugins/issues/new?template=compact-focus-beta.yml).
Compact Focus has no telemetry, so review corrections and transcript evidence
stay on the tester's machine unless they deliberately include redacted details
in a report.

## Local development and standalone CLI

Clone once, then load the same plugin directory on either host:

```bash
git clone https://github.com/divadbaroon/claude-plugins.git
cd claude-plugins

# Claude Code, one development session
claude --plugin-dir ./compact-focus

# Codex, local marketplace install
codex plugin marketplace add "$(pwd)"
codex plugin add compact-focus@papert-tools
```

Do not install both a marketplace copy and project-local copies of the hooks;
every matching hook runs.

The Python package exposes `compact-focus` and the short alias `cf`:

```bash
python3 -m pip install ./compact-focus
compact-focus --version
compact-focus status
compact-focus search nonlinear drift
compact-focus recall d-0123456789ab
compact-focus doctor
```

An independently installed CLI needs `--state-root` when inspecting the private
state directory owned by a marketplace installation.

## Review interface

The first screen asks what the next agent must not misinterpret. It appears
before any proposal so the proposal cannot anchor the answer. Empty Enter skips
it.

The document keeps knowledge type, status, retention, confidence, and contested
meaning independent. Preserve, summarize, and demote are editable sections;
every item can expose its exact transcript provenance.

| Key | Action |
| --- | --- |
| `↑` / `↓`, `j` / `k` | Navigate |
| `←` / `→` | Collapse or expand source provenance |
| `p`, `s`, `d`, `Space` | Change retention |
| `x`, `t` | Change status or knowledge type |
| `e`, `E`, `N` | Edit title, multiline summary, or next action |
| `m`, `S`, `M`, `n` | Move evidence, split, merge, or create an item |
| `r` | Resolve a contested interpretation |
| `g`, `[` / `]` | Show class rules or change the first-context percentage |
| `c` | Show estimated context cost for every prompt or continuation |
| `v` | Inspect rival problem representations |
| `/`, `u`, `?` | Search, undo, or show help |
| `Enter` | Approve and continue the pending compaction |
| `q` | Cancel compaction |

Class rules are floors, not decisions; explicit item edits win. “First 30%” is
calculated from cumulative attributable token mass, not message count.

## Recovery and feedback

Demoted evidence is written locally to SQLite/FTS and JSONL with stable IDs. It
is omitted from the carried contract but remains searchable.

Report failures naturally in the next prompt:

```text
the compaction lost the nonlinear drift constraint
the compaction misread the failed test as resolved
```

The first is recorded as an omission and the second as a misconstrual. The
`UserPromptSubmit` hook searches demoted evidence and the preserved trace,
injects relevant evidence into the turn, and stores the correction on the right
proposal axis. Human review edits are also retained as examples, never promoted
silently into universal rules.

On Claude Code, a post-compaction lexical audit checks the catastrophic
precommit independently and flags approved items with no anchors in the actual
carried summary. It prefers the transcript's `isCompactSummary` record over the
hook's raw generation payload, which can include hidden planning text. Because
Claude appends that record only after synchronous hooks finish, a bounded
detached reconciliation replaces the provisional hook-payload audit without
delaying the terminal. This is a triage heuristic, not semantic verification.
Codex does not expose plaintext
remote summaries, so its status records the audit as unavailable rather than
inventing a pass or failure.

## Context accounting

Compact Focus shows two separate quantities:

- estimated tokens and context percentage for each attributable episode;
- observed context that cannot be honestly attributed to an episode.

Claude values come from message usage plus explicit status or environment
metadata. Codex values come from rollout `token_count` events and the reported
model context window. Neither path invents a generic 200k denominator.

Codex automatic compaction can happen in the middle of a long agentic turn. In
that case, the parser groups post-boundary evidence at assistant progress
boundaries instead of presenting one unusably large turn.

## Background analysis and cost

Claude Code enables bounded background proposal analysis by default unless the
install option disables it. It starts at 50% of a known window, or 80k observed
tokens when the window is unknown, and refreshes only after 40k more tokens or
12 more episodes. The worker uses the authenticated Claude CLI with no tools,
low effort, and a default $0.10 cap.

Codex background analysis is opt-in because Codex CLI has no equivalent
per-invocation dollar cap:

```bash
COMPACT_FOCUS_CODEX_BACKGROUND=1 codex
```

The Codex worker is ephemeral, ignores user config and hooks, runs in an empty
read-only directory, receives a strict JSON schema, and defaults to
`gpt-5.6-luna`. Foreground `/compact` still opens immediately and preempts a
stale worker.

| Variable | Default | Meaning |
| --- | ---: | --- |
| `COMPACT_FOCUS_BACKGROUND` | host default | Override background analysis on either host |
| `COMPACT_FOCUS_CODEX_BACKGROUND` | `0` | Opt in to Codex background analysis |
| `COMPACT_FOCUS_MODEL` | `haiku` on Claude | General proposal model override |
| `COMPACT_FOCUS_CODEX_MODEL` | `gpt-5.6-luna` | Codex proposal model |
| `COMPACT_FOCUS_MAX_BUDGET_USD` | `0.10` | Claude worker spend cap |
| `COMPACT_FOCUS_WORKER_TIMEOUT` | `180` | Worker timeout in seconds |
| `COMPACT_FOCUS_PREP_THRESHOLD_PCT` | `50` | Known-window warm threshold |
| `COMPACT_FOCUS_PREP_USED_TOKENS` | `80000` | Unknown-window warm threshold |
| `COMPACT_FOCUS_PREP_REFRESH_TOKENS` | `40000` | Token drift before refresh |
| `COMPACT_FOCUS_PREP_REFRESH_EPISODES` | `12` | Episode drift before refresh |
| `COMPACT_FOCUS_AUTO` | `review` | Set `allow` to bypass auto-compaction review |
| `COMPACT_FOCUS_ASYNC_AUDIT` | `1` | Reconcile Claude's provisional audit against the carried transcript summary |
| `COMPACT_FOCUS_PROMPT_REINFORCEMENTS` | `3` | Number of early continuation prompts that receive the minimal precommit cue |
| `COMPACT_FOCUS_STATE_DIR` | plugin data directory | Explicit state override |

## Privacy and failure semantics

State lives under the host-provided private plugin data directory and survives
plugin updates. Directories are user-only where the platform permits it. The
plugin stores reconstructed source text, review actions, demoted evidence, and
explicit feedback locally. Raw base64 media is replaced by dimensions, byte
counts, and digests. Prior-turn reasoning/private thinking is excluded.

With background analysis enabled, bounded textual evidence is sent through a
child CLI authenticated as the current user. Compact Focus has no telemetry,
analytics endpoint, or independent network client.

- `q`, invalid coverage, or an unavailable inline terminal blocks that
  compaction attempt.
- If a proposal worker fails, the deterministic ledger preserves every source.
- A watchdog resumes the host if the editor process dies while the host is
  suspended.
- Foreground compaction never waits for the background lock or worker.
- Transcript JSONL is an internal and unstable interface on both hosts;
  unsupported Codex records are counted and unknown Claude blocks are retained
  as bounded metadata instead of silently disappearing.
- Existing Codex compactions made before Compact Focus are opaque. The first
  review can classify only post-boundary plaintext evidence; later reviews also
  carry Compact Focus's prior structured contract.

## Development and release

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m py_compile compact-focus/compact_focus/*.py
claude plugin validate . --strict
uv run --with pyyaml python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py ./compact-focus
python3 -m build ./compact-focus
```

Manual platform probes live in `tests/manual/`. The automated suite covers both
rollout formats, compaction boundaries, media privacy, proposal grounding,
editor mutations, exact coverage, recovery, worker cancellation, contract
restoration, stale proposal rebasing, and hook control responses.

Compact Focus is MIT licensed. See [CHANGELOG.md](./CHANGELOG.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md).
