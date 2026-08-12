import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "hc" / "src" / "human_compact" / "trajectory" / "web" / "bridge.js"
BUNDLE = ROOT / "hc" / "src" / "human_compact" / "trajectory" / "web" / "goals_bundle.html"
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
  querySelector: function () { return null; },
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
if (process.argv[3]) {
  const bundle = fs.readFileSync(process.argv[3], "utf8");
  const match = bundle.match(/<script type="__bundler\/template">\s*([\s\S]*?)\s*<\/script>/);
  sandbox.__bundleTemplate = JSON.parse(match[1]);
}
const result = vm.runInContext(process.argv[2], sandbox);
process.stdout.write(JSON.stringify(result));
"""


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class GoalPromptUiTests(unittest.TestCase):
    def run_js(self, expression):
        result = subprocess.run(
            [NODE, "-e", HARNESS, str(BRIDGE), expression, str(BUNDLE)],
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

    def test_three_way_merge_keeps_remote_analysis_and_explicit_local_edits(self):
        result = self.run_js(
            """(() => {
              const node = (id, title, extra, children) => Object.assign({
                id, title, prio: "normal", done: false, open: true,
                status: "todo", notes: "", desc: "", labels: [],
                children: children || []
              }, extra || {});
              const base = [
                node("g1", "Shared goal"),
                node("g-delete", "User deletes this")
              ];
              const local = [
                node("g1", "Shared goal", { prio: "high", notes: "human note" }),
                node("g-local", "Manual local goal")
              ];
              const remote = [
                node("g1", "Shared goal", { done: true, status: "todo" }, [
                  node("t:g1:0", "Analyzer todo")
                ]),
                node("g-delete", "User deletes this", { done: true }),
                node("g-remote", "Analyzer-added goal", { status: "inprog" })
              ];
              return window.__hcPromptUI.mergeTrees(base, local, remote);
            })()"""
        )
        by_id = {goal["id"]: goal for goal in result}
        self.assertEqual({"g1", "g-local", "g-remote"}, set(by_id))
        self.assertTrue(by_id["g1"]["done"])
        self.assertEqual("high", by_id["g1"]["prio"])
        self.assertEqual("human note", by_id["g1"]["notes"])
        self.assertEqual("t:g1:0", by_id["g1"]["children"][0]["id"])
        self.assertEqual("inprog", by_id["g-remote"]["status"])

    def test_bundle_patch_labels_add_controls_and_retains_active_completions(self):
        result = self.run_js(
            """(() => {
              const patch = window.__hcPromptUI.patchBundleSource;
              const source = __bundleTemplate;
              const patched = patch(source);
              return {
                idempotent: patch(patched) === patched,
                labeledSubgoal: patched.includes(">+ Add subgoal</span>"),
                nakedSubgoal: patched.includes(
                  'title="Add subgoal" style="width:18px;height:18px'
                ),
                labeledRoot: patched.includes('>+</span><span style="font-size:12.5px">Add goal</span>'),
                persistsRetention: patched.includes("v: 7, goals, selId, filter, activeRetained"),
                activeKeepsRetained: patched.includes("(!n.done || retained.has(n.id))"),
                rowCompletionRetains: patched.includes("kept.add(n.id)"),
                inspectorCompletionRetains: patched.includes("kept.add(sel.id)"),
                leavingActiveClears: patched.includes(
                  "s.filter === 'active' && k !== 'active' ? []"
                ),
                contentDrivenRows: patched.includes(
                  "gap:7px;min-height:29px;padding:0 8px;box-sizing:border-box"
                ) && !patched.includes("gap:7px;height:29px;padding:0 8px"),
                wrappingTitles: patched.includes(
                  "flex:1 1 auto;min-width:0;padding:5px 0;font-size:12.5px;line-height:1.45;overflow-wrap:anywhere"
                ),
                fixedControls: patched.includes(
                  "align-items:center;align-self:center;flex:none"
                ),
                selectedLabelRemoved: !patched.includes("SELECTED GOAL"),
                copyCaptionRemoved: !patched.includes("Copy appends goal metadata"),
                notesCaptionRemoved: !patched.includes(
                  "Markdown formats as you type · auto-saved with this goal"
                ),
                notesPlaceholderRemoved: !patched.includes("Plan in markdown —") &&
                  patched.includes('aria-label="Goal notes"')
              };
            })()"""
        )
        self.assertEqual(
            {
                "idempotent": True,
                "labeledSubgoal": True,
                "nakedSubgoal": False,
                "labeledRoot": True,
                "persistsRetention": True,
                "activeKeepsRetained": True,
                "rowCompletionRetains": True,
                "inspectorCompletionRetains": True,
                "leavingActiveClears": True,
                "contentDrivenRows": True,
                "wrappingTitles": True,
                "fixedControls": True,
                "selectedLabelRemoved": True,
                "copyCaptionRemoved": True,
                "notesCaptionRemoved": True,
                "notesPlaceholderRemoved": True,
            },
            result,
        )

    def test_reload_seed_preserves_only_visible_active_completions(self):
        result = self.run_js(
            """(() => {
              const seed = window.__hcPromptUI.seedPayload;
              const roots = [
                { id: "done", done: true, children: [] },
                { id: "active", done: false, children: [] }
              ];
              const active = seed({}, roots, {
                v: 7, filter: "active", selId: "done",
                activeRetained: ["done", "active", "missing"],
                paneTab: "notes", themeMode: "dark", view: "tree"
              });
              const afterFilterChange = seed({}, roots, {
                v: 7, filter: "done", activeRetained: ["done"]
              });
              return {
                version: active.v,
                filter: active.filter,
                selected: active.selId,
                retained: active.activeRetained,
                paneTab: active.paneTab,
                themeMode: active.themeMode,
                view: active.view,
                clearedOutsideActive: afterFilterChange.activeRetained
              };
            })()"""
        )
        self.assertEqual(
            {
                "version": 7,
                "filter": "active",
                "selected": "done",
                "retained": ["done"],
                "paneTab": "notes",
                "themeMode": "dark",
                "view": "tree",
                "clearedOutsideActive": [],
            },
            result,
        )


if __name__ == "__main__":
    unittest.main()
