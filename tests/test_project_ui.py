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
    "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
    "  calls.push([url, sent]);"
    "  var u = String(url); var body;"
    "  if (u.indexOf('/api/projects') >= 0) body = %s;"
    "  else if (u.indexOf('/api/chats') >= 0) body = %s;"
    "  else if (u.indexOf('/api/project.json') >= 0) body = %s;"
    "  else if (u.indexOf('/api/file') >= 0) body = %s;"
    "  else if (u.indexOf('/api/tree') >= 0) body = %s;"
    "  else if (sent && sent.op === 'open_project') body = { ok: true, url: 'http://127.0.0.1:8870/' };"
    "  else body = { ok: true, objective: sent ? sent.objective : '',"
    "                description: sent ? sent.description : '' };"
    "  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });"
    "};"
)

RECORD = ('{\n "schema_version": 1,\n "project": {\n  "name": "myrepo"\n }\n}\n')


PROJECTS = {"ok": True, "active": "/Users/me/work/myrepo", "projects": [
    {"cwd": "/Users/me/work/other", "name": "other", "chats": 4},
    {"cwd": "/Users/me/work/myrepo", "name": "myrepo", "chats": 3},
    {"cwd": "/Users/me/work/third", "name": "third", "chats": 1}]}


def fetch_js(chats=CHATS, readme=None, tree=None, record=None,
             projects=None):
    projects = projects if projects is not None else PROJECTS
    readme = readme if readme is not None else {
        "ok": True, "path": "README.md", "text": "# myrepo\n\nhello", "truncated": False}
    tree = tree if tree is not None else {
        "ok": True, "root": "/Users/me/work/myrepo",
        "tree": [{"n": "src/", "kids": [{"n": "app.py"}]}, {"n": "README.md"}]}
    record = record if record is not None else {
        "ok": True, "path": "/vault/projects/abc123.json", "written": True,
        "text": RECORD, "truncated": False}
    return FETCH % (json.dumps(projects), json.dumps(chats), json.dumps(record),
                    json.dumps(readme), json.dumps(tree))


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
    "var fire = function (type, target) {"
    "  listeners.filter(function (l) { return l[0] === type; })"
    "    .forEach(function (l) { l[1]({ type: type, target: target,"
    "      preventDefault: function () {}, stopPropagation: function () {} }); }); };"
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

    def test_the_menu_lists_the_projects_and_nothing_else(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "var menu = document.querySelector('.hc-project-menu');"
            "later(function () { return JSON.stringify([P.projectMenuShown(),"
            " slot.querySelector('.hc-project-name').getAttribute('data-hc-project-open'),"
            " texts(menu, 'hc-project-row-name'), texts(menu, 'hc-project-row-note'),"
            " menu.querySelector('.hc-project-new').textContent,"
            " menu.querySelector('.hc-project-chats-head') === null,"
            " calls.filter(function (c) { return String(c[0]).indexOf('/api/chats') >= 0; }).length]); });"))
        self.assertEqual(True, got[0])
        self.assertEqual("", got[1])
        # The one being looked at first, whatever order the server sent.
        self.assertEqual(["myrepo", "other", "third"], got[2])
        self.assertEqual(["active", "4 chats", "1 chat"], got[3])
        self.assertEqual("+ New project", got[4])
        # The chats of this project used to be listed under the switcher,
        # each with a link toggle. They are reachable as conversations in
        # the overview's context list now, and the prompt panel's chat
        # picker is where linking lives.
        self.assertEqual(True, got[5])
        self.assertEqual(0, got[6])

    def test_another_project_is_opened_beside_this_one(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "var opened = []; window.open = function (u) { opened.push(u); };"
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  var rows = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "    if (c.getAttribute('data-hc-goto') !== null) rows.push(c); walk(c); }); })(menu);"
            "  click(rows[1]);"
            "  return later(function () { return JSON.stringify(["
            "    rows.map(function (r) { return r.getAttribute('data-hc-goto'); }),"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'open_project'; })"
            "      .map(function (c) { return c[1].cwd; }), opened,"
            "    menu.querySelector('.hc-project-say').textContent]); }); });"))
        self.assertEqual([["/Users/me/work/myrepo", "/Users/me/work/other",
                           "/Users/me/work/third"],
                          ["/Users/me/work/other"],
                          ["http://127.0.0.1:8870/"],
                          "http://127.0.0.1:8870/"], got)

    def test_the_active_project_just_shuts_the_menu(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  var rows = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "    if (c.getAttribute('data-hc-goto') !== null) rows.push(c); walk(c); }); })(menu);"
            "  click(rows[0]);"
            "  return JSON.stringify([P.projectMenuShown(),"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'open_project'; }).length]); });"))
        self.assertEqual([False, 0], got)

    def test_a_new_project_names_a_directory_and_the_list_is_read_again(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  var form = menu.querySelector('.hc-project-newform');"
            "  var shut = form.getAttribute('data-hc-on');"
            "  click(menu.querySelector('.hc-project-new'));"
            "  var open = form.getAttribute('data-hc-on');"
            "  var field = menu.querySelector('.hc-project-newpath');"
            "  field.value = '~/Projects/fresh';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () { return JSON.stringify([shut, open,"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'new_project'; })"
            "      .map(function (c) { return c[1].cwd; }), field.value,"
            "    calls.filter(function (c) { return String(c[0]).indexOf('/api/projects') >= 0; }).length]); }); });"))
        self.assertEqual([None, "", ["~/Projects/fresh"], "", 2], got)

    def test_a_directory_that_is_not_there_is_reported_not_added(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: false, error: 'no such directory: /nope' }); } }); };"
            "  menu.querySelector('.hc-project-newpath').value = '/nope';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    return JSON.stringify([say.textContent, say.getAttribute('data-hc-bad')]); }); });"))
        self.assertEqual(["no such directory: /nope", ""], got)

    def test_clicking_the_name_again_or_escape_or_elsewhere_closes_it(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "var name = slot.querySelector('.hc-project-name');"
            "click(name); var a = P.projectMenuShown();"
            "click(name); var b = P.projectMenuShown();"
            "click(name); key('Escape', document.body); var c = P.projectMenuShown();"
            "click(name); click(header); var d = P.projectMenuShown();"
            "click(name); click(document.querySelector('.hc-project-chats-head')); var e = P.projectMenuShown();"
            "JSON.stringify([a, b, c, d, e, name.getAttribute('data-hc-project-open')]);"))
        self.assertEqual([True, False, False, False, True, ""], got)

    def test_the_overview_tab_opens_the_overview_and_closes_the_menu(self):
        # The menu carried an "Overview →" row back when the tabs were
        # inside the overview and invisible from the goals page. The tab bar
        # is always up now, so the row would be a second way to the same
        # place -- and the menu is the project switcher instead.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "P.renderViewTabs();"
            "click(slot.querySelector('.hc-project-name'));"
            "var tabs = document.querySelector('.hc-viewtabs');"
            "click(tabs.children[0]);"
            "JSON.stringify([P.projectMenuShown(), P.overviewShown(),"
            " document.documentElement.getAttribute('data-hc-overview'),"
            " document.querySelector('.hc-project-act') === null]);"))
        self.assertEqual([False, True, "", True], got)

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
            " deepText(box.querySelector('.hc-md')),"
            " texts(box, 'hc-overview-dir'), texts(box, 'hc-overview-file')]);")
        self.assertEqual([True, True, ["OVERVIEW", "GOALS"], "myrepo", "Ship the thing.",
                          "What are you trying to accomplish?", "myrepo", "Repository",
                          "git@github.com:acme/myrepo.git · feat/x",
                          # The record is this machine's, not the
                          # repository's, so it reads under the gear.
                          ["README.md", "Files"],
                          "myrepohello", ["src/"], ["app.py", "README.md"]], got)

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
        # Two panes, not three: the project's own record is this machine's
        # file rather than the repository's, and reads under the gear.
        self.assertEqual([{"readme": True, "files": False},
                          {"readme": False, "files": True},
                          ["hc-overview-pane-tab",
                           "hc-overview-pane-tab hc-overview-pane-tab-on"]], got)

    def test_a_reopened_overview_reads_only_what_it_shows(self):
        # The README and the tree are cached per directory. The record is
        # not read here at all any more -- it moved under the gear, and is
        # read when that panel opens.
        got = self.open(
            "P.closeOverview(); P.openOverview();"
            "return later(function () { return JSON.stringify("
            " ['/api/project.json', '/api/file', '/api/tree'].map(function (route) {"
            "   return calls.filter(function (c) { return String(c[0]).indexOf(route) >= 0; }).length; })); });")
        self.assertEqual([0, 1, 1], got)

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


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class PanelPlacementTests(BridgeTestCase):
    """Both panels hang from the control that opened them.

    They used to be placed from --hc-top, which is the underside of every
    fixed bar there is -- the header, the view tabs and the count pills. A
    menu opened from a name in the header appeared two rows below it.
    """

    def place(self, tail, rect=None):
        rect = rect if rect is not None else {"left": 96, "bottom": 30,
                                              "right": 250, "top": 8}
        return json.loads(self.run_js(
            PRELUDE + fetch_js()
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "var rect = %s;" % json.dumps(rect)
            + "var props = {};"
              "document.documentElement.style.setProperty ="
              "  function (k, v) { props[k] = v; };"
              "var css = function (k) { return props[k]; };"
            + tail))

    def test_the_project_menu_hangs_from_the_name(self):
        got = self.place(
            "var name = slot.querySelector('.hc-project-name');"
            "name.getBoundingClientRect = function () { return rect; };"
            "P.openProjectMenu();"
            "JSON.stringify([css('--hc-project-left'), css('--hc-project-top')]);")
        self.assertEqual(["96px", "35px"], got)

    def test_the_settings_panel_hangs_from_the_gear(self):
        got = self.place(
            "var gslot = document.createElement('span'); gslot.className = 'hc-settings';"
            "header.appendChild(gslot); P.gear.render();"
            "var gear = gslot.querySelector('.hc-gear');"
            "gear.getBoundingClientRect = function () { return rect; };"
            "document.documentElement.clientWidth = 1000;"
            "P.gear.open();"
            "JSON.stringify([css('--hc-settings-top'), css('--hc-settings-right')]);")
        self.assertEqual(["36px", "750px"], got)

    def test_a_control_that_cannot_be_measured_leaves_the_defaults(self):
        got = self.place(
            "var name = slot.querySelector('.hc-project-name');"
            "name.getBoundingClientRect = function () { return null; };"
            "P.openProjectMenu();"
            "JSON.stringify([css('--hc-project-left') || null,"
            " css('--hc-project-top') || null, P.projectMenuShown()]);")
        self.assertEqual([None, None, True], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ProjectListTests(BridgeTestCase):
    """A project is made by hand.

    The switcher used to list every directory this machine had run Claude
    Code in, which is a list of where you have been rather than of what you
    are working on -- ~/Downloads was on it, and /private/tmp.
    """

    def test_the_page_shows_what_the_server_sent_and_no_more(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js(projects={
                "ok": True, "active": "/Users/me/work/myrepo",
                "projects": [{"cwd": "/Users/me/work/myrepo",
                              "name": "myrepo", "chats": 3}]})
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "click(slot.querySelector('.hc-project-name-text'));"
            + "var menu = document.querySelector('.hc-project-menu');"
            + "later(function () { return JSON.stringify(["
            + " texts(menu, 'hc-project-row-name'),"
            + " texts(menu, 'hc-project-row-note')]); });"))
        self.assertEqual([["myrepo"], ["active"]], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ThemeTests(BridgeTestCase):
    """The theme moves in one step, not two.

    Everything the launch skin dresses reads the theme from data-hc-theme on
    the root, and the panels parented on <body> carry copies of the palette
    rather than inheriting it. Both were brought up to date by the 700ms
    sweep, so the header flipped at once and the rest of the page a beat
    later. The attribute is watched now instead of polled.
    """

    def theme(self, tail):
        return json.loads(self.run_js(
            PRELUDE + fetch_js()
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "var app = document.querySelector('.hc');"
            + "app.setAttribute('data-dark', 'false');"
            + "var vars = { '--ink': '#111', '--panel': '#fff' };"
            + "getComputedStyle = function () { return { getPropertyValue:"
            + "  function (k) { return vars[k] || ''; } }; };"
            + tail))

    def test_the_root_learns_the_theme_the_moment_the_artifact_does(self):
        got = self.theme(
            "P.repaintCopies();"
            "var light = document.documentElement.getAttribute('data-hc-theme');"
            "app.setAttribute('data-dark', 'true');"
            "P.repaintCopies();"
            "JSON.stringify([light,"
            " document.documentElement.getAttribute('data-hc-theme')]);")
        self.assertEqual(["light", "dark"], got)

    def test_the_panels_outside_the_app_are_repainted_with_it(self):
        got = self.theme(
            "P.openOverview(); P.gear.open();"
            "var over = document.querySelector('.hc-overview');"
            "var panel = P.gear.panel();"
            "over.style.props = {}; panel.style.props = {};"
            "[over, panel].forEach(function (n) {"
            "  n.style.setProperty = function (k, v) { n.style.props[k] = v; };"
            "  n.style.getPropertyValue = function (k) { return n.style.props[k] || ''; }; });"
            "vars['--ink'] = '#e6edf3'; vars['--panel'] = '#0d1117';"
            "app.setAttribute('data-dark', 'true');"
            "P.repaintCopies();"
            "JSON.stringify([over.style.props['--ink'], panel.style.props['--panel']]);")
        self.assertEqual(["#e6edf3", "#0d1117"], got)

    def test_a_browser_without_observers_still_gets_the_sweep(self):
        # watchTheme is the fast path, not the only one: mirrorRootState is
        # still called every sweep, so a page that cannot observe is a beat
        # behind rather than stuck.
        got = self.theme("JSON.stringify([P.watchTheme()]);")
        self.assertEqual([False], got)

    def test_the_watch_is_bound_to_the_root_not_to_the_element_it_reads(self):
        # This runtime re-renders nodes away rather than mutating them, so
        # an observer bound to whichever element carries data-dark today is
        # watching a detached node tomorrow -- and the copies quietly fall
        # back to the sweep, which is the beat this was meant to remove.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js()
            + "P.acceptState(%s);" % json.dumps(chat_state())
            + "var seen = [];"
            + "MutationObserver = function (fn) { this.observe ="
            + "  function (node, opts) { seen.push([String(node.tagName),"
            + "    opts.subtree === true, opts.attributeFilter]); }; };"
            + "var first = P.watchTheme();"
            + "var again = P.watchTheme();"
            + "JSON.stringify([first, again, seen]);"))
        self.assertEqual([True, False, [["html", True, ["data-dark"]]]], got)
