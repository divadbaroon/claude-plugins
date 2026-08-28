"""The bridge between the Vault's state and the adopted goal artifact.

The artifact is checked in byte-for-byte and owns all rendering; the bridge
only maps records onto the fields it reads, mirrors edits back, and makes its
add-source controls ask for a value. These tests hold that contract.
"""
import json
import re
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
  this.select = () => {};
  // One rectangle for every node. Nothing here lays anything out, so the
  // number matters only to code that has to put a floating thing somewhere
  // -- a test that cares about where sets `rect` on the node itself.
  this.rect = { left: 40, right: 120, top: 60, bottom: 76, width: 80, height: 16 };
  this.getBoundingClientRect = () => this.rect;
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
  // One matcher for both finders. Class selectors, attribute selectors
  // ([name] and [name="value"]) -- the page finds several of its controls
  // by attribute rather than by class -- and bare tag names, which the
  // document-level sweep has always taken as a comma-separated list.
  this.matches = (child, sel) => {
    const text = String(sel || "").trim();
    // Either quote, or none: the page writes [data-hc-notice='x'] in one
    // place and [data-hc-sb="url"] in another, and a matcher that knows
    // only one of them silently finds nothing -- which reads as a control
    // that does not work rather than a selector that was not understood.
    const attr = /^\[([^\]=]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]$/
      .exec(text);
    if (attr) {
      const got = child.getAttribute(attr[1]);
      const want = attr[2] !== undefined ? attr[2]
        : attr[3] !== undefined ? attr[3] : attr[4];
      return got !== null && (want === undefined || got === want);
    }
    if (text.startsWith(".")) {
      return String(child.className).split(" ").includes(text.slice(1));
    }
    if (text.indexOf(",") >= 0) {
      return text.split(",").map(t => t.trim().toUpperCase())
        .includes(String(child.tagName).toUpperCase());
    }
    // A bare token is a class name here as often as a tag: tests written
    // against this harness have always passed "hc-search-input" meaning
    // the class. Both readings are honoured, class first.
    return child.className === text
      || String(child.tagName).toUpperCase() === text.toUpperCase();
  };
  this.querySelector = (sel) => {
    const walk = (node) => {
      for (const child of node.children) {
        if (this.matches(child, sel)) return child;
        const deep = walk(child);
        if (deep) return deep;
      }
      return null;
    };
    return walk(this);
  };
  this.querySelectorAll = (sel) => {
    const out = [];
    (function walk(n) { (n.children || []).forEach((c) => {
      if (root.matches(c, sel)) out.push(c);
      walk(c); }); })(this);
    return out;
  };
  this.attrs = {};
  this.setAttribute = (k, v) => { this.attrs[k] = String(v); };
  this.getAttribute = (k) => (k in this.attrs ? this.attrs[k] : null);
  this.hasAttribute = (k) => k in this.attrs;
  this.removeAttribute = (k) => { delete this.attrs[k]; };
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
const pending = [];
const document = {
  readyState: "complete", documentElement: root, head: new El("head"),
  body: root, title: "Goals",
  addEventListener: (type, fn) => listeners.push([type, fn]),
  // A dialog that listens for Escape at the document takes its listener
  // away again when it closes; without this the close throws.
  removeEventListener: (type, fn) => {
    const at = listeners.findIndex(l => l[0] === type && l[1] === fn);
    if (at >= 0) listeners.splice(at, 1);
  },
  createElement: (t) => new El(t),
  getElementById: (id) => made.find(e => e.id === id) || null,
  querySelector: (s) => (s === ".hc" ? app : root.querySelector(s)),
  // Enough for a tag-name sweep: the bridge uses it to find a heading
  // by its text when the anchor it was given has been re-rendered away.
  // Walks the live tree, as a browser does: a node that has been
  // re-rendered away is not a result, and treating it as one sends the
  // button somewhere nobody can click it.
  querySelectorAll: (sel) => root.querySelectorAll(sel)
};
function XHR() {}
// Every synchronous route the boot path opens, in order, so a test can say
// which of them a scope pays for.
const xhrs = [];
XHR.prototype.open = function (method, url) {
  this._url = String(url || "");
  xhrs.push(this._url);
};
XHR.prototype.send = function () {
  this.responseText = this._url.indexOf("/api/health") >= 0
    ? (process.env.HC_HEALTH || "{}")
    : this._url.indexOf("/api/setup") >= 0
    ? (process.env.HC_SETUP || "{}")
    : this._url.indexOf("/api/briefings") >= 0
    ? (process.env.HC_BRIEFS || '{"ok":true,"goals":{}}')
    : (process.env.HC_STATE || "{}");
};
const sandbox = {
  console, document, XMLHttpRequest: XHR, made, require, calls, app, sub,
  listeners, xhrs,
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
      : (opts && opts.body && JSON.parse(opts.body).op === "prompt_preview")
      // The composed prompt and what it costs with no rows in it -- the rail
      // prices its TODO rows against the second number.
      ? { ok: true, prompt: "# Project\nRouter\n\n# The work\n\n- a row\n",
          context_tokens: 1400 }
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
  setInterval: () => 0,
  // Timers fire the moment they are set, which is what nearly every test
  // here wants. A test about something that is supposed to happen *later*
  // (a banner taking itself away after eight seconds) sets HC_DEFER_TIMEOUT
  // and drives the callback itself.
  setTimeout: (f) => {
    if (!f) return 0;
    if (process.env.HC_DEFER_TIMEOUT === "1") { pending.push(f); return pending.length; }
    f();
    return 0;
  },
  clearTimeout(id) { if (id) pending[id - 1] = null; },
  navigator: {}, store, pending,
  fireTimers: () => { const due = pending.slice(); pending.length = 0;
                      due.forEach(f => { if (f) f(); }); }
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


    def patched_bundle(self, tail, scope=None):
        """Apply patchBundleSource to the checked-in artifact, then evaluate.

        `scope` overrides what the bridge read from /api/state, because the
        patch itself is scope-aware: a chat workspace gets a PROMPT tab and a
        copyable prompt, a global vault keeps the folded AGENT pane.
        """
        return self.run_js(
            ("window.__hcScope = %s;" % json.dumps(scope) if scope else "")
            + "var fs = require('fs');"
            "var html = fs.readFileSync(%s, 'utf8');"
            "var src = JSON.parse(html.match("
            "  /<script type=\"__bundler\\/template\">\\s*([\\s\\S]*?)\\s*<\\/script>/)[1]);"
            "var out = window.__hcPromptUI.patchBundleSource(src);"
            % json.dumps(str(BUNDLE)) + tail)

    def roots(self, state=None):
        return self.run_js(
            "window.__hcPromptUI.rootsFromState(%s);" % json.dumps(state or STATE))


class LaunchDressedTests(BridgeTestCase):
    """What counts as ready to be looked at."""

    def ask(self, launch, style, text):
        return self.run_js(
            "var root = document.documentElement;"
            "root.getAttribute = function (n) {"
            "  return n === 'data-hc-launch' ? %s : null; };"
            "document.getElementById = function (id) {"
            "  return id === 'hc-launch-style' ? %s : null; };"
            "document.body = {textContent: %s};"
            "out = window.__hcPromptUI.launchDressed();"
            % (json.dumps(launch), "{}" if style else "null", json.dumps(text)))

    def test_ready_only_when_skinned_and_resolved(self):
        self.assertTrue(self.ask("chat", True, "Engelbart session saved 11:07"))

    def test_an_unresolved_binding_is_not_ready(self):
        self.assertFalse(self.ask("chat", True, "saved {{ updatedLabel }}"))

    def test_an_unskinned_or_empty_document_is_not_ready(self):
        self.assertFalse(self.ask(None, True, "Engelbart"))
        self.assertFalse(self.ask("chat", False, "Engelbart"))
        self.assertFalse(self.ask("chat", True, ""))


class TodoListModelTests(BridgeTestCase):
    """The list the workspace rail edits: rows of {id, text, depth, status}.

    Every key the list is about is a pure operation on the rows -- returning
    the rows and where the caret should land, or null when the browser should
    have the key -- so all of it is testable without a DOM. Rows carry ids the
    reader never sees; the markdown is derived, four spaces to the level.
    """

    def rows(self, spec):
        return [{"id": "t%08d" % i, "text": text, "depth": depth,
                 "status": "", "question": ""}
                for i, (text, depth) in enumerate(spec)]

    def model(self, expression, spec=(), **rest):
        return self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "var items = %s;"
            "out = (%s);" % (json.dumps(self.rows(spec)), expression), **rest)

    def shape(self, result):
        return [[r["text"], r["depth"]] for r in result["items"]], \
            result.get("index"), result.get("caret")

    # --- text out ------------------------------------------------------------

    def test_rows_serialize_to_bullets_four_spaces_to_the_level(self):
        self.assertEqual(
            "- one\n- two\n    - two a\n        - deep\n- three\n",
            self.model("L.serialize(items)",
                       [("one", 0), ("two", 0), ("two a", 1), ("deep", 2),
                        ("three", 0)]))

    def test_blank_rows_serialize_to_nothing(self):
        self.assertEqual("", self.model("L.serialize(items)", [("", 0)]))
        self.assertEqual("- a\n", self.model("L.serialize(items)",
                                             [("a", 0), ("  ", 1)]))

    def test_rows_serialize_with_their_states_for_a_prompt_body(self):
        # The copy a session receives names every row's state -- "active"
        # for a row not yet sent, its status word otherwise.
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.serializeStates(["
            "  {id: 't00000001', text: 'one', depth: 0, status: ''},"
            "  {id: 't00000002', text: 'two', depth: 1, status: 'building'},"
            "  {id: 't00000003', text: 'three', depth: 0, status: 'done'},"
            "  {id: 't00000004', text: '  ', depth: 0, status: 'queued'}]);")
        self.assertEqual(
            "- [active] one\n    - [building] two\n- [done] three\n", out)

    def test_the_last_unsent_row_survives_a_backspace_at_its_bullet(self):
        # Backspacing an empty bullet removes it -- except when it is the only
        # row still unsent. Rows out with a build sit below it and cannot be
        # typed into, so deleting it would leave no way to add a TODO at all.
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.backspace(["
            "  {id: 't00000001', text: '', depth: 0, status: ''},"
            "  {id: 't00000002', text: 'out', depth: 0, status: 'building'},"
            "  {id: 't00000003', text: 'gone', depth: 0, status: 'done'}],"
            "  0, 0);")
        self.assertIsNone(out)

    def test_a_second_unsent_row_is_still_removable(self):
        # The guard is the LAST unsent row, not every empty one: with two to
        # type into, backspacing one of them behaves as it always did.
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.backspace(["
            "  {id: 't00000001', text: 'keep', depth: 0, status: ''},"
            "  {id: 't00000002', text: '', depth: 0, status: ''}],"
            "  1, 0).items.length;")
        self.assertEqual(1, out)

    def test_the_last_unsent_row_survives_an_explicit_remove(self):
        # Cmd+Backspace takes the same guard: an empty last unsent row is not
        # removed, since there would be nothing to put in its place but another.
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.remove(["
            "  {id: 't00000001', text: '  ', depth: 0, status: ''},"
            "  {id: 't00000002', text: 'out', depth: 0, status: 'queued'}], 0);")
        self.assertIsNone(out)

    def test_the_todo_copy_carries_the_notes_as_context_only(self):
        # The Copy TODOs body: rows with states first, then the goal's notes
        # under a CONTEXT header that says not to act on them.
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.copyText("
            "  [{id: 't00000001', text: 'ship it', depth: 0, status: ''}],"
            "  '# Decisions\\n- keep sqlite\\n');")
        self.assertEqual(
            "TODOs (each with its current state):\n- [active] ship it\n"
            "\nCONTEXT — the goal's notes, for background only. Do NOT make"
            " any changes specified in these notes; act only on the TODOs"
            " above:\n# Decisions\n- keep sqlite\n", out)

    def test_the_todo_copy_without_notes_is_the_bare_state_list(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.copyText("
            "  [{id: 't00000001', text: 'ship it', depth: 0, status: ''}], '');")
        self.assertEqual("- [active] ship it\n", out)

    def test_the_copied_prompt_includes_todo_states(self):
        # The chat scope's recommended-prompt builder reads the rows, not the
        # bare markdown, so the states travel with the copied prompt body.
        out = self.patched_bundle(
            "out = [out.indexOf(\"'TODOs (each with its current state):\") >= 0,"
            "       out.indexOf('sel.todo_items') >= 0];", scope="chat")
        self.assertEqual([True, True], out)

    # --- screenshots pasted into a row ----------------------------------------

    SHOT_ROWS = (
        "[{id: 't00000001', text: 'fix the header [attachment #1]', depth: 0,"
        "  status: '', attachments: [{n: 1, path: '/s/one.png', name: 'one.png'}]},"
        " {id: 't00000002', text: 'and the footer', depth: 0, status: ''},"
        " {id: 't00000003', text: 'marker gone', depth: 0, status: '',"
        "  attachments: [{n: 2, path: '/s/two.png', name: 'two.png'}]}]")

    def test_a_pasted_image_lands_as_a_numbered_marker_on_the_row(self):
        # The marker is numbered past every attachment the list already
        # holds, goes in at the caret with a space before it when the text
        # runs straight into it, and the row remembers the file.
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "var r = L.attach(" + self.SHOT_ROWS + ", 1, 14,"
            "  {path: '/s/three.png', name: 'three.png'});"
            "out = [r.items[1].text, r.items[1].attachments, r.index, r.caret,"
            "       r.items[0].attachments.length];")
        self.assertEqual(
            ["and the footer [attachment #3]",
             [{"n": 3, "path": "/s/three.png", "name": "three.png"}],
             1, len("and the footer [attachment #3]"), 1], out)

    def test_a_marker_pasted_mid_row_keeps_the_tail_after_it(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "var r = L.attach([{id: 't00000001', text: 'ab cd', depth: 0, status: ''}],"
            "  0, 2, {path: '/s/a.png', name: 'a.png'});"
            "out = [r.items[0].text, r.caret];")
        self.assertEqual(["ab [attachment #1] cd", len("ab [attachment #1]")], out)
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "var r = L.attach([{id: 't00000001', text: '', depth: 0, status: ''}],"
            "  0, 0, {path: '/s/a.png', name: 'a.png'});"
            "out = r.items[0].text;")
        self.assertEqual("[attachment #1]", out)

    def test_attachment_lines_name_only_the_markers_still_in_some_row(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.attachmentLines(" + self.SHOT_ROWS + ");")
        self.assertEqual(["[attachment #1]: /s/one.png"], out)

    def test_the_todo_copy_ends_with_the_attachments_resolved(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.copyText(" + self.SHOT_ROWS + ", '# Decisions\\n- x\\n');")
        self.assertEqual(
            "TODOs (each with its current state):\n"
            "- [active] fix the header [attachment #1]\n"
            "- [active] and the footer\n"
            "- [active] marker gone\n"
            "\nAttachments (files the rows cite; open them for the rows"
            " that name them):\n[attachment #1]: /s/one.png\n"
            "\nCONTEXT — the goal's notes, for background only. Do NOT make"
            " any changes specified in these notes; act only on the TODOs"
            " above:\n# Decisions\n- x\n", out)
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.copyText(" + self.SHOT_ROWS + ", '');")
        self.assertEqual(
            "- [active] fix the header [attachment #1]\n"
            "- [active] and the footer\n"
            "- [active] marker gone\n"
            "\nAttachments (files the rows cite; open them for the rows"
            " that name them):\n[attachment #1]: /s/one.png\n", out)

    def test_rows_keep_their_attachments_through_every_list_operation(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "var items = " + self.SHOT_ROWS + ";"
            "out = [L.normalize(items)[0].attachments.length,"
            "       L.enter(items, 0, 3).items[0].attachments.length,"
            "       L.indent(items, 1).items[0].attachments.length,"
            "       'attachments' in L.normalize(items)[1]];")
        self.assertEqual([1, 1, 1, False], out)

    def test_the_copied_prompt_resolves_the_attachments_too(self):
        out = self.patched_bundle(
            "out = out.indexOf('Attachments (files the rows cite') >= 0;",
            scope="chat")
        self.assertTrue(out)

    def test_a_depth_jump_is_pulled_back_to_one_level(self):
        out = self.model("L.normalize(items).map(function (r) { return r.depth; })",
                         [("one", 0), ("deep", 3), ("deeper", 5)])
        self.assertEqual([0, 1, 2], out)

    def test_every_row_keeps_or_gets_an_id(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.normalize([{text: 'a', depth: 0}, {id: 't0000000a', text: 'b'}])"
            "  .map(function (r) { return [typeof r.id, r.id.length > 4, r.id]; });")
        self.assertEqual(["string", True], out[0][:2])
        self.assertEqual(["string", True, "t0000000a"], out[1])

    # --- the family one pick covers -----------------------------------------

    def test_a_parent_pick_covers_the_rows_nested_under_it(self):
        # Marking a parent to-build marks its children: the family is the
        # row and everything deeper, up to the next row at its own depth.
        out = self.model("L.family(items, 0)",
                         [("parent", 0), ("child", 1), ("grandchild", 2),
                          ("sibling", 0)])
        self.assertEqual([0, 1, 2], out)

    def test_a_leaf_is_a_family_of_one(self):
        out = self.model("L.family(items, 1)",
                         [("parent", 0), ("child", 1), ("sibling", 0)])
        self.assertEqual([1], out)

    def test_a_family_ends_at_a_shallower_row(self):
        out = self.model("L.family(items, 1)",
                         [("one", 0), ("two", 1), ("two a", 2), ("three", 0)])
        self.assertEqual([1, 2], out)

    def test_a_family_out_of_range_is_empty(self):
        self.assertEqual([], self.model("L.family(items, 5)", [("one", 0)]))

    # --- the bands the list is drawn in ---------------------------------------
    #
    # Rows not yet sent sit on top, rows out with the builder in the middle,
    # rows that came back done at the bottom -- with a rule between bands.

    def banded(self, spec):
        return [{"id": "t%08d" % i, "text": text, "depth": depth,
                 "status": status, "question": ""}
                for i, (text, depth, status) in enumerate(spec)]

    def band_model(self, expression, spec):
        return self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "var items = %s;"
            "out = (%s);" % (json.dumps(self.banded(spec)), expression))

    def test_rows_without_a_status_are_all_one_band(self):
        out = self.band_model("L.bands(items)",
                              [("one", 0, ""), ("two", 0, ""), ("three", 1, "")])
        self.assertEqual([0, 0, 0], out)

    def test_done_sinks_and_building_sits_between(self):
        out = self.band_model(
            "L.sectioned(items).map(function (r) { return r.text; })",
            [("done", 0, "done"), ("building", 0, "building"),
             ("active", 0, "")])
        self.assertEqual(["active", "building", "done"], out)

    def test_queued_asking_and_failed_all_ride_the_middle_band(self):
        out = self.band_model("L.bands(items)",
                              [("q", 0, "queued"), ("a", 0, "asking"),
                               ("f", 0, "failed"), ("d", 0, "done")])
        self.assertEqual([1, 1, 1, 2], out)

    def test_a_family_is_banded_whole(self):
        # A parent out with the builder keeps its done child beside it: the
        # family is done only when every row in it is.
        out = self.band_model("L.bands(items)",
                              [("parent", 0, "building"), ("child", 1, "done"),
                               ("sibling", 0, "")])
        self.assertEqual([1, 1, 0], out)

    def test_a_done_parent_with_an_unsent_child_is_still_out(self):
        out = self.band_model("L.bands(items)",
                              [("parent", 0, "done"), ("child", 1, "")])
        self.assertEqual([1, 1], out)

    # --- the way back from the build ------------------------------------------
    #
    # Queued, building, asking, and failed rows can be taken back to active.
    # The unit is the family: the control sits on the family's head, and
    # cancelling the head cancels every out row under it.

    def test_a_lone_out_row_is_its_own_head(self):
        out = self.band_model("L.cancelHead(items, 1)",
                              [("a", 0, ""), ("b", 0, "building"), ("c", 0, "")])
        self.assertEqual(1, out)

    def test_a_row_that_is_not_out_has_no_head(self):
        out = self.band_model("L.cancelHead(items, 0)",
                              [("a", 0, ""), ("b", 0, "done")])
        self.assertEqual(-1, out)
        self.assertEqual(-1, self.band_model("L.cancelHead(items, 1)",
                                             [("a", 0, ""), ("b", 0, "done")]))

    def test_an_out_child_under_an_out_parent_answers_to_the_parent(self):
        spec = [("parent", 0, "building"), ("child", 1, "building"),
                ("grandchild", 2, "queued"), ("sibling", 0, "asking")]
        self.assertEqual(0, self.band_model("L.cancelHead(items, 2)", spec))
        self.assertEqual(0, self.band_model("L.cancelHead(items, 1)", spec))
        self.assertEqual(3, self.band_model("L.cancelHead(items, 3)", spec))

    def test_an_out_child_under_an_unsent_parent_is_a_head_of_its_own(self):
        # The only kind of pick that makes this: a child picked alone.
        out = self.band_model("L.cancelHead(items, 1)",
                              [("parent", 0, ""), ("child", 1, "building")])
        self.assertEqual(1, out)

    def test_heads_are_the_out_rows_with_no_out_row_above_them(self):
        out = self.band_model(
            "L.cancelHeads(items)",
            [("p", 0, "building"), ("c", 1, "building"), ("d", 0, "done"),
             ("q", 0, "queued"), ("u", 0, ""), ("uc", 1, "failed"),
             ("a", 0, "")])
        self.assertEqual([0, 3, 5], out)

    def test_cancelling_a_head_takes_the_out_rows_of_its_family(self):
        out = self.band_model(
            "L.cancelIds(items, 0)",
            [("p", 0, "building"), ("c", 1, "building"), ("done", 1, "done"),
             ("fresh", 1, ""), ("sibling", 0, "queued")])
        self.assertEqual(["t00000000", "t00000001"], out)

    def test_the_cancel_control_is_drawn_on_heads_only(self):
        drawn = self.band_model(
            "JSON.stringify([0, 1].map(function (i) {"
            "  var node = L.rowNode(items[i], i === 0);"
            "  return [node.getAttribute('data-hc-todo-head') !== null,"
            "          node.children.map(function (c) { return c.className; }),"
            "          (node.children[0].getAttribute('data-hc-todo-cancel')),"
            "          node.children[0].getAttribute('contenteditable')];"
            "}))",
            [("p", 0, "building"), ("c", 1, "building")])
        head, child = json.loads(drawn)
        self.assertEqual([True, ["hc-todo-cancel", "hc-todo-row"], "t00000000",
                          "false"], head)
        self.assertEqual([False, ["hc-todo-row"], None, None], child)

    def test_the_cancel_control_takes_its_corner_from_the_stylesheet(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        self.assertIn(".hc-todo-cancel{position:absolute;top:", css)
        self.assertNotIn(".hc-todo-cancel{position:absolute;right:4px;bottom:", css)
        self.assertIn(".hc-todo[data-hc-todo-head] .hc-todo-row{padding-right", css)

    def test_the_cancel_control_sits_on_the_line_of_the_state_badge(self):
        # The x is on the head's first line, next to the "building" badge,
        # not at the bottom of the tile (which, for an asking row, is under
        # the question thread).
        drawn = self.band_model(
            "(function () {"
            "  var node = L.rowNode(items[0], true);"
            "  var css = window.__hcPromptUI.launchCss();"
            "  var m = /\\.hc-todo-cancel\\{([^}]*)\\}/.exec(css)[1];"
            "  return JSON.stringify([node.children[0].className,"
            "    /(^|;)top:/.test(m), /(^|;)bottom:/.test(m),"
            "    node.querySelector('.hc-todo-status').textContent]);"
            "})()",
            [("p", 0, "asking")])
        self.assertEqual(["hc-todo-cancel", True, False, "needs you"], json.loads(drawn))

    def test_children_under_an_out_head_carry_no_building_badge(self):
        # The head says "building" for the family; its children are quiet.
        drawn = self.band_model(
            "(function () {"
            "  var heads = L.cancelHeads(items);"
            "  return JSON.stringify(items.map(function (row, i) {"
            "    var b = L.rowNode(row, heads.indexOf(i) >= 0)"
            "      .querySelector('.hc-todo-status');"
            "    return b ? b.textContent : null;"
            "  }));"
            "})()",
            [("p", 0, "building"), ("c", 1, "building"), ("q", 1, "queued"),
             ("a", 1, "asking"), ("f", 1, "failed"), ("d", 1, "done"),
             ("lone", 0, "queued"), ("u", 0, ""), ("uc", 1, "building")])
        self.assertEqual(["building", None, None, "needs you", "failed", "done",
                          "queued", None, "building"], json.loads(drawn))

    def test_a_band_keeps_the_order_its_rows_were_in(self):
        out = self.band_model(
            "L.sectioned(items).map(function (r) { return r.text; })",
            [("d1", 0, "done"), ("a1", 0, ""), ("b1", 0, "building"),
             ("a2", 0, ""), ("d2", 0, "done")])
        self.assertEqual(["a1", "a2", "b1", "d1", "d2"], out)

    def test_sectioning_moves_the_rows_themselves_not_copies(self):
        out = self.band_model(
            "L.sectioned(items)[2] === items[0]",
            [("done", 0, "done"), ("active", 0, ""), ("building", 0, "building")])
        self.assertTrue(out)

    # --- enter -------------------------------------------------------------

    def test_enter_on_a_written_row_opens_a_sibling_below_it(self):
        out = self.model("L.enter(items, 0, 3)", [("one", 0)])
        self.assertEqual(([["one", 0], ["", 0]], 1, 0), self.shape(out))
        self.assertNotEqual(out["items"][0]["id"], out["items"][1]["id"])

    def test_enter_in_the_middle_of_a_row_carries_the_tail_down(self):
        out = self.model("L.enter(items, 0, 3)", [("one two", 0)])
        self.assertEqual(([["one", 0], ["two", 0]], 1, 0), self.shape(out))

    def test_enter_keeps_the_depth_of_the_row_it_leaves(self):
        out = self.model("L.enter(items, 1, 3)", [("one", 0), ("two", 1)])
        self.assertEqual(([["one", 0], ["two", 1], ["", 1]], 2, 0),
                         self.shape(out))

    def test_enter_on_an_empty_nested_row_outdents_it_instead(self):
        out = self.model("L.enter(items, 1, 0)", [("one", 0), ("", 1)])
        self.assertEqual(([["one", 0], ["", 0]], 1, 0), self.shape(out))

    def test_enter_on_an_empty_top_level_row_does_nothing_at_all(self):
        out = self.model("L.enter(items, 1, 0)", [("one", 0), ("", 0)])
        self.assertEqual(([["one", 0], ["", 0]], 1, 0), self.shape(out))

    # --- tab / shift-tab -----------------------------------------------------

    def test_tab_indents_under_the_row_above(self):
        out = self.model("L.indent(items, 1)", [("one", 0), ("two", 0)])
        self.assertEqual([["one", 0], ["two", 1]], self.shape(out)[0])

    def test_the_first_row_can_never_be_indented(self):
        out = self.model("L.indent(items, 0)", [("one", 0), ("two", 0)])
        self.assertEqual([["one", 0], ["two", 0]], self.shape(out)[0])

    def test_tab_cannot_skip_a_level(self):
        out = self.model("L.indent(items, 2)",
                         [("one", 0), ("two", 1), ("three", 1)])
        self.assertEqual([["one", 0], ["two", 1], ["three", 2]], self.shape(out)[0])
        out = self.model("L.indent(L.indent(items, 2).items, 2)",
                         [("one", 0), ("two", 1), ("three", 1)])
        self.assertEqual([["one", 0], ["two", 1], ["three", 2]], self.shape(out)[0],
                         "two levels under the row above is not a level")

    def test_indenting_a_row_takes_its_children_with_it(self):
        out = self.model("L.indent(items, 1)",
                         [("one", 0), ("two", 0), ("two a", 1), ("three", 0)])
        self.assertEqual([["one", 0], ["two", 1], ["two a", 2], ["three", 0]],
                         self.shape(out)[0])

    def test_shift_tab_outdents_and_stops_at_the_left_margin(self):
        out = self.model("L.outdent(items, 1)", [("one", 0), ("two", 1)])
        self.assertEqual([["one", 0], ["two", 0]], self.shape(out)[0])
        out = self.model("L.outdent(L.outdent(items, 1).items, 1)",
                         [("one", 0), ("two", 1)])
        self.assertEqual([["one", 0], ["two", 0]], self.shape(out)[0])

    # --- backspace and delete -------------------------------------------------

    def test_backspace_at_the_start_of_a_written_row_joins_it_upward(self):
        out = self.model("L.backspace(items, 1, 0)", [("one", 0), ("two", 0)])
        self.assertEqual(([["onetwo", 0]], 0, 3), self.shape(out))

    def test_backspace_on_an_empty_nested_row_outdents_before_deleting(self):
        out = self.model("L.backspace(items, 1, 0)", [("one", 0), ("", 1)])
        self.assertEqual([["one", 0], ["", 0]], self.shape(out)[0])

    def test_backspace_on_an_empty_last_row_removes_it(self):
        out = self.model("L.backspace(items, 1, 0)", [("one", 0), ("", 0)])
        self.assertEqual(([["one", 0]], 0, 3), self.shape(out))

    def test_backspace_inside_a_row_is_left_to_the_browser(self):
        self.assertIsNone(self.model("L.backspace(items, 0, 2)", [("one", 0)]))

    def test_the_only_row_is_never_removed_by_backspace(self):
        self.assertIsNone(self.model("L.backspace(items, 0, 0)", [("", 0)]))
        self.assertIsNone(self.model("L.backspace(items, 0, 0)", [("one", 0)]))

    def test_cmd_backspace_removes_the_row_and_leaves_one_to_type_in(self):
        out = self.model("L.remove(items, 1)", [("one", 0), ("two", 0), ("three", 0)])
        self.assertEqual(([["one", 0], ["three", 0]], 1, 5), self.shape(out))
        out = self.model("L.remove(items, 0)", [("one", 0)])
        self.assertEqual([["", 0]], self.shape(out)[0])

    # --- a selection across rows -----------------------------------------------

    def test_a_selection_across_rows_cuts_to_one_row_and_takes_the_typed_key(self):
        out = self.model("L.cut(items, 0, 2, 2, 3, '')",
                         [("one", 0), ("two", 1), ("three", 0)])
        self.assertEqual(([["onee", 0]], 0, 2), self.shape(out))
        out = self.model("L.cut(items, 2, 3, 0, 2, 'X')",
                         [("one", 0), ("two", 1), ("three", 0)])
        self.assertEqual(([["onXee", 0]], 0, 3), self.shape(out),
                         "backwards selections read the same as forwards")

    def test_copying_a_selection_across_rows_gives_the_rows_as_markdown(self):
        out = self.model("L.selectionText(items, 0, 1, 2, 2)",
                         [("one", 0), ("two", 1), ("three", 0)])
        self.assertEqual("- ne\n    - two\n- th", out)

    # --- paste -----------------------------------------------------------------
    #
    # A pasted list becomes one row per bullet, never one row holding the
    # whole body -- whether the bullets arrive as lines or run together on
    # one line, the shape a list takes when its newlines are lost.

    def paste(self, spec, index, caret, text):
        return self.model("L.paste(items, %d, %d, %s)"
                          % (index, caret, json.dumps(text)), spec)

    def test_pasting_bullet_lines_makes_one_row_per_bullet(self):
        out = self.paste([("", 0)], 0, 0, "- one\n- two\n- three")
        self.assertEqual(([["one", 0], ["two", 0], ["three", 0]], 2, 5),
                         self.shape(out))

    def test_pasted_rows_get_ids_of_their_own(self):
        out = self.paste([("", 0)], 0, 0, "- one\n- two")
        ids = [r["id"] for r in out["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids))

    def test_run_together_bullets_split_where_a_dash_glues_to_a_word(self):
        out = self.paste([("", 0)], 0, 0,
                         "- Create projects- Global vault- Explore options")
        self.assertEqual([["Create projects", 0], ["Global vault", 0],
                          ["Explore options", 0]], self.shape(out)[0])

    def test_a_spaced_dash_is_prose_and_never_splits(self):
        out = self.paste([("", 0)], 0, 0, "- read foo - the good one\n- two")
        self.assertEqual([["read foo - the good one", 0], ["two", 0]],
                         self.shape(out)[0])

    def test_a_body_that_does_not_open_with_a_bullet_keeps_its_dashes(self):
        out = self.paste([("", 0)], 0, 0, "use the built-in x- y flag")
        self.assertEqual(([["use the built-in x- y flag", 0]], 0, 26),
                         self.shape(out))

    def test_pasted_indentation_nests_from_the_caret_rows_depth(self):
        out = self.paste([("x", 1)], 0, 1,
                         "- parent\n    - child\n        - grandchild")
        self.assertEqual([["xparent", 1], ["child", 2], ["grandchild", 3]],
                         self.shape(out)[0])

    def test_a_subtree_copied_mid_list_rebases_to_the_shallowest_line(self):
        out = self.paste([("", 0)], 0, 0, "    - child\n        - grand")
        self.assertEqual([["child", 0], ["grand", 1]], self.shape(out)[0])

    def test_state_markers_from_a_copied_todo_body_are_dropped(self):
        out = self.paste([("", 0)], 0, 0, "- [active] one\n    - [done] two")
        self.assertEqual([["one", 0], ["two", 1]], self.shape(out)[0])

    def test_plain_lines_land_one_row_each(self):
        out = self.paste([("", 0)], 0, 0, "one\r\ntwo")
        self.assertEqual(([["one", 0], ["two", 0]], 1, 3), self.shape(out))

    def test_a_single_plain_fragment_pastes_inline(self):
        out = self.paste([("abcd", 0)], 0, 2, "foo")
        self.assertEqual(([["abfoocd", 0]], 0, 5), self.shape(out))

    def test_what_stood_after_the_caret_follows_the_last_fragment(self):
        out = self.paste([("headtail", 0)], 0, 4, "- one\n- two")
        self.assertEqual(([["headone", 0], ["twotail", 0]], 1, 3),
                         self.shape(out))

    def test_pasting_nothing_is_no_edit_at_all(self):
        self.assertIsNone(self.paste([("one", 0)], 0, 0, ""))
        self.assertIsNone(self.paste([("one", 0)], 0, 0, "  \n "))


class TodoSectionTests(BridgeTestCase):
    """Reading and replacing one section of the goal document, in the browser.

    The rail owns the TODOs section and nothing else: every other heading the
    reader or inference wrote has to come back byte for byte.
    """

    DOC = ("# Objective\nShip it\n\n# TODOs\n- one\n    - one a\n\n"
           "# Decisions\n- keep sqlite\n")

    def read(self, doc=None):
        return self.run_js(
            "out = window.__hcPromptUI.todoDoc.read(%s);"
            % json.dumps(self.DOC if doc is None else doc))

    def write(self, body, doc=None):
        return self.run_js(
            "out = window.__hcPromptUI.todoDoc.write(%s, %s);"
            % (json.dumps(self.DOC if doc is None else doc), json.dumps(body)))

    def test_it_reads_only_the_todos_section(self):
        self.assertEqual("- one\n    - one a\n", self.read())

    def test_a_document_without_the_section_reads_empty(self):
        self.assertEqual("", self.read("# Objective\nShip it\n"))

    def test_writing_leaves_every_other_section_byte_for_byte(self):
        out = self.write("- two\n")
        self.assertIn("# Objective\nShip it\n", out)
        self.assertIn("# Decisions\n- keep sqlite\n", out)
        self.assertEqual("- two\n", self.read(out))

    def test_writing_into_a_document_that_has_no_section_adds_it(self):
        out = self.write("- two\n", "# Objective\nShip it\n")
        self.assertEqual("- two\n", self.read(out))
        self.assertIn("# Objective\nShip it\n", out)

    def test_a_heading_inside_a_fence_is_not_a_heading(self):
        # The reader may paste a shell snippet under TODOs. Splitting on a
        # "# comment" at column 0 would tear their fence in half, in state
        # that is persisted and injected into later sessions.
        doc = ("# TODOs\n- run it\n```sh\n# install deps\nnpm i\n```\n\n"
               "# Decisions\n- keep sqlite\n")
        self.assertEqual("- run it\n```sh\n# install deps\nnpm i\n```\n",
                         self.run_js("out = window.__hcPromptUI.todoDoc.read(%s);"
                                     % json.dumps(doc)))

    def test_the_round_trip_of_an_untouched_section_changes_nothing(self):
        self.assertEqual(self.DOC, self.write(self.read()))


class TodoCostTests(BridgeTestCase):
    """The number in a row's lower right: what its build actually spent.

    A row that has not been built prints nothing there. The corner used to
    carry a guess at what the build would cost -- the context it opens on
    plus a median of the chat's earlier builds -- and a number nothing stood
    behind was the wrong thing beside a row still being written. What a build
    will cost is now the build's own word, on the watch panel: see
    BuildWatchTests.
    """

    # An older server still sends its cost block; the corner ignores it.
    COST = {"context_tokens": 2000, "row_tokens": 30000, "row_chars": 80,
            "samples": 4}

    def cost(self, row, cost=None):
        state = {"scope": "chat", "goals": [], "prompts": [],
                 "build_cost": self.COST if cost is None else cost}
        return self.run_js(
            "window.__hcPromptUI.acceptState(%s);"
            "out = window.__hcPromptUI.todoList.cost(%s);"
            % (json.dumps(state), json.dumps(row)))

    def row(self, text, **rest):
        row = {"id": "taaaa0001", "text": text, "depth": 0, "status": "",
               "question": ""}
        row.update(rest)
        return row

    def test_a_row_that_has_not_been_built_is_not_priced(self):
        # Whatever the server says a typical build here costs.
        self.assertIsNone(self.cost(self.row("x" * 80)))
        self.assertIsNone(self.cost(self.row("x" * 4000)))
        self.assertIsNone(self.cost(self.row("Add the route"), cost={}))
        # Nor a row out with the builder, or back from it unbuilt.
        self.assertIsNone(self.cost(self.row("Add the route", status="building")))
        self.assertIsNone(self.cost(self.row("Add the route", status="queued")))
        self.assertIsNone(self.cost(self.row("Add the route", status="failed")))

    def test_a_built_row_prints_what_it_actually_spent(self):
        out = self.cost(self.row("Add the route", status="done", tokens=8400))
        self.assertEqual("8.4k tok", out["label"])
        self.assertTrue(out["measured"])
        self.assertIn("spent", out["title"])
        self.assertNotIn("~", out["label"], "measured, not guessed")

    def test_an_empty_row_says_nothing(self):
        self.assertIsNone(self.cost(self.row("")))
        self.assertIsNone(self.cost(self.row("   ")))

    def test_the_label_never_claims_more_precision_than_it_has(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = [L.costLabel(940), L.costLabel(8437), L.costLabel(31700),"
            "       L.costLabel(0)];")
        self.assertEqual(["940", "8.4k", "32k", ""], out)

    def test_the_number_is_drawn_last_in_the_row_and_takes_no_caret(self):
        state = {"scope": "chat", "goals": [], "prompts": [],
                 "build_cost": self.COST}
        out = self.run_js(
            "window.__hcPromptUI.acceptState(%s);"
            "var node = window.__hcPromptUI.todoList.rowNode(%s, false);"
            "var slot = node.querySelector('.hc-todo-cost');"
            "out = [slot.textContent, slot.getAttribute('contenteditable'),"
            "  node.querySelector('.hc-todo-row').children.map("
            "    function (c) { return c.className; })];"
            % (json.dumps(state),
               json.dumps(self.row("x" * 80, status="done", tokens=2000))))
        self.assertEqual("2k tok", out[0])
        self.assertEqual("false", out[1], "an island, like the gutter")
        # After the badge a built row wears, and last of all.
        self.assertEqual(["hc-todo-dash", "hc-todo-line", "hc-todo-status",
                          "hc-todo-cost"], out[2])

    def test_an_unbuilt_row_s_corner_is_there_and_empty(self):
        # Drawn, so a measured number can land in it without a redraw under
        # the reader's caret -- but saying nothing, and taking no room.
        state = {"scope": "chat", "goals": [], "prompts": [],
                 "build_cost": self.COST}
        out = self.run_js(
            "window.__hcPromptUI.acceptState(%s);"
            "var node = window.__hcPromptUI.todoList.rowNode(%s, false);"
            "var slot = node.querySelector('.hc-todo-cost');"
            "out = [slot.textContent, slot.style.display];"
            % (json.dumps(state), json.dumps(self.row("x" * 80))))
        self.assertEqual(["", "none"], out)

    def test_the_stylesheet_puts_it_in_the_lower_right(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        self.assertIn(".hc-todo-cost{flex:none;align-self:flex-end", css)


class BuildWatchTests(BridgeTestCase):
    """The line under the rows while a build is out.

    A row that says "building" says nothing about what that means. The panel
    says how far in the run is, how much longer it has and what it will
    spend -- the build's own estimate, printed as its first protocol line, so
    the panel is "calculating" until that line lands -- and the last thing
    the agent did.
    """

    RUN = {"status": "running", "running": True, "rows": 2,
           "elapsed_s": 130, "eta_s": 350, "tokens": 0,
           "estimate": {"tokens": 120000, "minutes": 8,
                        "at": "2026-08-23T21:02:00+00:00"},
           "lines": 12, "can_open": True, "error": "",
           "last": {"at": "2026-08-23T21:04:09+00:00", "kind": "tool",
                    "text": "edited build.py"}}

    def line(self, run="the run above"):
        return self.run_js(
            "out = window.__hcPromptUI.todoList.watchLine(%s);"
            % json.dumps(self.RUN if run == "the run above" else run))

    def test_a_running_build_says_how_long_it_has_been_and_has_left(self):
        out = self.line()
        self.assertEqual("building · 2m in · about 6m left · ~120k tok",
                         out["meta"])
        self.assertEqual("edited build.py", out["last"])
        self.assertTrue(out["running"])
        self.assertIn("the build's own estimate", out["title"])
        self.assertIn("about 8m and 120,000 tokens for 2 rows", out["title"])

    def test_a_build_is_calculating_until_it_has_estimated_itself(self):
        out = self.line(dict(self.RUN, estimate=None, eta_s=None))
        self.assertEqual("building · 2m in · calculating…", out["meta"])
        self.assertIn("before it starts on the rows", out["title"])
        # An older server sends no estimate at all, and reads the same way.
        older = dict(self.RUN, per_row_s=240, measured=True)
        del older["estimate"]
        self.assertEqual("building · 2m in · calculating…",
                         self.line(older)["meta"])

    def test_an_estimate_that_has_run_out_says_so_rather_than_sliding(self):
        out = self.line(dict(self.RUN, eta_s=0))
        self.assertIn("longer than it estimated", out["meta"])
        self.assertNotIn("left", out["meta"])

    def test_a_stand_in_reads_as_a_guess_and_says_whose_it_is(self):
        # The build said nothing within the grace: the server hands the rail
        # what this chat's earlier builds cost, marked as such. A "~", not
        # an "about" -- and the title says where the number came from.
        guessed = dict(self.RUN["estimate"], source="measured")
        out = self.line(dict(self.RUN, estimate=guessed))
        self.assertEqual("building · 2m in · ~6m left · ~120k tok", out["meta"])
        self.assertIn("a stand-in from this chat's earlier builds", out["title"])
        self.assertIn("until the build prints its own estimate", out["title"])
        out = self.line(dict(self.RUN, estimate=dict(guessed, source="default"),
                             eta_s=0))
        self.assertIn("longer than expected", out["meta"])
        self.assertIn("nothing measured in this chat yet", out["title"])

    def test_a_build_that_has_stopped_is_not_given_a_countdown(self):
        out = self.line(dict(self.RUN, status="waiting", running=False,
                             eta_s=None))
        self.assertEqual("waiting on your answer · 2m in · ~120k tok",
                         out["meta"])
        self.assertFalse(out["running"])
        self.assertTrue(out["canOpen"], "its session can still be opened")

    def test_a_finished_build_says_what_it_spent_in_place_of_its_guess(self):
        out = self.line(dict(self.RUN, status="idle", running=False,
                             eta_s=None, tokens=131400))
        self.assertEqual("finished · 2m in · 131k tok", out["meta"])
        self.assertIn("it spent 131,400", out["title"])

    def test_a_goal_with_no_build_has_no_panel(self):
        self.assertIsNone(self.line(None))
        self.assertIsNone(self.line({}))

    def test_durations_are_short_and_even(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = [L.duration(0), L.duration(45), L.duration(130),"
            "       L.duration(3600), L.duration(4500)];")
        self.assertEqual(["0s", "45s", "2m", "1h", "1h 15m"], out)

    def test_the_stylesheet_marks_a_running_build(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        self.assertIn(".hc-todo-watch{", css)
        self.assertIn("hc-todo-pulse", css)
        # A guest reading someone else's workspace may see the log; opening a
        # window on their machine is not theirs to do.
        self.assertIn("[data-hc-readonly] [data-hc-todo-term]", css)
        # The terminal has a row of its own under the line, and a guest sees
        # neither the button nor the space it would have taken.
        self.assertIn(".hc-todo-watch-foot{", css)
        self.assertIn("[data-hc-readonly] .hc-todo-watch-foot", css)

    # --- the restart check, under the line ----------------------------------

    CHECK = {"status": "checking", "model": "sonnet", "effort": "high",
             "from_model": "opus", "why": "", "prompt": "",
             "at": "2026-08-26T06:37:00+00:00", "error": ""}

    def test_while_the_check_runs_the_line_says_finished_and_counts_nothing_down(self):
        out = self.line(dict(self.RUN, status="checking", running=True,
                             eta_s=None, tokens=131400, restart=self.CHECK))
        self.assertEqual("finished · 2m in · 131k tok", out["meta"])
        self.assertTrue(out["running"])
        self.assertTrue(out["checking"])
        self.assertEqual("checking", out["restart"]["status"])
        self.assertEqual(("sonnet", "high", "opus"),
                         (out["restart"]["model"], out["restart"]["effort"],
                          out["restart"]["fromModel"]))

    def test_a_build_not_yet_asked_carries_no_verdict(self):
        self.assertIsNone(self.line()["restart"])
        self.assertIsNone(self.line(dict(self.RUN, restart=None))["restart"])

    def paint(self, restart):
        return self.run_js(
            "var box = document.createElement('div');"
            "var L = window.__hcPromptUI.todoList;"
            "var node = L.renderRestart(box, L.restartOf({ restart: %s }), 'g1');"
            "var deep = function (n) { return String(n.textContent || '')"
            "  + (n.children || []).map(deep).join(''); };"
            "out = node ? { state: node.getAttribute('data-hc-todo-restart'),"
            "  text: deep(node), kids: node.children.map(function (k) { return k.className; }),"
            "  copy: (function () { var c = box.querySelector('[data-hc-todo-restart-copy]');"
            "    return c ? [c.textContent, c.getAttribute('data-hc-todo-restart-copy')] : null; })(),"
            "  prompt: (function () { var p = box.querySelector('.hc-todo-restart-prompt');"
            "    return p ? [p.tagName, p.textContent] : null; })(),"
            "  hide: (function () { var h = box.querySelector('[data-hc-todo-restart-hide]');"
            "    return h ? [h.textContent, h.getAttribute('data-hc-todo-restart-hide')] : null; })()"
            "  } : null;"
            % json.dumps(restart))

    def test_the_check_in_progress_names_the_model_it_moved_to(self):
        out = self.paint(self.CHECK)
        self.assertEqual("checking", out["state"])
        self.assertEqual(["hc-todo-restart-model", "hc-todo-restart-busy"], out["kids"])
        self.assertIn("modelopus→sonnet · effort high", out["text"])
        self.assertIn("checking whether these changes go stale without a local restart…",
                      out["text"])
        self.assertIsNone(out["copy"])
        # A build that ran on the CLI's default has nothing to strike through.
        out = self.paint(dict(self.CHECK, from_model=""))
        self.assertIn("model→sonnet · effort high", out["text"])

    def test_a_yes_draws_the_reason_and_the_prompt_with_a_copy_button(self):
        out = self.paint(dict(self.CHECK, status="yes",
                              why="the session-cache change lives in a long-running process",
                              prompt="Restart the goals-ui dev process so the new code loads."))
        self.assertEqual("yes", out["state"])
        self.assertEqual(["hc-todo-restart-why", "hc-todo-restart-send"], out["kids"])
        self.assertIn("⚠the session-cache change lives in a long-running process",
                      out["text"])
        self.assertIn("send to local claude code", out["text"])
        self.assertEqual(["copy", "g1"], out["copy"])
        self.assertEqual(["pre", "Restart the goals-ui dev process so the new code loads."],
                         out["prompt"])
        # A yes with no reason given still says what a yes means.
        out = self.paint(dict(self.CHECK, status="yes", prompt="p"))
        self.assertIn("the running instance keeps the old code until restarted.",
                      out["text"])

    def test_a_no_and_an_open_question_draw_nothing(self):
        for status in ("no", "unknown", "skipped"):
            self.assertIsNone(self.paint(dict(self.CHECK, status=status)), status)
        self.assertIsNone(self.paint(None))

    def test_either_state_offers_a_way_to_put_the_notice_away(self):
        # Waiting on the answer or holding it: both are the reader's to
        # dismiss, and the button names the goal it is dismissing for.
        self.assertEqual(["×", "g1"], self.paint(self.CHECK)["hide"])
        self.assertEqual(["×", "g1"],
                         self.paint(dict(self.CHECK, status="yes", prompt="p"))["hide"])

    YES = {"status": "yes", "model": "sonnet", "effort": "high",
           "from_model": "opus", "why": "a daemon holds the old code",
           "prompt": "Restart the dev process.",
           "at": "2026-08-26T06:37:00+00:00", "error": ""}

    def test_a_dismissed_notice_stays_gone_until_the_next_build_asks_again(self):
        out = self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "var yes = %s, later = %s;"
            "var paint = function (held, goalId) {"
            "  var box = document.createElement('div');"
            "  return !!L.renderRestart(box, L.restartOf({ restart: held }), goalId); };"
            "var before = paint(yes, 'g1');"
            "L.hideRestart('g1', L.restartOf({ restart: yes }));"
            "out = { before: before, after: paint(yes, 'g1'),"
            # A later build's verdict is news, and so is this one's own
            # spinner turning into an answer.
            "  again: paint(later, 'g1'),"
            "  checking: paint({ status: 'checking', model: 'sonnet',"
            "                    effort: 'high', at: yes.at }, 'g1'),"
            # And it is this goal's notice that was put away, not every goal's.
            "  elsewhere: paint(yes, 'g2'),"
            "  saved: JSON.parse(localStorage.getItem('hc-restart-hidden-v1')) };"
            % (json.dumps(self.YES),
               json.dumps(dict(self.YES, at="2026-08-27T09:00:00+00:00"))))
        self.assertTrue(out["before"])
        self.assertFalse(out["after"])
        self.assertTrue(out["again"])
        self.assertTrue(out["checking"])
        self.assertTrue(out["elsewhere"])
        # Written down, so a reload does not bring back what was put away.
        self.assertEqual({"g1": "yes@2026-08-26T06:37:00+00:00"}, out["saved"])

    def test_the_stylesheet_carries_the_check(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        for cls in (".hc-todo-restart{", ".hc-todo-restart-spin{", "hc-todo-spin",
                    ".hc-todo-restart-from{text-decoration:line-through",
                    ".hc-todo-restart-copy{", ".hc-todo-restart-prompt{",
                    ".hc-todo-restart-hide{"):
            self.assertIn(cls, css)


class RailSyncTests(BridgeTestCase):
    """What the rail owns in the store, and how the server's tree lands.

    The artifact reads the rail's fields (TODO rows, their markdown, the
    reader's prompt) once at boot; the rail writes them to the store as they
    change. So the artifact's own saves carry those fields from the store,
    and a tree the sync settles on is handed to the artifact's state rather
    than reloading the page.
    """

    GOALS = [{"id": "g1", "title": "A", "status": "todo",
              "todo_items": [{"id": "t1", "text": "one", "depth": 0,
                              "status": "queued", "question": ""}],
              "todos_md": "- one\n", "prompt_md": "p",
              "children": [{"id": "g1a", "title": "B", "todo_items": [],
                            "todos_md": "", "prompt_md": "", "children": []}]}]

    def test_the_artifacts_save_carries_the_rails_fields_from_the_store(self):
        out = self.run_js(
            "store['hc-vault-ui-v1'] = JSON.stringify({v: 7, goals: %s});"
            "out = window.__hcPromptUI.railFields([{id: 'g1', title: 'A renamed',"
            "  todo_items: [], todos_md: '', prompt_md: '', children: ["
            "    {id: 'g1a', title: 'B', todo_items: [{id: 't9', text: 'stale',"
            "     depth: 0, status: '', question: ''}], children: []},"
            "    {id: 'gNew', title: 'C', todo_items: [{id: 't2', text: 'mine',"
            "     depth: 0, status: '', question: ''}], children: []}]}]);"
            % json.dumps(self.GOALS))
        # The store's rows, markdown and prompt over the artifact's boot-time
        # copies; the artifact's own fields stay its own.
        self.assertEqual([("one", "queued")],
                         [(r["text"], r["status"]) for r in out[0]["todo_items"]])
        self.assertEqual("- one\n", out[0]["todos_md"])
        self.assertEqual("p", out[0]["prompt_md"])
        self.assertEqual("A renamed", out[0]["title"])
        # A child the store knows takes the store's (even empty) list; one
        # the store has never seen keeps what the artifact holds.
        self.assertEqual([], out[0]["children"][0]["todo_items"])
        self.assertEqual("mine", out[0]["children"][1]["todo_items"][0]["text"])

    def test_the_patched_artifact_saves_through_the_rail_and_publishes_a_setter(self):
        out = self.patched_bundle(
            "out = [out.indexOf(\"goals: (typeof window !== 'undefined' && "
            "window.__hcRailFields) ? window.__hcRailFields(goals) : goals,\") >= 0,"
            " out.indexOf('window.__hcSetGoals = (goals, selId) => this.set(') >= 0,"
            " out.indexOf(\"JSON.stringify({ v: 6, goals,\") >= 0,"
            " window.__hcPromptUI.patchMisses()];", scope="chat")
        self.assertEqual([True, True, False, []], out)

    def test_a_merge_lays_the_servers_build_state_over_the_pages_rows(self):
        # The page added a row (so its list differs from the base) while the
        # server marked the other one asking. The list is one field, but the
        # edit is the page's and the run is the server's -- both land.
        row = {"id": "t1", "text": "one", "depth": 0, "status": "", "question": ""}
        base = [{"id": "g1", "title": "A", "todo_items": [row], "children": []}]
        local = [{"id": "g1", "title": "A", "todo_items": [
            row, {"id": "t2", "text": "", "depth": 0, "status": "", "question": ""}],
            "children": []}]
        remote = [{"id": "g1", "title": "A", "todo_items": [
            {"id": "t1", "text": "one", "depth": 0, "status": "asking",
             "question": "which?"}], "children": []}]
        out = self.run_js("out = window.__hcPromptUI.mergeTrees(%s, %s, %s, {});"
                          % (json.dumps(base), json.dumps(local), json.dumps(remote)))
        self.assertEqual([("one", "asking", "which?"), ("", "", "")],
                         [(r["text"], r["status"], r["question"])
                          for r in out[0]["todo_items"]])

    def test_install_goals_hands_the_tree_to_the_artifact_without_a_reload(self):
        out = self.run_js(
            "store['hc-vault-ui-v1'] = JSON.stringify({v: 7, goals: [], selId: 'g1'});"
            "var got = null;"
            "window.__hcSetGoals = function (goals, selId) { got = [goals, selId]; };"
            "var ok = window.__hcPromptUI.installGoals(%s, 'r9');"
            "out = [ok, got, JSON.parse(store['hc-vault-ui-v1']).selId,"
            "       JSON.parse(store['hc-vault-ui-sync-v1']).revision,"
            "       JSON.parse(store['hc-vault-ui-v1']).goals.length];"
            % json.dumps(self.GOALS))
        self.assertTrue(out[0])
        self.assertEqual("g1", out[1][1])
        self.assertEqual("one", out[1][0][0]["todo_items"][0]["text"])
        self.assertEqual(["g1", "r9", 1], out[2:])

    def test_without_a_setter_install_goals_still_reloads(self):
        # The sandbox has no window.location: reaching the reload throws,
        # which is the proof that it was reached.
        out = self.run_js(
            "var threw = false;"
            "try { window.__hcPromptUI.installGoals(%s, 'r9'); }"
            "catch (e) { threw = true; } out = threw;" % json.dumps(self.GOALS))
        self.assertTrue(out)


class SoleRowAndBlankRowTests(BridgeTestCase):
    """Cmd+Enter with nothing picked, and the row a Build leaves behind."""

    def rows(self, spec):
        return [{"id": "t%08d" % i, "text": t, "depth": d, "status": s,
                 "question": ""} for i, (t, d, s) in enumerate(spec)]

    def ask(self, expression, spec):
        return self.run_js(
            "var L = window.__hcPromptUI.todoList; var items = %s; out = (%s);"
            % (json.dumps(self.rows(spec)), expression))

    def test_one_unsent_family_is_the_sole_pick(self):
        self.assertEqual([0], self.ask("L.sole(items)",
                                       [("one", 0, ""), ("", 0, ""), ("done", 0, "done")]))
        self.assertEqual([0, 1], self.ask("L.sole(items)",
                                          [("head", 0, ""), ("child", 1, ""),
                                           ("out", 0, "queued")]))
        self.assertEqual([1], self.ask("L.sole(items)",
                                       [("done", 0, "done"), ("again", 0, "failed")]))
        # two unsent families are a choice, and an empty list is nothing
        self.assertEqual([], self.ask("L.sole(items)", [("a", 0, ""), ("b", 0, "")]))
        self.assertEqual([], self.ask("L.sole(items)",
                                      [("", 0, ""), ("out", 0, "building")]))

    def test_a_build_leaves_an_empty_row_at_the_foot_of_the_active_band(self):
        out = self.ask("L.blankAfter(items, ['t00000001'])",
                       [("keep", 0, ""), ("send", 0, ""), ("out", 0, "queued")])
        self.assertEqual(["keep", "send", "", "out"], [r["text"] for r in out["items"]])
        self.assertEqual(out["items"][2]["id"], out["id"])
        # unless one is there already once the sent rows have left
        self.assertIsNone(self.ask("L.blankAfter(items, ['t00000000'])",
                                   [("send", 0, ""), ("", 0, "")]))
        out = self.ask("L.blankAfter(items, ['t00000000', 't00000001'])",
                       [("send", 0, ""), ("", 0, "")])
        self.assertEqual(["send", "", ""], [r["text"] for r in out["items"]])

    def test_the_answer_box_is_a_textarea_that_wraps(self):
        out = self.run_js(
            "var n = window.__hcPromptUI.todoList.rowNode("
            "  {id: 't1', text: 'x', depth: 0, status: 'asking', question: 'q'}, true);"
            "var box = n.querySelector('.hc-todo-answer');"
            "out = [box.tagName, box.getAttribute('rows'),"
            "       box.getAttribute('data-hc-todo-answer')];")
        self.assertEqual(["textarea", "1", "t1"], out)
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        css = "".join(css) if isinstance(css, list) else css
        answer = re.search(r"\.hc-todo-answer\{([^}]*)\}", css).group(1)
        self.assertIn("resize:none", answer)
        self.assertIn("overflow-wrap:anywhere", answer)
        self.assertIn("white-space:pre-wrap", answer)
        question = re.search(r"\.hc-todo-question\{([^}]*)\}", css).group(1)
        self.assertIn("white-space:pre-wrap", question)
        self.assertIn("overflow-wrap:anywhere", question)


class RailReconcileTests(BridgeTestCase):
    """The rail's rows against the store's, three ways.

    The rail holds its own list while the reader types in it, and writes
    that list whole. The store can change underneath it meanwhile -- a
    recovery, another window, a build marking rows, inference adding one --
    and a list written whole over that erased whatever the rail had never
    seen: /api/import reads a row it is not sent as deleted. What was
    missing was the rail's memory of what it last took from the store (the
    base). With it, a row in the store that is in neither the rail nor the
    base was added elsewhere and joins; a row in the base the rail no
    longer has was deleted here and stays gone; a row whose text the rail
    has not touched since the base takes the store's text.
    """

    def row(self, rid, text, depth=0, status=""):
        return {"id": rid, "text": text, "depth": depth, "status": status,
                "question": ""}

    def reconcile(self, items, stored, base):
        return self.run_js(
            "var L = window.__hcPromptUI.todoList;"
            "out = L.reconcile(%s, %s, %s);"
            % (json.dumps(items), json.dumps(stored), json.dumps(base)))

    def test_a_row_added_elsewhere_joins_the_rail_after_its_neighbour(self):
        a, b, c = self.row("ta", "a"), self.row("tb", "b"), self.row("tc", "c")
        out = self.reconcile([a, c], [a, b, c], [a, c])
        self.assertTrue(out["changed"])
        self.assertEqual(["ta", "tb", "tc"], [r["id"] for r in out["items"]])
        # With no neighbour before it in the store, it leads.
        out = self.reconcile([a], [b, a], [a])
        self.assertEqual(["tb", "ta"], [r["id"] for r in out["items"]])

    def test_a_row_the_rail_deleted_stays_deleted(self):
        a, b = self.row("ta", "a"), self.row("tb", "b")
        out = self.reconcile([a], [a, b], [a, b])
        self.assertFalse(out["changed"])
        self.assertEqual(["ta"], [r["id"] for r in out["items"]])

    def test_text_the_rail_never_touched_takes_the_stores_word(self):
        # The rail loaded the row blank; someone typed into it elsewhere.
        # Writing the rail's blank over that is the wipe.
        out = self.reconcile([self.row("ta", "")],
                             [self.row("ta", "typed elsewhere")],
                             [self.row("ta", "")])
        self.assertTrue(out["changed"])
        self.assertEqual("typed elsewhere", out["items"][0]["text"])
        # Depth the same way.
        out = self.reconcile([self.row("ta", "a"), self.row("tb", "b", 0)],
                             [self.row("ta", "a"), self.row("tb", "b", 1)],
                             [self.row("ta", "a"), self.row("tb", "b", 0)])
        self.assertEqual(1, out["items"][1]["depth"])

    def test_the_rails_own_edit_of_a_row_wins(self):
        out = self.reconcile([self.row("ta", "mine")],
                             [self.row("ta", "theirs")],
                             [self.row("ta", "orig")])
        self.assertFalse(out["changed"])
        self.assertEqual("mine", out["items"][0]["text"])

    def test_the_rails_own_new_rows_are_left_alone(self):
        a, mine = self.row("ta", "a"), self.row("tnew", "just typed")
        out = self.reconcile([a, mine], [a], [a])
        self.assertFalse(out["changed"])
        self.assertEqual(["ta", "tnew"], [r["id"] for r in out["items"]])
        # A row the rail has that the base never saw keeps the rail's text
        # even when the store holds another: without a base there is no
        # saying the rail left it alone.
        out = self.reconcile([self.row("ta", "mine")], [self.row("ta", "theirs")], [])
        self.assertEqual("mine", out["items"][0]["text"])

    def test_the_stores_state_is_not_the_reconciles_business(self):
        # Status and question are laid by todoLayState; reconcile leaves
        # them exactly as the rail has them.
        out = self.reconcile([self.row("ta", "a", 0, "")],
                             [self.row("ta", "a", 0, "done")],
                             [self.row("ta", "a")])
        self.assertEqual("", out["items"][0]["status"])

    # --- through the rail itself ------------------------------------------

    RAIL_DOM = (
        "var host = document.createElement('div'); host.className = 'hc-todos';"
        "var list = document.createElement('div'); list.className = 'hc-todos-list';"
        "var acts = document.createElement('div'); acts.className = 'hc-todos-actions';"
        "host.appendChild(list); host.appendChild(acts); panel.appendChild(host);"
        "var tabs = document.createElement('span'); tabs.className = 'hc-rail-tabs';"
        "panel.appendChild(tabs);"
        "window.__hcPromptUI.acceptState({scope: 'chat', revision: 'r1', prompts: [],"
        "  goals: [{id: 'g1', title: 'A', parent_goal_id: null, status: 'active',"
        "           prompt_ids: [], sources: []}]});"
        "function put(rows) {"
        "  var goals = [{id: 'g1', title: 'A', todo_items: rows, todos_md: '',"
        "                prompt_md: '', children: []}];"
        "  store['hc-vault-ui-v1'] = JSON.stringify({v: 7, goals: goals, selId: 'g1'});"
        "  store['hc-vault-ui-sync-v1'] = JSON.stringify({revision: 'r1', goals: goals});"
        "}"
        "var A = {id: 'ta', text: 'one', depth: 0, status: '', question: ''};"
        "var B = {id: 'tb', text: 'restored', depth: 0, status: '', question: ''};"
    )

    def test_a_row_the_store_gained_while_the_rail_had_focus_survives_its_save(self):
        # The rail loads [A]; the caret sits in it. The store gains B (a
        # sync landed it). The reader types into A and the rail saves.
        # Before: the save wrote [A] and the server read B as deleted.
        out = self.run_js(
            self.RAIL_DOM
            + "put([A]);"
            "window.__hcPromptUI.renderTodoRail();"
            "var loaded = window.__hcPromptUI.todoState().items.map(function (r) { return r.id; });"
            "put([A, B]);"
            "document.activeElement = list;"
            "window.__hcPromptUI.renderTodoRail();"
            "var seen = window.__hcPromptUI.todoState().items.map(function (r) { return r.id; });"
            "window.__hcPromptUI.todoState().items[0].text = 'one, edited';"
            "listeners.filter(function (l) { return l[0] === 'blur'; })"
            "  .forEach(function (l) { l[1]({target: list}); });"
            "out = new Promise(function (r) { setTimeout(r, 0); }).then(function () {"
            "  var posted = calls.filter(function (c) { return c[0] === '/api/import'; });"
            "  var rows = posted.length ? posted[posted.length - 1][1].goals[0].todo_items : null;"
            "  return [loaded, seen, rows && rows.map(function (r) { return [r.id, r.text]; })];"
            "});")
        self.assertEqual(["ta"], out[0])
        self.assertEqual(["ta", "tb"], out[1],
                         "a row the store gained must reach a rail mid-edit")
        self.assertEqual([["ta", "one, edited"], ["tb", "restored"]], out[2],
                         "the rail's save must carry the row it never typed")

    def test_a_blank_the_rail_loaded_does_not_wipe_text_typed_elsewhere(self):
        # This window loaded the row empty (as Enter leaves it); another
        # window typed into it. This window's save must not blank it back.
        out = self.run_js(
            self.RAIL_DOM
            + "var E = {id: 'te', text: '', depth: 0, status: '', question: ''};"
            "put([A, E]);"
            "window.__hcPromptUI.renderTodoRail();"
            "document.activeElement = list;"
            "put([A, {id: 'te', text: 'typed in the other window', depth: 0,"
            "         status: '', question: ''}]);"
            "window.__hcPromptUI.todoState().items[0].text = 'one, edited';"
            "listeners.filter(function (l) { return l[0] === 'blur'; })"
            "  .forEach(function (l) { l[1]({target: list}); });"
            "out = new Promise(function (r) { setTimeout(r, 0); }).then(function () {"
            "  var posted = calls.filter(function (c) { return c[0] === '/api/import'; });"
            "  var rows = posted[posted.length - 1][1].goals[0].todo_items;"
            "  return rows.map(function (r) { return [r.id, r.text]; });"
            "});")
        self.assertEqual([["ta", "one, edited"], ["te", "typed in the other window"]], out)


class HoldGroundTests(BridgeTestCase):
    """What the viewport is painted while the workspace is held back.

    The server masks the document it serves, but the artifact unpacks by
    replacing documentElement, which throws that mask away mid-hold. From
    there the canvas falls through to the artifact's own white body -- so the
    hold has to carry its own ground onto whatever root it now holds.
    """

    def ground(self, saved=None):
        return self.run_js(
            "localStorage.setItem('hc-vault-ui-v1', %s);"
            "out = window.__hcPromptUI.groundColor();"
            % json.dumps(json.dumps(saved or {})))

    def test_a_chat_workspace_is_held_on_its_own_dark_ground(self):
        self.assertEqual("#0d1117", self.ground())

    def test_a_reader_who_chose_light_is_held_on_light(self):
        self.assertEqual("#fff", self.ground({"themeMode": "light"}))

    def test_holding_paints_the_root_and_releasing_gives_it_back(self):
        held, shown = self.run_js(
            "var root = document.documentElement;"
            "window.__hcPromptUI.holdRoot(root);"
            "var held = [root.style.visibility, root.style.background];"
            "window.__hcPromptUI.releaseRoot(root);"
            "out = [held, [root.style.visibility, root.style.background]];")
        self.assertEqual(["hidden", "#0d1117"], held,
                         "a held root must hide itself and keep the ground")
        self.assertEqual(["", ""], shown,
                         "releasing must hand both back to the workspace")


class DeletedGoalTests(BridgeTestCase):
    """A goal the reader deleted is kept on disk but not drawn."""

    STATE = {"goals": [
        {"id": "g1", "title": "keep this one", "status": "active",
         "parent_goal_id": None},
        {"id": "g2", "title": "deleted one", "status": "abandoned",
         "parent_goal_id": None},
        {"id": "g3", "title": "finished one", "status": "completed",
         "parent_goal_id": None},
    ], "prompts": []}

    def test_an_abandoned_goal_is_left_out_of_the_tree(self):
        titles = [n["title"] for n in self.roots(self.STATE)]
        self.assertEqual(["keep this one", "finished one"], titles)

    def test_a_completed_goal_is_still_drawn(self):
        done = [n for n in self.roots(self.STATE) if n["title"] == "finished one"]
        self.assertEqual(1, len(done))
        self.assertTrue(done[0]["done"], "completed still renders struck through")


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

    def test_a_goal_a_stale_writer_lost_is_kept_and_a_tombstoned_one_is_not(self):
        # The server never erases a deleted goal: it stays in the payload,
        # marked abandoned. So a goal missing from the remote TREE splits two
        # ways -- named in the tombstones: deleted, drop it; not even
        # mentioned: a stale writer lost it, keep the local copy so the next
        # import puts it back. This is the add-a-goal-and-watch-it-vanish
        # bug: an analyzer or an old tab rewrote goals.json without the goal
        # the reader had just made.
        result = self.run_js(
            """(() => {
              const node = (id, title, extra) => Object.assign({
                id, title, prio: "normal", done: false, open: true,
                status: "todo", notes: "", desc: "", labels: [],
                children: []
              }, extra || {});
              const base = [node("g1", "Shared"), node("g-lost", "Just made"),
                            node("g-gone", "Deleted for real")];
              const local = base.map((n) => Object.assign({}, n));
              const remote = [node("g1", "Shared")];
              return window.__hcPromptUI.mergeTrees(
                base, local, remote, { "g-gone": true });
            })()""")
        self.assertEqual({"g1", "g-lost"},
                         set(goal["id"] for goal in result))

    def test_a_deleted_goal_a_stale_writer_resurrected_stays_deleted(self):
        # The mirror image: after the delete, base and local both lack the
        # goal, and a stale writer puts it back in the payload as active.
        # Without memory that looks like a goal somebody else just made; the
        # tombstone set says otherwise, and the merge drops it.
        result = self.run_js(
            """(() => {
              const node = (id, title) => ({
                id, title, prio: "normal", done: false, open: true,
                status: "todo", notes: "", desc: "", labels: [],
                children: []
              });
              const base = [node("g1", "Shared")];
              const local = [node("g1", "Shared")];
              const remote = [node("g1", "Shared"),
                              node("g-back", "Resurrected")];
              return window.__hcPromptUI.mergeTrees(
                base, local, remote, { "g-back": true });
            })()""")
        self.assertEqual(["g1"], [goal["id"] for goal in result])

    def test_a_child_rerooted_by_a_stale_writer_keeps_its_parent(self):
        # Losing the parent record makes the server re-root the child; the
        # local tree still holds the link and did not change it, so the
        # merge keeps it -- alongside the re-imported parent.
        result = self.run_js(
            """(() => {
              const node = (id, title, children) => ({
                id, title, prio: "normal", done: false, open: true,
                status: "todo", notes: "", desc: "", labels: [],
                children: children || []
              });
              const base = [node("g1", "Root", [node("g2", "Child")])];
              const local = [node("g1", "Root", [node("g2", "Child")])];
              const remote = [node("g2", "Child")];   // g1 lost, g2 re-rooted
              return window.__hcPromptUI.mergeTrees(base, local, remote, {});
            })()""")
        self.assertEqual(["g1"], [g["id"] for g in result])
        self.assertEqual(["g2"], [c["id"] for c in result[0]["children"]])

    def test_reconcile_remembers_a_local_deletion_as_a_tombstone(self):
        got = self.run_js(
            "localStorage.setItem('hc-vault-ui-sync-v1', JSON.stringify({"
            "  revision: 'r1', goals: [{ id: 'g1', title: 'kept', children: [] },"
            "                          { id: 'g2', title: 'deleted', children: [] }] }));"
            "localStorage.setItem('hc-vault-ui-v1', JSON.stringify({"
            "  goals: [{ id: 'g1', title: 'kept', children: [] }] }));"
            "window.location = window.location || {};"
            "window.location.reload = function () {};"
            "window.__hcPromptUI.reconcileState({ revision: 'r2', goals: ["
            "  { id: 'g1', title: 'kept', status: 'active' } ] });"
            "JSON.parse(localStorage.getItem('hc-deleted-goals-v1') || '{}');")
        self.assertIn("g2", got)


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

    def test_the_inspector_opens_on_context_when_the_reader_loads_it(self):
        # Restoring the last pane meant landing on AGENT or REVIEW for a goal
        # that had neither. The one exception is a reload we forced ourselves,
        # which has to put the reader back where it interrupted them.
        out = self.patched_bundle("out;")
        self.assertIn("? saved.paneTab : 'context',", out)
        self.assertIn("saved && saved.hcKeepPane", out)

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
class NoDemoDescriptionBackfillTests(BridgeTestCase):
    """The artifact's demo copy must never become a real goal's description.

    Its constructor filled any empty `desc` from a map keyed by the sample
    tree's own ids -- g1, g2, g3, g4 among them -- and those are exactly the
    ids the vault mints (`goals.next_goal_id`, `chat_synth`'s schema). So the
    first control that persisted anything, including a filter chip or the
    theme toggle, wrote four sentences nobody had written onto the reader's
    goals, and from there into the prompt they copied.
    """

    def artifact_source(self):
        return json.loads(re.search(
            r'<script type="__bundler/template">\s*([\s\S]*?)\s*</script>',
            BUNDLE.read_text()).group(1))

    def test_the_backfill_is_never_called_in_either_scope(self):
        for scope in (None, "chat"):
            with self.subTest(scope=scope or "global"):
                self.assertNotIn("ad(g0)",
                                 self.patched_bundle("out;", scope=scope))

    def test_the_artifact_itself_still_carries_the_collision(self):
        # A control. If the artifact ever stopped keying its demo copy by the
        # ids the vault mints, the test above would pass for the wrong reason
        # and this one would say so.
        source = self.artifact_source()
        self.assertIn("ad(g0)", source)
        self.assertIn("g1: 'Stand up the shared goal model", source)

    def test_a_real_tree_leaves_the_constructor_as_it_arrived(self):
        # Runs the emitted constructor body over goals shaped like the ones
        # the vault mints, rather than trusting the absence of a call.
        got = json.loads(self.patched_bundle(
            "var at = out.indexOf('const D = {');"
            "var body = out.slice(at, out.indexOf('this.state = {', at));"
            "var tree = [{ id: 'g1', desc: '', children: ["
            "  { id: 'a1', desc: '', children: [] }] },"
            "  { id: 'g2', desc: '', children: [] }];"
            "eval('(function (g0) {' + body + '\\nreturn g0; })')(tree);"
            "JSON.stringify(tree);", scope="chat"))
        self.assertEqual(
            ["", "", ""],
            [got[0]["desc"], got[0]["children"][0]["desc"], got[1]["desc"]])


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class NoDemoContextTests(BridgeTestCase):
    """The artifact's second demo door: its context defaults.

    `contextOf` sets every text field explicitly, including to "", so a goal
    the server knows about never reaches these. A goal the reader adds in the
    tree does: the artifact mints it with `ctx: null`, and until the next
    reload its assembled prompt carried a demo objective, a demo GitHub repo
    and a demo document -- text the reader then pastes into a real session.
    """

    def test_the_context_defaults_hold_nothing(self):
        for scope in (None, "chat"):
            with self.subTest(scope=scope or "global"):
                out = self.patched_bundle("out;", scope=scope)
                self.assertIn("const CTXDEF = {};", out)
                self.assertIn("const CODEDEF = [];", out)
                self.assertIn("const DOCDEF = [];", out)
                self.assertNotIn("Get the drawable frame populating", out)
                self.assertNotIn("divadbaroon/claude-plugins", out)
                self.assertNotIn("design-notes.md", out)

    def test_the_artifact_itself_still_carries_them(self):
        # A control, as above: these tests must fail for the right reason.
        source = json.loads(re.search(
            r'<script type="__bundler/template">\s*([\s\S]*?)\s*</script>',
            BUNDLE.read_text()).group(1))
        self.assertIn("Get the drawable frame populating", source)
        self.assertIn("divadbaroon/claude-plugins", source)


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
        # the title (as the header input) and its status stay
        self.assertIn("{{ titleRaw }}", out)
        self.assertIn("{{ stBadge }}", out)

    def test_the_title_is_edited_in_the_header(self):
        # The heading div gave way to an input bound to the same goal: blur
        # and keydown handlers reach the state map, and no second binding of
        # the read-only heading survives in the markup.
        out = self.patched_bundle("out;")
        self.assertIn('sc-camel-on-blur="{{ titleBlur }}"', out)
        self.assertIn('sc-camel-on-key-down="{{ titleKey }}"', out)
        self.assertIn('ref="{{ titleRef }}"', out)
        self.assertNotIn("{{ selTitle }}", out)

    def test_the_description_box_is_gone(self):
        # The notes document is the description; the textarea under the
        # title no longer renders, while its handlers stay dormant.
        out = self.patched_bundle("out;")
        self.assertNotIn("{{ descVal }}", out)
        self.assertNotIn("Add a description", out)

    def test_a_new_top_level_goal_is_added_at_the_top(self):
        # addUnder(null) prepends; a subgoal still appends under its parent,
        # whose add control sits below the children it joins. The cursor is
        # sent to the header input rather than a sidebar row: editId stays
        # null and the focus flag carries the new goal's id.
        out = self.patched_bundle("out;")
        self.assertIn("[n].concat(s.goals)", out)
        self.assertNotIn("s.goals.concat([n])", out)
        self.assertIn("children: (x.children || []).concat([n])", out)
        self.assertIn("this._focusTitle = n.id", out)

    def test_the_selected_goal_always_offers_an_add_subgoal_row(self):
        # The add row used to render only under goals that already had
        # children, so a childless goal offered no way in to a subgoal.
        out = self.patched_bundle("out;")
        self.assertIn("if (open && (kids.length || isSel)) rows.push({", out)
        self.assertNotIn("if (open && kids.length) rows.push({", out)

    def test_a_held_arrow_key_keeps_walking_the_tree(self):
        # Key repeat is let through for ArrowUp/ArrowDown only; every other
        # shortcut still fires once per press, so a held cmd+enter or
        # cmd+backspace cannot pour goals in or out.
        out = self.patched_bundle("out;")
        self.assertIn("if (e.repeat && e.key !== 'ArrowDown' "
                      "&& e.key !== 'ArrowUp') return;", out)
        self.assertNotIn("if (e.repeat) return;", out)

    def test_marking_done_cascades_to_children_and_folds_the_branch(self):
        # Both doors to 'done' -- the row's check and the inspector's status
        # control -- mark every child done and collapse the goal, instead of
        # touching one node and leaving live children hidden under a strike.
        out = self.patched_bundle("out;")
        self.assertNotIn("x => ({ ...x, done: !x.done })", out)
        self.assertNotIn("k === 'done' ? { ...x, done: true }", out)
        self.assertEqual(2, out.count(
            "const dn = (g) => ({ ...g, done: true, "
            "children: (g.children || []).map(dn) });"))
        self.assertIn("x => x.done ? { ...x, done: false } "
                      ": { ...dn(x), open: false }", out)
        self.assertIn("k === 'done' ? { ...dn(x), open: false }", out)

    def test_the_shipped_cascade_marks_every_descendant_done(self):
        # Run the exact lambda the patch ships, not a re-derivation of it.
        marked = self.run_js(
            "const dn = (g) => ({ ...g, done: true, "
            "children: (g.children || []).map(dn) });"
            "out = { ...dn({ id: 'p', done: false, children: ["
            "  { id: 'c1', done: false, children: ["
            "    { id: 'c1a', done: false }] },"
            "  { id: 'c2', done: false }] }), open: false };")
        self.assertTrue(marked["done"])
        self.assertFalse(marked["open"])
        self.assertTrue(all(c["done"] for c in marked["children"]))
        self.assertTrue(marked["children"][0]["children"][0]["done"])

    # --- sideways on the keyboard -------------------------------------------
    # Up and down walk the drawn rows; left and right move across the
    # hierarchy: right opens a folded branch or steps into its first drawn
    # child, left folds an open branch or steps out to the parent.

    TREE = ("[{ id: 'p', open: true, children: ["
            "   { id: 'c1', open: false, children: [{ id: 'c1a' }] },"
            "   { id: 'c2', open: true, children: [] }] },"
            " { id: 'q', open: true, children: [{ id: 'q1' }] }]")

    def step(self, rows, sel, back, tree=None):
        return self.run_js(
            "out = window.__hcPromptUI.treeStep(%s, %s, %s, %s);"
            % (tree or self.TREE, json.dumps(rows), json.dumps(sel),
               "true" if back else "false"))

    def test_right_on_an_open_branch_steps_into_its_first_drawn_child(self):
        self.assertEqual({"selId": "c1"},
                         self.step(["p", "c1", "c2", "q", "q1"], "p", False))

    def test_right_on_a_folded_branch_opens_it_without_moving(self):
        self.assertEqual({"fold": {"id": "c1", "open": True}},
                         self.step(["p", "c1", "c2", "q", "q1"], "c1", False))

    def test_right_on_a_leaf_or_an_open_branch_with_no_drawn_child_is_nothing(self):
        self.assertIsNone(self.step(["p", "c1", "c2", "q", "q1"], "c2", False))
        # Open, with children, but the filter hides every one of them: the
        # row after it is a sibling, not a child, so there is nowhere to go.
        self.assertIsNone(self.step(["p", "q"], "p", False))

    def test_left_on_an_open_branch_with_drawn_children_folds_it(self):
        self.assertEqual({"fold": {"id": "p", "open": False}},
                         self.step(["p", "c1", "c2", "q", "q1"], "p", True))

    def test_left_on_a_folded_branch_or_a_leaf_steps_out_to_the_parent(self):
        self.assertEqual({"selId": "p"},
                         self.step(["p", "c1", "c2", "q", "q1"], "c1", True))
        self.assertEqual({"selId": "p"},
                         self.step(["p", "c1", "c2", "q", "q1"], "c2", True))
        # Open but nothing drawn under it: folding would change nothing
        # the eye can see, so it steps out instead.
        self.assertEqual({"selId": "p"},
                         self.step(["p", "c2", "q"], "c2", True))

    def test_left_on_a_folded_root_is_nothing(self):
        self.assertIsNone(self.step(["p", "q"], "p", True,
                                    tree="[{ id: 'p', open: false, children: "
                                         "[{ id: 'x' }] }, { id: 'q' }]"))
        self.assertIsNone(self.step(["p"], "nope", True))
        self.assertIsNone(self.step(["p"], None, True))

    def test_the_keyboard_handler_asks_treestep_for_left_and_right(self):
        out = self.patched_bundle("out;")
        self.assertIn("if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') "
                      "{ if (typing) return; const step = "
                      "window.__hcPromptUI && window.__hcPromptUI.treeStep("
                      "this.state.goals, this._rowIds || [], "
                      "this.state.selId, e.key === 'ArrowLeft');", out)
        # Both outcomes are acted on: a fold rewrites the node, a move
        # reselects and scrolls the row into view the way up/down do.
        self.assertIn("if (step.fold) this.set(s => ({ goals: this.up("
                      "s.goals, step.fold.id, x => ({ ...x, "
                      "open: step.fold.open })) }));", out)
        self.assertIn("else this.set(() => ({ selId: step.selId, "
                      "editId: null }));", out)
        # Up and down still walk the rows as before.
        self.assertIn("if (e.key === 'ArrowDown' || e.key === 'ArrowUp') "
                      "{ if (typing) return; nav(e.key === 'ArrowUp'); "
                      "return; }", out)

    # --- folds survive a reload ---------------------------------------------
    # Which branches are folded is a view preference the server never hears
    # of. A tree rebuilt from the payload used to open every branch, so a
    # reload -- or the reload that follows any server-side change -- undid
    # every fold the reader had made.

    def test_a_tree_rebuilt_from_the_payload_keeps_the_readers_folds(self):
        roots = self.run_js(
            "store['hc-vault-ui-v1'] = JSON.stringify({ v: 7, goals: ["
            "  { id: 'g1', open: false, children: ["
            "    { id: 'g1a', open: true, children: [] }] }] });"
            "window.__hcPromptUI.rootsFromState(%s);" % json.dumps(STATE))
        self.assertFalse(roots[0]["open"])
        self.assertTrue(roots[0]["children"][0]["open"])

    def test_a_tree_with_nothing_remembered_opens_every_branch(self):
        roots = self.roots()
        self.assertTrue(roots[0]["open"])
        self.assertTrue(roots[0]["children"][0]["open"])

    def test_a_reload_keeps_the_folded_branches_folded(self):
        # The boot path writes the store from the payload. The fold the
        # store already held comes through, and so does the synced base --
        # or the next merge would read the fold as an edit of the payload.
        got = self.run_js(
            "store['hc-vault-ui-v1'] = JSON.stringify({ v: 7, selId: 'g1',"
            "  goals: [{ id: 'g1', open: false, children: ["
            "    { id: 'g1a', open: true, children: [] }] }] });"
            "window.__hcPromptUI.seedForTest();"
            "var saved = JSON.parse(store['hc-vault-ui-v1']);"
            "var base = JSON.parse(store['hc-vault-ui-sync-v1']);"
            "out = [saved.goals[0].open, saved.goals[0].children[0].open,"
            "       base.goals[0].open];")
        self.assertEqual([False, True, False], got)

    def test_a_fold_is_not_an_edit_the_server_is_asked_to_take(self):
        # A server-side change arrives while a branch is folded here. The
        # merge must not see the fold as a local edit of the payload -- that
        # posted an import carrying nothing, and the reload after it
        # unfolded the branch anyway.
        got = json.loads(self.run_js(
            "var reloads = 0;"
            "window.location = { reload: function () { reloads++; } };"
            "function state(rev, title) { return { scope: 'global',"
            "  revision: rev, prompts: [], agent_runs: {},"
            "  goals: [{ id: 'g1', title: title, parent_goal_id: null,"
            "            status: 'active' },"
            "          { id: 'g1a', title: 'sub', parent_goal_id: 'g1',"
            "            status: 'active' }] }; }"
            "window.__hcPromptUI.acceptState(state('r5', 'Ship it'));"
            "window.__hcPromptUI.reconcileState(state('r5', 'Ship it'));"
            "var before = reloads;"
            "var saved = JSON.parse(store['hc-vault-ui-v1']);"
            "saved.goals[0].open = false;"   # the reader folds g1
            "store['hc-vault-ui-v1'] = JSON.stringify(saved);"
            "calls.length = 0;"
            "window.__hcPromptUI.acceptState(state('r6', 'Ship it now'));"
            "window.__hcPromptUI.reconcileState(state('r6', 'Ship it now'));"
            "var after = JSON.parse(store['hc-vault-ui-v1']);"
            "var imports = calls.filter(function (c) {"
            "  return String(c[0]).indexOf('/api/import') >= 0; }).length;"
            "JSON.stringify([reloads - before, imports, after.goals[0].title,"
            "  after.goals[0].open]);"))
        self.assertEqual([1, 0, "Ship it now", False], got)

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

    def test_a_finished_run_brings_the_page_with_it(self):
        # Same goals, same revision — only the work changed. Without this the
        # artifact sat in the payload while the page showed the run still going.
        got = json.loads(self.run_js(
            "var reloads = 0;"
            "window.location = { reload: function () { reloads++; } };"
            "function state(runs) { return { scope: 'global', revision: 'r1',"
            "  goals: [{ id: 'g1', title: 'Ship it', parent_goal_id: null,"
            "    status: 'active' }], prompts: [], agent_runs: runs }; }"
            "var RUNNING = { g1: [{ status: 'running', tasks: [], files: [] }] };"
            "var DONE = { g1: [{ status: 'finished', tasks: [],"
            "  summary: 'what I did', files: [{ path: 'a.js', edits: 3 }] }] };"
            "store['hc-vault-ui-v1'] = JSON.stringify({ v: 7, selId: 'g1',"
            "  paneTab: 'artifact',"
            "  goals: window.__hcPromptUI.rootsFromState(state(RUNNING)) });"
            "window.__hcPromptUI.acceptState(state(RUNNING));"
            "window.__hcPromptUI.reconcileState(state(RUNNING));"
            "var before = reloads;"
            "window.__hcPromptUI.acceptState(state(DONE));"
            "window.__hcPromptUI.reconcileState(state(DONE));"
            "var saved = JSON.parse(store['hc-vault-ui-v1']);"
            "JSON.stringify([before, reloads, saved.paneTab,"
            "  saved.hcKeepPane === true]);"))
        self.assertEqual([0, 1, "artifact", True], got)

    def test_the_reader_stays_where_they_were_watching(self):
        # A page the reader loaded should open on CONTEXT. One forced on them
        # because a run finished should put them back on REVIEW.
        out = self.patched_bundle("out;")
        self.assertIn("paneTab: (saved && saved.hcKeepPane"
                      " && ['prompt', 'agent', 'artifact', 'context']"
                      ".indexOf(saved.paneTab) >= 0) ? saved.paneTab"
                      " : 'context',", out)

    def test_the_marker_is_spent_once_it_has_been_read(self):
        # Left set, every later reload would land on whichever pane happened
        # to be open when a run last finished.
        left = self.run_js(
            "store['hc-vault-ui-v1'] = JSON.stringify({ v: 7, selId: 'g1',"
            "  paneTab: 'artifact', hcKeepPane: true, goals: [] });"
            "window.__hcPromptUI.clearKeepPane();"
            "JSON.parse(store['hc-vault-ui-v1']).hcKeepPane === undefined;")
        self.assertTrue(left)

    def test_the_pane_stops_waiting_for_a_reload_to_notice_a_run(self):
        # The revision covers the goal tree; a run starting changes what a
        # node says about itself without changing the tree, so the stored copy
        # stayed right about the goals and stale about the work.
        shapes = json.loads(self.run_js(
            "function roots(runs, claim) {"
            "  return window.__hcPromptUI.rootsFromState({ scope: 'global',"
            "    revision: 'r1', goals: [{ id: 'g1', title: 'x' }],"
            "    prompts: [], agent_runs: runs || {},"
            "    agent_claim: claim || null }); }"
            "var shape = window.__hcPromptUI.paneShape;"
            "JSON.stringify({"
            "  idle: shape(roots()),"
            "  armed: shape(roots({}, { goal_id: 'g1' })),"
            "  running: shape(roots({ g1: [{ status: 'running', tasks: [] }] })),"
            "  ticked: shape(roots({ g1: [{ status: 'running', tasks: ["
            "    { task_id: '1', subject: 'a', status: 'completed' }] }] })),"
            "  done: shape(roots({ g1: [{ status: 'finished', tasks: [],"
            "    files: [{ path: 'a.js' }] }] })) });"))
        self.assertNotEqual(shapes["idle"], shapes["armed"])
        self.assertNotEqual(shapes["idle"], shapes["running"])
        self.assertNotEqual(shapes["running"], shapes["done"])

    def test_progress_within_a_run_is_not_worth_a_reload(self):
        # The feed draws task-by-task progress straight into the DOM.
        # Reloading the page for it would make the pane unusable.
        shapes = json.loads(self.run_js(
            "function roots(tasks) {"
            "  return window.__hcPromptUI.rootsFromState({ scope: 'global',"
            "    revision: 'r1', goals: [{ id: 'g1', title: 'x' }],"
            "    prompts: [], agent_runs: { g1: [{ status: 'running',"
            "      tasks: tasks }] } }); }"
            "var shape = window.__hcPromptUI.paneShape;"
            "JSON.stringify([shape(roots([])), shape(roots(["
            "  { task_id: '1', subject: 'a', status: 'completed' }]))]);"))
        self.assertEqual(shapes[0], shapes[1])

    def test_the_accent_box_waits_for_something_to_put_in_it(self):
        out = self.patched_bundle("out;")
        at = out.index("{{ artSummary }}")
        self.assertIn("{{ artHasSummary }}", out[at - 400:at])
        self.assertIn("artHasSummary: !!(art && String(art.summary || '')"
                      ".trim())", out)

    def test_the_decision_is_greyed_until_there_is_one_to_take(self):
        out = self.patched_bundle("out;")
        self.assertIn("{{ artDecideC }}", out)
        self.assertIn("{{ artApproveBg }}", out)
        self.assertIn("{{ artDecideCur }}", out)
        # and greyed is not merely cosmetic: the handlers refuse early
        self.assertIn("artApprove: () => { if (!(art && String(art.summary "
                      "|| '').trim())) return;", out)
        self.assertIn("revOpenFn: () => { if (art && String(art.summary || '')"
                      ".trim()) this.setState({ revOpen: true }); },", out)

    def test_the_section_is_named_for_what_it_holds(self):
        out = self.patched_bundle("out;")
        self.assertIn(">FINAL ARTIFACT</span>", out)
        self.assertNotIn(">ARTIFACT</span>", out)

    def test_review_opens_on_a_launch_before_any_artifact_exists(self):
        # Pressing run switches to REVIEW. If the tab only existed once an
        # artifact did, that switch landed on a pane that said to press run.
        out = self.patched_bundle("out;")
        self.assertIn("showReviewTab: !!(art || (sel && sel.agent"
                      " && (sel.agent.status === 'running'"
                      " || sel.agent.status === 'waiting')))", out)

    def test_the_feed_has_somewhere_to_draw_without_an_artifact(self):
        # Both anchors used to live inside the artifact card, so a run with
        # no artifact yet had nowhere to report itself.
        out = self.patched_bundle("out;")
        empty = out[out.index("{{ artEmpty }}"):out.index("{{ hasArtifact }}")]
        self.assertIn('<div class="hc-live"></div>', empty)
        self.assertIn('<div class="hc-live-rest"></div>', empty)

    def test_the_invitation_to_run_goes_quiet_once_a_run_starts(self):
        out = self.patched_bundle("out;")
        self.assertIn("artIdle: !art && !(sel && sel.agent"
                      " && (sel.agent.status === 'running'"
                      " || sel.agent.status === 'waiting')),", out)
        empty = out[out.index("{{ artEmpty }}"):out.index("{{ hasArtifact }}")]
        self.assertIn("{{ artIdle }}", empty)
        self.assertIn("No artifact yet", empty)

    def test_a_launched_run_reports_itself_before_the_first_hook(self):
        # The run record only exists once a hook fires. Until then there is a
        # real state — a terminal open with the prompt typed — and no row to
        # carry it, which read as the button having done nothing.
        rows = json.loads(self.run_js(
            "var top = document.createElement('div');"
            "top.className = 'hc-live';"
            "document.body.appendChild(top);"
            "window.__hcPromptUI.renderLive('g1', "
            "  [{ state: 'starting', did: [], checked: [] }]);"
            "JSON.stringify(top.children[0].children.map(function (c) "
            "{ return [c.className, c.textContent]; }));"))
        self.assertEqual([["hc-live-head",
                           "Starting \u2014 the terminal is open with your "
                           "prompt; press Enter there"]], rows)

    def test_the_feed_invents_that_row_only_for_a_real_claim(self):
        src = BRIDGE.read_text()
        feed = src[src.index("function watchRunFeed"):]
        self.assertIn("if (!rows.length && serverState.claim", feed)
        self.assertIn("&& serverState.claim.goal_id === id) {", feed)
        self.assertIn('rows = [{ state: "starting", did: [], checked: [] }];',
                      feed)

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
        # that the target exists inside REVIEW and nowhere else. There are two
        # anchors — one for a run with an artifact, one for a run without —
        # and artEmpty/hasArtifact are exclusive, so only ever one is drawn.
        out = self.patched_bundle("out;")
        pane = out.index('value="{{ showArt }}"')
        self.assertGreater(out.index('<div class="hc-live"></div>'), pane)
        self.assertEqual(2, out.count('class="hc-live"'))
        self.assertIn("artEmpty: !art, hasArtifact: !!art,", out)

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
        header = out.index(">FINAL ARTIFACT</span>")
        slot = out.index('<div class="hc-live-open-slot"></div>')
        self.assertLess(header, slot)
        self.assertLess(slot, out.index("{{ artSummary }}"))

    def test_created_sits_opposite_the_decision_on_the_cards_last_line(self):
        out = self.patched_bundle("out;")
        card = out.index("background:var(--panel2);padding:9px 12px")
        row = out.index("justify-content:space-between;align-items:center;"
                        "gap:16px;flex-wrap:wrap", card)
        created = out.index("{{ artWhen }}")
        decide = out.index("request revisions")
        end = out.index('<div class="hc-live-rest">', card)
        self.assertLess(row, created)
        self.assertLess(created, decide)
        self.assertLess(decide, end)
        # The stamp is not inside the conditional: withdrawing the buttons
        # while a revision is being written must not take the date with them.
        self.assertLess(created, out.index("{{ revClosed }}", row))

    def test_the_card_holds_the_run_and_activity_stands_apart(self):
        out = self.patched_bundle("out;")
        card = out.index("background:var(--panel2);padding:9px 12px")
        state = out.index('<div class="hc-live"></div>', card)
        summary = out.index("{{ artSummary }}")
        created = out.index("{{ artWhen }}")
        decide = out.index("request revisions")
        log = out.index('<div class="hc-live-rest"></div>', card)
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
        # one anchor per branch: with an artifact, and without
        self.assertEqual(2, out.count('class="hc-live"'))

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

    def test_agent_reads_name_then_prompt_then_run(self):
        # The notes box left this pane for CONTEXT, where the document the
        # user writes now lives; the rest of the AGENT order is unchanged.
        out = self.patched_bundle("out;")
        self.assertLess(out.index(">AGENT</div>"), out.index("hc-promptbox"))
        self.assertLess(out.index("hc-promptbox"), out.index("AGENT STATUS"))
        self.assertLess(out.index("AGENT STATUS"), out.index("{{ runAgent }}"))

    def test_the_notes_box_is_the_context_pane_itself(self):
        out = self.patched_bundle("out;")
        self.assertIn("showNotes: !!sel && paneTab === 'context'", out)
        self.assertNotIn("showNotes: !!sel && paneTab === 'agent'", out)
        self.assertNotIn("showNotes: !!sel && paneTab === 'prompt'", out)

    def test_the_pane_says_what_it_is_for(self):
        self.assertIn("Run Claude Code on this goal with the self-contained "
                      "context Vault has assembled. Progress appears in "
                      "REVIEW.", self.patched_bundle("out;"))

    def test_the_draft_does_not_restate_the_goal_title(self):
        out = self.patched_bundle("out;")
        self.assertNotIn("I am working on the goal:", out)
        self.assertNotIn("Within the main goal", out)

    def test_the_notes_box_invites_the_users_own_thoughts(self):
        # It is the goal's whole document now, not an addendum to a prompt,
        # so the invitation names the markup it renders as you type.
        out = self.patched_bundle("out;")
        self.assertIn("Write in markdown \u2014 # heading, - list, - [ ] task, "
                      "**bold**, `code`", out)
        self.assertNotIn("Add any other thoughts you would like the agent to "
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
        self.assertIn("showReviewTab: !!(art ||", out)

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
                 ">DOCUMENT CONTEXT</span>",
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
              ("'Objective'", "'Where this sits:", "'Code context:",
               "'Document context:", "'Related prompts, in my own words:",
               "'In my words'", "'Decisions'", "'Built'", "'Blockers'",
               "'Open questions'")]
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
        self.assertLess(draft.index("'Open questions'"),
                        draft.index("Implement this subgoal for me."))

    def test_a_prompt_row_carries_only_the_control_that_undoes_the_link(self):
        # The list is the record of what was said, not a place to act on it
        # -- except for the one act the record itself can be wrong about:
        # inference tying a prompt to the wrong goal.
        out = self.patched_bundle("out;")
        self.assertIn('sc-camel-on-click="{{ hr.del }}"', out)
        self.assertIn('title="Unlink this prompt"', out)
        self.assertNotIn("{{ hr.copy }}", out)
        self.assertNotIn("{{ hr.use }}", out)

    def test_each_prompt_names_the_conversation_it_came_from(self):
        # A quote without a source cannot be checked.
        out = self.patched_bundle("out;")
        self.assertIn("{{ hr.conv }}", out)
        # The separator travels with the source, so a prompt that has no
        # conversation leaves no punctuation behind.
        self.assertIn(
            "conv: p.conv ? ' \u00b7 conversation ' + p.conv : ''", out)
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

    def test_the_words_sit_under_the_document_they_are_evidence_for(self):
        # They used to be filed between the dormant textboxes. The pane is
        # one document now, and the prompts that fed it read below it.
        out = self.patched_bundle("out;")
        self.assertLess(out.index("{{ notesOverlay }}"),
                        out.index("RELATED PROMPTS"))
        self.assertLess(out.index("RELATED PROMPTS"),
                        out.index('<sc-if value="{{ showAgent }}"'))
        self.assertLess(out.index("BLOCKERS &amp; OPEN QUESTIONS"),
                        out.index("RELATED PROMPTS"))
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
        self.assertEqual([2, "+ add a chat", "hc-chat-addbtn"],
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
                          ["RELATED PROMPTS", "hc-chat-addbtn",
                           "hc-prompt-addbtn"]], got)

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
            "var first = pane().querySelector('.hc-prompt-addbtn');"
            "var second = pane().querySelector('.hc-prompt-addbtn');"     # the redraw
            + self.CLICK +
            "click(second);"
            "JSON.stringify([first !== second, !!document.querySelector('.hc-ask')]);")
        self.assertEqual([True, True], json.loads(opened))

    def test_the_picker_is_mounted_inside_the_workspace_so_it_takes_its_theme(self):
        # The theme's variables (--panel, --ink, --bd …) live on `.hc`. A
        # picker hung on <body> sat outside them and fell back to the light
        # defaults on a dark page.
        where = self.run_js(
            "localStorage.setItem('hc-vault-ui-v1',"
            "  JSON.stringify({ selId: 'g1' }));"
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.pick_state()) +
            "var hc = document.createElement('div'); hc.className = 'hc';"
            "document.body.appendChild(hc);"
            "window.__hcPromptUI.pickPrompt('g1', null);"
            "var ask = document.querySelector('.hc-ask');"
            "JSON.stringify(!!ask && ask.parentNode && ask.parentNode.className);")
        self.assertEqual("hc", json.loads(where))

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
            "var addbtn = slot.querySelector('.hc-prompt-addbtn');"
            "click(addbtn); click(addbtn);"
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
        self.assertEqual(2, count)

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
            "click(slot.querySelector('.hc-prompt-addbtn'));"
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
             "var s = document.createElement('div');"
             "s.className = 'hc-anpanel';"
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

    def test_a_hidden_panel_does_not_silence_the_other_page(self):
        # The goals panel stays in the document with display:none while the
        # conversations page is showing. Testing for its presence alone
        # suppressed the banner on both pages, and the conversations page has
        # no panel of its own to report instead.
        hidden = self.run_js(
            "var host = document.documentElement;"
            "var s = document.createElement('div');"
            "s.className = 'hc-anpanel';"
            "s.offsetParent = null;"           # what display:none reports
            "host.appendChild(s);"
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(self.SYNTH) +
            "window.__hcPromptUI.renderBanner();"
            "!!made.filter(function (e) { return e.className === 'hc-banner'"
            "  && e.parentNode; })[0];")
        self.assertTrue(hidden)

        shown = self.run_js(
            "var host = document.documentElement;"
            "var s = document.createElement('div');"
            "s.className = 'hc-anpanel';"
            "s.offsetParent = host;"           # laid out, so on screen
            "host.appendChild(s);"
            "window.__hcPromptUI.setSetupForTest(%s);" % json.dumps(self.SYNTH) +
            "window.__hcPromptUI.renderBanner();"
            "!!made.filter(function (e) { return e.className === 'hc-banner'"
            "  && e.parentNode; })[0];")
        self.assertFalse(shown)

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

    def test_the_panel_reports_any_analysis_not_only_the_tree_build(self):
        # Switching to the goals tab while conversations are still being read
        # used to show a banner over an empty tree.
        out = self.patched_bundle("out;")
        self.assertIn("anGoals: !!(window.__hcAnalysisNow && "
                      "window.__hcAnalysisNow().running)", out)
        self.assertIn("{{ anSpin }}", out)
        self.assertIn("{{ anTitle }}", out)
        self.assertIn("'Building Goals' : 'Reading your conversations'", out)
        # and the tree list steps aside for it
        self.assertIn("treeListDisp: (window.__hcAnalysisNow && "
                      "window.__hcAnalysisNow().running) ? 'none' : 'block'", out)

    def test_the_panel_is_found_by_class_not_by_the_words_in_it(self):
        # The heading names the phase now, so matching its text would break
        # the moment the phase changed.
        out = self.patched_bundle("out;")
        self.assertIn('class="hc-anpanel"', out)

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

    def test_rows_are_marked_by_conversation_not_by_position(self):
        # The list is filtered and re-sorted, so an index into a progress
        # array marks whichever row happens to be sitting there.
        out = self.patched_bundle("out;")
        self.assertIn("stShow: !!c.done", out)
        self.assertIn("an.active[c.id]", out)
        self.assertNotIn("anx.prog[i]", out)

    def test_every_conversation_in_the_pool_is_animated(self):
        # Extraction runs eight at a time. Marking one understated what was
        # happening by the size of the pool.
        got = json.loads(self.run_js(
            "window.__hcPromptUI.setSetupForTest({ running: true,"
            "  phase: 'extracting', active: ['s5','s6','s7','s8','s9','s10','s11','s12'],"
            "  conversations: { total: 20, analyzed: 5, pending: 15 } });"
            "var an = window.__hcAnalysisNow();"
            "var rows = [];"
            "for (var k = 0; k < 20; k++) {"
            "  var c = { id: 's' + k, done: k < 5 };"
            "  rows.push(c.done ? 'analyzed'"
            "    : (an.running && an.active[c.id]) ? 'analyzing' : 'queued'); }"
            "JSON.stringify(rows.reduce(function (acc, r) {"
            "  acc[r] = (acc[r] || 0) + 1; return acc; }, {}));"))
        self.assertEqual({"analyzed": 5, "analyzing": 8, "queued": 7}, got)

    def test_synthesis_animates_nothing_because_it_reads_them_all(self):
        # No single row is the one being worked on, so marking one would be
        # a claim about an order that does not exist.
        got = self.run_js(
            "window.__hcPromptUI.setSetupForTest({ running: true,"
            "  phase: 'synthesizing', active: ['s1','s2'],"
            "  conversations: { total: 2, analyzed: 2, pending: 0 } });"
            "var an = window.__hcAnalysisNow();"
            "[an.running, !!an.active['s1'], !!an.active['s2']].join(',');")
        self.assertEqual("true,false,false", got)

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


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ChatScopeSeedTests(BridgeTestCase):
    """A chat workspace boots into its own tree, not the vault's wizard."""

    CHAT = {"scope": "chat", "generated_at": "2026-08-16T00:00:00+00:00",
            "revision": "r1", "goals": [], "prompts": []}

    def payload(self, state=None, saved="null"):
        return self.run_js(
            "window.__hcPromptUI.seedPayload(%s, [], %s);"
            % (json.dumps(state or self.CHAT), saved))

    def test_the_wizard_is_answered_from_what_a_chat_actually_does(self):
        # mainDisp/gateShow/obShow in the artifact all read this one object.
        # Chat inference runs through the Claude CLI over a transcript the
        # chat is already keeping, so every field here is a report.
        setup = self.payload()["setup"]
        self.assertEqual({"sv": 9, "storage": True, "analysis": "claude",
                          "done": True}, setup)

    def test_a_dead_setup_route_cannot_drag_a_chat_into_onboarding(self):
        # /api/setup speaks for the global vault and answers ok:false here.
        # The chat seed must not be built from its silence.
        setup = self.run_js(
            "window.__hcPromptUI.setSetupForTest(null);"
            "window.__hcPromptUI.seedPayload(%s, [], null).setup;"
            % json.dumps(self.CHAT))
        self.assertTrue(setup["done"])
        self.assertTrue(setup["storage"])

    def test_a_global_vault_still_reports_its_own_answers(self):
        fresh = self.run_js(
            "window.__hcPromptUI.setSetupForTest(null);"
            "window.__hcPromptUI.seedPayload({ scope: 'global' }, [], null)"
            ".setup;")
        self.assertFalse(fresh["done"])
        self.assertFalse(fresh["storage"])
        self.assertIsNone(fresh["analysis"])

    def test_a_fresh_chat_page_lands_on_all(self):
        # A chat's tree is small and often finished; opening it filtered to
        # active reads as an empty workspace.
        self.assertEqual("all", self.payload()["filter"])
        self.assertEqual("all", self.payload(saved="{}")["filter"])

    def test_a_filter_the_reader_chose_here_survives(self):
        # v7 marks a store this bridge wrote, which is the only evidence
        # that the saved filter is a choice rather than the artifact's own
        # default of 'active'.
        self.assertEqual("done",
                         self.payload(saved="{ v: 7, filter: 'done' }")["filter"])

    def test_the_artifacts_own_default_is_not_read_as_a_choice(self):
        self.assertEqual(
            "all", self.payload(saved="{ v: 6, filter: 'active' }")["filter"])

    def test_a_global_page_still_opens_on_active(self):
        glob = {"scope": "global", "goals": [], "prompts": []}
        self.assertEqual("active", self.payload(glob)["filter"])
        self.assertEqual(
            "done", self.payload(glob, saved="{ filter: 'done' }")["filter"])

    def test_a_chat_has_nowhere_for_the_conversations_page_to_land(self):
        self.assertEqual(
            "goals", self.payload(saved="{ v: 7, page: 'convos' }")["page"])

    def test_a_global_page_keeps_the_conversations_page(self):
        glob = {"scope": "global", "goals": [], "prompts": []}
        self.assertEqual(
            "convos", self.payload(glob, saved="{ page: 'convos' }")["page"])

    def test_a_saved_agent_or_review_pane_resolves_to_context(self):
        for pane in ("agent", "artifact"):
            self.assertEqual("context", self.payload(
                saved="{ v: 7, paneTab: '%s' }" % pane)["paneTab"])

    def test_a_global_page_still_restores_those_panes(self):
        glob = {"scope": "global", "goals": [], "prompts": []}
        self.assertEqual("agent", self.payload(
            glob, saved="{ paneTab: 'agent' }")["paneTab"])


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ChatScopeSurfaceTests(BridgeTestCase):
    """Controls whose only backend answers 'global scope only' come off."""

    def tab_bar(self, *labels):
        """A stand-in for the artifact's re-rendered pane tab row."""
        return ("var bar = document.createElement('div');"
                "app.appendChild(bar);"
                + "".join(
                    "var t%d = document.createElement('span');"
                    "t%d.textContent = %s; bar.appendChild(t%d);"
                    % (i, i, json.dumps(label), i)
                    for i, label in enumerate(labels)))

    def nav_bar(self):
        """A stand-in for the artifact's header page nav."""
        return ("var nav = document.createElement('div'); app.appendChild(nav);"
                "var pg = document.createElement('span');"
                "pg.textContent = 'Goals'; nav.appendChild(pg);"
                "var pc = document.createElement('span');"
                "pc.textContent = 'Conversations'; nav.appendChild(pc);")

    def chat(self):
        return ("window.__hcPromptUI.acceptState("
                "{ goals: [], prompts: [], scope: 'chat' });")

    def displays(self, scope_js, labels):
        return self.run_js(
            scope_js + self.tab_bar(*labels)
            + "window.__hcPromptUI.renderChatSurface();"
            + "bar.children.map(function (n) { return n.style.display || ''; });")

    def test_the_agent_and_review_tabs_are_taken_off_a_chat_page(self):
        self.assertEqual(
            ["", "none", "none"],
            self.displays(self.chat(), ["CONTEXT", "AGENT", "REVIEW"]))

    def test_a_review_tab_that_appears_later_is_swept_too(self):
        # REVIEW sits behind an sc-if that only turns on once a run exists,
        # so it is not on the page when the first sweep runs.
        self.assertEqual(
            ["", "none"], self.displays(self.chat(), ["CONTEXT", "REVIEW"]))

    def test_a_global_page_keeps_every_tab(self):
        state = ("window.__hcPromptUI.acceptState("
                 "{ goals: [], prompts: [], scope: 'global' });")
        self.assertEqual(
            ["", "", ""],
            self.displays(state, ["CONTEXT", "AGENT", "REVIEW"]))

    def test_headings_that_share_a_tabs_name_are_left_alone(self):
        # 'AGENT' is also a heading inside the pane. Only the row holding
        # CONTEXT is the tab bar.
        out = self.run_js(
            self.chat()
            + "var head = document.createElement('span');"
            "head.textContent = 'AGENT'; app.appendChild(head);"
            + self.tab_bar("CONTEXT", "AGENT")
            + "window.__hcPromptUI.renderChatSurface();"
            "[head.style.display || '', bar.children[1].style.display || ''];")
        self.assertEqual(["", "none"], out)

    def test_the_conversations_nav_comes_off_a_chat_page(self):
        out = self.run_js(
            self.chat() + self.nav_bar()
            + "window.__hcPromptUI.renderChatSurface();"
            "nav.children.map(function (n) { return n.style.display || ''; });")
        self.assertEqual(["", "none"], out)

    def test_a_global_page_keeps_the_conversations_nav(self):
        out = self.run_js(
            "window.__hcPromptUI.acceptState("
            "{ goals: [], prompts: [], scope: 'global' });" + self.nav_bar()
            + "window.__hcPromptUI.renderChatSurface();"
            "nav.children.map(function (n) { return n.style.display || ''; });")
        self.assertEqual(["", ""], out)

    def test_a_goal_the_reader_titled_conversations_is_left_alone(self):
        # The nav row is the one holding both page names. A tree row that
        # happens to carry one of them is the reader's own goal.
        out = self.run_js(
            self.chat()
            + "var row = document.createElement('div'); app.appendChild(row);"
            "var goal = document.createElement('span');"
            "goal.textContent = 'Conversations'; row.appendChild(goal);"
            + self.nav_bar()
            + "window.__hcPromptUI.renderChatSurface();"
            "[goal.style.display || '', nav.children[1].style.display || ''];")
        self.assertEqual(["", "none"], out)

    def test_a_goal_the_reader_titled_context_cannot_re_anchor_the_tabs(self):
        # Without the companion check the sweep would take the first
        # CONTEXT it found -- a goal row -- and never reach the real bar.
        out = self.run_js(
            self.chat()
            + "var row = document.createElement('div'); app.appendChild(row);"
            "var goal = document.createElement('span');"
            "goal.textContent = 'CONTEXT'; row.appendChild(goal);"
            + self.tab_bar("CONTEXT", "AGENT", "REVIEW")
            + "window.__hcPromptUI.renderChatSurface();"
            "[goal.style.display || ''].concat("
            "  bar.children.map(function (n) { return n.style.display || ''; }));")
        self.assertEqual(["", "", "none", "none"], out)

    def test_a_lone_label_with_no_companion_anchors_nothing(self):
        self.assertEqual(
            [None, None],
            self.run_js(
                self.chat()
                + "var row = document.createElement('div'); app.appendChild(row);"
                "var a = document.createElement('span'); a.textContent = 'CONTEXT';"
                "row.appendChild(a);"
                "var b = document.createElement('span'); b.textContent = 'Goals';"
                "row.appendChild(b);"
                "[window.__hcPromptUI.paneTabBar(),"
                " window.__hcPromptUI.headerNav()];"))

    def test_the_keyboard_cycle_stops_at_the_tabs_a_chat_has(self):
        # ⌘↑/⌘↓ stepping onto AGENT or REVIEW would open a pane whose every
        # control errors here, and stepping onto PROMPT would swap the
        # document out for something the right rail is already showing.
        # CONTEXT is the only pane this workspace has left to open.
        out = self.patched_bundle("out;")
        self.assertIn(
            "const tabs = (typeof window !== 'undefined' && "
            "window.__hcScope === 'chat') ? ['context'] : "
            "['context', 'prompt', 'agent', 'artifact'];", out)
        self.assertNotIn(
            "    const tabs = ['context', 'prompt', 'agent', 'artifact'];", out)

    def test_the_seeded_filter_is_what_the_tree_opens_on(self):
        # The constructor read saved.selId and saved.paneTab but hardcoded
        # the filter, so the seed's answer never reached the chip row.
        out = self.patched_bundle("out;")
        self.assertIn(
            "filter: (saved && ['active', 'inprog', 'done', 'all']"
            ".indexOf(saved.filter) >= 0) ? saved.filter : 'active',", out)
        self.assertNotIn("\n      filter: 'active',\n", out)

    def test_the_store_it_writes_back_declares_the_seeded_version(self):
        # saved.v >= 7 is what the seed reads as "this origin is ours", and
        # what lets a reader's filter choice survive their next reload.
        out = self.patched_bundle("out;")
        self.assertIn(
            "localStorage.setItem('hc-vault-ui-v1', JSON.stringify({ v: 7,",
            out)
        self.assertNotIn("JSON.stringify({ v: 6,", out)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ScopeFallbackTests(BridgeTestCase):
    """A dead state route must not make a chat page look like the vault."""

    def test_health_names_the_scope_when_the_state_fetch_fails(self):
        self.assertEqual("chat", self.run_js(
            "window.__hcScope;", state={"broken": True},
            extra_env={"HC_HEALTH": '{"ok": true, "scope": "chat"}'}))

    def test_a_global_server_is_still_read_as_global(self):
        self.assertEqual("global", self.run_js(
            "window.__hcScope;", state={"broken": True},
            extra_env={"HC_HEALTH": '{"ok": true, "scope": "global"}'}))

    def test_nothing_answering_at_all_leaves_it_where_it_was(self):
        self.assertEqual("global", self.run_js(
            "window.__hcScope;", state={"broken": True}))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class DeadRouteTests(BridgeTestCase):
    """A chat workspace must not pay for the two routes that refuse it.

    /api/setup and /api/briefings speak for the global vault, and in chat
    scope both answer ok:false. Both were asked at boot -- as blocking
    synchronous XHRs on the path to first paint -- and /api/setup again on
    the analysis poll, for the life of the page.
    """

    CHAT = {"scope": "chat", "generated_at": "2026-08-16T00:00:00+00:00",
            "revision": "r1", "goals": [], "prompts": []}
    IS_CHAT = {"HC_HEALTH": '{"ok": true, "scope": "chat"}'}

    def boot_routes(self, **kwargs):
        return json.loads(self.run_js(
            "xhrs.length = 0;"
            "window.__hcPromptUI.seedForTest();"
            "JSON.stringify(xhrs);", **kwargs))

    def polled(self, **kwargs):
        return json.loads(self.run_js(
            "calls.length = 0;"
            "window.__hcPromptUI.watchAnalysis().then(function () {"
            "  return JSON.stringify(calls.map(function (c) {"
            "    return String(c[0]); })); });", **kwargs))

    def test_a_chat_boot_asks_neither_of_them(self):
        routes = self.boot_routes(state=self.CHAT, extra_env=self.IS_CHAT)
        self.assertNotIn("/api/setup", routes)
        self.assertNotIn("/api/briefings", routes)

    def test_it_finds_out_which_scope_it_is_before_deciding(self):
        # /api/health is the one route no scope gates, and it is cheap.
        routes = self.boot_routes(state=self.CHAT, extra_env=self.IS_CHAT)
        self.assertEqual(["/api/health", "/api/state"], routes)

    def test_a_global_boot_still_asks_both(self):
        routes = self.boot_routes()
        self.assertIn("/api/setup", routes)
        self.assertIn("/api/briefings", routes)
        self.assertIn("/api/state", routes)

    def test_a_chat_page_never_polls_the_analysis_route(self):
        self.assertEqual([], self.polled(state=self.CHAT,
                                         extra_env=self.IS_CHAT))

    def test_a_global_page_still_polls_it(self):
        self.assertIn("/api/setup", self.polled())


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class DocumentPaneTests(BridgeTestCase):
    """The Context pane is one markdown document, rendered as it is typed."""

    def test_the_document_is_what_the_context_tab_opens(self):
        out = self.patched_bundle("out;")
        self.assertIn("showNotes: !!sel && paneTab === 'context'", out)

    def test_a_chat_writes_the_document_in_the_rail_and_reads_it_in_the_middle(self):
        """The writing moved to the rail; the middle draws it back.

        A document is written about the TODOs beside it, so the editor is a
        tab of the same column they are -- and the wide side, which used to
        be a second place to look for the same box, prints what is being
        typed instead.
        """
        out = self.patched_bundle("out;", scope="chat")
        rail = out[out.index('class="hc-rail-notes"'):
                   out.index('class="hc-rail-prompt"')]
        # The editor is in the rail, and it is the only one on the page.
        self.assertIn("{{ notesChange }}", rail)
        self.assertEqual(1, out.count("{{ notesChange }}"))
        self.assertIn("hc-notes-edit", rail)
        # The prompts that fed it come with it: they are evidence for the
        # document, and reading them a column away from it says nothing.
        self.assertIn("RELATED PROMPTS", rail)
        self.assertEqual(1, out.count('<sc-for list="{{ histRows }}" as="hr"'))
        # And the middle is not a second copy of the document: it is the
        # mount the run preview is drawn into, which the artifact's own
        # state knows nothing about.
        middle = out[out.index('class="hc-preview"'):]
        self.assertIn('class="hc-preview-mount"', middle)
        self.assertNotIn("{{ notesOverlay }}", middle)
        self.assertNotIn("{{ notesChange }}", middle)

    def test_the_rail_reads_in_the_order_the_work_does(self):
        # Rows, then what they are for, then the prompt both are copied
        # into. A document filed after the prompt it feeds would be read
        # after the thing it explains.
        out = self.patched_bundle("out;", scope="chat")
        self.assertLess(out.index('class="hc-todos"'),
                        out.index('class="hc-rail-notes"'))
        self.assertLess(out.index('class="hc-rail-notes"'),
                        out.index('class="hc-rail-prompt"'))
        self.assertLess(out.index('class="hc-rail-prompt"'),
                        out.index('class="hc-rail-understand"'))

    def test_the_middle_says_which_of_the_two_it_is(self):
        # One tab is visible in a chat, so its word is the whole label the
        # pane gets. It said CONTEXT while it held the editor.
        out = self.patched_bundle("out;", scope="chat")
        self.assertIn(">PREVIEW</span>", out)
        self.assertNotIn(">CONTEXT</span>", out)
        self.assertIn(">CONTEXT</span>", self.patched_bundle("out;"))

    def test_a_workspace_with_no_rail_keeps_the_document_where_it_was(self):
        # The rail is a chat's. An artifact opened on its own has nowhere
        # else to write, so nothing about its pane moves.
        out = self.patched_bundle("out;")
        self.assertNotIn("hc-rail-notes", out)
        self.assertNotIn("hc-preview", out)
        self.assertEqual(1, out.count("{{ notesChange }}"))

    def test_the_textbox_pane_is_dormant_in_both_scopes(self):
        # Objective / code / document / decisions / blockers / built are no
        # longer reachable, in either scope. The markup and every handler
        # behind it stay: nothing about them is deleted, only unreached.
        for scope in (None, "chat"):
            out = self.patched_bundle("out;", scope=scope)
            self.assertIn("showCtx: false,", out, scope)
            self.assertNotIn("showCtx: !!sel && paneTab === 'context',", out,
                             scope)
            for kept in (">OBJECTIVE</span>", ">CODE CONTEXT</span>",
                         ">DOCUMENT CONTEXT</span>", ">DECISIONS</span>",
                         "{{ ctxObjectiveCh }}", "{{ codeRows }}",
                         "{{ docRows }}"):
                self.assertIn(kept, out, (scope, kept))

    def test_the_document_is_not_an_addendum_to_something_else(self):
        out = self.patched_bundle("out;")
        self.assertIn(">NOTES</div>", out)
        self.assertNotIn("ADDITIONAL NOTES", out)
        # A rule above it said "and also…"; it is the pane now, not a footer.
        self.assertNotIn("margin-top:20px;padding-top:14px;border-top:1px "
                         "solid var(--bd);font:600 9.5px", out)

    def test_the_editor_is_tall_enough_to_write_a_document_in(self):
        out = self.patched_bundle("out;")
        overlay = out[out.rindex("<div", 0, out.index("{{ notesOverlay }}")):
                      out.index("{{ notesOverlay }}")]
        self.assertIn("min-height:360px", overlay)
        self.assertNotIn("min-height:96px", overlay)

    def test_an_empty_goal_opens_on_the_default_headers(self):
        out = self.patched_bundle("out;")
        self.assertIn("notesVal: sel ? (sel.notes || window.__hcDefaultDoc "
                      "|| '') : '',", out)
        self.assertIn("notesOverlay: sel ? this.md(sel.notes || "
                      "window.__hcDefaultDoc || '') : null,", out)

    def test_the_default_document_is_the_one_the_backend_writes(self):
        # A heading the bridge shows but goals.py cannot append into would
        # send the user's text somewhere inference never looks.
        import re
        import sys
        sys.path.insert(0, str(ROOT / "hc" / "src"))
        from human_compact.trajectory import goals as GM
        src = BRIDGE.read_text()
        found = re.search(r'var DEFAULT_DOC = ("(?:[^"\\]|\\.)*");', src)
        self.assertIsNotNone(found, "bridge.js must name the default document")
        self.assertEqual(GM.default_doc(), json.loads(found.group(1)))
        self.assertIn("window.__hcDefaultDoc = DEFAULT_DOC;", src)
        self.assertEqual(GM.default_doc(),
                         self.run_js("window.__hcDefaultDoc;"))

    def test_the_prompts_that_fed_the_document_read_below_it(self):
        out = self.patched_bundle("out;")
        self.assertLess(out.index("{{ notesOverlay }}"),
                        out.index("RELATED PROMPTS"))
        self.assertIn('<span class="hc-prompt-add"></span>', out)
        self.assertIn('<sc-for list="{{ histRows }}" as="hr"', out)
        self.assertEqual(1, out.count('<sc-for list="{{ histRows }}" as="hr"'))

    def test_a_row_says_whether_a_machine_made_the_link(self):
        out = self.patched_bundle("out;")
        self.assertIn("origin: p.auto ? 'automatic' : 'yours',", out)
        self.assertIn("{{ hr.origin }}", out)

    def test_the_bridge_reports_which_links_inference_made(self):
        state = json.loads(json.dumps(STATE))
        state["goals"][0]["auto_prompt_ids"] = ["a#1"]
        self.assertEqual([True], [r["auto"] for r in
                                  self.roots(state)[0]["prompts"]])
        self.assertEqual([False], [r["auto"] for r in
                                   self.roots()[0]["prompts"]])

    def test_the_draft_is_assembled_from_the_document_sections(self):
        out = self.patched_bundle("out;")
        draft = out[out.index("const composeDraft"):out.index("const baseDraft")]
        self.assertIn("const secOf = (t) =>", draft)
        # between "# <title>" and the next H1; ## is not an H1, and neither
        # is a "# " line the user wrote inside a fenced code block
        self.assertIn("fence === null && line.indexOf('# ') === 0", draft)
        for title in ("In my words", "Decisions", "Built", "Blockers",
                      "Open questions"):
            self.assertIn("section('%s');" % title, draft)

    def test_the_draft_no_longer_reads_the_dormant_textboxes(self):
        out = self.patched_bundle("out;")
        draft = out[out.index("const composeDraft"):out.index("const baseDraft")]
        for gone in ("ctxGet('decided')", "ctxGet('built')", "ctxGet('hit')"):
            self.assertNotIn(gone, draft)

    def test_an_objective_the_document_does_not_carry_is_still_said(self):
        # The description inference recorded is a real answer to "what does
        # finishing this mean"; dropping it because the document's own
        # Objective section is empty would lose it from the prompt.
        out = self.patched_bundle("out;")
        draft = out[out.index("const composeDraft"):out.index("const baseDraft")]
        self.assertIn("secOf('Objective') || String(ctxGet('objective') "
                      "|| '').trim()", draft)

    DOC_WITH_FENCE = ("# Objective\nShip it.\n\n# Built\n```py\n"
                      "# Decisions\nx = 1\n```\n\n# Blockers\n\n")

    def sec_of(self, document, titles):
        """Run the patched composeDraft's own secOf over a document."""
        return json.loads(self.patched_bundle(
            "var at = out.indexOf('const secOf = (t) =>');"
            "var fnsrc = out.slice(at, out.indexOf('const section = (t) =>', at));"
            "var secOf = eval('(function (sel) { ' + fnsrc + ' return secOf; })')"
            "  (%s);"
            "JSON.stringify(%s.map(function (t) { return secOf(t); }));"
            % (json.dumps({"notes": document}), json.dumps(titles))))

    def test_a_heading_inside_a_fenced_block_is_body_not_a_section(self):
        # The server's split_doc is fence-aware. A client that is not tears
        # the user's code block in half in the prompt it hands the agent --
        # Built ends at the fence line and an invented Decisions section
        # carries the rest, unterminated.
        import sys
        sys.path.insert(0, str(ROOT / "hc" / "src"))
        from human_compact.trajectory import goals as GM
        got = self.sec_of(self.DOC_WITH_FENCE,
                          ["Objective", "Built", "Decisions", "Blockers"])
        self.assertIn("x = 1", got[1])
        self.assertEqual("```py\n# Decisions\nx = 1\n```", got[1])
        self.assertEqual("", got[2])       # nothing is extracted from inside
        self.assertEqual(["Ship it.", "", ""], [got[0], got[2], got[3]])
        # and it agrees with the parser that owns the grammar
        self.assertEqual(GM.section_body(self.DOC_WITH_FENCE, "Built"), got[1])
        self.assertIsNone(GM.section_body(self.DOC_WITH_FENCE, "Decisions"))

    def test_a_tilde_fence_hides_a_heading_the_same_way(self):
        document = "# Built\n~~~\n# Decisions\ny = 2\n~~~\n\n# Blockers\n"
        got = self.sec_of(document, ["Built", "Decisions"])
        self.assertEqual("~~~\n# Decisions\ny = 2\n~~~", got[0])
        self.assertEqual("", got[1])

    def test_a_fence_only_closes_on_its_own_marker(self):
        # A ``` inside a ~~~~ block does not close it, and a shorter run of
        # the same character does not either.
        document = ("# Built\n~~~~\n```\n# Decisions\nz = 3\n~~~\n~~~~\n"
                    "\n# Blockers\nreal\n")
        got = self.sec_of(document, ["Built", "Decisions", "Blockers"])
        self.assertIn("# Decisions", got[0])
        self.assertEqual("", got[1])
        self.assertEqual("real", got[2])

    def test_an_info_string_with_a_backtick_opens_nothing(self):
        # CommonMark: a backtick fence may not carry a backtick in its info
        # string, so this line is text and the heading after it is real.
        document = "# Built\n```a`b\n\n# Decisions\nkept\n"
        got = self.sec_of(document, ["Built", "Decisions"])
        self.assertEqual("```a`b", got[0])
        self.assertEqual("kept", got[1])


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ChatPromptTabTests(BridgeTestCase):
    """A chat workspace gets the PROMPT tab back, with nothing behind it
    that this scope cannot do."""

    def test_a_chat_page_offers_the_prompt_tab(self):
        out = self.patched_bundle("out;", scope="chat")
        self.assertIn('sc-camel-on-click="{{ tabPrompt }}"', out)
        self.assertIn(">PROMPT</span>", out)
        self.assertNotIn("prompt folded into agent", out)

    def test_a_global_page_still_folds_it_into_agent(self):
        out = self.patched_bundle("out;")
        self.assertIn("prompt folded into agent", out)
        self.assertNotIn('sc-camel-on-click="{{ tabPrompt }}"', out)

    def test_the_pane_follows_whichever_tab_opens_it(self):
        self.assertIn("showPrompt: !!sel && paneTab === 'prompt'",
                      self.patched_bundle("out;", scope="chat"))
        self.assertIn("showPrompt: !!sel && paneTab === 'agent'",
                      self.patched_bundle("out;"))

    def prompt_pane(self, scope=None):
        out = self.patched_bundle("out;", scope=scope)
        at = out.index('<sc-if value="{{ showPrompt }}"')
        return out[at:out.index("</sc-if>", at)]

    def test_a_chat_prompt_pane_is_the_prompt_and_a_way_to_take_it(self):
        pane = self.prompt_pane(scope="chat")
        self.assertIn("RECOMMENDED PROMPT", pane)
        self.assertIn("{{ draft }}", pane)
        self.assertIn('sc-camel-on-click="{{ copyPrompt }}"', pane)
        self.assertIn("{{ copyPromptLabel }}", pane)

    def test_a_chat_prompt_pane_is_read_only_and_says_so(self):
        # Nothing here keeps an edit -- not across a reload, not across a
        # tab switch -- so the box must not invite one and the copy must
        # not promise one.
        pane = self.prompt_pane(scope="chat")
        self.assertIn('readonly="readonly"', pane)
        self.assertNotIn("Edit it here", pane)
        self.assertNotIn("{{ promptInput }}", pane)
        self.assertIn("assembled from your goal document \u00b7 read-only",
                      pane)
        # And the way to take it away is still there.
        self.assertIn('sc-camel-on-click="{{ copyPrompt }}"', pane)

    def test_a_global_prompt_pane_is_still_the_agents_to_edit(self):
        # There the draft is what `runAgent` sends, so editing it is the
        # point; only the chat pane loses the box.
        pane = self.prompt_pane()
        self.assertNotIn("readonly", pane)
        self.assertIn("{{ promptInput }}", pane)

    def test_a_chat_prompt_pane_offers_no_run(self):
        # Every op behind a run answers "global scope only" here.
        pane = self.prompt_pane(scope="chat")
        self.assertNotIn("{{ runAgent }}", pane)
        self.assertNotIn("Run Claude Code on this goal", pane)
        self.assertNotIn("{{ genTodos }}", pane)

    def test_a_global_prompt_pane_is_unchanged(self):
        pane = self.prompt_pane()
        self.assertIn("Run Claude Code on this goal with the self-contained "
                      "context Vault has assembled. Progress appears in "
                      "REVIEW.", pane)
        self.assertIn('<details class="hc-promptbox">', pane)
        self.assertNotIn("{{ copyPrompt }}", pane)

    def test_copying_says_it_copied_only_once_the_clipboard_took_it(self):
        out = self.patched_bundle("out;", scope="chat")
        at = out.index("copyPrompt: () =>")
        handler = out[at:out.index("copyPromptLabel:", at)]
        self.assertIn("navigator.clipboard.writeText(t).then(done,", handler)
        self.assertIn("document.execCommand('copy')", handler)
        # execCommand answers whether it copied; "copied ✓" waits on that
        self.assertIn("if (fb()) done();", handler)
        self.assertNotIn("{ fb(); done(); }", handler)
        # the draft as it stands, not the draft plus a metadata footer, and
        # nothing is recorded as a prompt the user never sent
        self.assertNotIn("_copyMeta", handler)
        self.assertNotIn("recordPrompt", handler)
        self.assertIn("copyPromptLabel: copied ? 'copied ✓' : "
                      "'Copy prompt',", out)


    def test_the_fallback_only_claims_a_copy_the_browser_made(self):
        # execCommand returns false when the browser refuses. Saying
        # "copied" anyway is the copy rule's exact failure mode: the label
        # reports an act nothing performed.
        got = json.loads(self.patched_bundle(
            "var at = out.indexOf('copyPrompt: () =>');"
            "var fnsrc = out.slice(at + 'copyPrompt: '.length,"
            "  out.indexOf('copyPromptLabel:', at)).replace(/,\\s*$/, '');"
            "var mk = document.createElement;"
            "document.createElement = function (t) { var el = mk(t);"
            "  el.select = function () {};"
            "  el.remove = function () { if (el.parentNode)"
            "    el.parentNode.removeChild(el); }; return el; };"
            "navigator.clipboard = undefined;"
            "var said = [];"
            # the handler closes over the assembled draft in the bundle
            "var draft = 'the context';"
            "var fn = eval('(function () { return (' + fnsrc + '); })')"
            "  .call({ _draftEl: { value: 'the draft' },"
            "          setState: function (s) { said.push(s); } });"
            "document.execCommand = function () { return false; };"
            "fn(); var refused = said.slice(); said.length = 0;"
            "document.execCommand = function () { return true; };"
            "fn();"
            "JSON.stringify([refused, said]);", scope="chat"))
        self.assertEqual([[], [{"copied": True}, {"copied": False}]], got)

@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ChatPromptLinkTests(BridgeTestCase):
    """Linking a prompt to a goal has to work where the prompts live."""

    CLICK = ("function click(node) { var e = { target: node,"
             "  preventDefault: function () {}, stopPropagation: function () {} };"
             "  listeners.filter(function (l) { return l[0] === 'click'; })"
             "    .forEach(function (l) { l[1](e); }); }")

    KEY = ("function key(name) { var e = { key: name,"
           "  preventDefault: function () {} };"
           "  listeners.filter(function (l) { return l[0] === 'keydown'; })"
           "    .forEach(function (l) { l[1](e); }); }")

    def chat_state(self):
        state = json.loads(json.dumps(STATE))
        state["scope"] = "chat"
        state["prompts"].append(
            {"id": "a#2", "role": "user", "text": "and record the audio",
             "created_at": "2026-08-05"})
        return state

    def test_a_chat_can_add_a_prompt_to_a_goal(self):
        drawn = json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.chat_state()) +
            "var slot = document.createElement('span');"
            "slot.className = 'hc-prompt-add';"
            "document.body.appendChild(slot);"
            "var drew = window.__hcPromptUI.renderPromptAdd();"
            "JSON.stringify([drew, slot.children.length,"
            " slot.querySelector('.hc-prompt-addbtn').textContent]);"))
        self.assertEqual([True, 2, "+ add a prompt"], drawn)

    def test_the_picker_has_a_way_out_in_its_own_corner(self):
        got = json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.chat_state()) +
            "var btn = document.createElement('button');"
            "btn.focused = false; btn.focus = function () { btn.focused = true; };"
            "window.__hcPromptUI.pickPrompt('g1', btn);"
            "var x = document.querySelector('.hc-pick-close');"
            "var open1 = !!document.querySelector('.hc-ask');"
            "x.onclick();"
            "JSON.stringify([open1, !!x, x.textContent,"
            " !!document.querySelector('.hc-ask'), btn.focused]);"))
        self.assertEqual([True, True, "×", False, True], got)

    def test_escape_closes_the_picker_and_puts_the_reader_back(self):
        got = json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.chat_state()) +
            "var btn = document.createElement('button');"
            "btn.focused = false; btn.focus = function () { btn.focused = true; };"
            "window.__hcPromptUI.pickPrompt('g1', btn);"
            + self.KEY +
            "key('Escape');"
            "JSON.stringify([!!document.querySelector('.hc-ask'), btn.focused]);"))
        self.assertEqual([False, True], got)

    def test_a_key_that_is_not_escape_leaves_the_picker_open(self):
        got = json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.chat_state()) +
            "window.__hcPromptUI.pickPrompt('g1');"
            + self.KEY +
            "key('a');"
            "JSON.stringify([!!document.querySelector('.hc-ask')]);"))
        self.assertEqual([True], got)

    def test_the_close_button_is_styled_by_the_dialog_sheet(self):
        self.assertIn(".hc-pick-close{",
                      self.run_js("window.__hcPromptUI.dialogCss();"))

    def test_a_prompt_row_with_no_conversation_shows_no_separator(self):
        # Chat prompt records carry no session_id (chat_state writes the
        # id, ordinal, role, text and created_at, and nothing else), so a
        # separator emitted beside the value renders with nothing after it
        # on every row of every goal in the configuration /bart opens.
        got = json.loads(self.patched_bundle(
            "var at = out.indexOf('conv: p.conv ?');"
            "var expr = out.slice(at + 'conv: '.length,"
            "  out.indexOf(',\\n', at));"
            "var f = eval('(function (p) { return (' + expr + '); })');"
            "JSON.stringify([f({ conv: '' }), f({ conv: '11112222' })]);",
            scope="chat"))
        self.assertEqual(["", " \u00b7 conversation 11112222"], got)

    def test_the_row_no_longer_draws_a_separator_of_its_own(self):
        out = self.patched_bundle("out;", scope="chat")
        self.assertIn("{{ hr.when }}{{ hr.conv }}", out)
        self.assertNotIn("{{ hr.when }}<span", out)

    def test_attaching_from_a_chat_reaches_the_server(self):
        posted = json.loads(self.run_js(
            "localStorage.setItem('hc-vault-ui-v1',"
            "  JSON.stringify({ selId: 'g1' }));"
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.chat_state()) +
            "var slot = document.createElement('span');"
            "slot.className = 'hc-prompt-add';"
            "document.body.appendChild(slot);"
            "window.__hcPromptUI.renderPromptAdd();"
            + self.CLICK +
            "click(slot.querySelector('.hc-prompt-addbtn'));"
            "document.querySelector('.hc-pick-list').children[0].onclick();"
            "Promise.resolve().then(function () {}).then(function () {})"
            "  .then(function () { return JSON.stringify("
            "    calls.map(function (c) { return c[1]; }).filter(Boolean)); });"))
        self.assertEqual([{"op": "attach_prompt", "goal_id": "g1",
                           "prompt_id": "a#2"}],
                         [c for c in posted if c.get("op") == "attach_prompt"])

    # --- linked chats: one link for the workspace, one per goal -------------

    def scoped_state(self):
        # Two chats linked: "wide" for every goal (no chat_goals), "deep"
        # on g1a only. g1a hangs under g1.
        state = self.chat_state()
        state["prompts"].append(
            {"id": "w#1", "role": "user", "text": "from the wide chat",
             "created_at": "2026-08-06", "chat": "wide"})
        state["prompts"].append(
            {"id": "d#1", "role": "user", "text": "from the deep chat",
             "created_at": "2026-08-07", "chat": "deep", "chat_goals": ["g1a"]})
        state["prompts"].append(
            {"id": "t#1", "role": "user", "text": "from the top chat",
             "created_at": "2026-08-08", "chat": "top", "chat_goals": ["g1"]})
        return state

    def offered(self, goal_id):
        return json.loads(self.run_js(
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.scoped_state()) +
            "window.__hcPromptUI.pickPrompt(%s);" % json.dumps(goal_id) +
            "var list = document.querySelector('.hc-pick-list');"
            "JSON.stringify(list.children.map(function (r) "
            "{ return r.children[1] ? r.children[1].textContent : "
            "r.textContent; }));"))

    def test_a_goal_scoped_chat_is_offered_there_and_below_never_above(self):
        # A chat linked on g1a belongs to that branch: g1a sees it, its
        # parent g1 does not. One linked on g1 reaches g1 and g1a both, and
        # a workspace-wide link reaches everyone.
        self.assertEqual(["from the top chat", "from the deep chat",
                          "from the wide chat", "and record the audio",
                          "make it a desktop app"],
                         self.offered("g1a"))
        self.assertEqual(["from the top chat", "from the wide chat",
                          "and record the audio"],
                         self.offered("g1"))

    def test_the_goal_line_is_the_goal_and_its_ancestors(self):
        self.assertEqual(
            [{"g1a": True, "g1": True}, {"g1": True}, {}],
            json.loads(self.run_js(
                "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.chat_state()) +
                "JSON.stringify([window.__hcPromptUI.goalLine('g1a'),"
                " window.__hcPromptUI.goalLine('g1'),"
                " window.__hcPromptUI.goalLine(null)]);")))

    TICKS = ".then(function () {})" * 6

    CHATS = ("fetch = function (url, opts) {"
             "  calls.push([url, opts && opts.body ? JSON.parse(opts.body) : null]);"
             "  var body = String(url).indexOf('/api/chats') >= 0"
             "    ? { ok: true, linked: LINKED, available: ["
             "        { session_id: 'aaaaaaaa-1', project: 'alpha' },"
             "        { session_id: 'bbbbbbbb-2', project: 'beta' }] }"
             "    : { ok: true };"
             "  return Promise.resolve({ ok: true, json: function () {"
             "    return Promise.resolve(body); } }); };")

    def picker_rows(self, linked, goal_id, click=None):
        return json.loads(self.run_js(
            "localStorage.setItem('hc-vault-ui-v1',"
            "  JSON.stringify({ selId: 'g1a' }));"
            "window.__hcPromptUI.acceptState(%s);" % json.dumps(self.chat_state()) +
            "var LINKED = %s;" % json.dumps(linked) +
            self.CHATS +
            "var btn = document.createElement('button');"
            "window.__hcPromptUI.openChatPicker(btn, %s);" % json.dumps(goal_id) +
            "Promise.resolve()" + self.TICKS +
            "  .then(function () {"
            "    var list = document.querySelector('.hc-pick-list');"
            "    var rows = list.children.map(function (r) {"
            "      return [r.children[0].textContent, r.children[1].textContent]; });"
            + ("    list.children[%d].onclick();" % click if click is not None else "") +
            "    return Promise.resolve().then(function () {}).then(function () {})"
            "      .then(function () { return JSON.stringify([rows,"
            "        calls.map(function (c) { return c[1]; }).filter(Boolean)]); });"
            "  });"))

    def test_the_header_picker_links_for_every_goal(self):
        rows, posted = self.picker_rows(
            [{"session_id": "bbbbbbbb-2", "label": "beta", "goal_id": "g1a"}],
            None, click=0)
        # Linked rows first, then the rest; ids are shown short.
        self.assertEqual(
            [["bbbbbbbb · beta · linked on Capture interactions",
              "beta — click to link for every goal"],
             ["aaaaaaaa · alpha", "alpha — click to link"]],
            rows)
        self.assertEqual([{"op": "link_chat", "session_id": "bbbbbbbb-2",
                           "label": "beta"}],
                         [c for c in posted if c.get("op") == "link_chat"])

    def test_the_goal_picker_links_for_that_goal_and_reports_wider_links(self):
        # alpha is linked for the whole workspace, so g1a's picker shows it
        # covered and offers nothing to undo there; beta is linked on g1a
        # itself, so g1a's picker can unlink it -- scoped to g1a.
        rows, posted = self.picker_rows(
            [{"session_id": "aaaaaaaa-1", "label": "alpha"},
             {"session_id": "bbbbbbbb-2", "label": "beta", "goal_id": "g1a"}],
            "g1a", click=1)
        self.assertEqual(
            [["aaaaaaaa · alpha · linked for every goal", "alpha — click to link"],
             ["LINKED · bbbbbbbb · beta", "beta — click to unlink"]],
            rows)
        self.assertEqual([{"op": "unlink_chat", "session_id": "bbbbbbbb-2",
                           "label": "beta", "goal_id": "g1a"}],
                         [c for c in posted if c.get("op") == "unlink_chat"])

    def test_a_parents_link_is_reported_below_and_a_childs_is_not_above(self):
        # Linked on g1, beta covers g1a: g1a's picker says so. Linked on
        # g1a, alpha does not cover g1: g1's picker treats it as unlinked.
        rows, _ = self.picker_rows(
            [{"session_id": "aaaaaaaa-1", "label": "alpha", "goal_id": "g1a"},
             {"session_id": "bbbbbbbb-2", "label": "beta", "goal_id": "g1"}],
            "g1a")
        self.assertEqual(
            [["LINKED · aaaaaaaa · alpha", "alpha — click to unlink"],
             ["bbbbbbbb · beta · linked on Build the platform",
              "beta — click to link"]],
            rows)
        rows, posted = self.picker_rows(
            [{"session_id": "aaaaaaaa-1", "label": "alpha", "goal_id": "g1a"},
             {"session_id": "bbbbbbbb-2", "label": "beta", "goal_id": "g1"}],
            "g1", click=0)
        self.assertEqual(
            [["aaaaaaaa · alpha", "alpha — click to link"],
             ["LINKED · bbbbbbbb · beta", "beta — click to unlink"]],
            rows)
        self.assertEqual([{"op": "link_chat", "session_id": "aaaaaaaa-1",
                           "label": "alpha", "goal_id": "g1"}],
                         [c for c in posted if c.get("op") == "link_chat"])

