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
        if (child.className === want) return child;
        const deep = walk(child);
        if (deep) return deep;
      }
      return null;
    };
    return walk(this);
  };
  this.setAttribute = () => {};
  this.contains = (n) => {
    if (n === this) return true;
    return this.children.some((c) => c.contains && c.contains(n));
  };
  this.removeChild = (n) => { this.children = this.children.filter(c => c !== n); };
  made.push(this);
}
const root = new El("html");
const app = new El("div"); app.className = "hc"; root.appendChild(app);
// The real shape: a header block holding the subtitle, then the panel.
const header = new El("div"); header.className = "hc-head"; app.appendChild(header);
const sub = new El("div"); sub.className = "hc-sub"; header.appendChild(sub);
const panel = new El("div"); panel.className = "conv-panel"; app.appendChild(panel);
const document = {
  readyState: "complete", documentElement: root, head: new El("head"),
  body: root, addEventListener() {},
  createElement: (t) => new El(t),
  getElementById: (id) => made.find(e => e.id === id) || null,
  querySelector: (s) => (s === ".hc" ? app : root.querySelector(s)),
  querySelectorAll: () => []
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
        self.assertIn("waiting for its first step", out)


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

    def test_the_button_sits_with_the_heading_not_at_the_bottom(self):
        where = self.run_js(
            "var pane = document.createElement('div');"
            "pane.className = 'hc-live';"
            "document.body.appendChild(pane);"
            "window.__hcPromptUI.renderLive('g1', %s);"
            % json.dumps([dict(self.RUN, session_id="abc-123")]) +
            "JSON.stringify(pane.children[0].children.map(function (c) "
            "{ return c.className; }));")
        self.assertEqual(["hc-live-head", "hc-live-open"], json.loads(where))

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
        self.assertIn(".hc-live-log{max-height:320px;overflow-y:auto", css)

    def test_a_run_with_no_session_offers_no_button(self):
        classes = [c for c, _ in self.drawn(self.RUN)]
        self.assertNotIn("hc-live-open", classes)

    def test_a_question_is_marked_out(self):
        rows = dict((c, t) for c, t in
                    self.drawn(dict(self.RUN, attention="Migrate or not?")))
        self.assertEqual("Migrate or not?", rows["hc-live-ask"])

    def test_the_feed_is_drawn_on_the_agent_pane(self):
        out = self.patched_bundle("out;")
        self.assertIn('<div class="hc-live"></div><div style="margin-top:16px;'
                      "font:600 9.5px 'Source Code Pro',monospace;"
                      'letter-spacing:1px;color:var(--mut)">AGENT TODOS</div>',
                      out)

    def test_review_is_hidden_until_a_run_has_finished(self):
        out = self.patched_bundle("out;")
        self.assertIn('<sc-if value="{{ showReviewTab }}"', out)
        self.assertIn("showReviewTab: !!(art && art.finished)", out)

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

    def test_there_is_no_blinking_dot(self):
        css = self.run_js("window.__hcPromptUI.bannerCss();")
        self.assertNotIn("hc-banner-dot", css)
        self.assertNotIn("hc-pulse", css)

    def test_the_row_indicator_travels_instead_of_claiming_a_percentage(self):
        out = self.patched_bundle("out;")
        self.assertIn("hc-rowdots", out)
        self.assertNotIn("width:{{ cv.barW }}", out)
        css = self.run_js("window.__hcPromptUI.bannerCss();")
        self.assertIn("hc-travel", css)
        self.assertIn("infinite", css)

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
