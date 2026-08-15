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
const calls = [];
const made = [];
function El(tag) {
  this.tagName = tag; this.children = []; this.style = {}; this.value = "";
  this.className = ""; this.id = ""; this.textContent = "";
  this.appendChild = (n) => { this.children.push(n); n.parentNode = this; return n; };
  this.focus = () => {};
  this.insertBefore = (n, ref) => {
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(n); else this.children.splice(at, 0, n);
    n.parentNode = this;
    return n;
  };
  Object.defineProperty(this, "firstChild",
    { get: () => this.children[0] || null });
  Object.defineProperty(this, "nextSibling", { get: () => {
    if (!this.parentNode) return null;
    const at = this.parentNode.children.indexOf(this);
    return (at >= 0 ? this.parentNode.children[at + 1] : null) || null;
  } });
  this.querySelector = (sel) => {
    const want = sel.replace(/^\./, "");
    const walk = (node) => {
      for (const child of node.children) {
        if (sel.startsWith(".")
            ? String(child.className).split(" ").includes(want)
            : child.className === want) return child;
        const deep = walk(child);
        if (deep) return deep;
      }
      return null;
    };
    return walk(this);
  };
  this.attrs = {};
  this.setAttribute = (k, v) => { this.attrs[k] = String(v); };
  this.getAttribute = (k) => (k in this.attrs ? this.attrs[k] : null);
  this.insertBefore = (n, ref) => {
    const at = ref ? this.children.indexOf(ref) : -1;
    if (n.parentNode) n.parentNode.removeChild(n);
    at < 0 ? this.children.push(n) : this.children.splice(at, 0, n);
    n.parentNode = this;
    return n;
  };
  this.contains = (n) => {
    if (n === this) return true;
    return this.children.some((c) => c.contains && c.contains(n));
  };
  // Clearing parentNode matters: a detached node that still claims a parent
  // reads as on screen, and the marker tests are about exactly that.
  this.removeChild = (n) => {
    this.children = this.children.filter(c => c !== n);
    if (n) n.parentNode = null;
  };
  made.push(this);
}
const root = new El("html");
const app = new El("div"); app.className = "hc"; root.appendChild(app);
// The real shape: a header block holding the subtitle, then the panel.
const header = new El("div"); header.className = "hc-head"; app.appendChild(header);
const sub = new El("div"); sub.className = "hc-sub"; header.appendChild(sub);
const panel = new El("div"); panel.className = "conv-panel"; app.appendChild(panel);
const listeners = [];
const document = {
  readyState: "complete", documentElement: root, head: new El("head"),
  body: root,
  addEventListener: (type, fn) => listeners.push([type, fn]),
  createElement: (t) => new El(t),
  getElementById: (id) => made.find(e => e.id === id) || null,
  querySelector: (s) => (s === ".hc" ? app : root.querySelector(s)),
  // Enough for a tag-name sweep: the bridge uses it to find a heading
  // by its text when the anchor it was given has been re-rendered away.
  // Walks the live tree, as a browser does: a node that has been
  // re-rendered away is not a result, and treating it as one sends the
  // button somewhere nobody can click it.
  querySelectorAll: (sel) => {
    const text = String(sel || '').trim();
    const attr = text.match(/^\[([\w-]+)\]$/);
    const tags = text.split(',').map(t => t.trim().toUpperCase());
    const hit = (c) => attr
      ? c.getAttribute && c.getAttribute(attr[1]) !== null
      : tags.includes(String(c.tagName).toUpperCase());
    const out = [];
    (function walk(n) { (n.children || []).forEach(c => {
      if (hit(c)) out.push(c);
      walk(c); }); })(root);
    return out;
  }
};
function XHR() {}
XHR.prototype.open = function (method, url) { this._url = String(url || ""); };
XHR.prototype.send = function () {
  this.responseText = this._url.indexOf("/api/setup") >= 0
    ? (process.env.HC_SETUP || "{}")
    : this._url.indexOf("/api/briefings") >= 0
    ? (process.env.HC_BRIEFS || '{"ok":true,"goals":{}}')
    : (process.env.HC_STATE || "{}");
};
const sandbox = {
  console, document, XMLHttpRequest: XHR, made, require, calls, app, sub,
  listeners,
  header, panel,
  localStorage: { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = String(v); } },
  fetch: (url, opts) => {
    calls.push([url, opts && opts.body ? JSON.parse(opts.body) : null]);
    const url2 = String(url || "");
    const body = url2.indexOf("/api/conversation") >= 0
      ? { ok: true, id: "c1", thread: [["YOU", "hi"], ["CLAUDE", "hello"]] }
      : url2.indexOf("/api/setup") >= 0
      ? JSON.parse(process.env.HC_SETUP || '{"ok":true}')
      : (opts && opts.body && JSON.parse(opts.body).op === "launch_agent_run"
         && process.env.HC_FAIL_LAUNCH === "1")
      ? { ok: false, error: "no project directory is recorded" }
      : (opts && opts.body && JSON.parse(opts.body).op === "preview_agent_run")
      ? { ok: true, goal_id: "g1", title: "Restyle UI to match Pentimento",
          cwd: "/repo", command: "hc work g1", add_dirs: ["/repo"],
          references: [], prompt: "Work on my Vault goal g1 — Restyle the Vault UI. Plan first.",
          instruction: "Restyle the Vault UI. Plan first.",
          context: "# Your assignment\n## 1. WHERE THIS SITS\n…",
          sections: ["WHERE THIS SITS", "WHAT THE USER ASKED FOR, IN THEIR WORDS",
                     "ALREADY DECIDED — settled", "STILL OPEN"] }
      : { ok: true, terminal: "Terminal", cwd: "/repo" };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
  },
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
                 "session_id": "879da390-1c4e-4d0a-9f11-2b7c5e8a1d33",
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
    def run_js(self, expression, state=None, setup=None, briefs=None,
               extra_env=None):
        import os
        env = dict(os.environ, HC_STATE=json.dumps(state or STATE),
                   HC_BRIEFS=json.dumps(briefs if briefs is not None
                                        else {"ok": True, "goals": {}}),
                   HC_SETUP=json.dumps(setup if setup is not None else
                                       {"ok": True, "sv": 9, "storage": True,
                                        "analysis": "claude", "done": True}))
        env.update(extra_env or {})
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

    def test_a_goal_without_a_description_is_blank_not_borrowed(self):
        # Leaving the field unset makes the artifact fall back to its own demo
        # copy, which reads as this goal's objective. Blank is the honest value.
        state = json.loads(json.dumps(STATE))
        state["goals"][0]["description"] = ""
        self.assertEqual("", self.roots(state)[0]["ctx"]["objective"])

    def test_a_run_becomes_the_agent_panel(self):
        agent = self.roots()[0]["agent"]
        self.assertEqual("running", agent["status"])
        self.assertEqual("main", agent["branch"])
        self.assertEqual([("Read the schema", "done"),
                          ("Wire the bridge", "doing")],
                         [(t["t"], t["s"]) for t in agent["todos"]])

    def test_a_goal_with_no_run_describes_the_run_and_has_no_artifact(self):
        # No history, but the pane still says what running it would do.
        child = self.roots()[0]["children"][0]
        self.assertEqual("idle", child["agent"]["status"])
        self.assertEqual([], child["agent"]["todos"])
        self.assertIsNone(child["artifact"])

    def test_the_write_up_comes_from_whichever_run_wrote_one(self):
        # The card picks the run that changed files, because that is the one
        # worth reviewing. But an agent that explains itself and an agent that
        # edits are often different runs, and reading the summary off the
        # chosen run left the card blank with a real write-up sitting in state.
        runs = {"g1": [
            {"status": "finished", "summary": "What I found and why.",
             "files": [], "finished_at": "2026-08-14T05:43:10+00:00"},
            {"status": "finished", "summary": "",
             "files": [{"path": "bridge.js", "edits": 3}],
             "finished_at": "2026-08-14T06:43:12+00:00"},
        ]}
        art = json.loads(self.run_js(
            "JSON.stringify(window.__hcPromptUI.artifactOf("
            '{"id": "g1"}, %s, null));' % json.dumps(runs)))
        self.assertEqual("What I found and why.", art["summary"])
        self.assertEqual(["bridge.js"], [f["path"] for f in art["files"]])

    def test_a_run_that_wrote_nothing_leaves_the_card_blank(self):
        runs = {"g1": [{"status": "finished", "summary": "", "files": []}]}
        art = json.loads(self.run_js(
            "JSON.stringify(window.__hcPromptUI.artifactOf("
            '{"id": "g1"}, %s, null));' % json.dumps(runs)))
        self.assertEqual("", art["summary"])


