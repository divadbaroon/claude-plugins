"""Build the portable explorer from probe evidence and the HTML template."""
import argparse
import json
from pathlib import Path
import re
import shutil

HERE = Path(__file__).resolve().parent


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--output", type=Path, required=True)
    output = args.parse_args().output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence = json.loads((HERE / "evidence.json").read_text())
    template = (HERE / "explorer.html").read_text()
    aliases = {"B": "build.py", "O": "agents/orchestrator.py", "U": "ui.py",
               "A": "web/goal/actions.js", "S": "web/goal/services.js", "C": "agents/context.py"}
    spans = [("trajectory/" + aliases[a], int(s), int(e))
             for a, s, e in re.findall(r"\b([BOUASC])\((\d+),(\d+)\)", template)]
    spans += [("trajectory/" + f, int(s), int(e))
              for f, s, e in re.findall(r"ref\('([^']+)',(\d+),(\d+)\)", template)]
    selected = {}
    for name, start, end in spans:
        assert name in evidence["sources"], name
        raw = evidence["sources"][name]
        lines = raw["text"].splitlines()
        assert 0 < start <= end <= len(lines), (name, start, end, len(lines))
        item = selected.setdefault(name, {"sha256": raw["sha256"], "lines": {}, "spans": []})
        item["spans"].append([start, end])
        for i in range(start, end + 1):
            item["lines"][i] = lines[i - 1]
    for name, item in selected.items():
        item["text"] = "\n".join(item["lines"].get(i, "") for i in range(1, max(item["lines"]) + 1))
        del item["lines"]
    evidence["sources"] = selected
    # Escape HTML delimiters so source text cannot end its JSON script element.
    encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = template.replace("__EVIDENCE__", encoded)
    assert "__EVIDENCE__" not in html
    assert len(html.encode()) < 1_000_000
    (output / "index.html").write_text(html)
    validation = {"scope": evidence["scope"], "date": evidence["date"],
                  "probes": evidence["probes"], "no_live_model_calls": True,
                  "source_files": {k: {"sha256": v["sha256"], "spans": v["spans"]} for k, v in selected.items()}}
    (output / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
    (HERE / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
    shutil.copyfile(HERE / "README.md", output / "README.md")
    print(json.dumps({"output": str(output / "index.html"), "bytes": len(html.encode()),
                      "source_files": len(selected), "source_spans": len(spans), "probes": "passed"}))


if __name__ == "__main__":
    main()
