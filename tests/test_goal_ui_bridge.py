"""The bridge between the Vault's state and the adopted goal artifact.

The artifact is checked in byte-for-byte and owns all rendering; the bridge
only maps records onto the fields it reads, mirrors edits back, and makes its
add-source controls ask for a value. These tests hold that contract.
"""
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
const store = {};
const made = [];
function El(tag) {
  this.tagName = tag; this.children = []; this.style = {}; this.value = "";
  this.className = ""; this.id = ""; this.textContent = "";
  this.appendChild = (n) => { this.children.push(n); n.parentNode = this; return n; };
  this.focus = () => {};
  this.setAttribute = () => {};
  this.removeChild = (n) => { this.children = this.children.filter(c => c !== n); };
  made.push(this);
}
const root = new El("html");
const app = new El("div"); app.className = "hc"; root.appendChild(app);
const document = {
  readyState: "complete", documentElement: root, head: new El("head"),
  body: root, addEventListener() {},
  createElement: (t) => new El(t),
  getElementById: (id) => made.find(e => e.id === id) || null,
  querySelector: (s) => (s === ".hc" ? app : null),
  querySelectorAll: () => []
};
function XHR() {}
XHR.prototype.open = function (method, url) { this._url = String(url || ""); };
XHR.prototype.send = function () {
  this.responseText = this._url.indexOf("/api/setup") >= 0
    ? (process.env.HC_SETUP || "{}")
    : (process.env.HC_STATE || "{}");
};
const sandbox = {
  console, document, XMLHttpRequest: XHR, made, require,
  localStorage: { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = String(v); } },
  fetch: () => Promise.reject(new Error("not used")),
  setInterval: () => 0, setTimeout: (f) => { if (f) f(); return 0; },
  clearTimeout() {}, navigator: {}, store
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const result = vm.runInContext(process.argv[2], sandbox);
Promise.resolve(result).then(v =>
  process.stdout.write(JSON.stringify(v === undefined ? null : v)));
