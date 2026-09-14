# Paper repository benchmark implementation plan

> For agentic workers: use superpowers:subagent-driven-development for the implementation and review. The user has authorized adding this functionality to PR #104. Do not merge or publish a release.

**Goal:** Import a documented corpus of paper-linked research artifacts, choose exact cases by dependency/type, run them through David's existing Projects flow, and export enough structured evidence to debug every case.

**Architecture:** Keep the existing project checkout, assessment, environment, run-order, execution, repair, and preview services authoritative. Add a benchmark layer to the Projects GUI; instrument the existing service boundary rather than implement another runner. Persist bounded, redacted benchmark evidence locally and export a versioned JSON report.

**Tech stack:** Python >=3.9 standard library, existing Python local HTTP operations, framework-free ES modules and DOM helpers, unittest and existing Node UI tests.

**Spec:** The acceptance requirements and interfaces below are the specification for this bounded extension.

## Global constraints

- Work only in `/Users/hudsonmitchell-pullman/claude-plugins-paper-benchmark`, based on PR #104 at `5348ab800816a2dfa070ab57a5e6e2191762b018`.
- No changes to execution policy, environment approval, model budgets, or arbitrary external project files. No new cloud integrations or dependencies.
- Upload/import never clones or runs code. Only the explicit run action starts selected cases through the existing controller.
- Scores retain the original selected-case denominator. Export selected IDs, filters, full cohort size, stage outcomes, failures, blocked/unsupported/skipped/pending cases, and healthy startup separately. Healthy startup does not establish scientific correctness; inspection success does not establish executable success.
- Never record submitted environment values or secrets. Keep variable names, kinds, presence/missing status and evidence paths. Apply existing redaction plus export sanitization to logs, commands, model request traces, and nested data. Label truncation.
- CSV columns in order: `Git repo URL`, `Paper DOI`, `Paper Keyword(s)`, `What is in the git repo`, `Types of dependencies in the git repo`. Preserve additional columns as metadata. Multi-value fields use semicolons. Accept conventional casing/spacing aliases.
- Each CSV row is a paper/repository case. Multiple rows for one paper's distinct repositories are valid. Preserve stable IDs and row provenance; reject invalid GitHub URLs and malformed CSV with row-specific errors. Handle BOM, CRLF, quoted commas, escaped quotes and multiline quoted fields. A row can describe externally hosted datasets via an optional `Artifact URLs` column.
- All CSV and log work is local; no external messages. Push reviewed commits onto the existing PR branch only after verifying its current head and ancestry. No force push.

## Task 1: Benchmark GUI, selection, and diagnostic export

**Owner:** Implementer agent. You are not alone in the codebase; do not revert other changes. The root agent independently owns `benchmarks/paper-repositories/` and its dataset documentation/scripts. Do not spawn other agents. Do not commit other agents' files.

**Files:** Create focused benchmark module(s) in `hc/src/human_compact/trajectory/` and `hc/src/human_compact/trajectory/web/goal/`; integrate into `web/goal/project-workspace.js`, `web/goal/services.js`, `ui.py` and `web/goal/styles.css` as needed. Own related new `tests/test_project_benchmark.py` and Node UI tests. Document usage in `docs/paper-repository-benchmark.md`.

**Consumes:** Existing `createProjectWorkspace` sessions and `createSession` services, existing project controller status/stage/trace data. The root provides `benchmarks/paper-repositories/corpus.csv` with the five required headers above and extra evidence fields. Accept any user CSV with only the five required columns. No dependency on a hard-coded source filesystem path.

**Produces:** A Benchmark CSV tab in Projects; upload and bundled CSV access (download/sample is acceptable), searchable table with dependency and artifact-type filters, individual checkboxes, select visible / clear selection, explicit selected-case count and run selected button. Selecting one or two cases must run precisely those cases. Existing manually added projects must not leak into the batch. Show active filter and number of hidden selected cases or equivalent clear selection behavior.