class ChatNoticeTests(BridgeTestCase):
    """A goals workspace is a second window on a chat running in a terminal.

    The one thing it can say that the terminal cannot is that the terminal is
    finished. It says it where a finished TODO is said: a card in the
    top-right corner, an entry behind the bell, one unread count. These tests
    hold what it is allowed to say, and for how long.
    """

    SID = "7f3a1b2c-4d5e-4f60-8a9b-0c1d2e3f4a5b"

    PRELUDE = (
        "var A = window.__hcPromptUI.alerts;"
        "var slot = document.createElement('span'); slot.className = 'hc-alerts';"
        "header.appendChild(slot);"
        "window.__hcPromptUI.acceptState("
        "  { goals: [], prompts: [], scope: %s, session_id: %s });"
        # The first tick of the standing 700ms sweep, which is what names the
        # tab on a real page.
        "window.__hcPromptUI.renderChatSurface();"
        "var iso = function (ms) { return new Date(ms).toISOString(); };"
        "var now = Date.now();"
        "var fire = function (type, target, related) {"
        "  listeners.filter(function (l) { return l[0] === type; })"
        "    .forEach(function (l) { l[1]({ type: type, target: target,"
        "      relatedTarget: related || null, preventDefault: function () {},"
        "      stopPropagation: function () {} }); }); };"
        "var stack = function () {"
        "  var node = A.stack();"
        "  return node ? node.children : []; };"
        "var drawn = function () { return stack().map(function (n) {"
        "  var d = n.querySelector('.hc-alert-detail');"
        "  return [n.getAttribute('data-hc-alert-kind'),"
        "          n.querySelector('.hc-alert-title').textContent,"
        "          d ? d.textContent : null]; }); };"
    )

    def notices(self, tail, scope="chat", session=None, defer=True):
        """Timers stay pending unless a test drives them.

        The banner's whole subject is *when* it goes away; a harness that
        fires every timer the moment it is set would dismiss it before the
        assertion that it appeared.
        """
        return self.run_js(
            (self.PRELUDE % (json.dumps(scope),
                             json.dumps(self.SID if session is None else session)))
            + tail,
            extra_env={"HC_DEFER_TIMEOUT": "1"} if defer else None)

    def test_only_what_happened_after_this_page_opened_is_shown(self):
        # A chat that has been running for an hour has a store full of turns
        # that finished before anyone opened this window. Replaying them
        # would report old news as if it had just happened.
        out = self.notices(
            "window.__hcPromptUI.showNotices(["
            "  {id: 'n1', kind: 'session_stopped', at: iso(now - 60000),"
            "   detail: 'an older turn'},"
            "  {id: 'n2', kind: 'subagent_returned', at: iso(now + 5000),"
            "   detail: 'Explore: found it'}]);"
            "drawn();")
        self.assertEqual(
            [["subagent_returned", "A subagent returned", "Explore: found it"]],
            out)

    def test_a_session_notice_lands_in_the_stack_the_builder_writes_to(self):
        # The whole point of the change: one corner, one bell, one count.
        # A reader watching for a build coming back should not have to watch
        # a second surface for the chat answering.
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "var e = A.log()[0];"
            "[A.log().length, A.unread(), e.kind, e.text, e.goalId, e.rowId,"
            " document.querySelectorAll('.hc-notice').length];")
        self.assertEqual([1, 1, "session_stopped", "done", "", "", 0], out)

    def test_the_bell_counts_a_chat_answering_with_the_builds(self):
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "A.renderBell();"
            "var b = slot.querySelector('.hc-bell');"
            "[b.getAttribute('data-hc-unread'),"
            " b.querySelector('.hc-bell-count').textContent];")
        self.assertEqual(["1", "1"], out)

    def test_the_center_lists_it_without_a_goal_line(self):
        # A turn ending is about the conversation, not about one row of work.
        # A blank goal line under it would read as a goal named "".
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "A.open();"
            "var row = A.center().querySelector('.hc-alert-row');"
            "[row.getAttribute('data-hc-alert-kind'),"
            " row.querySelector('.hc-alert-detail').textContent,"
            " !!row.querySelector('.hc-alert-goal')];")
        self.assertEqual(["session_stopped", "done", False], out)

    def test_clicking_it_reads_it_and_moves_nothing(self):
        # A build's card takes the reader to the row it reports on. This one
        # names no row, so following it would drop the reader on a goal the
        # notice never mentioned.
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "var moved = null;"
            "window.__hcSelectGoal = function (id) { moved = id; };"
            "fire('click', stack()[0].querySelector('.hc-alert-title'));"
            "[A.unread(), stack().length, moved];")
        self.assertEqual([0, 0, None], out)

    def test_the_same_notice_is_never_shown_twice(self):
        # State is polled every 1.5s and the store keeps twenty rows, so the
        # same notice arrives again on every poll for the rest of the session.
        out = self.notices(
            "var row = [{id: 'n1', kind: 'session_stopped',"
            "            at: iso(now + 5000), detail: 'done'}];"
            "window.__hcPromptUI.showNotices(row);"
            "window.__hcPromptUI.showNotices(row);"
            "window.__hcPromptUI.showNotices(row);"
            "stack().length;")
        self.assertEqual(1, out)

    def test_at_most_three_stand_at_once(self):
        out = self.notices(
            "window.__hcPromptUI.showNotices([1,2,3,4,5].map(function (n) {"
            "  return {id: 'n' + n, kind: 'session_stopped',"
            "          at: iso(now + n * 1000), detail: 'turn ' + n}; }));"
            "drawn().map(function (row) { return row[2]; });")
        # The newest three: a stack that grows without bound covers the page
        # it is reporting on.
        self.assertEqual(["turn 3", "turn 4", "turn 5"], out)

    def test_a_kind_the_banner_has_no_words_for_says_nothing(self):
        # The copy is one of three sentences. An unknown kind would reach the
        # reader as a blank banner or a raw enum name.
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'compacted',"
            "  at: iso(now + 5000), detail: 'whatever'}]);"
            "[stack().length, A.log().length];")
        self.assertEqual([0, 0], out)

    def test_every_sentence_it_can_say_is_one_the_hook_proved(self):
        out = self.notices(
            "window.__hcPromptUI.showNotices("
            "  ['session_stopped', 'subagent_returned', 'session_ended']"
            "    .map(function (kind, i) { return {id: 'n' + i, kind: kind,"
            "      at: iso(now + 1000 + i), detail: ''}; }));"
            "drawn();")
        self.assertEqual(
            [["session_stopped", "Claude finished responding", None],
             ["subagent_returned", "A subagent returned", None],
             ["session_ended", "Session ended", None]], out)

    def test_a_notice_with_nothing_to_add_carries_no_empty_line(self):
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_ended',"
            "  at: iso(now + 5000), detail: ''}]);"
            "[stack().length, !!stack()[0].querySelector('.hc-alert-detail'),"
            " !!stack()[0].querySelector('.hc-alert-close')];")
        self.assertEqual([1, False, True], out)

    def test_a_global_vault_draws_no_banner(self):
        # There is no one session for it to report on.
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "[A.stack(), A.log().length, document.title];",
            scope="global")
        # And the sweep that names a chat tab never renames a vault's.
        self.assertEqual([None, 0, "Goals"], out)

    def test_the_tab_carries_the_mark_until_the_notice_is_read(self):
        # The workspace is usually on a second screen. The tab strip is the
        # only part of it a reader looking elsewhere can see -- so the mark
        # follows the unread count, not the six seconds the card is up. A
        # reader who was looking away for those six seconds is exactly the
        # reader it is for.
        out = self.notices(
            "var titles = [document.title];"
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "titles.push(document.title);"
            "window.fireTimers();"
            "titles.push(document.title);"
            "A.markAllRead();"
            "titles.push(document.title);"
            "titles;")
        self.assertEqual(
            ["Engelbart · 7f3a1b2c", "● Engelbart · 7f3a1b2c",
             "● Engelbart · 7f3a1b2c", "Engelbart · 7f3a1b2c"], out)

    def test_a_notice_takes_itself_away(self):
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "var before = stack().length;"
            "window.fireTimers();"
            "[before, stack().length];",
            defer=True)
        self.assertEqual([1, 0], out)

    def test_reading_it_keeps_it_on_screen(self):
        # Six seconds is not long enough to read a line and think about it,
        # so the clock stops while the pointer is on it and starts again when
        # it leaves.
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "var box = stack()[0];"
            "fire('mouseover', box);"
            "window.fireTimers();"
            "var held = stack().length;"
            "fire('mouseout', box, null);"
            "window.fireTimers();"
            "[held, stack().length];",
            defer=True)
        self.assertEqual([1, 0], out)

    def test_moving_across_its_own_text_is_not_leaving_it(self):
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "var box = stack()[0];"
            "fire('mouseover', box);"
            "fire('mouseout', box.querySelector('.hc-alert-title'),"
            "     box.querySelector('.hc-alert-detail'));"
            "window.fireTimers();"
            "stack().length;",
            defer=True)
        self.assertEqual(1, out)

    def test_the_close_control_dismisses_it_as_read(self):
        # × on a build's card marks it read and leaves it in the center. The
        # same control does the same thing here, which is what takes the
        # mark off the tab.
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "var box = stack()[0];"
            "fire('click', box.querySelector('.hc-alert-close'));"
            "[stack().length, A.log().length, A.unread(), document.title];")
        self.assertEqual([0, 1, 0, "Engelbart · 7f3a1b2c"], out)

    def test_a_dismissed_notice_does_not_come_back_on_the_next_poll(self):
        out = self.notices(
            "var row = [{id: 'n1', kind: 'session_stopped',"
            "            at: iso(now + 5000), detail: 'done'}];"
            "window.__hcPromptUI.showNotices(row);"
            "fire('click', stack()[0].querySelector('.hc-alert-close'));"
            "window.__hcPromptUI.showNotices(row);"
            "[stack().length, A.log().length];",
            defer=True)
        self.assertEqual([0, 1], out)

    def test_a_workspace_whose_server_stopped_says_so_until_one_lands(self):
        # Reopening a workspace that is running older code than the plugin
        # replaces it, which leaves this window pointed at a process that has
        # ended. Nothing else on the page changes when that happens, so
        # silence reads as a page that broke rather than one that closed.
        # It is said where everything else is said now -- a card in the
        # corner -- and taken back the moment a server answers again.
        out = self.notices(
            "var alive = false;"
            "fetch = function () { return alive"
            "  ? Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ goals: [], prompts: [],"
            "        scope: 'chat', session_id: 's' }); } })"
            "  : Promise.reject(new Error('gone')); };"
            "var R = window.__hcPromptUI.refreshState;"
            "var seen = [];"
            "R().then(function () { seen.push(stack().length); return R(); })"
            "  .then(function () { seen.push(stack().length); return R(); })"
            "  .then(function () { seen.push(drawn()); alive = true; return R(); })"
            "  .then(function () { seen.push(stack().length); return seen; });")
        self.assertEqual(
            [0, 0,
             [["server_gone", "This workspace is no longer running",
               "It was stopped or restarted — the newer one opens in its own "
               "tab. Nothing typed here is being saved."]],
             0],
            out)

    def test_the_banner_setting_governs_it_like_a_build(self):
        # One switch in the gear panel, not two. Turning banners off still
        # leaves the entry behind the bell to find later.
        out = self.notices(
            "A.setSettings({banners: false});"
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "[stack().length, A.log().length, A.unread()];")
        self.assertEqual([0, 1, 1], out)

    def test_junk_in_the_notice_list_is_stepped_over(self):
        # /api/state is a file on disk away from a process that may have been
        # killed mid-write. A malformed row may not take the page down.
        out = self.notices(
            "window.__hcPromptUI.showNotices(["
            "  null, 'nope', {}, {id: 'n1'},"
            "  {id: 'n2', kind: 'session_stopped', at: 'not a date'},"
            "  {id: 'n3', kind: 'session_stopped', at: iso(now + 5000)}]);"
            "[stack().length, A.log().length];")
        self.assertEqual([1, 1], out)

    def test_the_tab_is_named_after_the_conversation_it_watches(self):
        # A day with three of these open needs the tab strip to tell them
        # apart, and the goal tree is what they all have in common.
        self.assertEqual(
            "Engelbart · 7f3a1b2c",
            self.notices("window.__hcPromptUI.pageTitle();"))

    def test_a_workspace_that_was_never_told_its_session_still_has_a_name(self):
        # /api/state answers the session id; a page that booted before it
        # answered says what it knows rather than "undefined".
        self.assertEqual(
            ["Engelbart", "Engelbart"],
            self.notices("[window.__hcPromptUI.pageTitle(), document.title];",
                         session=""))

    def test_the_tab_keeps_its_name_after_the_artifact_wipes_it(self):
        # The artifact unpacks its template by replacing the whole
        # documentElement, which takes the <title> with it. That is why the
        # name is re-asserted by the standing sweep and not written once.
        out = self.notices(
            "var seen = [document.title];"
            # What the unpack does to the tab.
            "document.title = '';"
            "window.__hcPromptUI.renderChatSurface();"
            "seen.push(document.title);"
            "seen;")
        self.assertEqual(["Engelbart · 7f3a1b2c", "Engelbart · 7f3a1b2c"], out)

    def test_a_wipe_while_a_notice_is_unread_comes_back_marked(self):
        # The mark is derived from what the tab should say, not remembered
        # from what it did say: putting back the remembered string here would
        # restore the empty title the artifact had just left behind.
        out = self.notices(
            "window.__hcPromptUI.showNotices([{id: 'n1', kind: 'session_stopped',"
            "  at: iso(now + 5000), detail: 'done'}]);"
            "document.title = '';"
            "window.__hcPromptUI.renderChatSurface();"
            "var marked = document.title;"
            "A.markAllRead();"
            "document.title = '';"
            "window.__hcPromptUI.renderChatSurface();"
            "[marked, document.title];")
        self.assertEqual(["● Engelbart · 7f3a1b2c", "Engelbart · 7f3a1b2c"], out)

    def test_the_card_sits_above_the_prompt_picker(self):
        # Both are fixed overlays. A card under the picker is a card nobody
        # can dismiss while choosing a prompt; one at the top of the stacking
        # order covers a modal the reader is working in.
        css = self.run_js("window.__hcPromptUI.alerts.css();")
        dialog = self.run_js("window.__hcPromptUI.dialogCss();")
        self.assertIn("z-index:100002", css)
        self.assertIn("z-index:100000", dialog)