"""

STATE = {
    "scope": "global", "provider": "claude", "generated_at": "2026-08-13T00:00:00+00:00",
    "revision": "r1",
    "goals": [
        {"id": "g1", "title": "Build the platform", "parent_goal_id": None,
         "status": "in_progress", "priority": "high", "notes": "keep it small",
         "description": "Stand up the goal model.", "prompt_ids": ["a#1"],
         "sources": [{"id": "s1", "type": "github", "label": "divadbaroon/claude-plugins"},
                     {"id": "s2", "type": "local", "label": "~/Desktop/PapertLab/Demo"},
                     {"id": "s3", "type": "doc", "label": "design-notes.md"}]},
        {"id": "g1a", "title": "Capture interactions", "parent_goal_id": "g1",
         "status": "completed", "prompt_ids": [], "sources": []},
    ],
    "prompts": [{"id": "a#1", "role": "user", "text": "make it a desktop app",
                 "created_at": "2026-08-01"}],
    "agent_runs": {"g1": [{
        "session_id": "7c9f1a20-x", "status": "running", "git_branch": "main",
        "started_at": "2026-08-12T18:00:00+00:00", "finished_at": None,
        "user_prompt": "work on g1", "summary": "did a thing",
        "counts": {"completed": 1}, "tasks": [
            {"task_id": "1", "subject": "Read the schema", "status": "completed",
             "activeForm": ""},
            {"task_id": "2", "subject": "Wire the bridge", "status": "in_progress",
             "activeForm": "Wiring the bridge"}]}]},
}


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class BridgeTestCase(unittest.TestCase):
    def run_js(self, expression, state=None, setup=None):
        import os
        env = dict(os.environ, HC_STATE=json.dumps(state or STATE),
                   HC_SETUP=json.dumps(setup if setup is not None else
                                       {"ok": True, "sv": 9, "storage": True,
                                        "analysis": "claude", "done": True}))
        result = subprocess.run([NODE, "-e", HARNESS, str(BRIDGE), expression],
                                capture_output=True, text=True, check=False, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)


    def patched_bundle(self, tail):
        """Apply patchBundleSource to the checked-in artifact, then evaluate."""
        return self.run_js(
            "var fs = require('fs');"
            "var html = fs.readFileSync(%s, 'utf8');"
            "var src = JSON.parse(html.match("
            "  /<script type=\"__bundler\\/template\">\\s*([\\s\\S]*?)\\s*<\\/script>/)[1]);"
            "var out = window.__hcPromptUI.patchBundleSource(src);"
            % json.dumps(str(BUNDLE)) + tail)

    def roots(self, state=None):
        return self.run_js(
            "window.__hcPromptUI.rootsFromState(%s);" % json.dumps(state or STATE))


class NodeMappingTests(BridgeTestCase):
    """Server records become the fields the artifact renders."""

    def test_the_tree_keeps_its_shape_and_goal_fields(self):
        roots = self.roots()
        self.assertEqual(1, len(roots))
        top = roots[0]
        self.assertEqual("g1", top["id"])
        self.assertEqual("high", top["prio"])
        self.assertEqual("inprog", top["status"])
        self.assertEqual("keep it small", top["notes"])
        self.assertEqual(["g1a"], [c["id"] for c in top["children"]])
        self.assertTrue(top["children"][0]["done"])   # completed -> done

    def test_linked_prompts_become_prompt_history(self):
        row = self.roots()[0]["prompts"][0]
        self.assertEqual("a#1", row["id"])
        self.assertEqual("make it a desktop app", row["text"])
        self.assertIsInstance(row["ts"], int)

    def test_sources_split_into_code_and_document_context(self):
        ctx = self.roots()[0]["ctx"]
        self.assertEqual([("github", "divadbaroon/claude-plugins"),
                          ("local", "~/Desktop/PapertLab/Demo")],
                         [(c["type"], c["label"]) for c in ctx["code"]])
        self.assertEqual(["design-notes.md"], [d["label"] for d in ctx["docs"]])
        self.assertEqual("Stand up the goal model.", ctx["objective"])

    def test_a_goal_without_a_description_leaves_the_field_alone(self):
        # Absent means "the artifact keeps its own copy", not "blank".
        state = json.loads(json.dumps(STATE))
        state["goals"][0]["description"] = ""
        self.assertNotIn("objective", self.roots(state)[0]["ctx"])

    def test_a_run_becomes_the_agent_panel(self):
        agent = self.roots()[0]["agent"]
        self.assertEqual("running", agent["status"])
        self.assertEqual("main", agent["branch"])
        self.assertEqual([("Read the schema", "done"),
                          ("Wire the bridge", "doing")],
                         [(t["t"], t["s"]) for t in agent["todos"]])

    def test_a_goal_with_no_run_has_no_agent_or_artifact(self):
        child = self.roots()[0]["children"][0]
        self.assertIsNone(child["agent"])
        self.assertIsNone(child["artifact"])


class SeedTests(BridgeTestCase):
    """The artifact must boot into the goal view, not its own onboarding."""

    def seeded(self, setup=None):
        return self.run_js("JSON.parse(store['hc-vault-ui-v1']);", setup=setup)

    def test_a_set_up_vault_skips_the_wizard(self):
        payload = self.seeded()
        self.assertEqual(6, payload["v"])
        self.assertTrue(payload["setup"]["done"])
        self.assertEqual("claude", payload["setup"]["analysis"])
        self.assertEqual("goals", payload["page"])
        self.assertEqual("context", payload["paneTab"])

    def test_a_fresh_vault_shows_the_wizard(self):
        # The seed reports what the vault was actually told, so onboarding is
        # skipped only when it genuinely happened.
        payload = self.seeded({"ok": True, "sv": 9, "storage": False,
                               "analysis": None, "done": False})
        self.assertFalse(payload["setup"]["done"])
        self.assertFalse(payload["setup"]["storage"])
        self.assertIsNone(payload["setup"]["analysis"])

    def test_an_unreachable_server_does_not_claim_setup_is_done(self):
        payload = self.seeded({"ok": False})
        self.assertFalse(payload["setup"]["done"])

    def test_seeding_writes_the_store_the_artifact_reads(self):
        saved = self.run_js("JSON.parse(store['hc-vault-ui-v1']);")
        self.assertEqual(1, len(saved["goals"]))
        self.assertEqual("g1", saved["selId"])


class AddControlTests(BridgeTestCase):
    """The artifact's add controls append placeholders; make them ask first."""

    def test_all_three_controls_are_rewired_in_the_real_bundle(self):
        patched = self.run_js(
            "var fs = require('fs');"
            "var html = fs.readFileSync(%s, 'utf8');"
            "var src = JSON.parse(html.match("
            "  /<script type=\"__bundler\\/template\">\\s*([\\s\\S]*?)\\s*<\\/script>/)[1]);"
            "var out = window.__hcPromptUI.patchBundleSource(src);"
            "({ gh: out.indexOf(\"__hcAsk('github')\") >= 0,"
            "   local: out.indexOf(\"__hcAsk('local')\") >= 0,"
            "   doc: out.indexOf(\"__hcAsk('doc')\") >= 0,"
            "   placeholders: out.indexOf(\"label: 'owner/repo'\") >= 0 ||"
            "                 out.indexOf(\"label: 'notes.md'\") >= 0,"
            "   idempotent: window.__hcPromptUI.patchBundleSource(out) === out });"
            % json.dumps(str(BUNDLE)))
        self.assertTrue(patched["gh"], "github control not rewired")
        self.assertTrue(patched["local"], "local folder control not rewired")
        self.assertTrue(patched["doc"], "document control not rewired")
        self.assertFalse(patched["placeholders"], "a placeholder label survived")
        self.assertTrue(patched["idempotent"])

    def test_the_dialog_returns_what_was_typed(self):
        typed = self.run_js(
            "var p = window.__hcAsk('github');"
            "var box = made.filter(function (e) { return e.className === 'hc-ask-box'; })[0];"
            "var input = box.children[1];"
            "input.value = ' owner/repo ';"
            "box.children[2].children[1].onclick();"
            "p;")
        self.assertEqual("owner/repo", typed)

    def test_cancelling_attaches_nothing(self):
        self.assertIsNone(self.run_js(
            "var p = window.__hcAsk('doc');"
            "var box = made.filter(function (e) { return e.className === 'hc-ask-box'; })[0];"
            "box.children[2].children[0].onclick();"
            "p;"))

    def test_escape_attaches_nothing(self):
        self.assertIsNone(self.run_js(
            "var p = window.__hcAsk('local');"
            "var box = made.filter(function (e) { return e.className === 'hc-ask-box'; })[0];"
            "box.children[1].value = 'typed but abandoned';"
            "box.children[1].onkeydown({ key: 'Escape', preventDefault: function () {} });"
            "p;"))


