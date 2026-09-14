# Paper repository benchmark corpus

[`corpus.csv`](corpus.csv) contains **40 paper/repository cases from 39 papers** for the Projects benchmark in PR #104. It deliberately mixes small local artifacts with credentials, databases, remote data, older runtimes, containers, GPUs, notebooks, and multiple services. Upload it using **Projects → Benchmark CSV**; see the [GUI and diagnostic report guide](../../docs/paper-repository-benchmark.md).

This is a purposive coverage sample, not a random sample of research repositories. No execution success is claimed by inclusion. A benchmark score requires a recorded run on a fixed selection, and an application health check does not establish scientific reproduction.

## Contents and coverage

The unit is one paper paired with one repository. Super-NaturalInstructions has separate data and Tk-Instruct training repositories, so it contributes two cases. Multiple tags per case are intentional; counts overlap.

| Artifact tag | Cases | Meaning |
| --- | ---: | --- |
| `system` | 14 | Integrated application or benchmark environment |
| `tool` | 32 | Research software, including libraries |
| `simulation` | 8 | Simulator or interactive task environment |
| `cli_script` | 16 | Scripts invoked from a terminal |
| `cli_package` | 10 | Packaged command-line entry point |
| `dataset_in_git` | 8 | Data, task definitions or study materials committed to Git |
| `dataset_in_hf` | 5 | Associated dataset hosted on Hugging Face |
| `dataset_external` | 7 | Associated data on another host, such as S3, a university or EEG archive |
| `visualization` | 19 | Visualization implementation or authored visual artifact in Git |
| `dataset_visualization` | 15 | Software for visually inspecting data, embeddings, model outputs or simulations |

Examples include ChainForge and HypoCompass (HCI applications), WebArena (multiple Docker services and databases), WorkArena (gated remote instances), Gymnasium and MuJoCo (simulation), GSM8K (data in Git), DiffusionDB (data on HF), SciFact and MMLU (external data), and Distill's Feature Visualization (an interactive article with a committed static build).

`dataset_in_git` includes annotations and task definitions; it does not imply all raw data live in Git. HF model weights alone do not earn `dataset_in_hf`. WorkArena's HF artifact holds instance-access metadata; access to the hosted environment is still necessary. CLI and library cases need a task-specific functional check and do not become startup successes merely because a documentation server responds.

## CSV schema

The first five columns are the required upload contract. Semicolons separate tags within a cell; normal CSV quoting handles commas and newlines.

| Column | Interpretation |
| --- | --- |
| Git repo URL | Canonical public GitHub repository URL |
| Paper DOI | Verified publication DOI or explicitly labeled arXiv DOI |
| Paper Keyword(s) | Curator-assigned topical keywords, not necessarily author keywords |
| What is in the git repo | Artifact tags from the table above |
| Types of dependencies in the git repo | Filterable dependency tags |
| Case ID / Paper title / DOI kind | Stable curation label, registry title and `publication` or `arxiv_preprint` |
| Repository contents / Dependency details | Workflow-specific explanation, named libraries and runtime requirements |
| Env variables (types/names) | Names and roles only; no credentials or submitted values |
| Artifact URLs | Associated demo, dataset or material; same-repository Git paths are pinned to the reviewed commit |
| Paper source URL / Evidence URLs | DOI landing page and pinned primary-source repository files |
| Reviewed commit / Evidence checked at (UTC) | Revision inspected and corpus compilation timestamp; per-DOI check times are in provenance |
| Inspection scope | Selected-file count, selection limit and scope of inference |
| Expected evaluation | Curation guidance: web preview, library/CLI, data inspection, notebook or simulation |
| Known constraints | Gating, size, platform, legacy or workflow limitations to investigate |

Dependency tags describe documented workflows, including optional workflows. For example, `docker` does not mean Docker is always required, and `api_credentials` can describe an optional provider. Read **Dependency details** before treating tags as requirements. A missing tag means the bounded inspection did not establish that dependency; it is not proof of absence. Configuration stored in Python or TOML is distinguished from actual environment variables.

