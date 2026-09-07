Dataset upload
==============

Production `/` exposes Upload dataset and a small drop target in Dataset for a bound local project. The tab remains available for projects with no dataset so the user can supply one. Paper remains conditional. All rendering uses the shared store/actions/services and existing resource state; no browser-side dataset parser or separate data store is introduced.

`POST /api/project-dataset/upload` accepts one raw octet-stream body with Content-Length and an encoded original filename. Existing Host/Origin protection applies. The upload is limited to `min(HC_RESOURCE_MAX_BYTES, 50 MiB)` and streamed into the safe project resource directory, which is excluded from git. Paths, macros and arbitrary code are never executed. Failed inspection preserves the previous active dataset.

CSV/TSV, Parquet and XLSX retain their original bytes, filename, format, digest and provenance. Inspection uses the existing resource preparation boundary. XLSX uses read-only, data-only parsing, no external links or formula evaluation, bounded ZIP expansion and a macro refusal. The first useful worksheet is inspected; this is not a workbook editor. Text tables are counted only up to 10,000 rows, and Parquet uses metadata for counts. Preview output is at most ten rows and twenty columns, with bounded cell text and an 8 KB sample budget. Large Parquet row groups and oversized workbooks are refused rather than inflated without a bound.

A successful upload updates the single persisted `activeDatasetId` after inspection. Previous resources remain in the existing bounded history, and the new record names what it replaced. The existing context builder omits historical datasets once an explicit active dataset exists and tells agents that it supersedes old fallback references. The paper's parsed text and bounded grounding claims remain available. Goal-page revision/watching includes resource state, so open pages can observe readiness changes.

The upload history retains the existing twelve-resource project bound. Unsupported formats, encrypted/macro workbooks, non-tabular sheets and files above policy limits require another supported file. Empty or duplicate table headers are rejected. Full-dataset analysis and multi-sheet browsing are outside this change.
