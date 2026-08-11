# Compact Focus

Compact Focus makes Claude Code's `/compact` a reviewable transaction. You
state the failure that would be costly, inspect a grounded proposal, edit its
meaning and retention, and press Enter. The original pending compaction then
continues with the approved contract—no generated command to paste and no
second dialog.

Version 0.19.0 is tested against Claude Code 2.1.227 on macOS. Linux uses the
same POSIX terminal path. Native Windows is not yet supported.

## What happens

```text
normal turns ── optional background proposal ──┐
                                               v
one bare /compact ── precommit ── inline ledger ── approved contract
                                                        |
                                                        v
                                             original Claude compactor
                                                        |
                                                        v
                                      feedback + recoverable demotions
```

The foreground path never waits for the proposal model. If background analysis
is absent, stale, or still running, Compact Focus opens an immediate,
loss-averse episode view. A foreground `/compact` preempts and terminates any
background worker.

The ledger partitions every reconstructed source exactly once. Its validator
blocks approval if a source is missing, duplicated, invented, or attached to an
unresolved contested item.

## Install

Requirements:

- Claude Code with plugin hooks and `${CLAUDE_PLUGIN_DATA}` support
- Python 3.9 or newer
- macOS or Linux terminal

Install from the public marketplace:

```bash
claude plugin marketplace add divadbaroon/claude-plugins
claude plugin install compact-focus@papert-tools
```

To use only the deterministic foreground ledger and make no background model
call:

```bash
claude plugin install compact-focus@papert-tools --config background_analysis=false
```

Restart Claude Code after installation. Confirm the four plugin hooks with
`/hooks`, then run the normal `/compact` command when you want to compact.

Update later with:

```bash
claude plugin marketplace update papert-tools
claude plugin update compact-focus@papert-tools
```

For a one-session development load:

```bash
git clone https://github.com/divadbaroon/claude-plugins.git
cd claude-plugins
claude --plugin-dir ./compact-focus
```

Do not install both the marketplace copy and a project-level copy of the hooks;
both would fire.

## Review interface

The first screen asks what the next agent must not misinterpret. This happens
before the proposal is visible, so the proposal cannot anchor the answer. An
empty Enter skips it.

The main document separates `preserve`, `summarize`, and `demote`. Knowledge
type, status, retention, confidence, and contested meaning remain separate
fields.

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
| `c` | Show estimated context cost for every prompt/turn |
| `v` | Inspect rival problem representations |
| `/`, `u`, `?` | Search, undo, or show help |
| `Enter` | Approve and continue the pending compaction |
| `q` | Cancel compaction |

Class rules are defaults, not decisions. Explicit item edits win. “First 30%”
is calculated from cumulative attributable tokens, not the number of messages.

## Recovery and feedback

Demoted evidence is written to a project-local SQLite/FTS index and JSONL with
stable IDs. The approved compaction contract tells the next Claude session how
to recover it.

In Claude Code, report failures naturally:

```text
the compaction lost the nonlinear drift constraint
the compaction misread the failed test as resolved
```

The first form is recorded as an omission; the second as a misconstrual. The
hook searches both demoted evidence and the preserved source trace, injects
matching evidence into the turn, and stores the correction for future
proposals. Approved review edits are also stored as project feedback examples;
they are evidence, not automatically promoted into universal rules.

After compaction, a deterministic lexical audit checks whether each approved
preserve/summarize item left any anchors in Claude's resulting summary. It is a
triage signal exposed by `compact-focus status`, not semantic verification.

The plugin executable is available to Claude as `compact-focus`. From a cloned
repository, the same commands are available at `./compact-focus/bin/compact-focus`:

```bash
compact-focus status
compact-focus search nonlinear drift
compact-focus recall d-0123456789ab
compact-focus doctor
```

An independently installed Python CLI can point at marketplace state with
`--state-root ~/.claude/plugins/data/compact-focus-papert-tools`.

## Context accounting

Claude Code transcripts expose message usage but not a fully attributable
breakdown of system prompts, tool schemas, startup context, or host-side
micro-compaction. Compact Focus therefore shows two separate facts:

- estimated tokens and percentage for each prompt-led episode;
- how much observed context cannot honestly be attributed to an episode.

It reads an explicit context-window value from hook status or environment when
available. It does not invent a generic 200k denominator for unknown models.

## Background analysis and cost

Background analysis is configurable when the plugin is installed. It starts at
50% of a known window, or at 80k observed tokens when the window is unknown. A
fresh worker is not launched until at least 40k more tokens or 12 more episodes
have arrived. The worker uses the authenticated `claude` CLI, no tools, low
effort, a $0.10 cap, and bounded transcript evidence. Its measured duration and
reported cost are stored in the cycle metadata.

Set `COMPACT_FOCUS_BACKGROUND=0` to disable it entirely. Other controls:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `COMPACT_FOCUS_MODEL` | `haiku` | Proposal model |
| `COMPACT_FOCUS_MAX_BUDGET_USD` | `0.10` | Per-worker spend cap |
| `COMPACT_FOCUS_WORKER_TIMEOUT` | `180` | Worker timeout in seconds |
| `COMPACT_FOCUS_PREP_THRESHOLD_PCT` | `50` | Known-window warm threshold |
| `COMPACT_FOCUS_PREP_USED_TOKENS` | `80000` | Unknown-window warm threshold |
| `COMPACT_FOCUS_PREP_REFRESH_TOKENS` | `40000` | Token drift before refresh |
| `COMPACT_FOCUS_PREP_REFRESH_EPISODES` | `12` | Episode drift before refresh |
| `COMPACT_FOCUS_AUTO` | `review` | Set `allow` to bypass review for auto-compaction |
| `COMPACT_FOCUS_STATE_DIR` | plugin data directory | State override |

## Privacy and state

Installed state lives under `${CLAUDE_PLUGIN_DATA}`, normally
`~/.claude/plugins/data/compact-focus-papert-tools/`, and survives plugin
updates. Files are written atomically with user-only permissions where the
platform permits it.

Compact Focus stores reconstructed source text, review actions, demoted
evidence, and explicit feedback locally. Raw base64 image/document payloads are
never copied into proposal or recovery files; only metadata, dimensions, byte
counts, and cryptographic digests are retained. Prior-turn private thinking is
excluded.

With background analysis enabled, bounded textual evidence is sent through a
child Claude CLI process using the same account and provider configuration as
the parent session. Compact Focus has no independent telemetry or network
client.

## Failure semantics

- `q`, an invalid ledger, or an unavailable inline terminal blocks that
  compaction attempt rather than silently compacting without review.
- Focused commands such as `/compact focus on ...` pass through untouched.
- If the proposal worker fails, the deterministic ledger preserves every
  reconstructed source.
- A watchdog resumes Claude if the inline editor process dies while Claude is
  suspended.
- Transcript JSONL is an internal Claude Code format and may change. The parser
  records unsupported blocks as bounded metadata instead of silently dropping
  them.

Run `compact-focus doctor` to check Python, Claude CLI discovery, terminal
ownership, writable state, and SQLite search support.

## Development and release

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m py_compile compact-focus/compact_focus/*.py
claude plugin validate . --strict
python3 -m pip install ./compact-focus
compact-focus --version
```

Manual platform probes live in `tests/manual/`; the automated suite covers
trace boundaries, media privacy, proposal grounding, review mutations,
recovery, worker cancellation, stale-proposal rebasing, and hook transactions.

See [CHANGELOG.md](./CHANGELOG.md) and the repository
[CONTRIBUTING.md](../CONTRIBUTING.md).