class SourceRoundTripTests(BridgeTestCase):
    """Edits the artifact makes to context must come back as typed sources."""

    def test_code_and_docs_collapse_back_into_one_typed_list(self):
        rows = self.run_js(
            "window.__hcPromptUI.sourcesOfNode({ ctx: {"
            "  code: [{ id: 'c1', type: 'github', label: 'owner/repo' },"
            "          { id: 'c2', type: 'local', label: '~/proj' }],"
            "  docs: [{ id: 'd1', type: 'doc', label: 'spec.md' }] } });")
        self.assertEqual([("github", "owner/repo"), ("local", "~/proj"),
                          ("doc", "spec.md")],
                         [(r["type"], r["label"]) for r in rows])

    def test_blank_rows_are_dropped_and_labels_trimmed(self):
        rows = self.run_js(
            "window.__hcPromptUI.sourcesOfNode({ ctx: {"
            "  code: [{ id: 'c1', type: 'local', label: '   ' },"
            "          { id: 'c2', type: 'local', label: '  ~/keep  ' }],"
            "  docs: [] } });")
        self.assertEqual([("local", "~/keep")],
                         [(r["type"], r["label"]) for r in rows])

    def test_an_unknown_type_is_treated_as_a_local_path(self):
        rows = self.run_js(
            "window.__hcPromptUI.sourcesOfNode({ ctx: {"
            "  code: [{ id: 'c1', label: '~/thing' }], docs: [] } });")
        self.assertEqual("local", rows[0]["type"])


class MergeTests(BridgeTestCase):
    """Remote analysis and local edits both survive a refresh."""

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



if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class NoInventedDataTests(BridgeTestCase):
    """Nothing on screen may be a sample list or a simulated duration."""

    def test_the_progress_simulation_is_gone(self):
        out = self.patched_bundle("out;")
        self.assertNotIn("p + 0.9 + Math.random()", out)
        self.assertIn("setup.progress().then", out)

    def test_no_hardcoded_conversation_count_survives(self):
        out = self.patched_bundle("out;")
        self.assertNotIn("total: 63", out)
        self.assertNotIn("63 conversations", out)

    def test_the_sample_conversation_list_defers_to_the_vault(self):
        self.assertIn("window.__hcConvos) return window.__hcConvos",
                      self.patched_bundle("out;"))

    def test_the_patch_is_idempotent(self):
        self.assertTrue(self.patched_bundle(
            "out === window.__hcPromptUI.patchBundleSource(out);"))