# Every class the chat-scope template patches introduce, and therefore every
# class the stylesheet has something to dress. They are listed here rather
# than scraped from bridge.js so that a patch quietly losing its anchor -- or
# its class -- fails, instead of the test agreeing with whatever it finds.
LAUNCH_CLASSES = (
    "hc-row", "hc-rowtitle",
    "hc-shell", "hc-main",
    "hc-rail-left", "hc-rail-head", "hc-rail-name", "hc-rail-count",
    "hc-rail-right", "hc-rail-actions", "hc-rail-copy",
    "hc-rail-none", "hc-rail-understand",
    "hc-sources", "hc-sources-label", "hc-src", "hc-src-tag", "hc-src-label",
    "hc-src-rm", "hc-src-add", "hc-tabs",
    "hc-chip", "hc-chip-n", "hc-titlerow", "hc-chiprow", "hc-brand",
    "hc-subbar", "hc-viewtabs",
    "hc-panels", "hc-session", "hc-chats", "hc-handoff", "hc-alerts",
    "hc-settings", "hc-updated",
    "hc-search", "hc-search-field", "hc-search-glyph", "hc-search-input",
    "hc-search-clear", "hc-search-hits",
)


def _luminance(hexcolor):
    """WCAG relative luminance of a #rgb or #rrggbb string."""
    digits = hexcolor.lstrip("#")
    if len(digits) == 3:
        digits = "".join(d * 2 for d in digits)
    channels = []
    for pair in (digits[0:2], digits[2:4], digits[4:6]):
        c = int(pair, 16) / 255
        channels.append(c / 12.92 if c <= 0.04045
                        else ((c + 0.055) / 1.055) ** 2.4)
    return (0.2126 * channels[0] + 0.7152 * channels[1]
            + 0.0722 * channels[2])


