#!/usr/bin/env python3
"""Build/check the CSV from reviewed annotations and frozen primary-source evidence.

Normal build/check is offline. --refresh-evidence reads public DOI registries and
the local output of collect_evidence.py. It never installs repository code.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
HEADERS = ["Git repo URL", "Paper DOI", "Paper Keyword(s)", "What is in the git repo",
           "Types of dependencies in the git repo", "Case ID", "Paper title", "DOI kind",
           "Repository contents", "Dependency details", "Env variables (types/names)",
           "Artifact URLs", "Paper source URL", "Evidence URLs", "Reviewed commit",
           "Evidence checked at (UTC)", "Inspection scope", "Expected evaluation", "Known constraints"]
TYPES = {"system", "tool", "dataset_in_git", "dataset_in_hf", "dataset_external",
         "simulation", "cli_script", "cli_package", "visualization", "dataset_visualization"}


def read_json(url):
    request = Request(url, headers={"User-Agent": "paper-repository-benchmark/1.0 (metadata verification)", "Accept": "application/json"})
    for attempt in range(5):
        try:
            with urlopen(request, timeout=40) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code not in {429, 500, 502, 503, 504}:
                raise
            if attempt == 4:
                raise
            # Crossref's public pool permits one concurrent request. Honor that
            # limit and back off without turning transient throttling into data.
            time.sleep(min(16, 2 ** (attempt + 1)))


def paper_metadata(doi):
    if doi.lower().startswith("10.48550/arxiv."):
        url = "https://api.datacite.org/dois/" + quote(doi, safe="")
        data = read_json(url)["data"]["attributes"]
        return {"doi": data["doi"], "title": data["titles"][0]["title"],
                "kind": "arxiv_preprint", "registry_url": url, "paper_url": data["url"]}
    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    data = read_json(url)["message"]
    return {"doi": data["DOI"], "title": data["title"][0], "kind": "publication",
            "registry_url": url, "paper_url": "https://doi.org/" + data["DOI"]}


def refresh(annotations, caches):
    found = {}
    for cache in caches:
        for row in json.loads((cache / "index.json").read_text(encoding="utf-8")):
            found[row["repository"].lower()] = row
    metadata_cache = caches[0] / "paper-metadata.json"
    papers = json.loads(metadata_cache.read_text(encoding="utf-8")) if metadata_cache.exists() else {}
    dois = sorted({row["doi"] for row in annotations})
    for doi in dois:
        if doi not in papers:
            metadata = paper_metadata(doi)
            metadata["checked_at"] = datetime.now(timezone.utc).isoformat()
            papers[doi] = metadata
            metadata_cache.write_text(json.dumps(papers, indent=2) + "\n", encoding="utf-8")
            time.sleep(0.25)
        metadata = papers[doi]
        if metadata["doi"].lower() != doi.lower():
            raise ValueError("DOI mismatch: " + doi)
        print(doi, metadata["title"], flush=True)
    papers = {doi: papers[doi] for doi in dois}
    repos = {}
    for row in annotations:
        evidence = found[row["repo"].lower()]
        if evidence.get("error") or not evidence.get("files"):
            raise ValueError("Missing repository evidence: " + row["repo"])
        repos[row["repo"]] = evidence
    return {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
            "method": "Purposive coverage sampling; DOI identity from registry; repository linkage and dependency annotations reviewed against pinned primary-source files. No code execution inferred from metadata.",
            "papers": papers, "repositories": repos}


def render(annotations, evidence):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=HEADERS, lineterminator="\n")
    writer.writeheader()
    for a in annotations:
        repo = evidence["repositories"][a["repo"]]
        paper = evidence["papers"][a["doi"]]
        artifact_urls = re.sub(
            r"(https://github\.com/" + re.escape(a["repo"]) + r"/(?:tree|blob)/)[^/]+/",
            lambda match: match[1] + repo["revision"] + "/", a["urls"], flags=re.I)
        boundary = (f"Codex-reviewed README/citation and selected manifests at recorded SHA; {len(repo['files'])} files read, "
                    f"{repo['candidate_files']} eligible, limit {repo['file_limit']}; tree truncated={repo['tree_truncated']}. "
                    "Tags indicate documented workflows, including optional ones; transitive/runtime coverage is not exhaustive.")
        if repo.get("errors"):
            boundary += " Collection warnings: " + "; ".join(x["path"] + ": " + x["error"] for x in repo["errors"])
        writer.writerow(dict(zip(HEADERS, [
            "https://github.com/" + a["repo"], a["doi"], a["keywords"], a["types"], a["dependencies"],
            a["id"], paper["title"], paper["kind"], a["contents"], a["details"], a["env"], artifact_urls,
            paper["paper_url"], "; ".join(f["url"] for f in repo["files"]), repo["revision"],
            evidence["checked_at"], boundary, a["scope"], a["constraints"]
        ])))
    return stream.getvalue()


def validate(annotations, evidence):
    ids, pairs, coverage = set(), set(), Counter()
    for a in annotations:
        if a["id"] in ids:
            raise ValueError("Duplicate case ID: " + a["id"])
        ids.add(a["id"])
        pair = (a["repo"].lower(), a["doi"].lower())
        if pair in pairs:
            raise ValueError("Duplicate paper/repository pair: " + str(pair))
        pairs.add(pair)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", a["repo"]):
            raise ValueError("Invalid repository: " + a["repo"])
        if not re.fullmatch(r"10\.\d{4,9}/\S+", a["doi"]):
            raise ValueError("Invalid DOI: " + a["doi"])
        tags = {tag.strip() for tag in a["types"].split(";")}
        if tags - TYPES:
            raise ValueError("Unknown artifact types: " + str(tags - TYPES))
        coverage.update(tags)
        repo = evidence["repositories"][a["repo"]]
        if not re.fullmatch(r"[a-f0-9]{40}", repo["revision"]):
            raise ValueError("Invalid reviewed commit: " + a["id"])
        if evidence["papers"][a["doi"]]["doi"].lower() != a["doi"].lower():
            raise ValueError("DOI metadata mismatch: " + a["id"])
        for f in repo["files"]:
            if not re.fullmatch(r"[a-f0-9]{64}", f["sha256"]):
                raise ValueError("Invalid evidence digest: " + f["path"])
        if not a["keywords"] or not a["dependencies"] or not a["contents"]:
            raise ValueError("Missing annotation: " + a["id"])
    if len(annotations) < 30 or TYPES - coverage.keys():
        raise ValueError("Missing minimum sample or artifact coverage")
    return {"cases": len(ids), "papers": len({a["doi"].lower() for a in annotations}),
            "artifact_coverage": dict(sorted(coverage.items()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-evidence", action="store_true")
    parser.add_argument("--cache", type=Path, nargs="+", default=[])
    parser.add_argument("--check", action="store_true", help="Verify frozen CSV bytes and coverage without network access or writes")
    args = parser.parse_args()
    if args.check and args.refresh_evidence:
        parser.error("--check is offline; do not combine with --refresh-evidence")
    annotations = json.loads((ROOT / "annotations.json").read_text(encoding="utf-8"))
    if args.refresh_evidence:
        if not args.cache:
            parser.error("--refresh-evidence requires --cache")
        evidence = refresh(annotations, args.cache)
    else:
        evidence = json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))
    report = validate(annotations, evidence)
    output = render(annotations, evidence)
    if args.check:
        if (ROOT / "corpus.csv").read_bytes() != output.encode("utf-8"):
            raise SystemExit("corpus.csv is stale; run build_corpus.py")
    else:
        if args.refresh_evidence:
            (ROOT / "provenance.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (ROOT / "corpus.csv").write_text(output, encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
