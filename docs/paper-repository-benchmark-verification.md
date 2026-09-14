# Benchmark verification — 2026-09-13 Pacific

The benchmark extension was verified on macOS arm64 with Python 3.13.5. The final backend source is `5cde7b88f6f4ba49b84e04fa3c21a35c81f7611b`; the vendored wheel records that revision and SHA-256 `87cc281ebb3d325ef23ff579b7630f0ad884e40f2511222e070024d76f9a2724`. The six changed backend/UI files inside the wheel were compared byte-for-byte with their source files.

| Check | Result |
| --- | --- |
| All `test_project_*.py` tests, with resource warnings enabled as errors | 536 passed on final source |
| Goal-page routes, ES module integrity and telemetry regressions | 24 passed |
| Installer Node suite | 185 passed; final rebuilt artifact also passed both pack tests |
| Native installed browser → CLI → browser round trip | 5 passed with the final wheel |
| Frozen corpus validation and GUI parser | 40 cases, 39 papers, 19 columns; all ten declared artifact categories covered |

The installed round trip used an isolated `berkeley-research` checkout at `83361824d1dbd6602ac80dd4c1d7ea772b89dffa`, its current main revision during verification. It exercised the real installer, vendored wheel, installed hooks and loopback UI inside disposable machine directories. The user's existing installation was not replaced. This is local macOS evidence; coordinated Windows/Ubuntu/macOS CI and any required Firefox/WebKit compatibility checks remain pre-merge gates.

The new Chromium test uploads the complete corpus, combines dependency/type filters, selects two rows while one is hidden by search, runs two real native static processes, downloads JSON, and reloads the GUI without launching duplicates. Only remote checkout is substituted with disposable fixture directories. Those two startup results are **fixture results**, not evidence that ChainForge or HypoCompass was executed.

Additional regressions reproduce a real failed install followed by successful retry, two cases joining one existing native process, container blockers caught by the frontend, partial browser-storage recovery, eviction of the original run-order error event, and short configuration values plus credential-bearing nested trace keys. Independent review found and verified fixes for those attribution, recovery and redaction failures.

A separate public smoke uploaded the final corpus and selected only GSM8K (`openai/grade-school-math`). The real checkout succeeded at `3101c7d5072418e28b9008a6636bde82a006892c`, matching the reviewed SHA. Discovery found no application component; assessment returned **“Railpack is not installed”**. The JSON retained discovery, assessment, revision, first failure and fixed selection: corpus size 40, selected denominator 1, failed 1, startup health 0/1. No model calls or dataset-training commands were run. This verifies a real error-reporting path and establishes no full-corpus success rate.

Reproduce the principal checks from `claude-plugins`:

```sh
python3 benchmarks/paper-repositories/build_corpus.py --check
PYTHONPATH=hc/src:tests .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_project_*.py'
```

For the installed check, set `CLAUDE_PLUGINS_DIR` to this checkout and run the native round-trip command specified in `AGENTS.md` from `berkeley-research`. See the [usage and report guide](paper-repository-benchmark.md) for the GUI, outcome definitions and evidence limits, and the [corpus documentation](../benchmarks/paper-repositories/README.md) for sampling and citation provenance.
