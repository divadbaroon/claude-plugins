import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "hc" / "src" / "human_compact" / "trajectory" / "web" / "bridge.js"
NODE = shutil.which("node")

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const storage = {};
function XMLHttpRequest() {}
XMLHttpRequest.prototype.open = function () {};
XMLHttpRequest.prototype.send = function () {
  this.responseText = JSON.stringify({ goals: [], prompts: [] });
};
const document = {
  readyState: "loading",
  addEventListener: function () {},
  querySelectorAll: function () { return []; },
  getElementById: function () { return null; },
  documentElement: { contains: function () { return false; } }
};
const sandbox = {
  console,
  document,
  XMLHttpRequest,
  localStorage: {
    getItem: function (key) { return storage[key] || null; },
    setItem: function (key, value) { storage[key] = String(value); }
  },
  fetch: function () { return Promise.reject(new Error("not used")); },
  setInterval: function () { return 0; },
  setTimeout: function () { return 0; },
  clearTimeout: function () {},
  navigator: {}
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const result = vm.runInContext(process.argv[2], sandbox);
process.stdout.write(JSON.stringify(result));
"""


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class GoalPromptUiTests(unittest.TestCase):
    def run_js(self, expression):
        result = subprocess.run(
            [NODE, "-e", HARNESS, str(BRIDGE), expression],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_fuzzy_search_requires_every_token_and_tolerates_typos(self):
        result = self.run_js(
            """(() => {
              const score = window.__hcPromptUI.fuzzyScore;
              return {
                typo: score("rebld tmeout", "Debug hc goals rebuild timeout") !== null,
                missing: score("rebuild calendar", "Debug hc goals rebuild timeout") === null,
                exactBeatsTypo: score("rebuild timeout", "rebuild timeout") >
                  score("rebld tmeout", "rebuild timeout")
              };
            })()"""
        )
        self.assertEqual(
            {"typo": True, "missing": True, "exactBeatsTypo": True},
            result,
        )

    def test_picker_is_human_only_and_results_stay_newest_first(self):
        result = self.run_js(
            """(() => {
              const ui = window.__hcPromptUI;
              ui.acceptState({ goals: [], prompts: [
                { id: "old", role: "user", text: "rebuild timeout first", ordinal: 3 },
                { id: "assistant", role: "assistant", text: "hidden", ordinal: 99 },
                { id: "new", role: "user", text: "rebuild timeout last", ordinal: 8 }
              ] });
              return {
                empty: ui.rankedPrompts("").map(p => p.id),
                fuzzy: ui.rankedPrompts("rebld tmeout").map(p => p.id)
              };
            })()"""
        )
        self.assertEqual(
            {"empty": ["new", "old"], "fuzzy": ["new", "old"]},
            result,
        )


if __name__ == "__main__":
    unittest.main()
