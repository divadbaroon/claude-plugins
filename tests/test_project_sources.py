"""What a project has context in, beyond the directory it lives in.

The repository is always there and is never attached -- it is where the
chat was started. Everything after it the reader put there: another
repository, a document, or a conversation that happened. The list carries
all of them; the right-hand side reads whichever is selected, each kind the
way that kind can be read.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE
from test_project_ui import PRELUDE, chat_state, PROJECTS, CHATS

SOURCES = [
    {"id": "s1", "type": "chat", "label": "aaaaaaaa-1111-4111-8111-111111111111"},
    {"id": "s2", "type": "doc", "label": "docs/architecture.md"},
    {"id": "s3", "type": "github", "label": "acme/other"},
]


def state_with(sources=None):
    state = chat_state()
    state["project"]["sources"] = sources if sources is not None else SOURCES
    return state


def fetch_js(source=None, saved=None, chats=CHATS):
    source = source if source is not None else {
        "ok": True, "kind": "doc", "path": "docs/architecture.md",
        "text": "# Architecture notes\n\nEvent flow: hooks capture.",
        "truncated": False}
    saved = saved if saved is not None else {"ok": True, "sources": SOURCES}
    return (
        "fetch = function (url, opts) {"
        "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
        "  calls.push([String(url), sent]);"
        "  var u = String(url); var body;"
        "  if (u.indexOf('/api/source') >= 0) body = %s;"
        "  else if (u.indexOf('/api/chats') >= 0) body = %s;"
        "  else if (u.indexOf('/api/projects') >= 0) body = %s;"
        "  else if (sent && sent.op === 'set_project_meta') body = %s;"
        "  else body = { ok: true };"
        "  return Promise.resolve({ ok: true, json: function () {"
        "    return Promise.resolve(body); } });"
        "};" % (json.dumps(source), json.dumps(chats), json.dumps(PROJECTS),
                json.dumps(saved)))


class Overview:
    """The overview open, with the project's sources in it."""

    def open(self, tail, state=None, **fetch):
        return json.loads(self.run_js(
            PRELUDE + fetch_js(**fetch)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(state or state_with())
            + "P.openOverview();"
            + "var box = document.querySelector('.hc-overview');"
            + "var srcs = function () { var out = []; (function walk(n) {"
            + "  (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-source') !== null) out.push(c);"
            + "    walk(c); }); })(box); return out; };"
            + "later(function () { " + tail + " });"))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SourceListTests(Overview, BridgeTestCase):

    def test_the_list_names_every_source_by_its_kind(self):
        got = self.open(
            "return JSON.stringify([texts(box, 'hc-overview-src-glyph'),"
            " texts(box, 'hc-overview-src-name'),"
            " texts(box, 'hc-overview-src-kind'),"
            " srcs().map(function (s) { return s.getAttribute('data-hc-on'); })]);")
        self.assertEqual([["R", "C", "D", "R"],
                          ["myrepo", "Claude session: aaaaaaaa",
                           "architecture.md", "acme/other"],
                          ["Repository", "Conversation", "Document",
                           "Repository"],
                          # the repository is what the page opens on
                          ["", None, None, None]], got)

    def test_a_project_with_nothing_attached_shows_only_its_repository(self):
        got = self.open(
            "return JSON.stringify([texts(box, 'hc-overview-src-name'),"
            " box.querySelector('.hc-overview-addsrc').textContent]);",
            state=state_with([]))
        self.assertEqual([["myrepo"], "+ Add context"], got)

    def test_selecting_a_document_reads_it_and_hides_the_repository_pane(self):
        got = self.open(
            "click(srcs()[2]);"
            "return later(function () {"
            "  return JSON.stringify(["
            "    box.querySelector('.hc-overview-reading').getAttribute('data-hc-on'),"
            "    box.querySelector('[data-hc-repo-pane]').getAttribute('data-hc-off'),"
            "    calls.filter(function (c) { return c[0].indexOf('/api/source') >= 0; })"
            "      .map(function (c) { return c[0]; }),"
            "    deepText(box.querySelector('.hc-overview-reading')"
            "      .querySelector('.hc-md'))]); });")
        self.assertEqual(["", "", ["/api/source?id=s2"],
                          "Architecture notesEvent flow: hooks capture."], got)

    def test_going_back_to_the_repository_brings_its_panes_back(self):
        got = self.open(
            "click(srcs()[2]);"
            "return later(function () { click(srcs()[0]);"
            "  return JSON.stringify(["
            "    box.querySelector('.hc-overview-reading').getAttribute('data-hc-on'),"
            "    box.querySelector('[data-hc-repo-pane]').getAttribute('data-hc-off'),"
            "    srcs()[0].getAttribute('data-hc-on')]); });")
        self.assertEqual([None, None, ""], got)

    def test_a_conversation_reads_as_its_turns(self):
        got = self.open(
            "click(srcs()[1]);"
            "return later(function () {"
            "  return JSON.stringify([texts(box, 'hc-overview-turn-who'),"
            "    texts(box, 'hc-overview-turn-text'),"
            "    texts(box, 'hc-overview-more')]); });",
            source={"ok": True, "kind": "chat", "total": 9, "turns": [
                {"role": "user", "text": "make it a desktop app"},
                {"role": "assistant", "text": "on it"}]})
        self.assertEqual([["user", "assistant"],
                          ["make it a desktop app", "on it"],
                          ["… 7 earlier turns not shown"]], got)

    def test_a_repository_is_a_link_because_nothing_is_fetched(self):
        got = self.open(
            "click(srcs()[3]);"
            "return later(function () { var body = box.querySelector('[data-hc-src-body]');"
            "  var a = body.querySelector('hc-md-link');"
            "  return JSON.stringify([a === null ? null : a.getAttribute('href'),"
            "    texts(box, 'hc-overview-more')]); });",
            source={"ok": True, "kind": "github", "label": "https://github.com/acme/other"})
        self.assertEqual(["https://github.com/acme/other",
                          ["A repository, not a file: opened rather than"
                           " read here."]], got)

    def test_a_file_outside_the_project_says_so_rather_than_reading_it(self):
        got = self.open(
            "click(srcs()[2]);"
            "return later(function () { return JSON.stringify("
            "  texts(box.querySelector('.hc-overview-reading'),"
            "        'hc-overview-empty')); });",
            source={"ok": False, "kind": "doc",
                    "error": "that file is outside the project"})
        self.assertEqual(["that file is outside the project"], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class AddSourceTests(Overview, BridgeTestCase):

    def test_the_form_offers_the_three_kinds_and_opens_shut(self):
        got = self.open(
            "var form = box.querySelector('[data-hc-addform]');"
            "var shut = form.getAttribute('data-hc-on');"
            "click(box.querySelector('.hc-overview-addsrc'));"
            "return JSON.stringify([shut, form.getAttribute('data-hc-on'),"
            " texts(box, 'hc-overview-kind'),"
            " form.getAttribute('data-hc-kind-on'),"
            " form.querySelector('[data-hc-src-label]').getAttribute('placeholder')]);")
        self.assertEqual([None, "", ["Repository", "Document", "Conversation"],
                          "github", "owner/repo, or a link"], got)

    def test_each_kind_asks_for_what_that_kind_needs(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var form = box.querySelector('[data-hc-addform]');"
            "var field = form.querySelector('[data-hc-src-label]');"
            "var kinds = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "  if (c.getAttribute('data-hc-kind') !== null) kinds.push(c); walk(c); }); })(form);"
            "var seen = [];"
            "kinds.forEach(function (k) { click(k);"
            "  seen.push([form.getAttribute('data-hc-kind-on'),"
            "             field.getAttribute('placeholder')]); });"
            "return JSON.stringify(seen);")
        self.assertEqual([["github", "owner/repo, or a link"],
                          ["doc", "a file in this project, or a link"],
                          ["chat", "narrow the list \u2014 an id, a project"]], got)

    def test_a_typed_document_is_attached_with_the_whole_list_posted_back(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var form = box.querySelector('[data-hc-addform]');"
            "click(form.querySelector('[data-hc-kind=\"doc\"]'));"
            "form.querySelector('[data-hc-src-label]').value = 'docs/plan.md';"
            "click(form.querySelector('[data-hc-src-add]'));"
            "return later(function () { return JSON.stringify("
            "  calls.filter(function (c) { return c[1] && c[1].op === 'set_project_meta'; })"
            "    .map(function (c) { return c[1].sources.map(function (s) {"
            "      return [s.type, s.label]; }); })); });",
            state=state_with([SOURCES[1]]))
        self.assertEqual([[["doc", "docs/architecture.md"],
                           ["doc", "docs/plan.md"]]], got)

    def test_a_conversation_is_picked_from_every_chat_this_one_first(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var form = box.querySelector('[data-hc-addform]');"
            "click(form.querySelector('[data-hc-kind=\"chat\"]'));"
            "return later(function () {"
            "  var picks = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "    if (c.getAttribute('data-hc-pick-chat') !== null) picks.push(c); walk(c); }); })(form);"
            "  click(picks[0]);"
            "  return later(function () { return JSON.stringify(["
            "    picks.map(function (p) { return deepText(p); }),"
            "    calls.filter(function (c) { return c[1] && c[1].op === 'set_project_meta'; })"
            "      .map(function (c) { return c[1].sources[c[1].sources.length - 1]; })"
            "      .map(function (s) { return [s.type, s.label]; })]); }); });",
            state=state_with([]))
        # Every chat, not only this project's: the argument worth attaching
        # often happened in another repository. This project's come first,
        # and each row says which project it was.
        # dddddddd names the project but has no directory, so it cannot be
        # claimed by this one and sorts with the rest.
        self.assertEqual([["aaaaaaaamyrepo2d ago", "bbbbbbbbmyrepo2d ago",
                           "ccccccccother2d ago", "ddddddddmyrepo2d ago"],
                          [["chat", "aaaaaaaa-1111-4111-8111-111111111111"]]], got)

    def test_attaching_the_same_thing_twice_is_refused(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var form = box.querySelector('[data-hc-addform]');"
            "click(form.querySelector('[data-hc-kind=\"doc\"]'));"
            "form.querySelector('[data-hc-src-label]').value = 'docs/architecture.md';"
            "click(form.querySelector('[data-hc-src-add]'));"
            "var say = form.querySelector('[data-hc-src-say]');"
            "return JSON.stringify([say.textContent, say.getAttribute('data-hc-bad'),"
            " calls.filter(function (c) { return c[1] && c[1].op === 'set_project_meta'; }).length]);")
        self.assertEqual(["already attached", "", 0], got)

    def test_a_source_can_be_taken_off_again(self):
        got = self.open(
            "var x = srcs()[2].querySelector('[data-hc-drop-source]');"
            "click(x);"
            "return later(function () { return JSON.stringify("
            "  calls.filter(function (c) { return c[1] && c[1].op === 'set_project_meta'; })"
            "    .map(function (c) { return c[1].sources.map(function (s) { return s.id; }); })); });")
        self.assertEqual([["s1", "s3"]], got)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ConversationPickerTests(Overview, BridgeTestCase):
    """Every chat the workspace can see, the way the prompt picker lists them.

    It used to offer only this project's, on the reasoning that a project's
    context is its own -- but the reason to attach a conversation is often
    that it happened somewhere else. This project's come first and are
    marked; each row says which project, and when.
    """

    def picker(self, tail, state=None, **fetch):
        return self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var form = box.querySelector('[data-hc-addform]');"
            "click(form.querySelector('[data-hc-kind=\"chat\"]'));"
            "var picks = function () { var out = [];"
            "  (function walk(n) { (n.children || []).forEach(function (c) {"
            "    if (String(c.className) === 'hc-overview-srcchat') out.push(c);"
            "    walk(c); }); })(form); return out; };"
            "var field = form.querySelector('[data-hc-src-label]');"
            "return later(function () { " + tail + " });",
            state=state, **fetch)

    def test_each_row_says_which_project_and_when(self):
        got = self.picker(
            "return JSON.stringify([picks().map(function (p) {"
            "  return [deepText(p.querySelector('hc-overview-srcchat-id')),"
            "          deepText(p.querySelector('hc-overview-srcchat-where')),"
            "          deepText(p.querySelector('hc-overview-srcchat-when'))]; }),"
            " picks().map(function (p) { return p.querySelector("
            "   'hc-overview-srcchat-where').getAttribute('data-hc-here'); })]);",
            state=state_with([]))
        self.assertEqual([[["aaaaaaaa", "myrepo", "2d ago"],
                           ["bbbbbbbb", "myrepo", "2d ago"],
                           ["cccccccc", "other", "2d ago"],
                           ["dddddddd", "myrepo", "2d ago"]],
                          ["", "", None, None]], got)

    def test_typing_narrows_the_list_instead_of_naming_a_chat(self):
        got = self.picker(
            "field.value = 'other'; fire('input', field);"
            "var narrowed = picks().map(function (p) { return deepText("
            "  p.querySelector('hc-overview-srcchat-id')); });"
            "field.value = 'bbbb'; fire('input', field);"
            "var again = picks().map(function (p) { return deepText("
            "  p.querySelector('hc-overview-srcchat-id')); });"
            "key('Enter', field);"
            "return JSON.stringify([narrowed, again,"
            " calls.filter(function (c) { return c[1] && c[1].op === 'set_project_meta'; }).length]);",
            state=state_with([]))
        self.assertEqual([["cccccccc"], ["bbbbbbbb"], 0], got)

    def test_a_filter_that_matches_nothing_says_so(self):
        got = self.picker(
            "field.value = 'zzzz'; fire('input', field);"
            "return JSON.stringify([picks().length,"
            " texts(box.querySelector('[data-hc-src-chats]'), 'hc-overview-srcsay')]);",
            state=state_with([]))
        self.assertEqual([0, ["no chat matches that"]], got)

    def test_one_already_attached_is_shown_but_cannot_be_added_twice(self):
        got = self.picker(
            "var had = picks()[0];"
            "click(had);"
            "return JSON.stringify(["
            " had.getAttribute('data-hc-had'), had.getAttribute('data-hc-pick-chat'),"
            " deepText(had.querySelector('hc-overview-srcchat-when')),"
            " calls.filter(function (c) { return c[1] && c[1].op === 'set_project_meta'; }).length]);")
        self.assertEqual(["", None, "attached", 0], got)

    def test_the_add_button_stands_down_where_the_row_is_the_choice(self):
        got = self.picker(
            "var add = form.querySelector('[data-hc-src-add]');"
            "var off = add.getAttribute('data-hc-off');"
            "click(form.querySelector('[data-hc-kind=\"doc\"]'));"
            "return JSON.stringify([off, add.getAttribute('data-hc-off'),"
            " box.querySelector('[data-hc-src-chats]').getAttribute('data-hc-on')]);",
            state=state_with([]))
        self.assertEqual(["", None, None], got)

    def test_a_workspace_with_no_other_chats_says_so(self):
        got = self.picker(
            "return JSON.stringify(texts("
            "  box.querySelector('[data-hc-src-chats]'), 'hc-overview-srcsay'));",
            state=state_with([]), chats={"ok": True, "linked": [], "available": []})
        self.assertEqual(["no other chats to attach"], got)
