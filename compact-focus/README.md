# Compact Focus

Compact Focus turns the native `/compact` in Claude Code and Codex into a
human-reviewed transaction. One command opens a focused editor in a companion
terminal before compaction: inspect source-grounded clusters, label a cluster
or one source unit, stage clarifications, and explicitly submit. Compact Focus
then shows the exact carry-forward draft. Confirm it immediately, edit it
directly, or chat with a bounded model through repeated revisions. Only the
second confirmation returns control to the pending `/compact`; there is no
generated command to copy or second `/compact` to run.

Version 0.22.1 is tested against Claude Code 2.1.227 and Codex CLI 0.147.0 on
macOS. On Linux it uses tmux or a detected terminal emulator. Native Windows is
not yet supported.

## Transaction model

```text
normal turns ── optional bounded background proposal ──┐
                                                       v
one bare /compact ── companion terminal ── cluster/source review
                                                       |
                                         explicit Submit freezes a draft
                                                       |
                          b revises clusters <── exact draft ──> c chat / e edit
                                                       |
                         q blocks <── human decision ──> Enter confirms + returns
                                                       |
                ┌──────────────────────────────────────┴─────────────────────┐
                v                                                            v
       Claude compactor receives                                  Codex compacts natively
       the approved contract
                |                                                            |
                └──────────────────────┬─────────────────────────────────────┘
                                       v
                    host-supported context restores the contract
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
| Cluster/source editor and exact draft review | Companion terminal | Companion terminal |
| Conversational draft refinement before compaction | Bounded Claude child, on demand | Bounded Codex child, on demand |
| Cancel before compaction | Yes | Yes |
| Feed the approved contract into the native summarizer | Yes, best-effort and audited | No |
| Restore the contract into the immediate continuation | `SessionStart` + bounded precommit reinforcement | Full contract beside first prompt + bounded precommit reinforcement |
| Inspect and lexically audit the generated summary | Yes | No; remote summary text is encrypted |
| Recover demoted evidence and learn from explicit feedback | Yes | Yes |

Neither host exposes a supported plan-style conversational turn inside its main
chat while `PreCompact` is blocked. Claude creates its native summary only
after `PreCompact` returns, and `PostCompact` cannot replace that summary.
Compact Focus therefore performs the iterative review in the companion window
and passes the confirmed draft into Claude as the binding summary core. Claude's
native summarizer receives that contract but can still violate it.
Compact Focus audits the plaintext result and independently restores the full
contract after compaction, then reinforces only the contradiction-free
precommit beside the first three continuation prompts. Codex restores the full
contract as developer context beside the first post-compaction prompt, then the
minimal precommit beside the next two, without pretending to control or inspect
a summarizer interface that Codex does not expose.

## Install for Claude Code

Requirements: Claude Code with plugin hooks, Python 3.9+, and either macOS
Terminal.app, tmux, or a supported Linux terminal emulator.

```bash
claude plugin marketplace add divadbaroon/claude-plugins
claude plugin install compact-focus@papert-tools
```

For deterministic foreground review with no background model call:

```bash
claude plugin install compact-focus@papert-tools --config background_analysis=false
```

If Claude Code shows a plugin-source warning, review and accept it. Then start
a new Claude Code session and use the ordinary `/compact`. The
`/hooks` menu can verify that Compact Focus loaded, but it is read-only and is
not a separate trust step.

Update with:

```bash
claude plugin marketplace update papert-tools
claude plugin update compact-focus@papert-tools
```

## Install for Codex

Requirements: Codex CLI 0.147.0+ with lifecycle hooks enabled, Python 3.9+,
and either macOS Terminal.app, tmux, or a supported Linux terminal emulator.

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

Codex background model analysis is off by default. The deterministic companion
editor is the complete foreground workflow. It runs outside Codex's rendering
surface because Codex redraws its hook status until `PreCompact` returns.

## Friend beta

Installation is user-scoped by default, so one install applies to new local
sessions across projects. For a meaningful test, use a conversation containing
several decisions, a reversed assumption, and an unresolved question. Run the
ordinary `/compact`, edit any misconstrual in the companion ledger, and approve
from the Submit row. On the draft screen, press Enter to confirm, `c` to chat
through a revision, `e` to edit it directly, or `b` to return to the clusters.
The original chat waits at its hook status and continues automatically only
after the draft confirmation.

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

`/compact` opens the review in a new Terminal.app window on macOS or a new tmux
pane when the session is already inside tmux. Linux falls back to
`x-terminal-emulator`, GNOME Terminal, Konsole, or xterm. This separation is a
safety boundary: both chat hosts continue repainting their own terminal while a
compaction hook is running, so a curses editor cannot share that surface
without either corrupting the display or suspending the host process.

The document follows the cluster-first hierarchy in the interaction prototype:
each cluster shows one of `PRESERVE`, `COMPACT`, or `DELETE`; an independent
`TODO`, `IN PROGRESS`, `DONE`, or `BLOCKED` state; confidence; rationale; source
inventory; and staged user clarifications. Expanding it exposes every source
unit with its own label and work state. A source choice overrides its cluster
default. `DELETE` removes evidence from carried context but never erases local
recovery.

No navigation key approves compaction. Enter expands a selected cluster. Only
Enter on the final Submit row opens the exact draft, and only Enter on that
second screen confirms it. Draft chat applies validated structured changes back
to cluster/source state as well as revising the summary; direct editing remains
available when a model is unavailable or unwanted.

| Key | Action |
| --- | --- |
| `↑` / `↓`, `j` / `k` | Navigate |
| `Space`, `←` / `→`, `Enter` | Collapse or expand a cluster (`Enter` does not approve it) |
| `p`, `c`, `Delete` / `Ctrl-D` / `d` | Preserve, compact, or remove a cluster/source from active context |
| `x` | Cycle TODO, IN PROGRESS, DONE, BLOCKED |
| `e` | Stage a clarification without rewriting original evidence |
| `T`, `E`, `N` | Edit cluster title, multiline summary, or next action |
| `m`, `S`, `M`, `n` | Move evidence, split, merge, or create an item |
| `f` | Accept or resolve a contested interpretation |
| `r`, then `Space`, `[` / `]` | Open class rules, toggle one, or change the first-context percentage |
| `!` | Edit the global non-negotiable interpretation |
| `$` | Show estimated context cost for every prompt or continuation |
| `v` | Inspect rival problem representations |
| `/`, `u`, `?` | Search, undo, or show help |
| `Enter` on Submit | Open the exact carry-forward draft |
| Draft: `c`, `e`, `b`, `Enter` | Chat/refine, edit directly, return to clusters, or confirm |
| `q` | Cancel compaction |

Class rules are floors, not decisions; explicit cluster/source edits win. “First 30%” is
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

Draft chat is separate from background proposal analysis and runs only when the
user presses `c` on the draft screen. It sends the current bounded contract,
source excerpts, draft, and explicit feedback to an authenticated child CLI
with no tools or session persistence. Its schema can change only known
cluster/source IDs and validated fields. A static elapsed-time indicator remains
visible while it runs; failure leaves the draft intact and direct editing
available.

| Variable | Default | Meaning |
| --- | ---: | --- |
| `COMPACT_FOCUS_BACKGROUND` | host default | Override background analysis on either host |
| `COMPACT_FOCUS_CODEX_BACKGROUND` | `0` | Opt in to Codex background analysis |
| `COMPACT_FOCUS_MODEL` | `haiku` on Claude | General proposal model override |
| `COMPACT_FOCUS_CODEX_MODEL` | `gpt-5.6-luna` | Codex proposal model |
| `COMPACT_FOCUS_MAX_BUDGET_USD` | `0.10` | Claude worker spend cap |
| `COMPACT_FOCUS_DRAFT_MODEL` | `haiku` | Claude model used only for explicit draft chat |
| `COMPACT_FOCUS_DRAFT_MAX_BUDGET_USD` | `0.08` | Spend cap for one Claude draft revision |
| `COMPACT_FOCUS_DRAFT_MAX_CHARS` | `24000` | Maximum exact draft accepted at confirmation |
| `COMPACT_FOCUS_WORKER_TIMEOUT` | `180` | Worker timeout in seconds |
| `COMPACT_FOCUS_PREP_THRESHOLD_PCT` | `50` | Known-window warm threshold |
| `COMPACT_FOCUS_PREP_USED_TOKENS` | `80000` | Unknown-window warm threshold |
| `COMPACT_FOCUS_PREP_REFRESH_TOKENS` | `40000` | Token drift before refresh |
| `COMPACT_FOCUS_PREP_REFRESH_EPISODES` | `12` | Episode drift before refresh |
| `COMPACT_FOCUS_AUTO` | `review` | Set `allow` to bypass auto-compaction review |
| `COMPACT_FOCUS_ASYNC_AUDIT` | `1` | Reconcile Claude's provisional audit against the carried transcript summary |
| `COMPACT_FOCUS_PROMPT_REINFORCEMENTS` | `3` | Number of early continuation prompts that receive the minimal precommit cue |
| `COMPACT_FOCUS_STATE_DIR` | plugin data directory | Explicit state override |
| `COMPACT_FOCUS_REVIEW_TIMEOUT_SECONDS` | `3300` | Maximum wait for a companion review decision |
| `COMPACT_FOCUS_TERMINAL_LAUNCHER` | auto-detected | Custom launcher command; use `{script}` where the review command should appear |
| `COMPACT_FOCUS_MAC_TERMINAL` | `Terminal` | macOS application used for the companion window |

## Privacy and failure semantics

State lives under the host-provided private plugin data directory and survives
plugin updates. Directories are user-only where the platform permits it. The
plugin stores reconstructed source text, review actions, demoted evidence, and
explicit feedback locally. Raw base64 media is replaced by dimensions, byte
counts, and digests. Prior-turn reasoning/private thinking is excluded.

With background analysis enabled—or when the user explicitly starts draft
chat—bounded textual evidence is sent through a child CLI authenticated as the
current user. Compact Focus has no telemetry, analytics endpoint, or
independent network client.

- `q`, invalid coverage, or an unavailable companion terminal blocks that
  compaction attempt.
- If a proposal worker fails, the deterministic ledger preserves every source.
- If draft chat fails, the unmodified draft remains reviewable and directly
  editable; it never approves compaction.
- The editor never sends job-control signals to the host; a failed editor
  blocks only that compaction attempt instead of suspending the chat process.
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

The automated suite covers both rollout formats, compaction boundaries, media
privacy, proposal grounding, companion-terminal handoff, editor mutations,
exact coverage, recovery, worker cancellation, contract restoration, stale
proposal rebasing, and hook control responses.

Compact Focus is MIT licensed. See [CHANGELOG.md](./CHANGELOG.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md).
