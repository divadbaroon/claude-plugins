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
            "return JSON.stringify([srcs().map(function (s) {"
            " return deepText(s.querySelector('.hc-overview-chip-name')); }),"
            " srcs().map(function (s) { return s.getAttribute('title'); }),"
            " srcs().map(function (s) { return s.getAttribute('data-hc-on'); })]);")
        self.assertEqual([["Repository", "Document", "Paper",
                           "Claude session: aaaaaaaa", "architecture.md",
                           "acme/other"],
                          ["myrepo", "Working document", "Reading",
                           "Conversation · aaaaaaaa-1111-4111-8111-111111111111",
                           "Document · docs/architecture.md",
                           "Repository · acme/other"],
                          # the repository is what the page opens on
                          ["", None, None, None, None, None]], got)

    def test_a_project_with_nothing_attached_shows_only_its_repository(self):
        got = self.open(
            "return JSON.stringify([srcs().map(function (s) {"
            " return deepText(s.querySelector('.hc-overview-chip-name')); }),"
            " box.querySelector('.hc-overview-addsrc').getAttribute('title')]);",
            state=state_with([]))
        self.assertEqual([["Repository", "Document", "Paper"], "Add context"], got)

    def test_selecting_a_document_reads_it_and_hides_the_repository_pane(self):
        got = self.open(
            "click(srcs()[4]);"
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
            "click(srcs()[4]);"
            "return later(function () { click(srcs()[0]);"
            "  return JSON.stringify(["
            "    box.querySelector('.hc-overview-reading').getAttribute('data-hc-on'),"
            "    box.querySelector('[data-hc-repo-pane]').getAttribute('data-hc-off'),"
            "    srcs()[0].getAttribute('data-hc-on')]); });")
        self.assertEqual([None, None, ""], got)

    def test_a_conversation_reads_as_its_turns(self):
        got = self.open(
            "click(srcs()[3]);"
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
            "click(srcs()[5]);"
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
            "click(srcs()[4]);"
            "return later(function () { return JSON.stringify("
            "  texts(box.querySelector('.hc-overview-reading'),"
            "        'hc-overview-empty')); });",
            source={"ok": False, "kind": "doc",
                    "error": "that file is outside the project"})
        self.assertEqual(["that file is outside the project"], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class AddSourceModalTests(Overview, BridgeTestCase):
    """The picker is a dialog over the page, not a fold in the column.

    It used to unfold inside the 220px source list, where a conversation
    list of sixty rows pushed the rest of the page off the screen. The form
    is the same one; where it opens, and what closes it, is not.
    """

    def test_the_form_lives_in_a_dialog_outside_the_source_list(self):
        got = self.open(
            "var modal = box.querySelector('[data-hc-addmodal]');"
            "var form = box.querySelector('[data-hc-addform]');"
            "var list = box.querySelector('[data-hc-srcs]');"
            "return JSON.stringify([!!modal, modal.getAttribute('data-hc-on'),"
            " form.parentNode.getAttribute('data-hc-modal-card'),"
            " list.contains(form), modal.parentNode === box]);")
        self.assertEqual([True, None, "", False, True], got)

    def test_opening_marks_the_dialog_and_the_form_together(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var modal = box.querySelector('[data-hc-addmodal]');"
            "var form = box.querySelector('[data-hc-addform]');"
            "return JSON.stringify([P.addSourceShown(),"
            " modal.getAttribute('data-hc-on'), form.getAttribute('data-hc-on')]);")
        self.assertEqual([True, "", ""], got)

    def test_the_x_the_backdrop_and_escape_all_close_it(self):
        got = self.open(
            "var modal = box.querySelector('[data-hc-addmodal]');"
            "var add = box.querySelector('.hc-overview-addsrc');"
            "click(add); click(modal.querySelector('[data-hc-addclose]'));"
            "var byX = P.addSourceShown();"
            "click(add); click(modal); var byBack = P.addSourceShown();"
            "click(add); key('Escape', document.body);"
            "return JSON.stringify([byX, byBack, P.addSourceShown(),"
            " P.overviewShown()]);")
        # Escape takes the dialog and stops there: the page under it stays.
        self.assertEqual([False, False, False, True], got)

    def test_a_click_on_the_card_is_not_a_click_out_of_it(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "click(box.querySelector('[data-hc-modal-card]'));"
            "return JSON.stringify(P.addSourceShown());")
        self.assertTrue(got)

    def test_attaching_something_closes_the_dialog_over_the_list(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var form = box.querySelector('[data-hc-addform]');"
            "click(form.querySelector('[data-hc-kind=\"doc\"]'));"
            "form.querySelector('[data-hc-src-label]').value = 'docs/plan.md';"
            "click(form.querySelector('[data-hc-src-add]'));"
            "return later(function () { return JSON.stringify("
            "  [P.addSourceShown()]); });",
            state=state_with([SOURCES[1]]))
        self.assertEqual([False], got)

    def test_a_refused_attach_leaves_it_open_to_be_corrected(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var form = box.querySelector('[data-hc-addform]');"
            "click(form.querySelector('[data-hc-kind=\"doc\"]'));"
            "form.querySelector('[data-hc-src-label]').value = 'docs/architecture.md';"
            "click(form.querySelector('[data-hc-src-add]'));"
            "return JSON.stringify([P.addSourceShown(),"
            " form.querySelector('[data-hc-src-say]').textContent]);",
            state=state_with([SOURCES[1]]))
        self.assertEqual([True, "already attached"], got)


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
            "var x = srcs()[4].querySelector('[data-hc-drop-source]');"
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


# The rail under a goal's title, as the artifact leaves it: a label, then
# whatever that one goal has attached, then the control that attaches more.
RAIL = (
    "var rail = document.createElement('span'); rail.className = 'hc-sources';"
    "var lab = document.createElement('span');"
    "lab.className = 'hc-sources-label'; lab.textContent = 'SOURCES';"
    "rail.appendChild(lab);"
    "var own = document.createElement('span'); own.className = 'hc-src';"
    "var ownLab = document.createElement('span');"
    "ownLab.className = 'hc-src-label'; ownLab.textContent = 'goal-note.md';"
    "own.appendChild(ownLab); rail.appendChild(own);"
    "var plus = document.createElement('span');"
    "plus.className = 'hc-src-add'; plus.textContent = '+ Add source';"
    "rail.appendChild(plus); app.appendChild(rail);"
    "var chips = function () { return rail.children.filter(function (c) {"
    "  return c.getAttribute('data-hc-ctxsrc') !== null; }); };"
    "var tags = function () { return chips().map(function (c) {"
    "  return deepText(c.querySelector('.hc-src-tag')); }); };"
    "var names = function () { return chips().map(function (c) {"
    "  return deepText(c.querySelector('.hc-src-label')); }); };"
)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class InheritedSourceTests(BridgeTestCase):
    """The overview's context, drawn on every goal that inherits it.

    A goal sits inside the project, so the repository and everything the
    overview attaches are context it was written against. The rail under
    the title showed only what that one goal had attached, which in a fresh
    workspace was nothing at all.
    """

    def rail(self, tail, state=None):
        # Every tail here is a function body -- it opens with `return` --
        # so it needs a function to be in. Spliced at the top level it is
        # an illegal return, and the whole class fails before it draws.
        return json.loads(self.run_js(
            PRELUDE + fetch_js() + RAIL
            + "P.acceptState(%s);" % json.dumps(state or state_with())
            + "(function () {" + tail + "})();"))

    def test_the_rail_carries_the_repository_and_everything_attached_to_it(self):
        got = self.rail(
            "var drew = P.renderInheritedSources();"
            "return JSON.stringify([drew, tags(), names(),"
            " texts(rail, 'hc-src-label')]);")
        self.assertEqual(
            [True,
             ["REPO", "CHAT", "DOC", "GITHUB"],
             ["myrepo", "Claude session: aaaaaaaa", "architecture.md",
              "acme/other"],
             # the inherited ones read first, before the goal's own
             ["myrepo", "Claude session: aaaaaaaa", "architecture.md",
              "acme/other", "goal-note.md"]], got)

    def test_a_project_with_nothing_attached_still_names_its_repository(self):
        got = self.rail(
            "P.renderInheritedSources();"
            "return JSON.stringify([tags(), names()]);",
            state=state_with([]))
        self.assertEqual([["REPO"], ["myrepo"]], got)

    def test_a_second_pass_neither_duplicates_them_nor_keeps_a_stale_one(self):
        got = self.rail(
            "var first = P.renderInheritedSources();"
            "var again = P.renderInheritedSources();"
            "var many = chips().length;"
            "P.acceptState(%s);" % json.dumps(state_with([SOURCES[1]]))
            + "var moved = P.renderInheritedSources();"
            "return JSON.stringify([first, again, many, moved, names(),"
            " texts(rail, 'hc-src-label')]);")
        self.assertEqual([True, False, 4, True,
                          ["myrepo", "architecture.md"],
                          ["myrepo", "architecture.md", "goal-note.md"]], got)

    def test_an_inherited_source_carries_no_remove_and_says_where_it_is_from(self):
        got = self.rail(
            "P.renderInheritedSources();"
            "return JSON.stringify([chips().map(function (c) {"
            "  return c.querySelector('.hc-src-rm') === null; }),"
            " chips()[0].getAttribute('title'),"
            " chips()[0].getAttribute('role')]);")
        self.assertEqual([[True, True, True, True],
                          "myrepo — from the overview", "button"], got)

    def test_clicking_one_opens_the_overview_on_it(self):
        got = self.rail(
            "P.renderProjectChip(); P.renderInheritedSources();"
            "click(chips()[2]);"
            "return later(function () {"
            "  var box = document.querySelector('.hc-overview');"
            "  var on = []; (function walk(n) { (n.children || []).forEach("
            "    function (c) { if (c.getAttribute('data-hc-source') !== null"
            "      && c.getAttribute('data-hc-on') !== null) {"
            "      on.push(deepText(c.querySelector('.hc-overview-chip-name'))); }"
            "    walk(c); }); })(box);"
            "  return JSON.stringify([P.overviewShown(), on]); });")
        self.assertEqual([True, ["architecture.md"]], got)

    def test_a_workspace_with_no_project_draws_nothing(self):
        got = self.rail(
            "var drew = P.renderInheritedSources();"
            "return JSON.stringify([drew, chips().length]);",
            state=chat_state(project=False))
        self.assertEqual([False, 0], got)

    def test_the_sweep_is_what_puts_them_back_after_a_re_render(self):
        got = self.rail(
            "P.renderInheritedSources();"
            "chips().forEach(function (c) { rail.removeChild(c); });"
            "var gone = chips().length;"
            "var back = P.renderInheritedSources();"
            "return JSON.stringify([gone, back, names()]);")
        self.assertEqual([0, True,
                          ["myrepo", "Claude session: aaaaaaaa",
                           "architecture.md", "acme/other"]], got)
