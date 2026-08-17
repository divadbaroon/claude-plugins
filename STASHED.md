# Stashed for the /goals-ui launch

This release ships one thing: `/goals-ui`, chat-scoped goals for Claude Code.
Everything else the codebase can do is still here — no implementation file was
deleted — it is only unreachable by default. Set `HC_EXPERIMENTAL=1` to
re-enable the `hc` subcommands and the HTTP operations and routes behind them;
reinstall with `HC_EXPERIMENTAL=1 npx human-vault` to wire the global Vault
capture hooks, which a plain install leaves out. The full pre-launch build,
with every surface connected, is tagged `pre-launch-full-state`, and the
feature branches it came from are `feature/chat-goals`,
`feature/agent-execution` and `feature/review-workflow`.

Without the flag, a gated `hc` subcommand exits 2 with
`hc <cmd> is experimental in this release; set HC_EXPERIMENTAL=1 to enable it`,
and a gated HTTP op or route answers `200 {"ok": false, "error": "experimental
in this release; set HC_EXPERIMENTAL=1"}` so the browser reads it as
"unavailable" rather than as a network error. This is launch scoping, not a
security boundary.

## Inventory

| Capability | Where the code lives | How it was disconnected (commit) | How to re-enable | Notes |
| --- | --- | --- | --- | --- |
| Global Vault capture | `hc/src/human_compact/global_vault.py`; `hc/src/human_compact/assets/plugin/scripts/vault-hook.sh`, `vault-backfill.sh`; `cli.py` `backup_main`/`setup_main`/`global_hook_main`; `trajectory/ui.py` op `enable_capture` | `84898f2` drops the four `vault-hook.sh` entries from `hooks/hooks.json` and gates `hc backup` + `hc setup --global-vault yes`; `f0fb986` refuses the npm `--global-vault 1`/`--goals 1` flags; `73c7dcc` gates the `enable_capture` op; `c6af80f` makes the installer say which hook set it wired | `HC_EXPERIMENTAL=1 npx human-vault --non-interactive --global-vault 1`, or install with the flag and then `HC_EXPERIMENTAL=1 hc setup --global-vault yes` | `global-hook` itself stays dispatchable — only the hooks that call it are unwired. `hc setup --global-vault no` is *not* gated, so turning capture off never needs the flag. |
| Ollama / on-device global analysis | `trajectory/providers.py` (`Ollama`), `trajectory/extract.py`, `trajectory/synthesize.py` | `84898f2` — reachable only through `hc trajectory` / `analyze` / `refresh` / `worker`, all gated | `HC_EXPERIMENTAL=1 hc trajectory --provider ollama` (the choice is remembered in `~/.claude-vault/trajectory/config.json`) | Only the *global* pipeline is stashed. Chat-scope inference still honours `HC_CHAT_PROVIDER=ollama` / `HC_CHAT_MODEL` with no flag — that path is part of the launch surface. |
| Parallel extraction | `trajectory/worker.py`, `trajectory/extract.py` | `84898f2` gates `hc worker` | `HC_EXPERIMENTAL=1 hc worker`, or `hc trajectory --workers N` under the flag | The in-product spawn at `global_vault.py:299` (`python -m human_compact.cli worker`) inherits the shell's environment, not the flag used at install time, so under an experimental install the vault hooks fire and the worker still exits 2 unless `HC_EXPERIMENTAL=1` is exported in the user's shell. `hooks.experimental.json` prefixes the four `vault-hook.sh` commands with `HC_EXPERIMENTAL=1 ` (`f75d96a`), which covers the hook itself but not a hand-run `hc`. |
| Global Claude analysis | `cli.py` `trajectory_main`, `analyze_main`, `refresh_main`, `worker_main`; `trajectory/discover.py`, `goal_synth.py`, `synthesize.py`, `graph_build.py`; `trajectory/ui.py` op `start_analysis` | `84898f2` gates the four commands; `73c7dcc` gates the op | `HC_EXPERIMENTAL=1 hc analyze` / `hc refresh` / `hc trajectory`; the op needs the flag on the process running the server | Sends conversation-derived digests to Anthropic through the user's own `claude` CLI, which is why it is opt-in twice over. |
| Analysis progress banner | `GET /api/setup` in `trajectory/ui.py`; `seed()` / `refreshSetup()` and the banner block in `trajectory/web/bridge.js` | `73c7dcc` gates the route; `860fd32` stops the chat workspace from seeding the global onboarding at all | Flag on the server process; the banner reappears in global scope | Both JS callers already guarded on `body.ok`, so the gated route degrades to "no banner" rather than an error. |
| Conversation list + full thread | `GET /api/conversation` in `trajectory/ui.py`; `loadThread()` and the Conversations nav in `bridge.js` | `73c7dcc` gates the route; `860fd32` + `09c1ebe` remove the Conversations nav item in chat scope (anchored on the Goals sibling, not on matching the word) | Flag on the server process, in global scope | A goal *titled* "Conversations" is no longer erased by the nav hiding — that was the fix in `09c1ebe`. |
| Global goal workspace | `cli.py` `ui_main`; `trajectory/ui.py`; `trajectory/web/goals_bundle.html` + `bridge.js` | `84898f2` gates `hc ui` | `HC_EXPERIMENTAL=1 hc ui` | The same server code serves the chat workspace (`hc chat-ui`, ungated) — the scope, not the binary, is what changes. |
| Goal-bound agent runs | `trajectory/agent_exec.py`; `cli.py` `work_main`; ops `start_agent_run`, `cancel_agent_run`, `launch_agent_run`, `resume_agent_run`; AGENT pane in `bridge.js` | `84898f2` gates `hc work`; `73c7dcc` gates the four ops; `860fd32` hides the AGENT tab in chat scope | `HC_EXPERIMENTAL=1 hc work <goal>`; flag on the server process for the ops | Task observation (`TaskCreate`/`TaskUpdate`/`TaskList` → `trajectory/agent-runs/<session>.json`) still runs from the hooks; only starting and resuming a bound session is disconnected. |
| Model-generated plans | `GET /api/plan` in `trajectory/ui.py`; `loadPlan()` in `bridge.js` | `73c7dcc` | Flag on the server process | `loadPlan()` also early-returns in chat scope, so it is doubly unreachable at launch. |
| Review pane | `GET /api/review` in `trajectory/ui.py`; REVIEW tab in `bridge.js` | `73c7dcc` gates the route; `860fd32` hides the tab in chat scope | Flag on the server process, in global scope | Branch `feature/review-workflow` has the fuller version. |
| Briefing payload | `GET /api/briefing`, `GET /api/briefings` in `trajectory/ui.py`; `seed()` in `bridge.js`; `agent_exec.py` briefing builder | `73c7dcc` | Flag on the server process | `hc work`'s own briefing prepend is unchanged; this is the browser's read of it. |
| Opening override | op `set_opening` in `trajectory/ui.py` | `73c7dcc` | Flag on the server process | This is the one gated op the chat workspace could otherwise reach from ordinary goal editing; no launch UI offers it, so nothing claims a write that would now be refused. |
| Important items | `cli.py` `mark_main`; `trajectory/goals.py` important-item store | `84898f2` gates `hc mark` | `HC_EXPERIMENTAL=1 hc mark …` | No browser surface existed for this either way. See "Not stashed, but noted" below — chat inference can still write `important.items` internally. |
| Natural-language correction + evidence inspection | `cli.py` `goals_main` | `84898f2` gates `hc goals` | `HC_EXPERIMENTAL=1 hc goals` | CLI-only; the browser never exposed it. |
| Lens | `cli.py` `lens_main`; `trajectory/lens.py` | `84898f2` gates `hc lens` | `HC_EXPERIMENTAL=1 hc lens` | The derived compaction lens, including its `--browser` view. |
| Evidence graph | `cli.py` `trajectory_main --browser` / `lens_main --browser`; `trajectory/serve.py`; `trajectory/web/index.html`, `web/goals.html`, `web/static/sigma.min.js`, `graphology*.min.js` | `84898f2` — both entry commands are gated, and nothing else starts `serve.py` | `HC_EXPERIMENTAL=1 hc trajectory --browser` (or `hc lens --browser`) | The legacy evidence view; a different server from the goal workspace. |
| Pipeline status | `cli.py` `status_main` | `84898f2` gates `hc status` | `HC_EXPERIMENTAL=1 hc status` | Also the only surface for analysis-failure detail. The chat-scope analyzer error is separate and stays visible in `/api/state`. |
| `/hc-ui` legacy skill | `cli.py` `LEGACY_HC_UI_SKILL_DIR` + the legacy-removal path; `_LEGACY_DIGESTS["goals-ui"]`; `trajectory/chat_state.py` `_is_goals_ui_launcher` | `ef3efad` renames the command to `/goals-ui` and removes `~/.claude/skills/hc-ui` on install *when the installer recognizes it as its own* | Not re-enablable, and not meant to be — the rename is permanent | `_is_goals_ui_launcher` still recognizes the old spelling so that `/hc-ui` lines in transcripts recorded before the rename are still kept out of the goal model. An unrecognized `~/.claude/skills/hc-ui` is left on disk for the user to remove; the uninstall note lists it. |
| The `hooks.experimental.json` swap | `hc/src/human_compact/assets/plugin/hooks/hooks.json` (chat-only, the default) and `hooks/hooks.experimental.json` (chat + Vault); `cli.py` `_asset_overrides` / `_stage_asset` / `_asset_digest` | `84898f2` creates the pair and the install-time swap; `c6af80f` adds the sync test and the install-time announcement; `f75d96a` + `8cc16d7` keep both files in step for the injection hooks | Install with `HC_EXPERIMENTAL=1` — the staged tree substitutes `hooks.experimental.json` for `hooks.json` before the ownership marker and the digest check, so the installed tree is still validated | The two files are pinned equal apart from the Vault entries by `test_the_experimental_hooks_are_the_default_set_plus_vault_entries`, so they cannot drift. The install prints which set it wired. |