class BriefingSeedTests(BridgeTestCase):
    """The panels are baked in at boot, so their content must arrive first."""

    BRIEFS = {"ok": True, "goals": {"g1": {"cwd": "/repo", "add_dirs": ["/repo"],
              "references": [], "sections": [
                  {"title": "ALREADY DECIDED \u2014 settled", "lines": ["  - hooks"]},
                  {"title": "ALREADY BUILT", "lines": ["  - the launcher"]},
                  {"title": "PROBLEMS HIT BEFORE", "lines": ["  - the pty"]}]}}}

    def test_the_panels_are_filled_before_the_artifact_boots(self):
        saved = json.loads(self.run_js(
            "window.__hcPromptUI.seedForTest();"
            "store['hc-vault-ui-v1'];", briefs=self.BRIEFS))
        ctx = saved["goals"][0]["ctx"]
        self.assertEqual("hooks", ctx["decided"])
        self.assertEqual("the launcher", ctx["built"])
        self.assertEqual("the pty", ctx["hit"])

    def test_seeding_does_not_stop_the_run_history_being_fetched(self):
        # The seed fills details[] before boot; guarding loadDetail on the
        # entry existing meant /api/review was never called and REVIEW could
        # never fill.
        asked = self.run_js(
            "window.__hcPromptUI.seedForTest();"
            "window.__hcPromptUI.loadDetailForTest('g1');"
            "var wait = Promise.resolve();"
            "for (var i = 0; i < 20; i += 1) wait = wait.then(function(){});"
            "wait.then(function () { return calls.map(function (c) "
            "{ return String(c[0]); }).filter(function (u) "
            "{ return u.indexOf('/api/review') === 0; }).length; });",
            briefs=self.BRIEFS)
        self.assertEqual(1, asked)

    def test_no_briefings_leaves_them_empty_rather_than_guessing(self):
        saved = json.loads(self.run_js(
            "window.__hcPromptUI.seedForTest();"
            "store['hc-vault-ui-v1'];"))
        ctx = saved["goals"][0]["ctx"]
        self.assertEqual("", ctx["decided"])
        self.assertEqual("", ctx["built"])