def _contrast(fg, bg):
    """WCAG 2.1 contrast ratio between two CSS hex colours."""
    lighter, darker = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class LaunchSkinTests(BridgeTestCase):
    """The three-column skin: one root attribute, and only in a chat."""

    def test_the_tabs_are_the_headers_second_row_and_read_as_the_counts_do(self):
        # One header of two rows (--hc-row each) under one rule: the brand
        # and the project on the first, the view tabs and the filter counts
        # on the second. The tabs used to be a 32px strip of their own,
        # fixed under a one-row header at its own indent with its own rule
        # -- a second bar, and one that came apart from the header on a
        # trackpad bounce.
        css = self.run_js("window.__hcPromptUI.launchCss();")
        row = 37
        self.assertIn("--hc-row:%dpx;--hc-top:%dpx" % (row, row * 2), css)
        self.assertIn(".hc>div:first-child{position:sticky;top:0;z-index:19;"
                      "background:var(--bg);height:var(--hc-top);box-sizing:border-box;"
                      "padding:0 16px calc(var(--hc-row) - 1px)!important;", css)
        # The second row is the header's own child at its foot, at the
        # header's indent, with no rule of its own.
        self.assertIn(".hc-subbar{position:absolute;left:0;right:0;bottom:0;"
                      "height:calc(var(--hc-row) - 1px);", css)
        self.assertRegex(css, r"\.hc-subbar\{[^}]*padding:0 16px\}")
        self.assertNotRegex(css, r"\.hc-subbar\{[^}]*border-top")
        self.assertNotIn(".hc-viewtabs{position:fixed", css)
        self.assertNotIn(".hc-pillbar", css)
        self.assertNotIn("[data-hc-viewtabs]", css)
        # The tabs are set as the counts at the row's other end are: 11px,
        # the same tracking, title case; the open one is bold and stands on
        # the header's rule.
        self.assertIn(".hc-viewtab{position:relative;display:inline-flex;"
                      "align-items:center;height:100%;font:500 11px 'Source Code Pro',"
                      "monospace;letter-spacing:.2px;color:var(--fnt);", css)
        self.assertNotRegex(css, r"\.hc-viewtab\{[^}]*uppercase")
        self.assertIn(".hc-viewtab[data-hc-on]{font-weight:700;color:var(--ink)}", css)
        self.assertIn(".hc-viewtab[data-hc-on]::after{content:'';position:absolute;"
                      "left:0;right:0;bottom:-1px;height:2px;", css)
        # The counts: the artifact's own row, lifted to the second row's
        # right end as words with no box around them, and gone on the
        # overview -- counts describe the tree, and the overview is not it.
        self.assertRegex(css, r"\.hc-titlerow\{position:fixed;top:var\(--hc-row\);"
                              r"left:auto;right:16px;height:calc\(var\(--hc-row\) - 1px\);")
        self.assertIn("[data-hc-launch][data-hc-overview] .hc-titlerow"
                      "{display:none!important}", css)
        self.assertIn(".hc-chip{padding:0;border:0;border-radius:0;background:transparent;",
                      css)
        # With the notice bar (34) under the header, what is under it drops
        # by that much; the header keeps its two rows.
        self.assertIn("[data-hc-launch][data-hc-notice]{--hc-top:%dpx}" % (row * 2 + 34), css)
        self.assertIn("[data-hc-launch][data-hc-notice] .hc>div:first-child{height:%dpx}"
                      % (row * 2), css)
        self.assertIn("[data-hc-launch][data-hc-notice] .hc>div:nth-child(2)"
                      "{padding-top:34px!important}", css)
        self.assertIn(".hc-notice-stack{position:fixed;top:%dpx;" % (row * 2), css)
        # And the page does not rubber-band: a bounce would carry the
        # sticky header off while the fixed counts stayed.
        self.assertRegex(css, r"\[data-hc-launch\]\{[^}]*overscroll-behavior:none\}")

    def test_the_stamp_is_a_tick_that_leads_the_header_tools(self):
        # The header used to end in a clock that changed every minute. It
        # now opens the right-hand group with "saved" and a tick, and keeps
        # the time as the stamp's title for whoever wants it.
        css = self.run_js("window.__hcPromptUI.launchCss();")
        self.assertIn(".hc-updated{order:-2;color:var(--fnt)", css)
        self.assertIn(".hc-panels{order:-1;", css)

    def test_the_rail_toggles_stand_down_on_the_overview(self):
        # They arrange the goals page. On the overview there is no rail to
        # hide, so they were two buttons that did nothing visible.
        css = self.run_js("window.__hcPromptUI.launchCss();")
        self.assertIn("[data-hc-launch] .hc-panels{order:-1;", css)
        self.assertIn("[data-hc-launch][data-hc-overview] .hc-panels{display:none}",
                      css)

    def chat(self):
        return ("window.__hcPromptUI.acceptState("
                "{ goals: [], prompts: [], scope: 'chat',"
                "  session_id: '7f3a1b2c-0000' });")

    def patch_report(self, scope):
        """Which anchors missed, and which classes never reached the source.

        The skin is applied by patching the artifact's template *source*
        before the runtime unpacks it, so a moved anchor is not a crash: the
        pair no-ops and the layout silently does not apply. Every browser
        assertion phrased as "this is hidden" or "this is styled" passes
        vacuously in that state, which is why the anchors are asserted here,
        against the checked-in bundle, and not only through a page.
        """
        return self.patched_bundle(
            "[window.__hcPromptUI.patchMisses(),"
            " %s.filter(function (c) {"
            "   return out.indexOf('class=\"' + c + '\"') < 0; })];"
            % json.dumps(list(LAUNCH_CLASSES)),
            scope=scope)

    def test_every_anchor_the_launch_shell_names_is_found_in_the_artifact(self):
        misses, missing = self.patch_report("chat")
        self.assertEqual([], misses)
        self.assertEqual([], missing)

    def test_a_global_vault_gets_a_source_with_none_of_those_names_in_it(self):
        # The same reduce runs, and every pair is a no-op: nothing missed,
        # and not one of the launch classes is in what a vault is served.
        misses, missing = self.patch_report("global")
        self.assertEqual([], misses)
        self.assertEqual(sorted(LAUNCH_CLASSES), sorted(missing))

    def test_the_copy_button_label_clears_aa_in_both_themes(self):
        # 11.5px bold is not "large text", so both themes owe 4.5:1 -- and
        # the fill is a variable each theme redefines, so the label cannot be
        # one colour. The light fill is dark enough that only white clears
        # it; the dark theme's fill is bright enough that only near-black
        # does. This caught a 3.69:1 label the eye read as fine.
        css = self.run_js("window.__hcPromptUI.launchCss();")

        def first(pattern):
            found = re.search(pattern, css)
            self.assertIsNotNone(found, pattern)
            return found.group(1)

        dark = r"\[data-hc-launch\]\[data-hc-theme=\"dark\"\]"
        pairs = [
            (first(r"\[data-hc-launch\]\{[^}]*--hc-ok:(#[0-9a-fA-F]{3,6})"),
             first(r"\[data-hc-launch\] \.hc-rail-copy\{"
                   r"[^}]*?(?<![-\w])color:(#[0-9a-fA-F]{3,6})")),
            (first(dark + r"\{[^}]*--hc-ok:(#[0-9a-fA-F]{3,6})"),
             first(dark + r" \.hc-rail-copy\{"
                          r"[^}]*?(?<![-\w])color:(#[0-9a-fA-F]{3,6})")),
        ]
        self.assertEqual(2, len({label for _, label in pairs}))
        for fill, label in pairs:
            self.assertGreaterEqual(round(_contrast(label, fill), 2), 4.5,
                                    (label, fill))

    def test_a_global_vault_is_never_dressed(self):
        # Every rule in the sheet is behind [data-hc-launch], and the
        # attribute is only ever written in a chat -- so a global vault does
        # not so much as load the stylesheet.
        out = self.run_js(
            "window.__hcPromptUI.acceptState("
            "  { goals: [], prompts: [], scope: 'global' });"
            "var applied = window.__hcPromptUI.applyLaunchSkin();"
            "[applied, document.documentElement.getAttribute('data-hc-launch'),"
            " document.getElementById('hc-launch-style') ? 1 : 0];")
        self.assertEqual([False, None, 0], out)

    def test_a_chat_is_dressed_once_and_stays_dressed(self):
        out = self.run_js(
            self.chat()
            + "var first = window.__hcPromptUI.applyLaunchSkin();"
            "var again = window.__hcPromptUI.applyLaunchSkin();"
            "[first, again,"
            " document.documentElement.getAttribute('data-hc-launch'),"
            " document.getElementById('hc-launch-style').textContent"
            "   .indexOf('[data-hc-launch]') === 0];")
        self.assertEqual([True, False, "chat", True], out)

    def test_every_rule_in_the_sheet_is_gated_on_the_root_attribute(self):
        css = self.run_js("window.__hcPromptUI.launchCss();")
        # A @keyframes block is not a selector and cannot dress anything on
        # its own: it applies only where an animation names it, and those
        # declarations are rules like any other, gated below. Its steps
        # ("0%,100%{...}") would otherwise read as ungated rules.
        frames = re.compile(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}")
        selectors = frames.sub("", css)
        rules = [rule for rule in selectors.split("}") if rule.strip()]
        self.assertTrue(rules)
        stray = [rule for rule in rules
                 if not rule.lstrip().startswith("[data-hc-launch]")]
        self.assertEqual([], stray)
        # And the animation is only ever reached from a gated rule.
        for name in re.findall(r"@keyframes\s+([\w-]+)", css):
            for line in css.split("}"):
                if name in line and "@keyframes" not in line:
                    self.assertTrue(line.lstrip().startswith("[data-hc-launch]"),
                                    "%s is used by an ungated rule" % name)

    def test_the_workspace_is_full_bleed(self):
        # The columns meet the window on every side and each other on one
        # shared line: no outer padding, no gap, no radius, and every rail
        # keeps only the border it shares with the document.
        css = self.run_js("window.__hcPromptUI.launchCss();")
        self.assertIn(".hc>div:nth-child(2){max-width:none!important;"
                      "padding:0!important}", css)
        self.assertIn(".hc-shell{gap:0!important", css)
        self.assertIn(".hc-rail-left{position:relative;flex:0 0 var(--hc-left)"
                      "!important;height:calc(100vh - var(--hc-top))!important;"
                      "padding:0 0 6px!important;border-width:0 1px 0 0!important;"
                      "border-radius:0!important}", css)
        # A column, because the pane's one child is a preview that takes
        # whatever height is left. The display is !important for the same
        # reason the width is: the artifact writes one inline.
        self.assertIn(".hc-main{display:flex!important;flex-direction:column;"
                      "flex:1 1 auto!important;order:2;"
                      "height:calc(100vh - var(--hc-top))!important;top:0!important;"
                      "border:0!important;border-radius:0!important", css)
        self.assertIn(".hc-main>.hc-preview{flex:1 1 auto", css)
        self.assertRegex(css, r"\.hc-rail-right\{[^}]*border-width:0 0 0 1px;"
                              r"border-radius:0;")
        # The header is a fixed height -- two rows of --hc-row -- and
        # --hc-top is exactly that height, so the columns are sized against
        # it, not against a guess.
        self.assertIn("--hc-row:37px;--hc-top:74px", css)
        # Sticky: the pills are pinned to the viewport, so the bar they sit
        # in must not scroll away from under them.
        self.assertIn(".hc>div:first-child{position:sticky;top:0;z-index:19;"
                      "background:var(--bg);height:var(--hc-top);", css)

    def test_the_brand_is_the_one_serif_and_the_pills_ride_in_the_header(self):
        css = self.run_js("window.__hcPromptUI.launchCss();")
        self.assertRegex(css, r"\.hc-brand\{font:600 15px Georgia,[^}]*serif!important")
        # No marker before the name: the brand is the word alone.
        self.assertNotIn(".hc-brand::before", css)
        # The filter counts' row is lifted into the header's second row by
        # position, and takes no height where the artifact renders it: the
        # middle bar is gone.
        self.assertRegex(css, r"\.hc-titlerow\{position:fixed;"
                              r"top:var\(--hc-row\);left:auto;right:16px;"
                              r"height:calc\(var\(--hc-row\) - 1px\);"
                              r"margin:0;padding:0!important")

    def layout(self, tail):
        # The harness's root has a bare style object; the bridge writes the
        # rail widths as CSS variables, so give it the two calls it uses.
        return self.run_js(
            self.chat()
            + "var props = {};"
            "document.documentElement.style.setProperty ="
            "  function (k, v) { props[k] = v; };"
            "document.documentElement.style.getPropertyValue ="
            "  function (k) { return props[k] || ''; };"
            + tail)

    def test_the_rails_start_at_a_quarter_of_the_window_each(self):
        # 1 : 2 : 1 -- the harness has no window width, so the default falls
        # back to 1440 and a quarter of that.
        self.assertEqual(
            {"left": 360, "right": 360, "hideLeft": False, "hideRight": False},
            self.layout("window.__hcPromptUI.railLayout();"))

    def test_a_dragged_width_is_clamped_kept_and_drawn(self):
        out = self.layout(
            "var ui = window.__hcPromptUI;"
            "var a = ui.setRailWidth('left', 380);"
            "var b = ui.setRailWidth('left', 40);"
            "var c = ui.setRailWidth('right', 9000);"
            "var d = ui.setRailWidth('right', 'nonsense');"
            "[a, b, c, d, props['--hc-left'], props['--hc-right'],"
            " JSON.parse(localStorage.getItem('hc-launch-layout-v2'))];")
        self.assertEqual([380, 200, 720, 360, "200px", "360px",
                          {"left": 200, "right": 360,
                           "hideLeft": False, "hideRight": False}], out)

    def test_hiding_a_rail_is_a_root_attribute_and_survives_the_page(self):
        out = self.layout(
            "var ui = window.__hcPromptUI; var root = document.documentElement;"
            "var h = ui.setRailHidden('right', true);"
            "var on = root.getAttribute('data-hc-hide-right') !== null;"
            "var t = ui.toggleRail('right');"
            "var off = root.getAttribute('data-hc-hide-right') !== null;"
            "ui.toggleRail('left');"
            "[h, on, t, off, root.getAttribute('data-hc-hide-left') !== null,"
            " JSON.parse(localStorage.getItem('hc-launch-layout-v2')).hideLeft];")
        self.assertEqual([True, True, False, False, True, True], out)

    def test_the_header_gets_one_toggle_per_rail_that_reads_the_layout(self):
        out = self.layout(
            "var slot = document.createElement('span');"
            "slot.className = 'hc-panels'; app.appendChild(slot);"
            "var ui = window.__hcPromptUI;"
            "ui.renderPanelToggles(); ui.renderPanelToggles();"
            "ui.setRailHidden('left', true);"
            "[slot.children.length,"
            " slot.children.map(function (b) {"
            "   return [b.getAttribute('data-hc-panel'), b.className]; })];")
        self.assertEqual([2, [["left", "hc-panel"], ["right", "hc-panel hc-panel-on"]]],
                         out)

    def test_the_injection_card_says_only_what_the_state_proves(self):
        # A chat nobody has opened the workspace for has been told nothing,
        # and the card says that rather than leaving the line off.
        self.assertEqual(
            [["head", "context injection"],
             ["off", "not sent to Claude yet"],
             ["", "reads: prompt · subagent · task"],
             ["off", "off · /bart turns it back on"]],
            self.run_js(
                "window.__hcPromptUI.injectionLines("
                "  { cached: false, last_delta_chars: null, last_at: null,"
                "    active: false, reads: ['prompt', 'subagent', 'task'] });"))

    def test_no_line_on_the_card_claims_the_model_read_anything(self):
        # The snapshot behind these numbers records what the hook *rendered*
        # into a turn; Claude Code may still drop or compact it. "sent" is
        # what this side can prove, and it is what every line says.
        rows = self.run_js(
            "window.__hcPromptUI.injectionLines("
            "  { cached: true, last_delta_chars: 570,"
            "    last_at: '2026-08-17T10:49:20+00:00',"
            "    active: true, reads: ['session start', 'prompt'] });")
        self.assertEqual([], [row for row in rows if "read it" in row[1]])
        self.assertIn(["", "reads: session start · prompt"], rows)
        self.assertEqual(
            1, len([row for row in rows if row[1].startswith("last sent ")]),
            rows)

    def test_a_pending_change_is_sized_as_an_estimate(self):
        # Characters over four, and marked "~": the browser cannot count
        # tokens, so it must not print a number that looks like it did.
        rows = self.run_js(
            "window.__hcPromptUI.injectionLines("
            "  { cached: true, last_delta_chars: 570, last_at: null,"
            "    active: true, reads: [] });")
        self.assertIn(["on", "goal document sent ✓"], rows)
        self.assertIn(["", "~143 tok changed since it was last sent"], rows)
        self.assertIn(["on", "on · /bart disable turns it off"], rows)

    def test_a_document_the_model_is_current_on_says_so(self):
        rows = self.run_js(
            "window.__hcPromptUI.injectionLines("
            "  { cached: true, last_delta_chars: 0, last_at: null,"
            "    active: true, reads: [] });")
        self.assertIn(["", "unchanged since it was last sent"], rows)
        self.assertEqual(
            [], [row for row in rows if "tok" in row[1]])

    def test_a_state_with_no_injection_draws_no_card(self):
        # `null` is what the bridge holds before the first poll lands, and an
        # empty card is a claim about a chat nothing is known about yet.
        self.assertEqual([], self.run_js(
            "window.__hcPromptUI.injectionLines(null);"))