## Not stashed, but noted

These are live on this branch. They are limits or leftovers, not disconnected
capabilities, and they are listed here because they can still surprise a user.

- **`merge_goals` drops sources on the global tree.** `goals.apply_ops` merges
  a goal's evidence, important items, prompt links and children into the
  destination, but not its `sources` and not its markdown document — both are
  lost with the source goal. Pre-existing. It is not chat-reachable:
  `merge_goals` is absent from the chat analyzer's allowed op set, so only the
  global synthesizer can emit it, and only under `HC_EXPERIMENTAL=1`.
- **A `SubagentStop` ingest can cost the next subagent its snapshot.**
  `SubagentStop` runs a full ingest and takes the session lock; a
  `SubagentStart --inject-only` that arrives while it is held waits 0.5 s and
  then skips rather than stalling the turn. The skipped injection is not lost
  data — the snapshot never advanced, so the next render restates the same
  change — but that one subagent starts without its goal context.
  Self-healing, and deliberately preferred over a 5-second hook stall.
- **Every tool batch spawns two hook processes.** `PostToolBatch` carries an
  async ingest entry and a synchronous `--inject-only` entry. Both fire in
  every session with the plugin installed, including chats that never ran
  `/goals-ui` — the opt-in check happens *inside* the process. The check is one
  small JSON read; the process spawn is not free. If tool-batch latency
  regresses, the cheapest fix is a marker file the shell script can stat before
  exec'ing Python.