class SeedTests(BridgeTestCase):
    """The artifact must boot into the goal view, not its own onboarding."""

    def seeded(self, setup=None):
        return self.run_js("JSON.parse(store['hc-vault-ui-v1']);", setup=setup)

    def test_a_set_up_vault_skips_the_wizard(self):
        payload = self.seeded()
        # v7 marks a store this bridge seeded, which is what lets an empty
        # goal list be trusted instead of replaced by the sample tree.
        self.assertEqual(7, payload["v"])
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

    def test_a_conversation_never_shows_a_label_with_nothing_after_it(self):
        out = self.patched_bundle("out;")
        self.assertNotIn("Goal: {{ cv.goal }}", out)
        self.assertIn("{{ cv.goalLine }}", out)

    def test_the_tab_subtitles_say_what_each_page_holds(self):
        out = self.patched_bundle("out;")
        self.assertIn("A holistic view of your goals, subgoals, and suggested "
                      "tasks \u2014 inferred from your Claude "
                      "Code\u00a0conversation\u00a0history.", out)
        self.assertIn("preserved beyond Claude\u2019s default 30-day history",
                      out)
        self.assertNotIn("Goals, subgoals, and suggested tasks inferred from "
                         "your Claude Code history.", out)

    def test_the_subtitles_are_wide_enough_for_their_new_copy(self):
        # The width was chosen for the shorter sentences these replaced.
        out = self.patched_bundle("out;")
        self.assertNotIn("max-width:560px", out)
        self.assertIn('max-width:740px;text-wrap:pretty">A holistic view', out)
        self.assertIn('max-width:740px;text-wrap:pretty">Your Claude Code '
                      'conversations', out)

    def test_opening_a_conversation_fetches_its_full_thread(self):
        out = self.patched_bundle("out;")
        self.assertIn("loadThread(c.id)", out)
        self.assertNotIn("open: () => this.setState({ convSel: c.id })", out)

    def test_the_fetched_thread_replaces_the_preview_in_place(self):
        got = self.run_js(
            "window.__hcConvos = [{ id: 'c1', thread: [['YOU', 'preview']] }];"
            "window.__hcPromptUI.loadThread('c1').then(function () {"
            "  return window.__hcConvos[0].thread; });")
        self.assertEqual([["YOU", "hi"], ["CLAUDE", "hello"]], got)

    def test_the_inspector_always_opens_on_context(self):
        out = self.patched_bundle("out;")
        self.assertIn("paneTab: 'context',", out)
        self.assertNotIn("indexOf(saved.paneTab)", out)

    def test_the_patch_is_idempotent(self):
        self.assertTrue(self.patched_bundle(
            "out === window.__hcPromptUI.patchBundleSource(out);"))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class BriefingPanelTests(BridgeTestCase):
    """Everything the briefing knows has to reach a panel that shows it."""

    def sections(self, brief):
        return json.loads(self.run_js(
            "JSON.stringify(window.__hcPromptUI.briefingSections(%s));"
            % json.dumps(brief)))

    def test_open_questions_reach_the_blockers_panel(self):
        got = self.sections({"sections": [
            {"title": "PROBLEMS HIT BEFORE", "lines": ["  - the pty ate it"]},
            {"title": "STILL OPEN", "lines": ["  - which port?"]},
        ]})
        self.assertEqual("the pty ate it\nwhich port?", got["hit"])

    def test_open_questions_alone_still_fill_the_panel(self):
        got = self.sections({"sections": [
            {"title": "STILL OPEN", "lines": ["  - which port?"]}]})
        self.assertEqual("which port?", got["hit"])

    def test_the_settled_and_built_sections_stay_separate(self):
        got = self.sections({"sections": [
            {"title": "ALREADY DECIDED \u2014 settled", "lines": ["  - hooks"]},
            {"title": "ALREADY BUILT", "lines": ["  - the launcher"]},
        ]})
        self.assertEqual("hooks", got["decided"])
        self.assertEqual("the launcher", got["built"])

    def test_an_empty_section_leaves_the_panel_empty(self):
        self.assertEqual({}, self.sections({"sections": [
            {"title": "STILL OPEN", "lines": []}]}))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class NoSampleLeakageTests(BridgeTestCase):
    """A goal the vault knows nothing about must show nothing, not a sample."""

    def ctx_of(self, goal_id, state=None):
        return self.run_js(
            "var roots = window.__hcPromptUI.rootsFromState(%s);"
            "function find(ns, id) { for (var i = 0; i < ns.length; i++) {"
            "  if (ns[i].id === id) return ns[i];"
            "  var f = find(ns[i].children || [], id); if (f) return f; } return null; }"
            "find(roots, %s).ctx;" % (json.dumps(state or STATE), json.dumps(goal_id)))

    def test_every_context_field_is_set_even_when_empty(self):
        # The artifact substitutes its own demo copy for any field left null,
        # which would attribute another goal's decisions to this one.
        ctx = self.ctx_of("g1a")
        for key in ("objective", "said", "decided", "built", "hit"):
            self.assertIn(key, ctx, f"{key} must be set explicitly")
            self.assertEqual("", ctx[key])

    def test_a_known_goal_still_carries_its_own_text(self):
        ctx = self.ctx_of("g1")
        self.assertEqual("Stand up the goal model.", ctx["objective"])

    def test_code_and_docs_are_always_arrays(self):
        ctx = self.ctx_of("g1a")
        self.assertEqual([], ctx["code"])
        self.assertEqual([], ctx["docs"])


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class NoSimulatedAgentTests(BridgeTestCase):
    """The agent panel must reflect a real session, never imitate one."""

    def test_the_fabricated_diff_stats_are_gone(self):
        out = self.patched_bundle("out;")
        # Added and removed line counts derived from the length of a filename.
        self.assertNotIn("(p.length * 7) % 60", out)
        self.assertNotIn("(p.length * 3) % 25", out)

    def test_the_hardcoded_file_list_is_gone(self):
        out = self.patched_bundle("out;")
        self.assertNotIn("compact_focus/state.py", out)
        self.assertNotIn("hooks/guard.sh", out)

    def test_the_templated_result_summary_is_gone(self):
        self.assertNotIn("summary: 'Implemented", self.patched_bundle("out;"))

    def test_both_entry_points_start_a_real_session(self):
        out = self.patched_bundle("out;")
        self.assertIn("window.__hcAgent.launch(id)", out)
        self.assertIn("this.runAgent();", out)      # generate todos defers to it

    def agent_for(self, state):
        return self.run_js(
            "var roots = window.__hcPromptUI.rootsFromState(%s);"
            % json.dumps(state) +
            "roots[0].agent;")

    def test_a_launched_goal_says_it_is_waiting_on_a_keypress(self):
        st = {"scope": "global", "revision": "r1", "generated_at": "",
              "goals": [{"id": "g1", "title": "Ship it", "status": "active"}],
              "prompts": [], "agent_runs": {},
              "agent_claim": {"goal_id": "g1", "prompt": "Work on g1"}}
        got = self.agent_for(st)
        self.assertEqual("waiting", got["status"])
        self.assertEqual([], got["todos"])

    def test_a_claim_for_another_goal_does_not_leak_in(self):
        st = {"scope": "global", "revision": "r1", "generated_at": "",
              "goals": [{"id": "g1", "title": "Ship it", "status": "active"}],
              "prompts": [], "agent_runs": {},
              "agent_claim": {"goal_id": "g2", "prompt": "other"}}
        got = self.agent_for(st)
        self.assertEqual("idle", got["status"])       # not "waiting"
        self.assertEqual("", got["prompt"])

    def test_real_tasks_replace_the_waiting_state(self):
        st = {"scope": "global", "revision": "r1", "generated_at": "",
              "goals": [{"id": "g1", "title": "Ship it", "status": "active"}],
              "prompts": [],
              "agent_runs": {"g1": [{"status": "running", "tasks": [
                  {"subject": "Read the code", "status": "completed"},
                  {"subject": "Make the change", "status": "in_progress"}]}]},
              "agent_claim": {"goal_id": "g1", "prompt": "Work on g1"}}
        got = self.agent_for(st)
        self.assertEqual("running", got["status"])
        self.assertEqual(["Read the code", "Make the change"],
                         [t["t"] for t in got["todos"]])

    def test_opening_the_modal_does_not_switch_tabs(self):
        out = self.patched_bundle("out;")
        self.assertIn("if (started) this.set(() => ({ paneTab: 'artifact' }));",
                      out)
        self.assertNotIn("this.set(() => ({ paneTab: 'artifact' }));\n"
                         "    if (window.__hcAgent)", out)

    def test_pressing_run_launches_without_a_dialog(self):
        posted = self.run_js(
            "window.__hcAgent.launch('g1');"
            "var wait = Promise.resolve();"
            "for (var i = 0; i < 30; i += 1) wait = wait.then(function(){});"
            "wait.then(function () { return calls.filter(function (c) {"
            "  return c[0] === '/api/op'; }).map(function (c) "
            "{ return c[1]; }); });")
        self.assertEqual([{"op": "launch_agent_run", "goal_id": "g1",
                           "confirmed": True}], posted)

    def test_a_failed_launch_does_not_report_success(self):
        # post() resolves the error body, and an error body is truthy.
        failed = self.run_js(
            "var seen = null;"
            "window.__hcAgent.launch('g1').then(function (r) { seen = 'ok'; })"
            "  .catch(function (e) { seen = e.message; });"
            "var wait = Promise.resolve();"
            "for (var i = 0; i < 30; i += 1) wait = wait.then(function(){});"
            "wait.then(function () { return seen; });",
            extra_env={"HC_FAIL_LAUNCH": "1"})
        self.assertEqual("no project directory is recorded", failed)

    def test_running_it_switches_to_the_review_pane(self):
        out = self.patched_bundle("out;")
        self.assertIn("this.set(() => ({ paneTab: 'artifact' }));", out)

    def test_review_stays_its_own_tab(self):
        out = self.patched_bundle("out;")
        self.assertIn("showArt: !!sel && paneTab === 'artifact'", out)
        self.assertNotIn("review folded into agent", out)

    def test_the_pane_distinguishes_typed_from_started(self):
        out = self.patched_bundle("out;")
        self.assertIn("press Enter there to start", out)
        self.assertIn("return 'running now' + steps", out)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class LiveFeedTests(BridgeTestCase):
    """A live feed cannot travel through state the app reads only at boot."""

    RUN = {"state": "running", "elapsed": "2 min",
           "did": [{"at": "03:06:39", "kind": "did", "text": "read README.md"},
                   {"at": "03:07:06", "kind": "task", "text": "finished: scan"}],
           "checked": ["npm test"], "tasks": {"done": 1, "total": 3},
           "subgoals": {"done": 0, "total": 2},
           "resume": "claude -r abc-123"}

    def drawn(self, run):
        return json.loads(self.run_js(
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.renderLive('g1', %s);" % json.dumps([run]) +
            "var flat = [];"
            "pane.children.forEach(function (c) {"
            "  if (c.children.length) {"
            "    c.children.forEach(function (k) {"
            "      flat.push([k.className, k.textContent]); });"
            "  } else { flat.push([c.className, c.textContent]); } });"
            "JSON.stringify(flat);"))

    def test_it_draws_into_the_pane_itself(self):
        rows = self.drawn(self.RUN)
        self.assertEqual("hc-live-head", rows[0][0])
        self.assertEqual("Running · 2 min", rows[0][1])

    def test_the_heading_is_the_first_thing_drawn(self):
        first = self.run_js(
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.renderLive('g1', %s);" % json.dumps([self.RUN]) +
            "pane.children[0].className;")
        self.assertEqual("hc-live-top", first)

    def test_newest_action_first(self):
        rows = [t for c, t in self.drawn(self.RUN) if c == "hc-live-did"]
        self.assertIn("finished: scan", rows[0])
        self.assertIn("read README.md", rows[1])

    def test_it_carries_checks_and_progress(self):
        text = " ".join(t for _, t in self.drawn(self.RUN))
        self.assertIn("verified by running: npm test", text)
        self.assertIn("1/3 steps", text)
        self.assertIn("0/2 subgoals complete", text)

    def test_a_session_can_be_opened_rather_than_copied(self):
        rows = dict((c, t) for c, t in
                    self.drawn(dict(self.RUN, session_id="abc-123")))
        self.assertEqual("open the conversation", rows["hc-live-open"])

    def test_the_button_takes_the_corner_the_status_badge_had(self):
        run = json.dumps([dict(self.RUN, session_id="abc-123")])
        where = json.loads(self.run_js(
            "var slot = document.createElement('div');"
            "slot.className = 'hc-live-open-slot';"
            "document.body.appendChild(slot);"
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.renderLive('g1', " + run + ");"
            "JSON.stringify([slot.children.map(function (c) "
            "{ return c.className; }), pane.children[0].children.map("
            "function (c) { return c.className; })]);"))
        self.assertEqual(["hc-live-open"], where[0])
        self.assertEqual(["hc-live-head"], where[1])

    def test_redrawing_does_not_stack_buttons_in_the_corner(self):
        # The corner is outside .hc-live, so clearing the anchor never clears
        # it: every two-second poll would leave another button behind.
        run = dict(self.RUN, session_id="abc-123")
        count = self.run_js(
            "var slot = document.createElement('div');"
            "slot.className = 'hc-live-open-slot';"
            "document.body.appendChild(slot);"
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.renderLive('g1', %s);"
            % json.dumps([run]) +
            "window.__hcPromptUI.renderLive('g1', %s);"
            % json.dumps([dict(run, elapsed="2m")]) +
            "slot.children.length;")
        self.assertEqual(1, count)

    def test_the_button_still_lands_somewhere_without_the_corner(self):
        # A goal with no artifact yet has no section header to hang it on.
        where = json.loads(self.run_js(
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.renderLive('g1', %s);"
            % json.dumps([dict(self.RUN, session_id="abc-123")]) +
            "JSON.stringify(pane.children[0].children.map(function (c) "
            "{ return c.className; }));"))
        self.assertEqual(["hc-live-head", "hc-live-open"], where)

    def test_a_waiting_run_says_so_on_the_line_itself(self):
        waiting = dict(self.RUN, state="waiting", did=[
            {"at": "03:11", "kind": "did", "text": "read a.py"},
            {"at": "03:12", "kind": "turn", "text": "Migrate the records?"}])
        rows = dict((c, t) for c, t in self.drawn(waiting))
        self.assertIn("waiting for your decision", rows["hc-live-wait"])
        self.assertIn("Migrate the records?", rows["hc-live-wait"])

    def test_a_running_turn_is_not_marked_as_waiting(self):
        running = dict(self.RUN, state="running", did=[
            {"at": "03:12", "kind": "turn", "text": "Done with that part."}])
        classes = [c for c, _ in self.drawn(running)]
        self.assertNotIn("hc-live-wait", classes)

    def test_a_finished_run_lets_review_appear(self):
        st = {"scope": "global", "revision": "r1", "generated_at": "",
              "goals": [{"id": "g1", "title": "Ship it", "status": "active"}],
              "prompts": [],
              "agent_runs": {"g1": [{"status": "finished", "tasks": []}]}}
        got = self.run_js(
            "window.__hcPromptUI.setDetailForTest('g1', %s);"
            % json.dumps({"review": [{"state": "finished"}], "sections": {}}) +
            "var roots = window.__hcPromptUI.rootsFromState(%s);"
            % json.dumps(st) + "roots[0].artifact.finished;")
        self.assertTrue(got)

    def test_a_new_run_does_not_hide_a_finished_one(self):
        # Reading rows[0] hid every past artifact the moment a run started.
        st = {"scope": "global", "revision": "r1", "generated_at": "",
              "goals": [{"id": "g1", "title": "Ship it", "status": "active"}],
              "prompts": [], "agent_runs": {"g1": [
                  {"status": "running", "tasks": [], "files": []},
                  {"status": "finished", "tasks": [],
                   "files": [{"path": "a.py", "edits": 2}]}]}}
        got = self.run_js(
            "var roots = window.__hcPromptUI.rootsFromState(%s);"
            % json.dumps(st) + "roots[0].artifact;")
        self.assertTrue(got["finished"])
        self.assertEqual(["a.py"], [f["path"] for f in got["files"]])

    def test_changes_come_from_the_run_that_changed_something(self):
        st = {"scope": "global", "revision": "r1", "generated_at": "",
              "goals": [{"id": "g1", "title": "Ship it", "status": "active"}],
              "prompts": [], "agent_runs": {"g1": [
                  {"status": "finished", "tasks": [], "files": []},
                  {"status": "finished", "tasks": [],
                   "files": [{"path": "real.py", "edits": 6}]}]}}
        got = self.run_js(
            "var roots = window.__hcPromptUI.rootsFromState(%s);"
            % json.dumps(st) + "roots[0].artifact.files;")
        self.assertEqual(["real.py"], [f["path"] for f in got])

    def test_a_running_run_keeps_review_hidden(self):
        st = {"scope": "global", "revision": "r1", "generated_at": "",
              "goals": [{"id": "g1", "title": "Ship it", "status": "active"}],
              "prompts": [],
              "agent_runs": {"g1": [{"status": "running", "tasks": [],
                                     "files": []}]}}
        got = self.run_js(
            "window.__hcPromptUI.setDetailForTest('g1', %s);"
            % json.dumps({"review": [{"state": "running"}], "sections": {}}) +
            "var roots = window.__hcPromptUI.rootsFromState(%s);"
            % json.dumps(st) + "roots[0].artifact.finished;")
        self.assertFalse(got)

    def test_it_polls_whenever_the_anchor_is_on_screen(self):
        # The feed moved panes once already; binding the poll to a pane name
        # is what left it fetching for a pane it no longer draws into.
        asked = self.run_js(
            "store['hc-vault-ui-v1'] = JSON.stringify({ selId: 'g1' });"
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.watchRunFeed();"
            "var wait = Promise.resolve();"
            "for (var i = 0; i < 20; i += 1) wait = wait.then(function(){});"
            "wait.then(function () { return calls.filter(function (c) {"
            "  return String(c[0]).indexOf('/api/review') === 0; }).length; });")
        self.assertEqual(1, asked)

    def test_it_does_not_poll_with_no_anchor(self):
        asked = self.run_js(
            "store['hc-vault-ui-v1'] = JSON.stringify({ selId: 'g1' });"
            "window.__hcPromptUI.watchRunFeed();"
            "var wait = Promise.resolve();"
            "for (var i = 0; i < 20; i += 1) wait = wait.then(function(){});"
            "wait.then(function () { return calls.filter(function (c) {"
            "  return String(c[0]).indexOf('/api/review') === 0; }).length; });")
        self.assertEqual(0, asked)

    def test_a_quiet_run_says_it_may_be_waiting(self):
        rows = dict((c, t) for c, t in
                    self.drawn(dict(self.RUN, quiet_for="4 min")))
        self.assertEqual("nothing for 4 min — it may be waiting for you in "
                         "the terminal", rows["hc-live-idle"])

    def test_the_guess_is_dropped_once_it_is_known(self):
        # A real Stop tells us; then there is nothing left to infer.
        classes = [c for c, _ in self.drawn(
            dict(self.RUN, state="waiting", quiet_for="4 min"))]
        self.assertNotIn("hc-live-idle", classes)

    def test_the_log_is_its_own_box_like_changes(self):
        rows = self.drawn(self.RUN)
        self.assertEqual(["ACTIVITY"],
                         [t for c, t in rows if c == "hc-live-title"])
        css = self.run_js("window.__hcPromptUI.liveCss();")
        # CHANGES is a heading at 14px then a bordered box at 6px
        self.assertIn(".hc-live-title{margin-top:14px", css)
        self.assertIn(".hc-live-log{margin-top:6px", css)

    def test_nothing_pads_the_top_of_the_run_state(self):
        self.assertIn(".hc-live{margin-top:0}",
                      self.run_js("window.__hcPromptUI.liveCss();"))

    def test_the_subgoal_breadcrumb_is_gone(self):
        out = self.patched_bundle("out;")
        self.assertNotIn("{{ crumb }}", out)
        # the title, its status and the description stay
        self.assertIn("{{ selTitle }}", out)
        self.assertIn("{{ stBadge }}", out)
        self.assertIn("{{ descVal }}", out)

    def test_running_the_agent_is_the_only_way_to_get_a_plan(self):
        out = self.patched_bundle("out;")
        self.assertNotIn(">generate todos<", out)
        self.assertIn("{{ runAgent }}", out)

    def test_the_agent_section_is_named_for_status(self):
        out = self.patched_bundle("out;")
        self.assertIn("AGENT STATUS", out)
        self.assertNotIn("AGENT TODOS", out)

    def test_a_run_with_no_actions_gets_no_activity_heading(self):
        classes = [c for c, _ in self.drawn(dict(self.RUN, did=[]))]
        self.assertNotIn("hc-live-title", classes)

    def test_the_prompt_is_separated_like_the_other_sections(self):
        css = self.run_js("window.__hcPromptUI.paneCss();")
        self.assertIn(".hc-promptbox{margin-top:14px;padding-top:14px;"
                      "border-top:1px solid var(--bd,#e6e6e6)}", css)
        # The same heading treatment ADDITIONAL NOTES and AGENT STATUS use,
        # so it reads as their peer rather than a control inside AGENT.
        self.assertIn("font:600 9.5px 'Source Code Pro',monospace;"
                      "letter-spacing:1px;color:var(--mut,#575757)", css)

    def test_the_pane_styles_do_not_depend_on_an_analysis_running(self):
        # They lived in the banner's stylesheet, which is only injected while
        # something is being analyzed. On a settled vault — every vault, most
        # of the time — the prompt section rendered as a bare <details>: a
        # browser triangle, no divider, no heading. Adjusting the rules could
        # never fix that, because the sheet was not on the page.
        for rule in (".hc-promptbox", ".hc-promptsum", ".hc-rowbar"):
            self.assertNotIn(rule, self.run_js(
                "window.__hcPromptUI.bannerCss();"))
            self.assertIn(rule, self.run_js("window.__hcPromptUI.paneCss();"))

    def test_boot_puts_the_pane_styles_on_the_page(self):
        # A settled vault: nothing pending, so no banner is ever rendered.
        sheet = self.run_js(
            "var s = document.getElementById('hc-pane-style');"
            "s ? ['.hc-promptbox{', '.hc-prompt-addbtn{'].filter(function (r) {"
            "  return String(s.textContent).indexOf(r) >= 0; }).join(',') : '';",
            setup={"ok": True, "sv": 9, "storage": True, "analysis": "claude",
                   "done": True, "running": False,
                   "conversations": {"total": 3, "analyzed": 3, "pending": 0}})
        self.assertEqual(".hc-promptbox{,.hc-prompt-addbtn{", sheet)

    def test_the_log_scrolls_instead_of_growing(self):
        where = self.run_js(
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.renderLive('g1', %s);" % json.dumps([self.RUN]) +
            "JSON.stringify(pane.children.map(function (c) "
            "{ return c.className; }));")
        self.assertIn("hc-live-log", json.loads(where))
        css = self.run_js("window.__hcPromptUI.liveCss();")
        self.assertIn("max-height:320px;overflow-y:auto", css)

    def test_the_open_button_is_secondary_not_accent(self):
        # Opening a terminal is not the decision on this pane; approve is.
        css = self.run_js("window.__hcPromptUI.liveCss();")
        self.assertIn(".hc-live-open{flex:none;border:1px solid "
                      "var(--bd2,#d5d5d5);background:var(--hov,#f4f4f4);"
                      "color:var(--mut,#575757)", css)
        # the decision box keeps the accent; only the button loses it
        button = css[css.index(".hc-live-open{"):css.index(".hc-live-open:hover")]
        self.assertNotIn("--acc", button)
        self.assertIn("var(--accbg,#f5e2d9)", css)

    def test_a_run_with_no_session_offers_no_button(self):
        classes = [c for c, _ in self.drawn(self.RUN)]
        self.assertNotIn("hc-live-open", classes)

    def test_the_state_sits_in_the_card_and_the_log_outside_it(self):
        split = json.loads(self.run_js(
            "var top = document.createElement('div');"
            "top.className = 'hc-live';"
            "document.body.appendChild(top);"
            "var rest = document.createElement('div');"
            "rest.className = 'hc-live-rest';"
            "document.body.appendChild(rest);"
            "window.__hcPromptUI.renderLive('g1', %s);"
            % json.dumps([dict(self.RUN, attention="Migrate or not?")]) +
            "JSON.stringify([top.children.map(function (c) "
            "{ return c.className; }), rest.children.map(function (c) "
            "{ return c.className; })]);"))
        self.assertIn("hc-live-top", split[0])
        self.assertIn("hc-live-log", split[1])
        self.assertNotIn("hc-live-log", split[0])

    def test_the_question_box_scrolls_rather_than_growing(self):
        css = self.run_js("window.__hcPromptUI.liveCss();")
        self.assertIn(".hc-live-ask{margin:0 0 8px;max-height:220px;"
                      "overflow-y:auto", css)

    def test_the_split_target_does_not_stack_between_renders(self):
        # It is a second host; leaving it alone would append on every poll.
        counts = json.loads(self.run_js(
            "var top = document.createElement('div');"
            "top.className = 'hc-live';"
            "document.body.appendChild(top);"
            "var rest = document.createElement('div');"
            "rest.className = 'hc-live-rest';"
            "document.body.appendChild(rest);"
            "window.__hcPromptUI.renderLive('g1', %s);" % json.dumps([self.RUN]) +
            "var once = rest.children.length;"
            "window.__hcPromptUI.renderLive('g2', %s);" % json.dumps([self.RUN]) +
            "JSON.stringify([once, rest.children.length]);"))
        self.assertEqual(counts[0], counts[1])
        self.assertGreater(counts[0], 0)

    def test_a_waiting_run_is_still_marked_in_the_log(self):
        waiting = dict(self.RUN, state="waiting", attention="Migrate or not?",
                       did=[{"at": "03:12", "kind": "turn",
                             "text": "Migrate or not?"}])
        rows = dict((c, t) for c, t in self.drawn(waiting))
        self.assertIn("waiting for your decision", rows["hc-live-wait"])

    def test_the_feed_is_drawn_on_the_review_pane(self):
        # Placement within the pane is pinned by the ordering test; here just
        # that the target exists inside REVIEW and nowhere else.
        out = self.patched_bundle("out;")
        pane = out.index('value="{{ showArt }}"')
        self.assertGreater(out.index('<div class="hc-live"></div>'), pane)
        self.assertEqual(1, out.count('class="hc-live"'))

    def test_the_message_is_the_accented_box_it_used_to_have(self):
        # It is Claude's own words in a pane otherwise made of metadata, and
        # when the run is waiting it is the thing being asked.
        out = self.patched_bundle("out;")
        at = out.index("{{ artSummary }}")
        box = out[out.rindex("<div", 0, at):at]
        self.assertIn("border:1px solid var(--acc)", box)
        self.assertIn("background:var(--accbg)", box)
        # Accent frames it; the words stay body text, since the whole point
        # is that it is long enough to read.
        self.assertIn("color:var(--dtxt)", box)
        # A long write-up must not push the decision off the card.
        self.assertIn("max-height:230px;overflow-y:auto", box)
        self.assertIn("white-space:pre-wrap", box)

    def test_the_corner_holds_the_way_in_not_a_restated_status(self):
        # PENDING REVIEW said what "Completed \u00b7 1h" already says.
        out = self.patched_bundle("out;")
        self.assertNotIn("{{ artStatusLab }}", out)
        header = out.index(">ARTIFACT</span>")
        slot = out.index('<div class="hc-live-open-slot"></div>')
        self.assertLess(header, slot)
        self.assertLess(slot, out.index("{{ artSummary }}"))

    def test_created_sits_opposite_the_decision_on_the_cards_last_line(self):
        out = self.patched_bundle("out;")
        row = out.index("justify-content:space-between;align-items:center;"
                        "gap:16px;flex-wrap:wrap")
        created = out.index("{{ artWhen }}")
        decide = out.index("request revisions")
        end = out.index('<div class="hc-live-rest">')
        self.assertLess(row, created)
        self.assertLess(created, decide)
        self.assertLess(decide, end)
        # The stamp is not inside the conditional: withdrawing the buttons
        # while a revision is being written must not take the date with them.
        self.assertLess(created, out.index("{{ revClosed }}", row))

    def test_the_card_holds_the_run_and_activity_stands_apart(self):
        out = self.patched_bundle("out;")
        card = out.index("background:var(--panel2);padding:9px 12px")
        state = out.index('<div class="hc-live"></div>')
        summary = out.index("{{ artSummary }}")
        created = out.index("{{ artWhen }}")
        decide = out.index("request revisions")
        log = out.index('<div class="hc-live-rest"></div>')
        changes = out.index(">CHANGES</div>")
        # state, message, created and the decision are all one card
        self.assertLess(card, state)
        self.assertLess(state, summary)
        self.assertLess(summary, created)
        self.assertLess(created, decide)
        # ACTIVITY is its own section, like CHANGES
        self.assertLess(decide, log)
        self.assertLess(log, changes)
        self.assertEqual(1, out.count("request revisions"))
        self.assertEqual(1, out.count('class="hc-live"'))

    def test_the_message_is_not_printed_twice_in_one_card(self):
        # The card's summary is the same text the question box carried.
        classes = [c for c, _ in
                   self.drawn(dict(self.RUN, attention="Migrate or not?"))]
        self.assertNotIn("hc-live-ask", classes)

    def test_the_artifact_box_keeps_created_and_drops_branch(self):
        out = self.patched_bundle("out;")
        self.assertIn("{{ artWhen }}", out)
        self.assertNotIn("{{ artBranch }}", out)
        # the AGENT pane has its own branch line; only the artifact's went
        self.assertIn("{{ agentBranch }}", out)

    def test_agent_reads_name_then_prompt_then_notes_then_run(self):
        out = self.patched_bundle("out;")
        self.assertLess(out.index(">AGENT</div>"), out.index("hc-promptbox"))
        self.assertLess(out.index("hc-promptbox"), out.index("ADDITIONAL NOTES"))
        self.assertLess(out.index("ADDITIONAL NOTES"), out.index("AGENT STATUS"))
        self.assertLess(out.index("AGENT STATUS"), out.index("{{ runAgent }}"))

    def test_the_notes_box_moved_to_the_agent_pane(self):
        out = self.patched_bundle("out;")
        self.assertIn("showNotes: !!sel && paneTab === 'agent'", out)

    def test_the_pane_says_what_it_is_for(self):
        self.assertIn("Run Claude Code on this goal with the self-contained "
                      "context Vault has assembled. Progress appears in "
                      "REVIEW.", self.patched_bundle("out;"))

    def test_the_draft_does_not_restate_the_goal_title(self):
        out = self.patched_bundle("out;")
        self.assertNotIn("I am working on the goal:", out)
        self.assertNotIn("Within the main goal", out)

    def test_the_notes_box_invites_the_users_own_thoughts(self):
        out = self.patched_bundle("out;")
        self.assertIn("Add any other thoughts you would like the agent to "
                      "know...", out)
        self.assertNotIn("Plan in markdown", out)

    def test_the_prompt_heading_is_the_disclosure_itself(self):
        # The disclosure replaced the section heading; keeping both printed
        # RECOMMENDED PROMPT twice, once inside the thing it labels.
        out = self.patched_bundle("out;")
        self.assertIn('<summary class="hc-promptsum">RECOMMENDED PROMPT'
                      "</summary>", out)
        self.assertEqual(1, out.count("RECOMMENDED PROMPT"))

    def test_review_opens_as_soon_as_a_run_exists(self):
        # It carries the live feed now, so gating it on completion would hide
        # the run while it is the thing worth watching.
        out = self.patched_bundle("out;")
        self.assertIn('<sc-if value="{{ showReviewTab }}"', out)
        self.assertIn("showReviewTab: !!art,", out)

    def test_the_prompt_is_a_dropdown_on_the_agent_pane(self):
        out = self.patched_bundle("out;")
        self.assertIn("showPrompt: !!sel && paneTab === 'agent'", out)
        self.assertIn('<details class="hc-promptbox">'
                      '<summary class="hc-promptsum">RECOMMENDED PROMPT'
                      "</summary>", out)
        # collapsed by default, and it is the same editable prompt as before
        self.assertNotIn('<details class="hc-promptbox" open>', out)
        self.assertIn("{{ draft }}", out)

    def test_prompt_is_no_longer_its_own_tab(self):
        out = self.patched_bundle("out;")
        self.assertIn("prompt folded into agent", out)
        self.assertNotIn('sc-camel-on-click="{{ tabPrompt }}"', out)

    def test_a_started_session_with_no_steps_reads_plainly(self):
        out = self.patched_bundle("out;")
        # With no tasks captured, steps is empty and the line is just the state.
        self.assertIn("return 'running now' + steps", out)
        self.assertIn("td.length ? ' \u00b7 ' + dn + ' of ' + td.length", out)
        self.assertNotIn("waiting for its first step", out)

    def test_the_status_line_names_the_state_it_is_actually_in(self):
        # It only ever asked "is it running?", so a goal that had never run
        # announced "finished - output ready to review".
        out = self.patched_bundle("out;")
        self.assertNotIn("output ready to review", out)
        for state, said in (
                ("idle", "nothing has run on this goal yet"),
                ("proposed", "the steps below are a suggestion"),
                ("waiting", "press Enter there to start"),
                ("running", "running now"),
                ("done", "the result is in REVIEW")):
            self.assertIn(said, out, state)
        self.assertIn("if (a.awaiting) return 'waiting for your reply", out)

    def test_the_users_own_words_are_on_the_context_pane(self):
        # Every other panel is derived from them. They were only reachable
        # through the PROMPT tab, so removing that tab took them off the
        # screen entirely — histRows was still being computed with nothing
        # rendering it.
        out = self.patched_bundle("out;")
        self.assertIn("RELATED PROMPTS", out)
        self.assertIn('<sc-for list="{{ histRows }}" as="hr"', out)
        self.assertIn("{{ hr.text }}", out)
        self.assertIn("{{ hr.when }}", out)
        self.assertIn("{{ histEmpty }}", out)

    def test_the_add_button_is_styled_by_the_sheet_injected_at_boot(self):
        # On the dialog sheet it would only be styled once a dialog had been
        # opened, which is how a section once rendered unstyled for weeks.
        pane = self.run_js("window.__hcPromptUI.paneCss();")
        dialog = self.run_js("window.__hcPromptUI.dialogCss();")
        self.assertIn(".hc-prompt-addbtn{", pane)
        self.assertNotIn(".hc-prompt-addbtn{", dialog)

    def test_the_pane_reads_in_the_order_the_work_is_approached(self):
        # What finishing means, where it sits, what it can read, what is in
        # the way, what is done. Decisions follow what was built: both are
        # settled, and neither belongs between the goal and its blockers.
        out = self.patched_bundle("out;")
        order = [">OBJECTIVE</span>", "WHERE THIS SITS", ">CODE CONTEXT</span>",
                 ">DOCUMENT CONTEXT</span>", "RELATED PROMPTS",
                 "BLOCKERS &amp; OPEN QUESTIONS", ">ALREADY BUILT</span>",
                 ">DECISIONS</span>"]
        at = [out.index(name) for name in order]
        self.assertEqual(sorted(at), at, order)
        for name in order:
            self.assertEqual(1, out.count(name), name)

    def test_the_pane_shows_where_the_goal_sits_in_the_tree(self):
        out = self.patched_bundle("out;")
        self.assertIn('<sc-for list="{{ ctxTrail }}" as="tr"', out)
        self.assertIn("{{ tr.title }}", out)
        self.assertIn("ctxTrail: (trail || []).map((n, i) => ({", out)
        # the focused goal is marked, and depth is real indentation
        self.assertIn("pad: (i * 14) + 'px'", out)
        self.assertIn("c: n === sel ? 'var(--ink)' : 'var(--fnt)'", out)

    def test_nothing_points_at_the_row_you_are_already_on(self):
        # The focused goal is the one the inspector is open on; an arrow
        # saying so is a label for something the reader can already see. It
        # carries its weight in colour instead.
        out = self.patched_bundle("out;")
        self.assertNotIn("this one", out)
        draft = out[out.index("const composeDraft"):out.index("const baseDraft")]
        self.assertIn("Where this sits:", draft)

    def test_the_recommended_prompt_follows_the_pane_it_is_built_from(self):
        out = self.patched_bundle("out;")
        draft = out[out.index("const composeDraft"):out.index("const baseDraft")]
        at = [draft.index(name) for name in
              ("'Objective:", "'Where this sits:", "'Code context:",
               "'Document context:", "'Related prompts, in my own words:",
               "'Blockers & open questions:", "'Already built:",
               "'Established decisions:")]
        self.assertEqual(sorted(at), at)

    def test_the_prompt_quotes_the_words_the_pane_lists(self):
        out = self.patched_bundle("out;")
        draft = out[out.index("const composeDraft"):out.index("const baseDraft")]
        self.assertIn("const said = (sel.prompts || []).slice().reverse();",
                      draft)
        # newest first, quoted, and flattened so a pasted block stays one line
        self.assertIn("String(q.text || '').replace(/\\s+/g, ' ').trim()",
                      draft)

    def test_the_recommended_prompt_ends_by_asking_for_the_work(self):
        out = self.patched_bundle("out;")
        draft = out[out.index("const composeDraft"):out.index("const baseDraft")]
        self.assertIn("blocks.push(isSub ? 'Implement this subgoal for me.' : "
                      "'Implement this goal for me.');", draft)
        # it is the last thing said, after the context it is asking about
        self.assertLess(draft.index("'Established decisions:"),
                        draft.index("Implement this subgoal for me."))

    def test_the_prompt_rows_carry_no_controls(self):
        # The list is the record of what was said, not a place to act.
        out = self.patched_bundle("out;")
        self.assertNotIn("{{ hr.copy }}", out)
        self.assertNotIn("{{ hr.del }}", out)
        self.assertNotIn("{{ hr.use }}", out)

    def test_each_prompt_names_the_conversation_it_came_from(self):
        # A quote without a source cannot be checked.
        out = self.patched_bundle("out;")
        self.assertIn("{{ hr.conv }}", out)
        self.assertIn("conv: p.conv ? 'conversation ' + p.conv : ''", out)
        rows = self.roots()[0]["prompts"]
        self.assertTrue(all(r["conv"] for r in rows), rows)

    def test_the_conversation_is_the_prefix_the_evidence_ids_use(self):
        state = json.loads(json.dumps(STATE))
        state["prompts"][0]["session_id"] = "879da390-1c4e-4d0a-9f11-abc"
        self.assertEqual("879da390", self.roots(state)[0]["prompts"][0]["conv"])

    def test_a_prompt_with_no_session_claims_no_conversation(self):
        state = json.loads(json.dumps(STATE))
        state["prompts"][0].pop("session_id", None)
        self.assertEqual("", self.roots(state)[0]["prompts"][0]["conv"])

    def test_the_words_come_before_what_stands_in_their_way(self):
        # They are context to read with the sources, not a footnote: what was
        # asked for lands before the blockers and the settled sections.
        out = self.patched_bundle("out;")
        self.assertLess(out.index(">DOCUMENT CONTEXT</span>"),
                        out.index("RELATED PROMPTS"))
        self.assertLess(out.index("RELATED PROMPTS"),
                        out.index("BLOCKERS &amp; OPEN QUESTIONS"))
        self.assertEqual(1, out.count("RELATED PROMPTS"))

    def test_a_long_history_scrolls_rather_than_pushing_the_pane_down(self):
        out = self.patched_bundle("out;")
        at = out.index('<sc-for list="{{ histRows }}"')
        box = out[out.rindex("<div", 0, at):at]
        self.assertIn("max-height:420px;overflow-y:auto", box)

    def test_the_stamp_is_the_date_that_was_recorded_not_a_clock(self):
        # created_at is a date. Every prompt ever captured showed 5:00 PM,
        # which was the formatter inventing a time nobody recorded.
        out = self.patched_bundle("out;")
        at = out.index("when: (() => { const d2 = new Date(p.ts);")
        expr = out[at:out.index("})(),", at)]
        self.assertIn("day: 'numeric', year: 'numeric'", expr)
        self.assertNotIn("toLocaleTimeString", expr)

    def test_a_bare_date_is_read_in_the_readers_own_timezone(self):
        # Date.parse("2026-08-04") is midnight UTC, which west of Greenwich
        # renders as the 3rd -- the stamp was a day off, not just imprecise.
        state = json.loads(json.dumps(STATE))
        state["prompts"][0]["created_at"] = "2026-08-04"
        ts = self.roots(state)[0]["prompts"][0]["ts"]
        parts = json.loads(self.run_js(
            "var d = new Date(%d);" % ts +
            "JSON.stringify([d.getFullYear(), d.getMonth() + 1, d.getDate()]);"))
        self.assertEqual([2026, 8, 4], parts)

    def test_the_reader_can_attach_one_of_their_own_prompts(self):
        # Which of your words belong to which goal is a judgement the
        # inference only guesses at, so the reader gets to say.
        out = self.patched_bundle("out;")
        self.assertIn('<span class="hc-prompt-add"></span>', out)
        rendered = self.run_js(
            "var slot = document.createElement('span');"
            "slot.className = 'hc-prompt-add';"
            "document.body.appendChild(slot);"
            "window.__hcPromptUI.renderPromptAdd();"
            "JSON.stringify([slot.children.length,"
            " slot.children[0] && slot.children[0].textContent,"
            " slot.children[0] && slot.children[0].className]);")
        self.assertEqual([1, "+ add a prompt", "hc-prompt-addbtn"],
                         json.loads(rendered))

    def test_the_button_finds_its_place_without_the_anchor(self):
        # The anchor is an empty span in a template the artifact re-renders
        # from its own state, and an empty element is exactly what a renderer
        # is free to drop. The heading is text the pane has to draw.
        got = json.loads(self.run_js(
            "var row = document.createElement('div');"
            "document.body.appendChild(row);"
            "var head = document.createElement('span');"
            "head.textContent = 'RELATED PROMPTS';"
            "row.appendChild(head);"
            "var found = window.__hcPromptUI.promptAddSlot() === row;"
            "var drew = window.__hcPromptUI.renderPromptAdd();"
            "JSON.stringify([found, drew, row.children.map(function (c) "
            "{ return c.className || c.textContent; })]);"))
        self.assertEqual([True, True,
                          ["RELATED PROMPTS", "hc-prompt-addbtn"]], got)

    def test_the_anchor_still_wins_when_it_survives(self):
        got = self.run_js(
            "var row = document.createElement('div');"
            "document.body.appendChild(row);"
            "var head = document.createElement('span');"
            "head.textContent = 'RELATED PROMPTS';"
            "row.appendChild(head);"
            "var slot = document.createElement('span');"
            "slot.className = 'hc-prompt-add';"
            "document.body.appendChild(slot);"
            "window.__hcPromptUI.promptAddSlot() === slot;")
        self.assertTrue(got)

    def test_the_click_survives_the_pane_being_redrawn_under_it(self):
        # The artifact re-renders this pane from its own state every time a
        # poll lands, which destroys whatever button was there. A handler
        # bound to that node dies with it, and the click goes nowhere — which
        # is exactly what "clicking does nothing" looks like.
        opened = self.run_js(
            "localStorage.setItem('hc-vault-ui-v1',"
            "  JSON.stringify({ selId: 'g1' }));"
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "function pane() {"
            "  var host = document.documentElement;"
            "  while (host.children.length) host.removeChild(host.children[0]);"
            "  var row = document.createElement('div');"
            "  host.appendChild(row);"
            "  var head = document.createElement('span');"
            "  head.textContent = 'RELATED PROMPTS';"
            "  row.appendChild(head);"
            "  window.__hcPromptUI.renderPromptAdd();"
            "  return row;"
            "}"
            "var first = pane().children[1];"
            "var second = pane().children[1];"     # the redraw
            + self.CLICK +
            "click(second);"
            "JSON.stringify([first !== second, !!document.querySelector('.hc-ask')]);")
        self.assertEqual([True, True], json.loads(opened))

    def test_a_second_click_does_not_stack_a_second_picker(self):
        count = self.run_js(
            "localStorage.setItem('hc-vault-ui-v1',"
            "  JSON.stringify({ selId: 'g1' }));"
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "var slot = document.createElement('span');"
            "slot.className = 'hc-prompt-add';"
            "document.body.appendChild(slot);"
            "window.__hcPromptUI.renderPromptAdd();"
            + self.CLICK +
            "click(slot.children[0]); click(slot.children[0]);"
            "made.filter(function (e) { return e.className === 'hc-ask' "
            "  && e.parentNode; }).length;")
        self.assertEqual(1, count)

    def test_a_picker_that_cannot_draw_says_so_instead_of_nothing(self):
        # It runs inside a promise executor, so a throw rejects a promise the
        # caller only listens to for a chosen id. Silence is the worst
        # possible report for a button.
        said = self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "window.__hcPromptUI.pickPrompt('g1');"
            "var f = document.querySelector('.hc-ask-input');"
            "f.value = { toLowerCase: null };"
            "var list = document.querySelector('.hc-pick-list');"
            "while (list.children.length) list.removeChild(list.children[0]);"
            "f.oninput ? (function () { try { f.oninput(); } catch (e) "
            "  { return 'threw'; } return 'quiet'; })() : 'no handler';")
        # oninput is not the guarded path; the guarded one is the first draw.
        self.assertIn(said, ("threw", "quiet"))
        src = BRIDGE.read_text()
        self.assertIn("Could not list your prompts: ", src)
        self.assertIn('button.textContent = "could not open it: " + error;', src)
        self.assertIn("var needle = str(filter.value).trim().toLowerCase();", src)

    def test_the_button_is_not_stacked_by_the_poll_that_draws_it(self):
        count = self.run_js(
            "var slot = document.createElement('span');"
            "slot.className = 'hc-prompt-add';"
            "document.body.appendChild(slot);"
            "window.__hcPromptUI.renderPromptAdd();"
            "window.__hcPromptUI.renderPromptAdd();"
            "window.__hcPromptUI.renderPromptAdd();"
            "slot.children.length;")
        self.assertEqual(1, count)

    PICK_STATE = None                       # built in the test, see below

    CLICK = ("function click(node) { var e = { target: node,"
             "  preventDefault: function () {}, stopPropagation: function () {} };"
             "  listeners.filter(function (l) { return l[0] === 'click'; })"
             "    .forEach(function (l) { l[1](e); }); }")

    def pick_state(self):
        state = json.loads(json.dumps(STATE))
        state["prompts"].append(
            {"id": "a#2", "role": "user", "text": "and record the audio",
             "created_at": "2026-08-05"})
        state["prompts"].append(
            {"id": "a#3", "role": "assistant", "text": "sure, doing that",
             "created_at": "2026-08-05"})
        return state

    def test_the_picker_offers_only_prompts_not_already_on_the_goal(self):
        rows = json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "window.__hcPromptUI.pickPrompt('g1');"
            "var list = document.querySelector('.hc-pick-list');"
            "JSON.stringify(list.children.map(function (r) "
            "{ return r.children[1] ? r.children[1].textContent : "
            "r.textContent; }));"))
        # a#1 is already on g1 and a#3 is Claude, not the reader
        self.assertEqual(["and record the audio"], rows)

    def test_the_picker_says_so_when_there_is_nothing_left_to_add(self):
        rows = json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(STATE) +
            "window.__hcPromptUI.pickPrompt('g1');"
            "var list = document.querySelector('.hc-pick-list');"
            "JSON.stringify(list.children.map(function (r) "
            "{ return r.textContent; }));"))
        self.assertEqual(["Every prompt on record is already on this goal."],
                         rows)

    def test_picking_a_prompt_attaches_it_on_the_server(self):
        posted = json.loads(self.run_js(
            "localStorage.setItem('hc-vault-ui-v1',"
            "  JSON.stringify({ selId: 'g1' }));"
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "var slot = document.createElement('span');"
            "slot.className = 'hc-prompt-add';"
            "document.body.appendChild(slot);"
            "window.__hcPromptUI.renderPromptAdd();"
            + self.CLICK +
            "click(slot.children[0]);"
            "document.querySelector('.hc-pick-list').children[0].onclick();"
            # the click resolves a promise; let its handlers run
            "Promise.resolve().then(function () {}).then(function () {})"
            "  .then(function () { return JSON.stringify("
            "    calls.map(function (c) { return c[1]; }).filter(Boolean)); });"))
        attach = [c for c in posted if c.get("op") == "attach_prompt"]
        self.assertEqual([{"op": "attach_prompt", "goal_id": "g1",
                           "prompt_id": "a#2"}], attach)

    def test_opening_the_picker_writes_nothing(self):
        # Browsing your own history must not edit the goal.
        posted = json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "window.__hcPromptUI.pickPrompt('g1');"
            "JSON.stringify(calls.map(function (c) { return c[1]; })"
            "  .filter(Boolean));"))
        self.assertEqual([], posted)

    def test_a_capped_list_says_what_it_left_out(self):
        # A silently truncated list reads as the whole record. The cap is set
        # high enough that no real vault meets it, and low enough that a
        # pathological one cannot lock the tab building rows.
        src = BRIDGE.read_text()
        self.assertIn("var PICK_LIMIT = 2000;", src)
        self.assertIn("Showing the newest ", src)

    def test_the_picker_counts_what_it_is_offering(self):
        # "All of them" is only a claim unless the reader can check it.
        said = self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "window.__hcPromptUI.pickPrompt('g1');"
            "document.querySelector('.hc-pick-count').textContent;")
        self.assertEqual("1 prompts of yours are not on this goal yet", said)

    def test_filtering_says_how_much_of_the_whole_is_left(self):
        said = self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "window.__hcPromptUI.pickPrompt('g1');"
            "var f = document.querySelector('.hc-ask-input');"
            "f.value = 'zzzz'; f.oninput();"
            "document.querySelector('.hc-pick-count').textContent;")
        self.assertEqual("0 of 1 match", said)

    def test_the_modal_grows_and_the_list_inside_it_scrolls(self):
        css = self.run_js("window.__hcPromptUI.dialogCss();")
        # The box is capped against the viewport and the list takes the slack,
        # so the filter and the cancel button stay reachable at any length.
        self.assertIn("max-height:min(84vh,760px)", css)
        self.assertIn(".hc-pick-list{flex:1;min-height:0", css)
        self.assertIn("overflow-y:auto;overscroll-behavior:contain", css)

    def test_the_pane_list_scrolls_without_taking_the_page_with_it(self):
        out = self.patched_bundle("out;")
        at = out.index('<sc-for list="{{ histRows }}"')
        box = out[out.rindex("<div", 0, at):at]
        self.assertIn("max-height:420px;overflow-y:auto;"
                      "overscroll-behavior:contain", box)

    def test_the_prompts_shown_are_the_ones_the_server_recorded(self):
        state = json.loads(json.dumps(STATE))
        rows = self.roots(state)[0]["prompts"]
        self.assertEqual([p["text"] for p in state["prompts"]
                          if p["id"] in state["goals"][0]["prompt_ids"]],
                         [r["text"] for r in rows])

    def test_a_goal_nobody_asked_for_carries_no_prompts(self):
        state = json.loads(json.dumps(STATE))
        state["goals"][0]["prompt_ids"] = []
        self.assertEqual([], self.roots(state)[0]["prompts"])

    def test_the_pane_follows_the_selection_back_to_context(self):
        # Every pane is about the selected goal. Carrying AGENT or REVIEW over
        # to the next goal lands on a tab that may not be offered for it.
        out = self.patched_bundle("out;")
        self.assertIn("selId: n.id, paneTab: 'context'", out)
        self.assertIn("selId: ids[nx], editId: null, paneTab: 'context'", out)
        self.assertIn("selId: curConv.goalId, editId: null, paneTab: 'context'",
                      out)

    def test_starting_a_run_still_opens_the_pane_that_shows_it(self):
        # The one move away from CONTEXT that the reader asked for.
        out = self.patched_bundle("out;")
        self.assertIn("if (started) this.set(() => ({ paneTab: 'artifact' }));",
                      out)

    def test_the_status_carries_no_invented_progress_bar(self):
        # A percentage over a step count the agent invents as it goes is not
        # progress; on a finished run it drew an empty tan track.
        out = self.patched_bundle("out;")
        self.assertNotIn("width:{{ agentPct }}", out)

    def test_the_status_offers_no_control_that_does_nothing(self):
        # "stop" never stopped the session, and "clear" only blanked local
        # state until the next poll refilled it.
        out = self.patched_bundle("out;")
        self.assertNotIn("{{ agentActionLabel }}", out)
        self.assertNotIn("{{ agentAction }}", out)

    def test_an_untouched_goal_gets_no_empty_status_heading(self):
        out = self.patched_bundle("out;")
        self.assertIn("agentShow: !!(sel && sel.agent && "
                      "(sel.agent.status !== 'idle' || "
                      "(sel.agent.todos || []).length)),", out)

    def test_review_no_longer_repeats_the_log(self):
        out = self.patched_bundle("out;")
        for gone in ("revHeadline", "NEEDS YOUR DECISION",
                     "VERIFIED BY RUNNING", "reopen this session"):
            self.assertNotIn(gone, out)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class EmptyVaultTests(BridgeTestCase):
    """An empty vault is a real state; the artifact must not invent goals."""

    EMPTY = {"scope": "global", "provider": "claude", "revision": "r0",
             "goals": [], "prompts": [], "agent_runs": {}}

    def test_the_seed_declares_the_version_the_gate_trusts(self):
        # Without this the artifact treats an empty list as "nothing saved" and
        # falls back to its sample tree, which the sync then persists as the
        # user's own goals.
        payload = self.run_js("JSON.parse(store['hc-vault-ui-v1']);",
                              state=self.EMPTY)
        self.assertGreaterEqual(payload["v"], 7)
        self.assertEqual([], payload["goals"])

    def test_the_sample_tree_cannot_load_over_an_empty_vault(self):
        out = self.patched_bundle("out;")
        self.assertIn("(saved.goals.length || saved.v >= 7)", out)

    def test_a_seeded_tree_still_loads(self):
        payload = self.run_js("JSON.parse(store['hc-vault-ui-v1']);")
        self.assertEqual(1, len(payload["goals"]))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class AnalysisBannerTests(BridgeTestCase):
    """Work happening outside the page has to be visible inside it."""

    RUNNING = {"ok": True, "sv": 9, "storage": True, "analysis": "claude",
               "done": True, "running": True, "phase": "extracting",
               "conversations": {"total": 89, "analyzed": 12, "pending": 77},
               "current": {"id": "aaaaaaaa", "title": "Debugging the overlay"},
               "convos": []}

    def banner(self, setup):
        return self.run_js(
            "window.__hcSetupTest = %s;" % json.dumps(setup) +
            "window.__hcPromptUI.setSetupForTest(window.__hcSetupTest);"
            "window.__hcPromptUI.renderBanner();"
            "var b = made.filter(function (e) {"
            "  return e.className === 'hc-banner'; })[0];"
            "b ? b.children.map(function (c) { return c.textContent; }) : null;")

    SYNTH = {"ok": True, "sv": 9, "storage": True, "analysis": "claude",
             "done": True, "running": True, "phase": "synthesizing",
             "conversations": {"total": 3, "analyzed": 3, "pending": 0}}

    def banner_with(self, setup, spinner):
        return self.run_js(
            ("var host = document.documentElement;"
             "var s = document.createElement('span');"
             "s.textContent = 'Building Goals';"
             "host.appendChild(s);" if spinner else "") +
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(setup) +
            "window.__hcPromptUI.renderBanner();"
            "!!made.filter(function (e) { return e.className === 'hc-banner'"
            "  && e.parentNode; })[0];")

    def test_the_banner_stands_down_only_while_the_panel_reports(self):
        # The panel draws a spinner in the middle of the tree while the tree
        # is being built. A banner saying the same thing directly above it is
        # not twice the information.
        self.assertFalse(self.banner_with(self.SYNTH, spinner=True))

    def test_the_page_is_never_silent_about_an_analysis(self):
        # The spinner lives in state the artifact keeps in memory, so a reload
        # takes it away while the tree is still being built. Inferring the
        # phase from counts instead would silence the banner too, and then
        # nothing on screen would say anything was happening.
        self.assertTrue(self.banner_with(self.SYNTH, spinner=False))

    def test_the_analysis_moves_to_the_goals_page_when_the_reading_ends(self):
        # It used to wait for the whole analysis, synthesis included, so the
        # tree build happened on the conversations page with nothing to see.
        out = self.patched_bundle("out;")
        run = out[out.index("  startAnalysis()"):out.index("this._anT = setInterval(tick, 2000)")]
        self.assertIn("if (!total || done < total) {", run)
        self.assertIn("this.set(() => ({ page: 'goals', convSel: null,", run)
        # and only once: switching every poll would pin the reader there
        self.assertIn("if (!this._anSwitched) {", run)
        # the marker clears only when nothing is running any more
        self.assertIn("if (!s.running) {", run)

    def test_the_goals_phase_is_what_the_spinner_is_gated_on(self):
        out = self.patched_bundle("out;")
        self.assertIn("anGoals: !!(anx && anx.phase === 'goals')", out)
        self.assertIn("{{ anSpin }}", out)
        self.assertIn("Building Goals", out)
        # and the tree list steps aside for it
        self.assertIn("treeListDisp: (anx && anx.phase === 'goals') "
                      "? 'none' : 'block'", out)

    def test_there_is_no_blinking_dot(self):
        css = self.run_js("window.__hcPromptUI.bannerCss();")
        self.assertNotIn("hc-banner-dot", css)
        self.assertNotIn("hc-pulse", css)

    def test_the_row_indicator_sweeps_instead_of_claiming_a_percentage(self):
        # A conversation reports no progress of its own, so a filled bar
        # would be a number the vault does not have.
        out = self.patched_bundle("out;")
        self.assertIn("hc-rowbar", out)
        self.assertNotIn("hc-rowdots", out)
        self.assertNotIn("width:{{ cv.barW }}", out)
        # It marks a row being worked on, which outlives any banner, so it
        # rides on the sheet that is always injected.
        css = self.run_js("window.__hcPromptUI.paneCss();")
        self.assertIn("animation:hc-sweep 2.8s ease-in-out infinite", css)
        self.assertIn("@keyframes hc-sweep{0%{left:-45%}100%{left:100%}}", css)
        self.assertIn("prefers-reduced-motion", css)

    def test_only_the_conversation_being_read_is_animated(self):
        # Every unfinished row used to carry the same animation, which said
        # the machine was busy on all of them at once.
        out = self.patched_bundle("out;")
        self.assertIn("barShow: !!(ph && p > 0 && p < 100)", out)
        self.assertIn("qShow: !!(ph && p <= 0)", out)

    def test_every_row_says_which_of_the_three_states_it_is_in(self):
        out = self.patched_bundle("out;")
        for said in ("{{ cv.st }}", ">in queue<", ">analyzing"):
            self.assertIn(said, out)
        # analyzed / analyzing / queued, in that one column
        cell = out[out.index("width:118px"):out.index("{{ cv.meta }}")]
        self.assertLess(cell.index("{{ cv.stShow }}"), cell.index("{{ cv.qShow }}"))
        self.assertLess(cell.index("{{ cv.qShow }}"), cell.index("{{ cv.barShow }}"))

    def test_the_three_states_line_up_in_one_column(self):
        # Different widths per state would leave the list ragged down the
        # side, which is the edge the reader scans.
        out = self.patched_bundle("out;")
        self.assertIn('<span style="display:inline-flex;width:118px;'
                      'justify-content:flex-end;align-items:center">', out)
        self.assertNotIn("width:78px", out)
        self.assertEqual(1, out.count("width:118px"))

    def test_it_says_why_the_reading_is_happening(self):
        parts = self.banner(self.RUNNING)
        joined = " ".join(parts)
        self.assertIn("Reading your conversations to work out what you are "
                      "building", joined)
        self.assertIn("reading: Debugging the overlay", joined)

    def test_it_reports_how_many_run_at_once(self):
        parts = self.banner(dict(self.RUNNING, inflight=8))
        self.assertIn("8 at a time", " ".join(parts))

    def test_one_at_a_time_is_not_worth_saying(self):
        parts = self.banner(dict(self.RUNNING, inflight=1))
        self.assertNotIn("at a time", " ".join(parts))

    def test_it_names_what_is_running_and_which_conversation(self):
        parts = self.banner(self.RUNNING)
        self.assertIn("Reading your conversations to work out what you are "
                      "building", parts)
        self.assertIn("reading: Debugging the overlay", parts)
        self.assertIn("12 of 89", parts)

    def test_it_says_when_it_is_building_goals_instead(self):
        setup = dict(self.RUNNING, phase="synthesizing")
        self.assertIn("Working out your goals from what it read",
                      self.banner(setup))

    def test_it_explains_the_wait_when_no_conversation_is_named(self):
        setup = dict(self.RUNNING, current=None)
        self.assertIn("your goals appear here when this finishes",
                      self.banner(setup))

    def test_it_disappears_when_nothing_is_running(self):
        setup = dict(self.RUNNING, running=False, current=None,
                     conversations={"total": 89, "analyzed": 89, "pending": 0})
        self.assertIsNone(self.banner(setup))

    def test_it_appears_without_anything_priming_the_state_first(self):
        # The watcher used to skip its own first fetch: "is anything running"
        # is false until something is fetched, so nothing ever was.
        out = self.run_js(
            "window.__hcPromptUI.watchAnalysis().then(function () {"
            "  return made.filter(function (e) {"
            "    return e.className === 'hc-banner'; }).length; });",
            setup=self.RUNNING)
        self.assertEqual(1, out)

    def test_it_spans_the_panel_by_sharing_its_container(self):
        # A child of the header would take the header's width; the banner has
        # to be the panel's sibling to line up with it.
        order = json.loads(self.run_js(
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(self.RUNNING) +
            "window.__hcPromptUI.renderBanner();"
            "JSON.stringify(app.children.map(function (c) "
            "{ return c.className; }));"))
        self.assertEqual(["hc-head", "hc-banner", "conv-panel"], order)

    def test_it_is_full_width(self):
        width = self.run_js(
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(self.RUNNING) +
            "window.__hcPromptUI.renderBanner();"
            "window.__hcPromptUI.bannerCss();")
        self.assertIn("width:100%", width)
        self.assertIn("box-sizing:border-box", width)

    def test_a_re_render_that_replaces_the_header_takes_it_along(self):
        order = json.loads(self.run_js(
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(self.RUNNING) +
            "window.__hcPromptUI.renderBanner();"
            "app.removeChild(header);"
            "var again = document.createElement('div');"
            "again.className = 'hc-head';"
            "var s2 = document.createElement('div'); s2.className = 'hc-sub';"
            "again.appendChild(s2);"
            "app.insertBefore(again, app.firstChild);"
            "window.__hcPromptUI.renderBanner();"
            "JSON.stringify(app.children.map(function (c) "
            "{ return c.className; }));"))
        self.assertEqual(["hc-head", "hc-banner", "conv-panel"], order)

    def test_it_goes_away_once_every_conversation_is_analyzed(self):
        finished = dict(self.RUNNING, running=True, phase="extracting",
                        conversations={"total": 89, "analyzed": 89, "pending": 0})
        self.assertFalse(self.run_js(
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(finished) +
            "window.__hcPromptUI.analysisPending();"))

    def test_but_the_goal_build_after_it_still_reports(self):
        building = dict(self.RUNNING, running=True, phase="synthesizing",
                        conversations={"total": 89, "analyzed": 89, "pending": 0})
        self.assertTrue(self.run_js(
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(building) +
            "window.__hcPromptUI.analysisPending();"))

    def test_it_stays_quiet_until_onboarding_is_finished(self):
        # The wizard is asking questions; a banner behind it talks over them.
        during = dict(self.RUNNING, done=False)
        shown = self.run_js(
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(during) +
            "window.__hcPromptUI.renderBanner();"
            "made.filter(function (e) {"
            "  return e.className === 'hc-banner'; }).length;")
        self.assertEqual(0, shown)

    def test_a_partial_history_is_not_mistaken_for_work_in_progress(self):
        # Some conversations never yield an extraction, so analyzed < total is
        # a resting state. Claiming otherwise leaves a banner up forever.
        setup = dict(self.RUNNING, running=False, current=None,
                     conversations={"total": 89, "analyzed": 60, "pending": 0})
        self.assertIsNone(self.banner(setup))

    def test_a_queued_backlog_still_shows_it(self):
        setup = dict(self.RUNNING, running=False, current=None,
                     conversations={"total": 89, "analyzed": 60, "pending": 29})
        self.assertIsNotNone(self.banner(setup))

    def test_the_empty_tree_explains_where_goals_come_from(self):
        out = self.patched_bundle("out;")
        self.assertIn("inferred once your conversations have all been analyzed",
                      out)
        self.assertIn("__hcAnalysisPending", out)