## Evidence and reproduction

`annotations.json` holds curation compiled and reviewed by Codex against primary sources; these labels have not been independently human-annotated. `provenance.json` records 39 DOI registry records (18 publication and 21 arXiv preprint DOIs), 40 commit SHAs, and 312 inspected repository files with SHA-256 digests and source URLs. The collector excludes symlinks and inspects up to 14 shallow README/citation/manifests per repository, with a 120 KB file limit. All included repository collections finished without collection errors. This is not a recursive audit of every source file or transitive dependency.

DOI identity and titles were checked against Crossref or DataCite. Paper/repository associations were reviewed using author-maintained READMEs, citation files and publication pages. For example, the [Voyager author page](https://idl.uw.edu/papers/voyager) identifies the original 2015/2016 system; it is distinct from Voyager 2. The [Distill publication](https://distill.pub/2017/feature-visualization/) identifies DOI `10.23915/distill.00007`. Current repository revisions can differ substantially from the paper-era implementation; this corpus targets the inspected revision and makes no claim that it is the historical artifact.

`artifact_checks.json` records 34 unauthenticated HTTP HEAD checks, all returning HTTP 200 during collection. Same-repository Git artifact paths were also checked against the reviewed Git trees. Header availability does not prove permission to download gated data, download completeness or executable correctness. No large datasets, model weights or dependency stacks were downloaded for corpus curation.

Validate the frozen corpus offline, from the repository root (Python 3.9+):

```sh
python3 benchmarks/paper-repositories/build_corpus.py --check
```

After editing reviewed annotations, regenerate the CSV without network access:

```sh
python3 benchmarks/paper-repositories/build_corpus.py
```

To recollect evidence at the existing reviewed commits, use an empty external cache and an authenticated GitHub CLI. The collector only reads GitHub metadata and selected files; it never installs or executes repository code:

```sh
python3 benchmarks/paper-repositories/collect_evidence.py \
  --csv benchmarks/paper-repositories/corpus.csv \
  --cache /tmp/paper-repository-evidence
python3 benchmarks/paper-repositories/build_corpus.py \
  --refresh-evidence --cache /tmp/paper-repository-evidence
```

DOI requests run serially with retry/backoff. A partial DOI cache allows interrupted collection to resume; use a new cache directory to recheck all registry records. Recollection does not update the curated dependency labels. Re-review annotations when changing revisions. The GUI records the actual checkout SHA separately: the **Reviewed commit** column is evidence, not an instruction to reset a checkout.

## Interpreting runs

Keep the selected IDs, dataset hash, host, actual revisions, configuration status and resource budget with each result. Publish the full selected-case denominator and the counts of pending, blocked, failed and unsupported cases alongside startup successes. Report easy/local and credential/container/GPU strata separately when useful, with the selection disclosed. Retrying a failed case should preserve its first failure and distinguish eventual startup from first-attempt startup.

The GUI's diagnostic score measures observed startup health. Assessing whether a dataset was correctly identified, a CLI task produced the expected output, or a paper was reproduced requires additional per-case acceptance criteria. Do not count a correct “unsupported” diagnosis as an executable success or reinterpret skipped cases as passes. A 100% score on two chosen apps says nothing about the remaining 38 cases.

The documentation choices follow the motivation, composition, collection and intended-use questions in [Datasheets for Datasets](https://arxiv.org/abs/1803.09010). Preserving evaluation configuration and disclosing choices follows the reproducibility concerns described by the lm-eval authors in [Lessons from the Trenches on Reproducible Evaluation of Language Models](https://arxiv.org/abs/2405.14782). Those papers inform the method; they do not validate this corpus's representativeness.

Candidate exclusions: OpenHands' current repository had evolved into a substantially different product during inspection; Stanford Alpaca lacked a verified paper DOI for the intended artifact; sktime's proposed citation linkage was not sufficiently established in the bounded review. These exclusions reflect the artifact/citation rule, not observed execution outcomes. Source projects and datasets retain their own licenses and access terms; this directory contains metadata and annotations only.
