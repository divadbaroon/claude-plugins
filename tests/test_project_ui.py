"""The project in the workspace: a chip in the header, a menu, an overview.

The header reads "Engelbart / <project> ▾ / ● session". The name opens a
menu with the project's facts, a way into its overview, and the chats
started in the same directory, each linkable as a prompt source. The
overview draws over the document column: the name and objective, and the
repository as context -- its README, read from the server, with an Ask tab
beside it that answers a question from that one document alone. The node
harness from test_goal_ui_bridge holds that contract.
"""
import json
import time
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


# Two days before whenever the suite runs, not a fixed instant: the picker
# renders "2d ago" against the clock, and a frozen epoch means the label the
# test asserts changes by itself overnight.
TWO_DAYS_AGO = int(time.time()) - 2 * 86400

CHATS = {"ok": True,
         "linked": [{"session_id": "aaaaaaaa-1111-4111-8111-111111111111",
                     "label": "twin"},
                    {"session_id": "bbbbbbbb-2222-4222-8222-222222222222",
                     "label": "scoped", "goal_id": "g1"}],
         "available": [
             {"session_id": "aaaaaaaa-1111-4111-8111-111111111111",
              "project": "myrepo", "cwd": "/Users/me/work/myrepo",
              "mtime": TWO_DAYS_AGO, "size": 10},
             {"session_id": "bbbbbbbb-2222-4222-8222-222222222222",
              "project": "myrepo", "cwd": "/Users/me/work/myrepo",
              "mtime": TWO_DAYS_AGO, "size": 10},
             {"session_id": "cccccccc-3333-4333-8333-333333333333",
              "project": "other", "cwd": "/Users/me/work/other",
              "mtime": TWO_DAYS_AGO, "size": 10},
             {"session_id": "dddddddd-4444-4444-8444-444444444444",
              "project": "myrepo", "cwd": "", "mtime": TWO_DAYS_AGO, "size": 10}]}

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
    "  else if (u.indexOf('/api/readme') >= 0"
    "           || u.indexOf('/api/file') >= 0) body = %s;"
    "  else if (u.indexOf('/api/tree') >= 0) body = %s;"
    "  else if (u.indexOf('/api/ask') >= 0) body = %s;"
    "  else if (sent && sent.op === 'open_project') body = { ok: true, url: 'http://127.0.0.1:8870/' };"
    "  else body = { ok: true, objective: sent ? sent.objective : '',"
    "                name: sent ? sent.name : '',"
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
             projects=None, ask=None):
    projects = projects if projects is not None else PROJECTS
    readme = readme if readme is not None else {
        "ok": True, "path": "README.md", "text": "# myrepo\n\nhello", "truncated": False}
    tree = tree if tree is not None else {
        "ok": True, "root": "/Users/me/work/myrepo",
        "tree": [{"n": "src/", "kids": [{"n": "app.py"}]}, {"n": "README.md"}]}
    record = record if record is not None else {
        "ok": True, "path": "/vault/projects/abc123.json", "written": True,
        "text": RECORD, "truncated": False}
    ask = ask if ask is not None else {
        "ok": True, "asked": "what is this", "answer": "It ships **one** thing."}
    return FETCH % (json.dumps(projects), json.dumps(chats), json.dumps(record),
                    json.dumps(readme), json.dumps(tree), json.dumps(ask))


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

    def test_a_new_project_is_made_by_name_and_the_list_is_read_again(self):
        # The box used to ask for a directory, which made starting a project
        # an errand -- you had to have made one somewhere first. A name is
        # what is sent now, and the server gives it a home of its own.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  var form = menu.querySelector('.hc-project-newform');"
            "  var shut = form.getAttribute('data-hc-on');"
            "  click(menu.querySelector('.hc-project-new'));"
            "  var open = form.getAttribute('data-hc-on');"
            "  var field = menu.querySelector('.hc-project-newname');"
            "  field.value = 'Fresh start';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () { return JSON.stringify([shut, open,"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'new_project'; })"
            "      .map(function (c) { return c[1].name; }), field.value,"
            "    calls.filter(function (c) { return String(c[0]).indexOf('/api/projects') >= 0; }).length]); }); });"))
        self.assertEqual([None, "", ["Fresh start"], "", 2], got)

    def test_a_folder_is_pointed_at_rather_than_typed(self):
        # A path is the one thing in this box nobody wants to spell, so the
        # machine's own chooser is opened and what it answers with lands in
        # the field -- still text, still editable, and nothing is made of it
        # until Add.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  click(menu.querySelector('.hc-project-new'));"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: true, cwd: '/Users/me/work/picked',"
            "        name: 'picked' }); } }); };"
            "  click(menu.querySelector('.hc-project-browse'));"
            "  return later(function () {"
            "    var field = menu.querySelector('.hc-project-newname');"
            "    var seat = menu.querySelector('.hc-project-parent');"
            "    return JSON.stringify(["
            "      calls.filter(function (c) { return c[1] && c[1].op === 'pick_directory'; })"
            "        .map(function (c) { return c[1].start; }),"
            "      field.value, seat.getAttribute('data-hc-cwd'),"
            "      deepText(seat)]); }); });"))
        # The dialog opens where the reader already is, and what comes back
        # is where the project will be *made* -- the name box is left for
        # the name. A path typed there still adopts that directory; that is
        # a different thing, and this button no longer does it.
        self.assertEqual([["/Users/me/work/myrepo"], "",
                          "/Users/me/work/picked",
                          "in /Users/me/work/picked"], got)

    def test_closing_the_dialog_leaves_what_was_typed_alone(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  click(menu.querySelector('.hc-project-new'));"
            "  menu.querySelector('.hc-project-newname').value = 'Fresh start';"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: true, cancelled: true }); } }); };"
            "  click(menu.querySelector('.hc-project-browse'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    return JSON.stringify([menu.querySelector('.hc-project-newname').value,"
            "      say.textContent, say.getAttribute('data-hc-bad')]); }); });"))
        self.assertEqual(["Fresh start", "", None], got)

    def test_a_machine_with_no_chooser_says_so_and_the_box_still_works(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: false,"
            "        error: 'no folder chooser on this machine' }); } }); };"
            "  click(menu.querySelector('.hc-project-browse'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    return JSON.stringify([say.textContent, say.getAttribute('data-hc-bad')]); }); });"))
        self.assertEqual(["no folder chooser on this machine", ""], got)

    def test_a_server_older_than_this_page_asks_to_be_restarted(self):
        # This script is re-read from disk on every load and the process
        # answering it is not, so a workspace left open across an edit
        # reaches a server that has never heard of this control. "unknown
        # operation: pick_directory" is true and useless; the restart is the
        # whole fix, so that is what the line says.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: false,"
            "        error: 'unknown operation: pick_directory' }); } }); };"
            "  click(menu.querySelector('.hc-project-browse'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    return JSON.stringify([say.textContent, say.getAttribute('data-hc-bad')]); }); });"))
        self.assertEqual(
            ["this workspace's server is older than this page — "
             "restart it and try again", ""], got)

    def test_a_server_older_than_the_wording_is_read_the_same_way(self):
        # Older servers than that one had no word for the operation at all --
        # every op they did not know came back "unknown or invalid op". A
        # workspace open across a long enough gap is exactly the one that most
        # needs the restart, so its answer must read as staleness too.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: false,"
            "        error: 'unknown or invalid op' }); } }); };"
            "  click(menu.querySelector('.hc-project-browse'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    var A = window.__hcPromptUI.alerts; var log = A.log();"
            "    return JSON.stringify([say.textContent, say.getAttribute('data-hc-bad'),"
            "      log.length, log[0] && log[0].kind, A.unread()]); }); });"))
        # And the card is raised on the way past -- where a finished TODO's
        # is: an entry behind the bell, unread, so the mark outlives the menu
        # and the restart does not stop being the fix. (The banner itself has
        # come and gone by the time this looks: later() runs the clock out.)
        self.assertEqual(
            ["this workspace's server is older than this page — "
             "restart it and try again", "", 1, "server_stale", 1],
            got)

    def test_an_empty_box_asks_for_a_name_rather_than_a_directory(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  var field = menu.querySelector('.hc-project-newname');"
            "  field.value = '   ';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    return JSON.stringify([say.textContent, field.getAttribute('placeholder'),"
            "      calls.filter(function (c) { return c[1] && c[1].op === 'new_project'; }).length]); }); });"))
        self.assertEqual(["type a name first", "Name your project", 0], got)

    def test_a_directory_that_is_not_there_is_reported_not_added(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: false, error: 'no such directory: /nope' }); } }); };"
            "  menu.querySelector('.hc-project-newname').value = '/nope';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    return JSON.stringify([say.textContent, say.getAttribute('data-hc-bad')]); }); });"))
        self.assertEqual(["no such directory: /nope", ""], got)

    def test_a_repository_is_cloned_into_the_project_being_made(self):
        # A project that already exists somewhere else is given as its URL,
        # beside the name it should be called here. Both travel in the one
        # operation; the clone is the server's job.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  click(menu.querySelector('.hc-project-new'));"
            "  fetch = function (url, opts) { var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "    calls.push([String(url), sent]);"
            "    var body = { ok: true, projects: [] };"
            "    if (sent && sent.op === 'new_project') body = { ok: true, name: 'The widget',"
            "      cwd: '/vault/workspaces/the-widget', cloned: sent.repo, chats: 0 };"
            "    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } }); };"
            "  menu.querySelector('.hc-project-newname').value = 'The widget';"
            "  var repo = menu.querySelector('.hc-project-newrepo');"
            "  repo.value = 'git@github.com:me/widget.git';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () { return JSON.stringify(["
            "    repo.getAttribute('placeholder'),"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'new_project'; })"
            "      .map(function (c) { return [c[1].name, c[1].repo]; }),"
            "    menu.querySelector('.hc-project-newname').value, repo.value,"
            "    menu.querySelector('.hc-project-say').textContent]); }); });"))
        self.assertEqual(["Git repo to clone (optional)",
                          [["The widget", "git@github.com:me/widget.git"]],
                          "", "", "cloned The widget"], got)

    def test_a_name_somebody_used_is_handed_back_to_be_typed_over(self):
        # The fix for a taken name is another name, so what was typed stays
        # in the box with the cursor back in it rather than being cleared.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  var field = menu.querySelector('.hc-project-newname');"
            "  var back = []; field.focus = function () { back.push('focus'); };"
            "  field.select = function () { back.push('select'); };"
            "  fetch = function (url, opts) { calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
            "    return Promise.resolve({ ok: true, json: function () {"
            "      return Promise.resolve({ ok: false, duplicate: true, name: 'Fresh start',"
            "        error: 'a project is already called \"Fresh start\" — name this one something else' }); } }); };"
            "  field.value = 'Fresh start';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () { var say = menu.querySelector('.hc-project-say');"
            "    return JSON.stringify([say.textContent, say.getAttribute('data-hc-bad'),"
            "      field.value, back,"
            "      menu.querySelector('.hc-project-setup') === null]); }); });"))
        self.assertEqual(
            ['a project is already called "Fresh start" — name this one '
             'something else', "", "Fresh start", ["focus", "select"], True],
            got)

    def test_a_project_with_nothing_in_it_is_asked_what_it_is_for(self):
        # It has no chat behind it and so no workspace of its own to open:
        # the two questions are asked here, and written to its record.
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  click(menu.querySelector('.hc-project-new'));"
            "  fetch = function (url, opts) { var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "    calls.push([String(url), sent]);"
            "    var body = { ok: true, projects: [] };"
            "    if (sent && sent.op === 'new_project') body = { ok: true, name: 'Fresh start',"
            "      cwd: '/vault/workspaces/fresh-start', chats: 0, setup: true };"
            "    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } }); };"
            "  menu.querySelector('.hc-project-newname').value = 'Fresh start';"
            "  click(menu.querySelector('.hc-project-addbtn'));"
            "  return later(function () {"
            "    var setup = menu.querySelector('.hc-project-setup');"
            "    var head = setup.querySelector('.hc-project-setup-head').textContent;"
            "    var form = menu.querySelector('.hc-project-newform');"
            "    setup.querySelector('[data-hc-project-objective]').value = 'Ship the redesign.';"
            "    setup.querySelector('[data-hc-project-about]').value = 'It replaces the rail.';"
            "    click(setup.querySelector('.hc-project-addbtn'));"
            "    return later(function () { return JSON.stringify([head,"
            "      form.getAttribute('data-hc-on'),"
            "      calls.filter(function (c) { return c[1] && c[1].op === 'project_setup'; })"
            "        .map(function (c) { return [c[1].cwd, c[1].objective, c[1].description]; }),"
            "      menu.querySelector('.hc-project-setup') === null,"
            "      menu.querySelector('.hc-project-say').textContent]); }); }); });"))
        self.assertEqual(["Set up Fresh start", None,
                          [["/vault/workspaces/fresh-start",
                            "Ship the redesign.", "It replaces the rail."]],
                          True, "saved"], got)

    def test_a_project_nobody_has_worked_in_is_set_up_from_its_row(self):
        # It has no workspace to switch to, so its row asks the questions
        # instead of opening a page that would say "no chat yet".
        rows = {"ok": True, "active": "/Users/me/work/myrepo", "projects": [
            {"cwd": "/Users/me/work/myrepo", "name": "myrepo", "chats": 3},
            {"cwd": "/vault/workspaces/fresh-start", "name": "Fresh start",
             "chats": 0, "objective": ""},
            {"cwd": "/vault/workspaces/answered", "name": "Answered",
             "chats": 0, "objective": "Ship it."}]}
        got = json.loads(self.run_js(
            PRELUDE + fetch_js(projects=rows) + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  var rows = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "    if (c.getAttribute('data-hc-goto') !== null) rows.push(c); walk(c); }); })(menu);"
            "  click(rows[1]);"
            "  var setup = menu.querySelector('.hc-project-setup');"
            "  return JSON.stringify([texts(menu, 'hc-project-row-note'),"
            "    setup.getAttribute('data-hc-project-setup'),"
            "    setup.querySelector('.hc-project-setup-head').textContent,"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'open_project'; }).length]); });"))
        # A project that has answered already is not asked again; one that
        # has been worked in is switched to as before.
        self.assertEqual([["active", "set up", ""],
                          "/vault/workspaces/fresh-start",
                          "Set up Fresh start", 0], got)

    def test_the_questions_can_be_left_for_later_and_nothing_is_sent(self):
        got = json.loads(self.run_js(
            PRELUDE + fetch_js() + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state()) +
            "click(slot.querySelector('.hc-project-name-text'));"
            "later(function () {"
            "  var menu = document.querySelector('.hc-project-menu');"
            "  P.askProjectSetup({ cwd: '/vault/workspaces/fresh-start', name: 'Fresh start' });"
            "  var setup = menu.querySelector('.hc-project-setup');"
            "  click(setup.querySelector('.hc-project-setup-later'));"
            "  return later(function () { return JSON.stringify(["
            "    menu.querySelector('.hc-project-setup') === null,"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'project_setup'; }).length,"
            "    menu.querySelector('.hc-project-say').textContent]); }); });"))
        self.assertEqual([True, 0, ""], got)

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
            {"session_id": "aaaaaaaa-1111-4111-8111-111111111111",
             "mtime": TWO_DAYS_AGO, "linked": True},
            {"session_id": "bbbbbbbb-2222-4222-8222-222222222222",
             "mtime": TWO_DAYS_AGO, "linked": False},
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
            " texts(box, 'hc-overview-tab'), box.querySelector('.hc-overview-name').value,"
            " box.querySelector('.hc-overview-objective').value,"
            " box.querySelector('.hc-overview-objective').getAttribute('placeholder'),"
            " box.querySelector('.hc-overview-src-name').textContent,"
            " box.querySelector('.hc-overview-src-kind').textContent,"
            " deepText(box.querySelector('.hc-overview-repo-meta'))]);")
        self.assertEqual([True, True, ["OVERVIEW", "GOALS"], "myrepo", "Ship the thing.",
                          "What are you trying to accomplish?", "myrepo", "Repository",
                          "git@github.com:acme/myrepo.git · feat/x"], got)

    def test_the_repository_pane_reads_its_readme_and_offers_ask(self):
        # The README is back -- it is the one piece of a project's context
        # nobody attaches by hand. The file tree is not: a project page is
        # not a file browser, and it stood in front of the sources somebody
        # actually attached. One route reads the front page, so the browser
        # is not guessing at four spellings a round trip at a time.
        got = self.open(
            "return JSON.stringify([texts(box, 'hc-overview-pane-tab'),"
            " deepText(box.querySelector('[data-hc-pane-body=\"repo\"]')),"
            " box.querySelector('.hc-overview-tree') === null,"
            " ['/api/readme', '/api/file', '/api/tree'].map(function (route) {"
            "   return calls.filter(function (c) {"
            "     return String(c[0]).indexOf(route) >= 0; }).length; })]);")
        self.assertEqual([["Readme", "Ask"], "myrepohello", True, [1, 0, 0]],
                         got)

    def test_a_project_without_a_readme_says_so(self):
        got = self.open(
            "return JSON.stringify(["
            " deepText(box.querySelector('[data-hc-pane-body=\"repo\"]')),"
            " box.querySelector('.hc-md') === null]);",
            readme={"ok": False, "error": "this project has no README"})
        self.assertEqual(["this project has no README", True], got)

    def test_the_ask_tab_sends_the_question_and_renders_the_answer(self):
        # Asked in front of one document and answered from it: the pane the
        # question was typed in is the id the server is given, and the
        # answer comes back as markdown, not as a transcript line.
        got = self.open(
            "P.showPane('repo', 'ask');"
            "var field = box.querySelector('[data-hc-ask-field=\"repo\"]');"
            "field.value = '  what is this  ';"
            "P.runAsk('repo');"
            "return later(function () { return JSON.stringify(["
            "  calls.filter(function (c) { return String(c[0]).indexOf('/api/ask') >= 0; })"
            "    .map(function (c) { return c[1]; }),"
            "  deepText(box.querySelector('[data-hc-ask-out=\"repo\"]')),"
            "  box.querySelector('[data-hc-ask-say=\"repo\"]').textContent,"
            "  calls.filter(function (c) { return String(c[0]).indexOf('/api/readme') >= 0; }).length]); });")
        self.assertEqual([[{"id": "", "question": "what is this"}],
                          "It ships one thing.", "", 1], got)

    def test_a_question_that_failed_says_why_and_draws_no_answer(self):
        got = self.open(
            "P.showPane('repo', 'ask');"
            "box.querySelector('[data-hc-ask-field=\"repo\"]').value = 'why';"
            "P.runAsk('repo');"
            "return later(function () { return JSON.stringify(["
            "  box.querySelector('[data-hc-ask-say=\"repo\"]').textContent,"
            "  box.querySelector('[data-hc-ask-say=\"repo\"]').getAttribute('data-hc-bad'),"
            "  deepText(box.querySelector('[data-hc-ask-out=\"repo\"]'))]); });",
            ask={"ok": False, "error": "the claude CLI is not on PATH"})
        self.assertEqual(["the claude CLI is not on PATH", "", ""], got)

    def test_an_empty_question_is_not_sent(self):
        got = self.open(
            "P.showPane('repo', 'ask');"
            "box.querySelector('[data-hc-ask-field=\"repo\"]').value = '   ';"
            "var sent = P.runAsk('repo');"
            "return JSON.stringify([sent,"
            " box.querySelector('[data-hc-ask-say=\"repo\"]').textContent,"
            " calls.filter(function (c) { return String(c[0]).indexOf('/api/ask') >= 0; }).length]);")
        self.assertEqual([False, "ask something first", 0], got)

    def test_the_tab_moves_and_the_readme_is_not_read_twice(self):
        # Going to Ask and back redraws the front page from what was read
        # the first time: a second fetch would blink the pane for nothing.
        got = self.open(
            "P.showPane('repo', 'ask');"
            "var onAsk = box.querySelector('[data-hc-ask-field=\"repo\"]') !== null;"
            "P.showPane('repo', 'readme');"
            "return later(function () { return JSON.stringify([onAsk,"
            "  box.querySelector('[data-hc-ask-field=\"repo\"]') === null,"
            "  deepText(box.querySelector('[data-hc-pane-body=\"repo\"]')),"
            "  texts(box, 'hc-overview-pane-tab-on'),"
            "  calls.filter(function (c) { return String(c[0]).indexOf('/api/readme') >= 0; }).length]); });")
        self.assertEqual([True, True, "myrepohello", ["Readme"], 1], got)

    def test_a_reopened_overview_reads_nothing_it_does_not_show(self):
        # The record is not read here at all -- it moved under the gear, and
        # is read when that panel opens.
        got = self.open(
            "P.closeOverview(); P.openOverview();"
            "return later(function () { return JSON.stringify("
            " ['/api/project.json', '/api/file', '/api/tree'].map(function (route) {"
            "   return calls.filter(function (c) { return String(c[0]).indexOf(route) >= 0; }).length; })); });")
        self.assertEqual([0, 0, 0], got)

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

    def test_the_card_carries_no_menu_button_and_every_line_is_a_field(self):
        # The ⋯ opened the project switcher, which the header chip already
        # is; and a name that could only be changed from behind that menu
        # was a label pretending to be read-only. All three lines are typed
        # where they are read.
        got = self.open(
            "var head = box.querySelector('.hc-overview-head');"
            "return JSON.stringify([box.querySelector('[data-hc-overview-more]') === null,"
            " (head.children || []).length,"
            " box.querySelector('[data-hc-name]').tagName,"
            " box.querySelector('[data-hc-about]').tagName,"
            " box.querySelector('.hc-overview-objective').tagName]);")
        self.assertEqual([True, 1, "input", "textarea", "textarea"], got)

    def test_the_name_is_renamed_where_it_is_read(self):
        got = self.open(
            "var field = box.querySelector('[data-hc-name]');"
            "field.value = 'The Workspace';"
            "key('Enter', field);"
            "return later(function () { return JSON.stringify(["
            "  calls.filter(function (c) { return c[1] && c[1].op === 'set_project_meta'; })"
            "    .map(function (c) { return c[1]; }),"
            "  field.value,"
            "  deepText(document.querySelector('.hc-project-name'))]); });")
        self.assertEqual([[{"op": "set_project_meta", "name": "The Workspace"}],
                          "The Workspace", "The Workspace▾"], got)

    def test_a_name_that_did_not_change_is_not_posted(self):
        got = self.open(
            "var field = box.querySelector('[data-hc-name]');"
            "field.value = '  myrepo  ';"
            "P.saveName(field);"
            "return JSON.stringify(calls.filter(function (c) {"
            "  return c[1] && c[1].op === 'set_project_meta'; }).length);")
        self.assertEqual(0, got)

    def test_a_name_being_typed_is_not_overwritten_by_the_server(self):
        got = self.open(
            "var field = box.querySelector('[data-hc-name]');"
            "fire('focusin', field); field.value = 'half-typed';"
            "P.renderOverview();"
            "var mid = field.value;"
            "fire('focusout', field);"
            "return later(function () { return JSON.stringify([mid, field.value]); });")
        self.assertEqual(["half-typed", "half-typed"], got)

    def test_the_overview_redraws_for_a_different_project(self):
        other = chat_state()
        other["project"] = {"cwd": "/Users/me/work/other", "name": "other", "branch": "",
                            "remote": "", "objective": ""}
        got = self.open(
            "var first = box;"
            "P.acceptState(%s); var drew = P.renderOverview();" % json.dumps(other) +
            "var box2 = document.querySelector('.hc-overview');"
            "return JSON.stringify([drew, box2 !== first, box2.querySelector('.hc-overview-name').value,"
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


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class NewProjectParentTests(BridgeTestCase):
    """A name, and where the folder for it goes."""

    def form(self, tail):
        return json.loads(self.run_js(
            PRELUDE + fetch_js()
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "click(slot.querySelector('.hc-project-name-text'));"
            + "later(function () {"
            + "  var menu = document.querySelector('.hc-project-menu');"
            + "  click(menu.querySelector('.hc-project-new'));"
            + "  var name = menu.querySelector('.hc-project-newname');"
            + "  var seat = menu.querySelector('.hc-project-parent');"
            + tail + " });"))

    def test_the_chosen_parent_is_sent_with_the_name(self):
        got = self.form(
            "seat.setAttribute('data-hc-cwd', '/Users/me/Projects');"
            "name.value = 'Engelbart';"
            "click(menu.querySelector('[data-hc-project-add]'));"
            "return later(function () { return JSON.stringify("
            "  calls.filter(function (c) { return c[1] && c[1].op === 'new_project'; })"
            "    .map(function (c) { return [c[1].name, c[1].parent]; })); });")
        self.assertEqual([["Engelbart", "/Users/me/Projects"]], got)

    def test_with_no_parent_chosen_the_server_decides_where(self):
        got = self.form(
            "name.value = 'Engelbart';"
            "click(menu.querySelector('[data-hc-project-add]'));"
            "return later(function () { return JSON.stringify("
            "  calls.filter(function (c) { return c[1] && c[1].op === 'new_project'; })"
            "    .map(function (c) { return [c[1].name, c[1].parent]; })); });")
        self.assertEqual([["Engelbart", ""]], got)

    def test_the_parent_is_forgotten_once_the_project_is_made(self):
        # Otherwise the next project silently lands beside the last one.
        got = self.form(
            "seat.setAttribute('data-hc-cwd', '/Users/me/Projects');"
            "seat.textContent = 'in /Users/me/Projects';"
            "name.value = 'Engelbart';"
            "click(menu.querySelector('[data-hc-project-add]'));"
            "return later(function () { return JSON.stringify("
            "  [seat.getAttribute('data-hc-cwd'), deepText(seat), name.value]); });")
        self.assertEqual([None, "", ""], got)
