# Paper repository benchmark

The **Projects → Benchmark CSV** tab imports paper/repository cases and routes each selected case through the existing checkout, discovery, assessment, environment, run-order, execution, repair, and preview services. It adds bookkeeping and diagnostics, not another runner or a scientific correctness oracle.

## Import and select

1. Open Projects and select **Benchmark CSV**. Upload `benchmarks/paper-repositories/corpus.csv` from this checkout, or use **Download CSV template** to prepare another dataset. The template demonstrates the schema; its placeholder repository is not a verified research artifact.
2. Search the table or filter by dependency and artifact type. Tags are separated by semicolons; case, spaces, and hyphens normalize for filtering. Every original column remains available in the report metadata.
3. Check individual rows, or **Select visible**. Hidden selected rows remain selected; the count explicitly reports them. **Clear selection** removes all selected rows.
4. Click **Run selected (N)**. Exactly those N cases receive new Projects sessions. Manually added projects are excluded. Duplicate clicks are ignored. Use the existing **Automatic** control to choose whether assessment continues automatically or pauses for manual steps.
5. Open a case from the table or project sidebar for configuration, approvals, errors, retries, and preview. **New cohort** explicitly enables another run of the same selection. Prior reports remain in local storage subject to the retention limit.

Imports never clone or run repositories. Only GitHub HTTPS `owner/repository` URLs are accepted; userinfo, other hosts, unsafe schemes, query strings, fragments, and empty names are rejected. CSV content is never evaluated as code. The existing Projects execution and environment-approval policies remain authoritative.

Required CSV headers (conventional casing/spacing and basic aliases are accepted):

```csv
Git repo URL,Paper DOI,Paper Keyword(s),What is in the git repo,Types of dependencies in the git repo
```

The parser supports UTF-8 BOM, CRLF, quoted commas, escaped quotes, and multiline cells. Malformed rows report physical line numbers. Limits are 1 MiB and 500 cases per dataset. IDs derive from the source row and cell content; dataset SHA-256 identifies the exact uploaded text. Multiple rows may reference the same repository and remain distinct cases. They share the existing managed checkout, so concurrent runs can contend for the same environment or service ports; that contention is an outcome, not a reason to silently deduplicate cases.

The optional corpus `Reviewed commit` identifies the revision inspected during corpus collection. It does **not** pin execution. After discovery, the report records `actualCommit`, whether it equals the reviewed commit, and tracked-file dirtiness. Untracked files are not audited. Existing checkouts are reused under the normal Projects policy.

## Outcomes and denominator

A cohort's selected IDs and original dataset size are immutable. Changing filters after launching does not change the denominator. **Refresh outcomes** and **Download diagnostic JSON** update evidence from retained run/order records; export works while other cases are pending or blocked.

The report's counts sum to its fixed selected-case denominator:

| Outcome | Meaning |
| --- | --- |
| `pending` | No controller operation has started. |
| `in_progress` | A boundary is pending or the latest observation is nonterminal. A pending boundary may have been interrupted. |
| `blocked` | Missing configuration, a controller request for input, or pending approval. |
| `unsupported` | The controller rejects the artifact, or an apparent HTTP success belongs to a declared dataset, CLI, simulation, script, or library without an explicit application/system type. |
| `failed` | Checkout, assessment, execution, or another observed controller operation failed. |
| `interrupted` | An observed stopped run. Retained controller restart failures may instead be reported as failed/blocked with their reason. |
| `healthy_startup` | The latest applicable controller observation reports a running, healthy application. |

`healthyStartup` is the count of **current observed healthy application startups**, not an estimate of scientific reproducibility or correctness. A later process failure or unavailable retained run can change that case's current outcome; earlier observations remain evidence. A dataset inspection or successful CLI exit is not a startup pass. Artifact type is supplied metadata, so incorrect type labels are a validity threat. Sampling and dependency coverage also affect any reported rate; this is a diagnostic cohort, not a representative population estimate.

## Diagnostic report and local persistence

Reports use `schemaVersion: 1`, with cohort ID, creation/export times, host/runtime information, dataset hash/name, selected IDs, original cohort size, filters, per-case DOI/repository/dependencies/extra metadata, and timestamped service observations. Responses include existing command, working-directory, exit-code, stdout/stderr, repair-attempt, and model-request traces when exposed by the controller.

Each operation persists a pending event **before** execution, then its response and boundary duration. Unexpected server exceptions retain their type; browser transport failures retain the operation name without copying exception text that might echo configuration. Retry observations append instead of replacing the original failure. A compact first-failure record survives polling eviction. Checkout and discovery share one service boundary. Per-subprocess timing, complete output, and every intermediate state are not available from all existing controllers; recorded boundary duration is not a substitute for process duration.

Persistence is local under `$HUMAN_COMPACT_HOME/project-benchmarks/` (default `~/.human-compact/project-benchmarks/`): atomic private JSON files, at most 24 cohorts and 24 datasets. Each cohort is capped at 8 MiB, each case at 40 observations, each response at approximately 48 KB, and individual strings at 8,000 characters. Nested lists/dictionaries also have limits. Early and recent observations are retained; global byte pressure replaces large response bodies with labeled excerpts and may evict intermediate events. `droppedEvents`, `evidenceTruncated`, and `evidenceLimits` describe those losses. These are bounded diagnostics, not complete process traces.

The workspace restores benchmark identity and available sessions on reopen. If browser storage is cleared, the most recent server-side cohort reconstructs sessions without automatically running new cases. Reports remain available when a session lacks a run ID or its retained run becomes unavailable. Older cohorts can be exported by ID through the same local `project_benchmark` operation; there is no archive picker in this version. Concurrent tabs or server processes should not write the same cohort; atomic files avoid partial JSON but do not provide a cross-process event merge.

Submitted environment values, including public configuration values, are excluded from benchmark request logs. The server removes value maps, applies existing credential-pattern redaction, and redacts known submitted values recursively before persistence/export. It reloads known values from authoritative local environment storage after a server restart. Variable names and statuses remain available. Benchmark operations bypass general request/response snapshots so raw environment submissions do not enter telemetry through the new path. Unrecognized secrets embedded in arbitrary repository text cannot be exhaustively identified by pattern matching.

## Debug with Codex

Download JSON while the failure is still visible, then give Codex the report and the relevant repository checkout. Ask it to compare the first failure, actual checkout revision, assessment/model evidence, environment variable **names and statuses**, retry history, and latest run stages. Do not add environment values to the report. For missing/truncated evidence, inspect the existing local controller run named by `runId` rather than assuming the report captured every step.

Focused verification:

```sh
PYTHONPATH=hc/src:tests .venv/bin/python -m unittest test_project_benchmark test_project_analysis test_project_run test_project_order test_project_checkout test_project_environment test_project_env_contracts test_project_agent_trace
```

The benchmark suite includes an actual disposable local process: a failed install with exit code 7 followed by a healthy HTTP startup, with both attempts retained in the report. It also tests CSV boundaries, fixed-denominator mixed outcomes, secret redaction across a server-memory reset, exact GUI selection, refresh recovery, duplicate-click locking, and local HTTP scope/Host/Origin protections. These checks do not replace the repository's native installed cross-repository merge gates.
