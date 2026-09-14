#!/usr/bin/env python3
"""Read public repository evidence at pinned commits; never install or run it.

Requires authenticated GitHub CLI for API rate limits. Uses its existing auth,
never reads or prints the token. The cache is intentionally outside the repo.
This collects evidence for human review; it does not infer dependency labels.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


def gh(endpoint):
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:400])
    return json.loads(result.stdout)


def collect(repo, cache, revision=None):
    metadata = gh("repos/" + repo)
    repo = metadata["full_name"]
    sha = revision or gh("repos/" + repo + "/commits/" + quote(metadata["default_branch"], safe=""))["sha"]
    directory = cache / repo.replace("/", "__") / sha
    directory.mkdir(parents=True, exist_ok=True)
    tree_file = directory / "tree.json"
    tree = json.loads(tree_file.read_text()) if tree_file.exists() else gh("repos/" + repo + "/git/trees/" + sha + "?recursive=1")
    tree_file.write_text(json.dumps(tree))
    paths = [x["path"] for x in tree["tree"] if x["type"] == "blob" and x.get("mode") != "120000" and x.get("size", 0) <= 120_000]
    pattern = re.compile(r"(^|/)(readme[^/]*|citation\.(cff|bib)|pyproject.toml|setup.py|setup.cfg|requirements[^/]*\.txt|package.json|dockerfile[^/]*|docker-compose[^/]*|compose\.ya?ml|environment\.ya?ml|\.env\.(example|sample|template)|.*\.env\.example)$", re.I)
    eligible = [p for p in paths if pattern.search(p) and not re.search(r"(^|/)(node_modules|vendor|third_party|\.git)/", p)]
    # Shallow manifests first. A bounded read is disclosed in evidence.json.
    eligible.sort(key=lambda p: (p.count("/"), 0 if "readme" in p.lower() else 1, p.lower()))
    chosen = eligible[:14]
    files, errors = [], []
    for path in chosen:
        dest = directory / "files" / path
        try:
            if not dest.exists():
                response = gh("repos/" + repo + "/contents/" + quote(path) + "?ref=" + sha)
                data = base64.b64decode(response["content"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            data = dest.read_bytes()
            files.append({"path": path, "url": f"https://github.com/{repo}/blob/{sha}/{path}", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        except (RuntimeError, KeyError, ValueError, OSError) as exc:
            errors.append({"path": path, "error": str(exc)})
    result = {"repository": repo, "revision": sha, "default_branch": metadata["default_branch"], "archived": metadata["archived"], "tree_truncated": tree.get("truncated", False), "candidate_files": len(eligible), "file_limit": 14, "files": files, "errors": errors}
    (directory / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", nargs="+", help="owner/repo names to inspect")
    parser.add_argument("--csv", type=Path, help="Recollect pinned revisions from corpus.csv")
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    jobs = [(r, None) for r in args.repos or []]
    if args.csv:
        import csv
        with args.csv.open(newline="", encoding="utf-8-sig") as stream:
            jobs.extend((row["Git repo URL"].removeprefix("https://github.com/"), row["Reviewed commit"]) for row in csv.DictReader(stream))
    if not jobs:
        parser.error("provide --repos or --csv")
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(collect, repo, args.cache, revision): repo for repo, revision in jobs}
        for future in as_completed(pending):
            repo = pending[future]
            try:
                result = future.result()
                results.append(result)
                print(repo, result["revision"][:12], len(result["files"]), "files", flush=True)
            except Exception as exc:
                results.append({"repository": repo, "error": str(exc)})
                print(repo, "ERROR", str(exc), flush=True)
    args.cache.mkdir(parents=True, exist_ok=True)
    (args.cache / "index.json").write_text(json.dumps(sorted(results, key=lambda x: x["repository"].lower()), indent=2) + "\n")
    if any(r.get("error") or r.get("errors") for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