class DevServerStripTests(BridgeTestCase):
    """The strip that says whether this project's dev server is up.

    It is the one control in the rail whose subject is not the goal tree: a
    goal whose work is a web interface has a page, and the strip's whole job
    is to say whether that page is being served, start it when it is not, and
    hand over the address the process itself printed.
    """

    STOPPED = {"ok": True, "status": "stopped", "can_start": True,
               "framework": "Next.js", "command": "npm run dev",
               "cwd": "/Users/x/app", "url": "", "last": []}

    def paint(self, state, open_log=False, lines=None):
        return self.run_js(
            "var box = document.createElement('div');"
            "window.__hcPromptUI.dev.paint(box, 'g1', %s, %s, %s);"
            "var head = box.children[0] ? box.children[0].children : [];"
            "out = {display: box.style.display,"
            "  state: box.getAttribute('data-hc-dev-state'),"
            "  head: head.map(function (c) { return [c.className, c.textContent,"
            "     c.title || '', c.href || '',"
            "     c.getAttribute('data-hc-dev-start') !== null ? 'start'"
            "     : c.getAttribute('data-hc-dev-force') !== null ? 'force'"
            "     : c.getAttribute('data-hc-dev-stop') !== null ? 'stop'"
            "     : c.getAttribute('data-hc-dev-log') !== null ? 'log' : '']; }),"
            "  last: box.children[1] ? box.children[1].textContent : null,"
            "  bad: box.children[1]"
            "    ? box.children[1].getAttribute('data-hc-dev-bad') : null,"
            "  log: box.children[2]"
            "    ? box.children[2].children.map(function (c) {"
            "        return c.textContent; }) : null};"
            % (json.dumps(state), "true" if open_log else "false",
               json.dumps(lines or [])))

    def buttons(self, painted):
        return [[row[1], row[4]] for row in painted["head"] if row[4]]

    def test_a_project_that_is_not_running_offers_to_run_it(self):
        out = self.paint(self.STOPPED)
        self.assertEqual("stopped", out["state"])
        self.assertEqual("Next.js · not running", out["head"][1][1])
        self.assertEqual([["Start", "start"], ["Log", "log"]],
                         self.buttons(out))
        # What it would run, said before it runs it.
        self.assertEqual("run npm run dev here", out["head"][2][2])

    def test_a_running_one_links_to_the_address_it_printed(self):
        out = self.paint(dict(self.STOPPED, status="running",
                              url="http://127.0.0.1:3210/",
                              last=["  ✓ Ready in 900ms"]))
        self.assertEqual("running", out["state"])
        link = out["head"][2]
        self.assertEqual("hc-dev-link", link[0])
        self.assertEqual("127.0.0.1:3210", link[1])
        self.assertEqual("http://127.0.0.1:3210/", link[3])
        self.assertEqual([["Stop", "stop"], ["Log", "log"]], self.buttons(out))
        self.assertEqual("  ✓ Ready in 900ms", out["last"])

    def test_a_busy_port_is_named_as_somebody_elses_not_as_this_project(self):
        out = self.paint(dict(self.STOPPED, status="in_use",
                              other_url="http://127.0.0.1:3000/"))
        self.assertEqual("Next.js · port busy", out["head"][1][1])
        self.assertIn("the workspace did not start it", out["head"][2][2])
        self.assertEqual([["Start anyway", "force"], ["Log", "log"]],
                         self.buttons(out))

    def test_a_server_that_failed_says_so_where_the_last_line_goes(self):
        out = self.paint(dict(self.STOPPED,
                              error="the dev server exited before it served anything",
                              last=["Type error: nope"]))
        self.assertIn("exited before it served", out["last"])
        self.assertEqual("1", out["bad"])

    def test_a_directory_with_nothing_to_serve_gets_no_strip(self):
        out = self.paint({"ok": True, "status": "stopped", "can_start": False,
                          "error": "package.json has no dev or start script"})
        self.assertEqual("none", out["display"])
        self.assertEqual([], out["head"])

    def test_the_log_prints_what_the_server_printed(self):
        out = self.paint(dict(self.STOPPED, status="running",
                              url="http://127.0.0.1:3210/"),
                         open_log=True,
                         lines=["$ npm run dev", "  - Local: http://localhost:3210"])
        self.assertEqual(["$ npm run dev", "  - Local: http://localhost:3210"],
                         out["log"])
        self.assertEqual("Hide log", self.buttons(out)[-1][0])

    def test_an_open_log_with_nothing_in_it_says_that(self):
        out = self.paint(self.STOPPED, open_log=True)
        self.assertEqual(["nothing printed yet"], out["log"])

    def test_the_strip_sits_above_the_build_panel(self):
        out = self.run_js(
            "var host = document.createElement('div');"
            "var watch = document.createElement('div');"
            "watch.className = 'hc-todo-watch';"
            "var actions = document.createElement('div');"
            "actions.className = 'hc-todos-actions';"
            "host.appendChild(watch); host.appendChild(actions);"
            "window.__hcPromptUI.dev.seed('g1', %s);"
            "window.__hcPromptUI.dev.render(host, 'g1');"
            "window.__hcPromptUI.dev.render(host, 'g1');"
            "out = host.children.map(function (c) { return c.className; });"
            % json.dumps(self.STOPPED))
        # Made once, wherever it is rendered from, and in front of the panel
        # the build writes into rather than after it.
        self.assertEqual(["hc-dev", "hc-todo-watch", "hc-todos-actions"], out)

    def test_the_stylesheet_carries_the_strip(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        self.assertIn(".hc-dev{flex:none", css)
        self.assertIn('.hc-dev[data-hc-dev-state="running"] .hc-dev-dot', css)
