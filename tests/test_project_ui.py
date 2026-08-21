"""The project in the workspace: a chip in the header, a menu, an overview.

The header reads "Engelbart / <project> ▾ / ● session". The name opens a
menu with the project's facts, a way into its overview, and the chats
started in the same directory, each linkable as a prompt source. The
overview draws over the document column: the name and objective, and the
repository as context -- README and file tree, both from the server. The
node harness from test_goal_ui_bridge holds that contract.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE, STATE


def chat_state(project=True):
    state = json.loads(json.dumps(STATE))
    state["scope"] = "chat"
    state["session_id"] = "7f3a1b2c-4d5e-4f60-8a9b-0c1d2e3f4a5b"
    if project:
        state["project"] = {"cwd": "/Users/me/work/myrepo", "name": "myrepo",
                            "branch": "feat/x",
                            "remote": "git@github.com:acme/myrepo.git",
                            "objective": "Ship the thing."}
    return state


CHATS = {"ok": True,
         "linked": [{"session_id": "aaaaaaaa-1111-4111-8111-111111111111",
                     "label": "twin"},
                    {"session_id": "bbbbbbbb-2222-4222-8222-222222222222",
                     "label": "scoped", "goal_id": "g1"}],
         "available": [
             {"session_id": "aaaaaaaa-1111-4111-8111-111111111111",
              "project": "myrepo", "cwd": "/Users/me/work/myrepo",
              "mtime": 1787280000, "size": 10},
             {"session_id": "bbbbbbbb-2222-4222-8222-222222222222",
              "project": "myrepo", "cwd": "/Users/me/work/myrepo",
              "mtime": 1787280000, "size": 10},
             {"session_id": "cccccccc-3333-4333-8333-333333333333",
              "project": "other", "cwd": "/Users/me/work/other",
              "mtime": 1787280000, "size": 10},
             {"session_id": "dddddddd-4444-4444-8444-444444444444",
              "project": "myrepo", "cwd": "", "mtime": 1787280000, "size": 10}]}

# The fetch the harness ships answers every GET alike; these tests answer
# the project's routes with what the server would say.
FETCH = (
    "fetch = function (url, opts) {"
    "  calls.push([url, opts && opts.body ? JSON.parse(opts.body) : null]);"
    "  var u = String(url); var body;"
    "  if (u.indexOf('/api/chats') >= 0) body = %s;"
    "  else if (u.indexOf('/api/file') >= 0) body = %s;"
    "  else if (u.indexOf('/api/tree') >= 0) body = %s;"
    "  else body = { ok: true, objective: opts && opts.body ? JSON.parse(opts.body).objective : '' };"
    "  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });"
    "};"
)


def fetch_js(chats=CHATS, readme=None, tree=None):
    readme = readme if readme is not None else {
        "ok": True, "path": "README.md", "text": "# myrepo\n\nhello", "truncated": False}
    tree = tree if tree is not None else {
        "ok": True, "root": "/Users/me/work/myrepo",
        "tree": [{"n": "src/", "kids": [{"n": "app.py"}]}, {"n": "README.md"}]}
    return FETCH % (json.dumps(chats), json.dumps(readme), json.dumps(tree))


PRELUDE = (
    "var P = window.__hcPromptUI;"
    "var slot = document.createElement('span'); slot.className = 'hc-project';"
    "header.appendChild(slot);"
    "var click = function (node) {"
    "  listeners.filter(function (l) { return l[0] === 'click'; })"
    "    .forEach(function (l) { l[1]({ target: node, preventDefault: function () {}, stopPropagation: function () {} }); });"
    "};"
    "var key = function (k, target, mods) {"
    "  var ev = Object.assign({ key: k, target: target, preventDefault: function () {} }, mods || {});"
    "  listeners.filter(function (l) { return l[0] === 'keydown'; }).forEach(function (l) { l[1](ev); });"
    "};"
    # The harness's textContent is a plain property: a node's own text,
    # not its children's. deepText reads the subtree the way a browser would.
    "var deepText = function (n) { return String(n.textContent || '') +"
    "  (n.children || []).map(deepText).join(''); };"
    "var texts = function (node, cls) {"
    "  var out = [];"
    "  (function walk(n) { (n.children || []).forEach(function (c) {"
    "    if (String(c.className).split(' ').indexOf(cls) >= 0) out.push(deepText(c));"
    "    walk(c); }); })(node);"
    "  return out;"
    "};"
    # Fetches resolve on microtasks the harness cannot fast-forward; a test
    # that reads what a fetch drew waits a few hops first.
    "var later = function (fn) { var p = Promise.resolve();"
    "  for (var i = 0; i < 12; i++) p = p.then(function () {});"
    "  return p.then(fn); };"
)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ProjectChipTests(BridgeTestCase):

    def test_the_chip_names_the_project_after_the_brand(self):
        got = json.loads(self.run_js(
            PRELUDE + "P.acceptState(%s);" % json.dumps(chat_state()) +
            "var drew = P.renderProjectChip();"
            "var again = P.renderProjectChip();"
            "var name = slot.querySelector('.hc-project-name');"
            "JSON.stringify([drew, again, slot.children.length,"
            " slot.children[0].textContent, name.querySelector('.hc-project-name-text').textContent,"
            " name.getAttribute('role'), name.title]);"))
        self.assertEqual([True, False, 2, "/", "myrepo", "button",
                          "This chat's project: /Users/me/work/myrepo"], got)

    def test_a_renamed_project_redraws_only_the_name(self):
        renamed = chat_state()
        renamed["project"]["name"] = "otherrepo"
        got = json.loads(self.run_js(
            PRELUDE + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "var first = slot.querySelector('.hc-project-name');"
            "P.acceptState(%s);" % json.dumps(renamed) +
            "var drew = P.renderProjectChip();"
            "JSON.stringify([drew, first === slot.querySelector('.hc-project-name'),"
            " slot.querySelector('.hc-project-name-text').textContent]);"))
        self.assertEqual([True, True, "otherrepo"], got)

    def test_no_project_or_global_scope_leaves_the_slot_empty(self):
        got = json.loads(self.run_js(
            PRELUDE + "P.acceptState(%s);" % json.dumps(chat_state(project=False)) +
            "var a = P.renderProjectChip();"
            "var g = %s; g.scope = 'global'; P.acceptState(g);" % json.dumps(chat_state()) +
            "var b = P.renderProjectChip();"
            "JSON.stringify([a, b, slot.children.length]);"))
        self.assertEqual([False, False, 0], got)

    def test_a_project_that_goes_away_takes_the_chip_with_it(self):
        got = json.loads(self.run_js(
            PRELUDE + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "P.acceptState(%s);" % json.dumps(chat_state(project=False)) +
            "var drew = P.renderProjectChip();"
            "JSON.stringify([drew, slot.children.length]);"))
        self.assertEqual([True, 0], got)

    def test_the_header_patch_leaves_the_slot_after_the_brand(self):
        got = json.loads(self.patched_bundle(
            "JSON.stringify([out.indexOf('class=\"hc-brand\"') >= 0,"
            " out.indexOf('Engelbart</span><span class=\"hc-project\"></span>') >= 0]);",
            scope="chat"))
        self.assertEqual([True, True], got)
        got = json.loads(self.patched_bundle(
            "JSON.stringify([out.indexOf('hc-project') >= 0]);", scope="global"))
        self.assertEqual([False], got, "a global vault has no project chip")


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ProjectMenuTests(BridgeTestCase):

    def test_the_menu_has_the_facts_the_overview_and_the_projects_chats(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "var menu = document.querySelector('.hc-project-menu');"
            "later(function () { return JSON.stringify([P.projectMenuShown(),"
            " slot.querySelector('.hc-project-name').getAttribute('data-hc-project-open'),"
            " menu.querySelector('.hc-project-act').textContent,"
            " texts(menu, 'hc-project-fact-k'), texts(menu, 'hc-project-fact-v'),"
            " texts(menu, 'hc-project-chat-name'), texts(menu, 'hc-project-link'),"
            " calls.filter(function (c) { return String(c[0]).indexOf('/api/chats') >= 0; }).length]); });"))
        self.assertEqual(True, got[0])
        self.assertEqual("", got[1])
        self.assertEqual("Overview →", got[2])
        self.assertEqual(["directory", "branch", "origin"], got[3])
        self.assertEqual(["/Users/me/work/myrepo", "feat/x", "git@github.com:acme/myrepo.git"], got[4])
        # This chat first, then the two others started in the same directory
        # -- not the one from another directory, nor the one with no cwd.
        self.assertEqual(["7f3a1b2c · this chat", "aaaaaaaa", "bbbbbbbb"],
                         [t.split(" · ")[0] + (" · this chat" if "this chat" in t else "") for t in got[5]])
        # The global link is what the button reflects: the goal-scoped one
        # on bbbbbbbb does not count as linked here.
        self.assertEqual(["linked", "link"], got[6])
        self.assertEqual(1, got[7])

    def test_clicking_the_name_again_or_escape_or_elsewhere_closes_it(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "var name = slot.querySelector('.hc-project-name');"
            "click(name); var a = P.projectMenuShown();"
            "click(name); var b = P.projectMenuShown();"
            "click(name); key('Escape', document.body); var c = P.projectMenuShown();"
            "click(name); click(header); var d = P.projectMenuShown();"
            "click(name); click(document.querySelector('.hc-project-facts')); var e = P.projectMenuShown();"
            "JSON.stringify([a, b, c, d, e, name.getAttribute('data-hc-project-open')]);"))
        self.assertEqual([True, False, False, False, True, ""], got)

    def test_the_link_toggle_posts_the_workspace_wide_link(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name'));"
            "later(function () {"
            "  var buttons = [];"
            "  (function walk(n) { (n.children || []).forEach(function (c) {"
            "    if (c.className === 'hc-project-link') buttons.push(c); walk(c); }); })(document.querySelector('.hc-project-menu'));"
            "  click(buttons[0]); click(buttons[1]);"
            "  return JSON.stringify(calls.filter(function (c) { return c[1] && /link_chat/.test(c[1].op); }).map(function (c) { return c[1]; }));"
            "});"))
        self.assertEqual([{"op": "unlink_chat", "session_id": "aaaaaaaa-1111-4111-8111-111111111111"},
                          {"op": "link_chat", "session_id": "bbbbbbbb-2222-4222-8222-222222222222",
                           "label": "bbbbbbbb"}], got)

    def test_the_overview_row_opens_the_overview_and_closes_the_menu(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name'));"
            "click(document.querySelector('.hc-project-act'));"
            "JSON.stringify([P.projectMenuShown(), P.overviewShown(),"
            " document.documentElement.getAttribute('data-hc-overview')]);"))
        self.assertEqual([False, True, ""], got)

    def test_remote_hrefs(self):
        got = json.loads(self.run_js(
            "JSON.stringify(['git@github.com:acme/myrepo.git', 'https://github.com/acme/myrepo.git',"
            " 'https://gitlab.com/a/b', 'ssh://weird', ''].map(window.__hcPromptUI.remoteHref));"))
        self.assertEqual(["https://github.com/acme/myrepo", "https://github.com/acme/myrepo",
                          "https://gitlab.com/a/b", "", ""], got)

    def test_chats_of_a_project_are_the_ones_started_in_its_directory(self):
        got = json.loads(self.run_js(
            "JSON.stringify(window.__hcPromptUI.projectChatsOf(%s, {cwd: '/Users/me/work/myrepo'}));"
            % json.dumps(CHATS)))
        self.assertEqual([
            {"session_id": "aaaaaaaa-1111-4111-8111-111111111111", "mtime": 1787280000, "linked": True},
            {"session_id": "bbbbbbbb-2222-4222-8222-222222222222", "mtime": 1787280000, "linked": False},
        ], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class OverviewTests(BridgeTestCase):

    def open(self, tail, state=None, **fetch):
        return json.loads(self.run_js(
            PRELUDE + fetch_js(**fetch)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(state or chat_state())
            + "P.openOverview();"
            + "var box = document.querySelector('.hc-overview');"
            + "later(function () { " + tail + " });"))

    def test_the_overview_shows_the_project_its_objective_and_its_repository(self):
        got = self.open(
            "return JSON.stringify([P.overviewShown(), !!box,"
            " texts(box, 'hc-overview-tab'), box.querySelector('.hc-overview-name').textContent,"
            " box.querySelector('.hc-overview-objective').value,"
            " box.querySelector('.hc-overview-objective').getAttribute('placeholder'),"
            " box.querySelector('.hc-overview-src-name').textContent,"
            " box.querySelector('.hc-overview-src-kind').textContent,"
            " deepText(box.querySelector('.hc-overview-repo-meta')),"
            " texts(box, 'hc-overview-pane-tab'),"
            " box.querySelector('.hc-overview-readme').textContent,"
            " texts(box, 'hc-overview-dir'), texts(box, 'hc-overview-file')]);")
        self.assertEqual([True, True, ["OVERVIEW", "GOALS"], "myrepo", "Ship the thing.",
                          "What are you trying to accomplish?", "myrepo", "Repository",
                          "git@github.com:acme/myrepo.git · feat/x", ["README.md", "Files"],
                          "# myrepo\n\nhello", ["src/"], ["app.py", "README.md"]], got)

    def test_the_readme_pane_is_on_first_and_files_can_be_brought_up(self):
        got = self.open(
            "var panes = function () { var out = {};"
            "  (function walk(n) { (n.children || []).forEach(function (c) {"
            "    if (c.className === 'hc-overview-pane') out[c.getAttribute('data-hc-pane')] = c.getAttribute('data-hc-pane-on') !== null;"
            "    walk(c); }); })(box); return out; };"
            "var before = panes();"
            "var tabs = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "  if (String(c.className).indexOf('hc-overview-pane-tab') === 0) tabs.push(c); walk(c); }); })(box);"
            "click(tabs[1]); var after = panes();"
            "return JSON.stringify([before, after, tabs.map(function (t) { return t.className; })]);")
        self.assertEqual([{"readme": True, "files": False}, {"readme": False, "files": True},
                          ["hc-overview-pane-tab", "hc-overview-pane-tab hc-overview-pane-tab-on"]], got)

    def test_a_missing_readme_and_an_empty_tree_say_so(self):
        got = self.open(
            "return JSON.stringify(texts(box, 'hc-overview-empty'));",
            readme={"ok": False, "error": "no such file in the project"},
            tree={"ok": True, "root": "/Users/me/work/myrepo", "tree": []})
        self.assertEqual(["No README.md in this project.", "The project directory is empty."], got)

    def test_folders_fold_and_the_top_level_starts_open(self):
        got = self.open(
            "var dir = box.querySelector('.hc-overview-dir');"
            "var a = dir.getAttribute('data-hc-open');"
            "click(dir); var b = dir.getAttribute('data-hc-open');"
            "click(dir); var c = dir.getAttribute('data-hc-open');"
            "return JSON.stringify([a, b, c]);")
        self.assertEqual(["", None, ""], got)

    def test_the_goals_tab_and_escape_close_the_overview(self):
        got = self.open(
            "var tabs = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "  if (String(c.className).split(' ').indexOf('hc-overview-tab') >= 0) tabs.push(c); walk(c); }); })(box);"
            "click(tabs[1]); var a = P.overviewShown();"
            "P.openOverview(); var b = P.overviewShown();"
            "key('Escape', document.body); var c = P.overviewShown();"
            "return JSON.stringify([a, b, c]);")
        self.assertEqual([False, True, False], got)

    def test_the_objective_is_saved_on_the_way_out_and_not_when_unchanged(self):
        got = self.open(
            "var ta = box.querySelector('.hc-overview-objective');"
            "ta.value = 'Ship the thing.'; P.saveObjective(ta);"
            "ta.value = '  Ship it well.  ';"
            "key('Enter', ta, { metaKey: true });"
            "return JSON.stringify([calls.filter(function (c) { return c[1] && c[1].op === 'set_project_objective'; }).map(function (c) { return c[1]; })]);")
        self.assertEqual([[{"op": "set_project_objective", "objective": "  Ship it well.  "}]], got)

    def test_the_overview_redraws_for_a_different_project(self):
        other = chat_state()
        other["project"] = {"cwd": "/Users/me/work/other", "name": "other", "branch": "",
                            "remote": "", "objective": ""}
        got = self.open(
            "var first = box;"
            "P.acceptState(%s); var drew = P.renderOverview();" % json.dumps(other) +
            "var box2 = document.querySelector('.hc-overview');"
            "return JSON.stringify([drew, box2 !== first, box2.querySelector('.hc-overview-name').textContent,"
            " deepText(box2.querySelector('.hc-overview-repo-meta')), box2.querySelector('.hc-overview-objective').value]);")
        self.assertEqual([True, True, "other", "/Users/me/work/other", ""], got)

    def test_without_a_project_the_overview_cannot_open(self):
        got = json.loads(self.run_js(
            PRELUDE + "P.acceptState(%s);" % json.dumps(chat_state(project=False)) +
            "JSON.stringify([P.openOverview(), P.overviewShown(), !!document.querySelector('.hc-overview')]);"))
        self.assertEqual([False, False, False], got)

    def test_the_overview_css_places_it_over_the_document_column(self):
        css = self.run_js("window.__hcPromptUI.projectCss();")
        # The whole window under the header: both rails are covered, and the
        # page's own variables paint it -- copied across, since the palette
        # is declared on .hc and the overview lives on <body>.
        self.assertIn(".hc-overview{display:none;position:fixed;top:var(--hc-top,37px);left:0;right:0;bottom:0", css)
        self.assertIn("background:var(--bg,#fff);color:var(--ink,#111)", css)
        self.assertIn("[data-hc-overview] .hc-overview{display:block}", css)

    def test_the_overview_and_menu_take_the_pages_palette(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js()
            + "getComputedStyle = function () { return { getPropertyValue: function (n) {"
            "  return { '--bg': '#0d1117', '--ink': '#e6edf3', '--panel': '#0d1117' }[n] || ''; } }; };"
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "var seen = {}; var track = function (node) { node.style.setProperty = function (k, v) { seen[k] = v; node.style[k] = v; };"
            "  node.style.getPropertyValue = function (k) { return node.style[k] || ''; }; };"
            "var made0 = made.length;"
            "P.openOverview();"
            "var box = document.querySelector('.hc-overview');"
            "track(box); var changed = P.syncProjectTheme(box); var again = P.syncProjectTheme(box);"
            "JSON.stringify([changed, again, seen]);"))
        self.assertEqual([True, False, {"--bg": "#0d1117", "--ink": "#e6edf3", "--panel": "#0d1117"}], got)


if __name__ == "__main__":
    unittest.main()