**Logging contract:** Versioned JSON report with run ID, creation/export times, host/runtime info, dataset identity/hash where available, cohort/selection metadata and per-case paper/repo/dependency metadata. Capture timestamped checkout/discovery/assessment/environment/run-order/install/build/start/health/repair/error stages as exposed by the existing flow; include command, working directory, exit code, duration, bounded stdout/stderr, retries and existing model request traces. Surface exceptions and interrupted/pending cases. Persist locally so a GUI refresh does not silently lose the diagnostic report; retain original evidence on retry. Reuse the existing run persistence when possible, but pre-run errors also need evidence. JSON download must work for mixed successful/failed/pending batches and must not depend on every case finishing. Export server-sanitized values and never env submissions. Explicitly describe missing/truncated evidence rather than imply complete process traces.

**Acceptance tests (write first, see fail, then implement):**

- Parse five-column CSV containing BOM/CRLF, quoted comma, multiline quoted cell, escaped quotes; retain extra metadata and provide useful malformed row errors. Non-GitHub, userinfo, unsafe schemes, empty owner/repo must not run.
- Dependency/type filters find normalized tags. Selecting two filtered rows launches only those two, once each; zero selection does nothing; hiding rows cannot silently change the cohort.
- Export a mixed batch with healthy startup, checkout failure, missing configuration, unsupported artifact, and pending case. Counts sum to selected rows, denominator stays fixed, and unexecuted cases never pass.
- Include real stage evidence from existing local disposable run fixtures; retain original failure when retry succeeds and pre-run exceptions when no run ID exists.
- A secret entered for configuration must not appear in persistence or exported JSON, including nested traces. Variable names/statuses stay useful.
- Refresh/reopen restores benchmark metadata/log identity; duplicate clicks do not duplicate launches. Existing project workspace Node tests still pass.
- Existing local loopback/shared-workspace security boundaries continue to protect new operations. No requests execute uploaded CSV cell content.

- [ ] Read relevant existing controllers and tests; add failing behavior tests.
- [ ] Implement parser, bounded local persistence/export, and GUI integration using existing run controller.
- [ ] Run focused Python and Node tests; fix failures.
- [ ] Document actual controls, score definitions, log limits, and how to debug with Codex.
- [ ] Self-review and commit owned files. Write report with commit SHA, files, tests, limitations and unresolved concerns.

## Task 2: Verified research artifact corpus

**Owner:** Root agent, independently of Task 1. Files only in `benchmarks/paper-repositories/` plus scratch research outside the repository.

- [ ] Compile at least 30 verified paper/repository rows covering systems, tools, simulations, CLI scripts/packages, data in GitHub/Hugging Face/external hosts, and GitHub visualization code.
- [ ] Verify paper DOI and paper-to-repository relation from primary sources. Use arXiv DOI when that is the verified paper DOI and label it as a preprint DOI.
- [ ] Inspect README and dependency/config manifests at recorded commit SHAs; record env variable kinds/names, databases, APIs, libraries, Docker, hardware/runtime requirements and optionality. Absence of evidence is unknown, not proof of no dependency.
- [ ] Include source URLs, artifact URLs, checked date, reviewed revision, inspection boundary and expected limitations as extra columns. Record a reproducible collection/validation process and machine-readable provenance without vendoring article text or secrets.
- [ ] Explain purposive sampling and the limits of claiming a general success rate. Include a CSV schema/coverage validation command and generate a local copy accessible to the user.

## Task 3: Review, end-to-end verification, and PR update

**Owner:** Root with a separately dispatched reviewer after Task 1 completes.

- [ ] Review code and corpus against the contracts; fix substantive findings with implementer.
- [ ] Run focused regression suites and a real browser workflow: upload corpus, filter, select a small subset, launch disposable/local cases through the actual HTTP/controller path, export and inspect JSON.
- [ ] Run a bounded real public-repository smoke test when local dependencies permit; report blocked prerequisites explicitly rather than score them as passes.
- [ ] Check final diff, commit corpus, verify PR head did not diverge, and push additive commits to PR #104's feature branch. Leave the PR unmerged; cross-repository three-OS gates are required before merge.
