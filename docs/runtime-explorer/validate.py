"""Check the portable script and match its lane simulator to installed Python.

Run probe.py first with the audited runtime. No model, browser, or build starts.
UI behavior is checked separately through the browser.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def main():
    evidence = json.loads((HERE / "evidence.json").read_text())
    guide = (HERE / "guide.js").read_text()
    template = (HERE / "explorer.html").read_text().replace("__GUIDE_SCRIPT__", guide)
    script = "\n".join(re.findall(r"<script>(.*?)</script>", template, re.S))
    selection = guide[guide.index("const word="):guide.index("const lanePresets=")]
    parity = "const E=" + json.dumps({k: evidence[k] for k in ("lane_patterns", "lane_cases")}) + ";\n"
    parity += selection
    parity += """
for (const c of E.lane_cases) {
  const actual = classifyLane(c.rows);
  if (actual.quick !== c.quick) throw Error(c.id + ': ' + JSON.stringify(actual));
}
if (!classifyLane(['Delete the database'], 'quick').quick) throw Error('forced quick');
if (classifyLane(['Add a button'], 'full').quick) throw Error('forced full');
console.log(JSON.stringify({python_parity_cases:E.lane_cases.length,override_cases:2,passed:true}));
"""
    with tempfile.TemporaryDirectory(prefix="engelbart-explorer-validation-") as tmp:
        check = Path(tmp) / "script.js"
        check.write_text(script)
        subprocess.run(["node", "--check", str(check)], check=True)
        check.write_text(parity)
        subprocess.run(["node", str(check)], check=True)


if __name__ == "__main__":
    main()