- **Global scope lost its source-attach UI and `WHERE THIS SITS`.** Making the
  Context pane a single markdown document set `showCtx: false` in *both*
  scopes, which took the only controls for attaching a repo, a local folder or
  a document to a goal (`codeAddGh`, `codeAddLocal`, `docAdd`) and the tree
  trail off the screen everywhere. Nothing is deleted and `set_sources` still
  works server-side; global is experimental-only on this branch, which is why
  this was accepted. Gating `showCtx` on `!chat` brings it back.
- **The `full file:` pointer can flip between two paths.** The injected header
  names the mirrored document at `<claude project dir>/goals-ui/<session
  id>.md`, but falls back to the internal `goal_context.md` path when the
  mirror cannot be written. A transient failure therefore announces the same
  document at two different paths across one conversation.
- **Chat inference can still write `important.items`**, and `IMPORTANT:` lines
  derived from them can still appear in the injected context, even though
  `hc mark` — the only way to create them by hand — is gated. Nothing
  user-facing offers the feature; the lines are model-facing.
- **Abandoned status does not collapse into "done" in the browser.** That is
  P1 feature work, deliberately not attempted on this branch.
- **`/api/state` stays open** and still reports `agent_runs`, `agent_claim` and
  `analyzer`. In chat scope those fields are empty or per-chat truth. On a
  global-scope server the agent-run claim is still *readable* even though every
  write to it is gated.
