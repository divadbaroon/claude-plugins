"""The Saved page: a shelf the project keeps and hands to nobody.

A source is context -- attached so the chats of this project are told about
it. What a reader saves here is not: the paper they mean to read, the thread
they want to find again. It lives in the project's own record under its own
key, the page that lists it is the only thing that reads it, and files get
onto it by pointing at them in this machine's own chooser rather than by
spelling a path.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_goal_ui_bridge import BridgeTestCase, NODE  # noqa: E402
from test_project_json import ProjectFixture, server_for  # noqa: E402
from test_project_ui import PRELUDE, chat_state, PROJECTS, CHATS  # noqa: E402
from test_chat_ui_server import get_json, post_json  # noqa: E402


SAVED = [
    {"id": "v1", "kind": "file", "label": "attention.pdf",
     "ref": "/Users/me/papers/attention.pdf"},
    {"id": "v2", "kind": "link", "label": "escapingflatland.substack.com",
     "ref": "https://escapingflatland.substack.com/p/childhoods"},
]


class SavedRecordTests(unittest.TestCase):
    """What the record holds, and what it will not hold."""

    def test_a_bare_string_becomes_a_row_that_says_what_it_is(self):
        got = PS.normalize_saved(["/Users/me/papers/attention.pdf",
                                  "https://example.com/a/b"])
        self.assertEqual(["file", "link"], [row["kind"] for row in got])
        self.assertEqual(["attention.pdf", "example.com"],
                         [row["label"] for row in got])
        self.assertEqual(["v1", "v2"], [row["id"] for row in got])

    def test_the_reference_decides_the_kind_not_the_caller(self):
        got = PS.normalize_saved([{"kind": "link", "ref": "/tmp/paper.pdf"},
                                  {"kind": "file", "ref": "https://x.dev/p"}])
        self.assertEqual(["file", "link"], [row["kind"] for row in got])

    def test_a_row_the_reader_named_keeps_its_name(self):
        got = PS.normalize_saved([{"ref": "https://x.dev/p", "label": "The p"}])
        self.assertEqual("The p", got[0]["label"])

    def test_the_same_thing_twice_is_one_row(self):
        got = PS.normalize_saved(["https://x.dev/p", "https://x.dev/p"])
        self.assertEqual(1, len(got))

    def test_a_row_with_nothing_in_it_is_dropped(self):
        self.assertEqual([], PS.normalize_saved([{"label": "no reference"},
                                                 "", 7, None]))
        self.assertEqual([], PS.normalize_saved("not a list"))


class SavedFileTests(ProjectFixture):
    """The shelf on disk, and the ops that write it."""

    def test_the_project_file_carries_the_shelf_beside_its_sources(self):
        PS.save_project(self.root, self.project,
                        {"objective": "Ship it.", "saved": SAVED})
        section = self.written()["project"]
        self.assertEqual(["/Users/me/papers/attention.pdf",
                          "https://escapingflatland.substack.com/p/childhoods"],
                         [row["ref"] for row in section["saved"]])
        self.assertEqual(SAVED, PS.load_project(self.root, self.project)["saved"])

    def test_writing_something_else_does_not_erase_the_shelf(self):
        PS.save_project(self.root, self.project, {"saved": SAVED})
        record = PS.load_project(self.root, self.project)
        record["description"] = "unrelated"
        PS.save_project(self.root, self.project, record)
        self.assertEqual(2, len(PS.load_project(self.root, self.project)["saved"]))

    def test_the_workspace_writes_the_shelf_and_reads_it_back(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op", {"op": "set_project_meta",
                                              "saved": SAVED})
            self.assertTrue(out["ok"], out)
            self.assertEqual(["attention.pdf",
                              "escapingflatland.substack.com"],
                             [row["label"] for row in out["saved"]])
            state = get_json(url + "/api/state")
            self.assertEqual(2, len(state["project"]["saved"]))

    def test_the_op_refuses_a_shelf_that_is_not_a_list(self):
        with server_for(self.trajdir) as url:
            out = post_json(url + "/api/op",
                            {"op": "set_project_meta", "saved": "no"})
        self.assertFalse(out["ok"])

    def test_what_a_build_is_told_about_the_project_leaves_the_shelf_out(self):
        """The whole reason the shelf is not a source.

        A source is quoted into what a build opens on; a saved thing is not.
        The record here holds one of each, and the reference of the saved one
        appears nowhere else -- so finding it in the opening lines would mean
        the shelf had been handed over.
        """
        from human_compact.trajectory import build as BUILD
        PS.save_project(self.root, self.project, {
            "objective": "Ship it.",
            "sources": [{"type": "github", "label": "acme/attached"}],
            "saved": [{"ref": "/Users/me/papers/unmentionable.pdf"}]})
        lines = "\n".join(BUILD.project_lines(self.session, self.root))
        self.assertIn("acme/attached", lines)
        self.assertNotIn("unmentionable", lines)


class PickFilesTests(unittest.TestCase):
    """The chooser, which is the point of the row: a path is something you
    can point at long before it is something you can spell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = Path(self.tmp.name)
        self.paper = self.here / "attention.pdf"
        self.paper.write_text("x")

    def run_with(self, stdout="", stderr="", code=0):
        done = mock.Mock(stdout=stdout, stderr=stderr, returncode=code)
        # Exercise the result contract, not whether this CI image happens to
        # ship zenity or kdialog. Platform command selection has its own tests.
        with mock.patch.object(ui, "_file_chooser_command",
                               return_value=["test-file-chooser"]), \
             mock.patch("subprocess.run", return_value=done) as ran:
            out = ui.pick_files(str(self.here))
        return out, ran

    def test_every_line_the_chooser_prints_is_a_file(self):
        second = self.here / "childhoods.md"
        second.write_text("y")
        out, _ = self.run_with("%s\n%s\n" % (self.paper, second))
        self.assertTrue(out["ok"], out)
        self.assertEqual(["attention.pdf", "childhoods.md"],
                         [f["name"] for f in out["files"]])
        self.assertEqual([str(self.paper.resolve()), str(second.resolve())],
                         [f["path"] for f in out["files"]])

    def test_closing_the_dialog_is_not_a_failure(self):
        out, _ = self.run_with("", "User canceled. (-128)", 1)
        self.assertEqual({"ok": True, "cancelled": True}, out)

    def test_a_chooser_that_could_not_open_says_why(self):
        out, _ = self.run_with("", "no display", 1)
        self.assertFalse(out["ok"])
        self.assertEqual("no display", out["error"])

    def test_a_directory_is_not_a_file(self):
        out, _ = self.run_with("%s\n" % self.here)
        self.assertFalse(out["ok"])


# --- the page ---------------------------------------------------------------

def state_with(saved=None, sources=None):
    state = chat_state()
    state["project"]["saved"] = SAVED if saved is None else saved
    state["project"]["sources"] = sources or []
    return state


def fetch_js(picked=None, saved=None):
    picked = picked if picked is not None else {
        "ok": True, "files": [{"path": "/Users/me/papers/new.pdf",
                               "name": "new.pdf"}]}
    return (
        "fetch = function (url, opts) {"
        "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
        "  calls.push([String(url), sent]);"
        "  var u = String(url); var body;"
        "  if (sent && sent.op === 'pick_files') body = %s;"
        "  else if (sent && sent.op === 'set_project_meta')"
        "    body = { ok: true, saved: sent.saved || [], sources: sent.sources || [] };"
        "  else if (u.indexOf('/api/chats') >= 0) body = %s;"
        "  else if (u.indexOf('/api/projects') >= 0) body = %s;"
        "  else body = { ok: true };"
        "  return Promise.resolve({ ok: true, json: function () {"
        "    return Promise.resolve(body); } });"
        "};" % (json.dumps(picked), json.dumps(CHATS), json.dumps(PROJECTS)))


class Saved:
    """The overview open on its Saved page."""

    def open(self, tail, state=None, **fetch):
        return json.loads(self.run_js(
            PRELUDE + fetch_js(**fetch)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(
                state if state is not None else state_with())
            + "P.openOverview();"
            + "var box = document.querySelector('.hc-overview');"
            + "var tab = function (name) { var out = null;"
            + "  (function walk(n) { (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-overview-tab') === name) out = c;"
            + "    walk(c); }); })(box); return out; };"
            + "var rows = function () { var out = [];"
            + "  (function walk(n) { (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-saved-row') !== null) out.push(c);"
            + "    walk(c); }); })(box); return out; };"
            + "var meta = function () { return calls.filter(function (c) {"
            + "  return c[1] && c[1].op === 'set_project_meta'; }); };"
            + "later(function () { " + tail + " });"))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SavedPageTests(Saved, BridgeTestCase):

    def test_the_box_has_its_tabs_and_opens_on_the_overview(self):
        got = self.open(
            "return JSON.stringify([texts(box, 'hc-overview-tab'),"
            " P.overviewPage(),"
            " box.querySelector('.hc-overview-main').getAttribute('data-hc-on'),"
            " box.querySelector('.hc-saved').getAttribute('data-hc-on')]);")
        self.assertEqual([["OVERVIEW", "SAVED", "FAQ", "ARCHIVE", "GOALS"],
                          "overview", "", None],
                         got)

    def test_the_saved_tab_swaps_the_page_under_the_tabs(self):
        got = self.open(
            "click(tab('saved'));"
            "return JSON.stringify([P.overviewPage(),"
            " box.querySelector('.hc-overview-main').getAttribute('data-hc-on'),"
            " box.querySelector('.hc-saved').getAttribute('data-hc-on'),"
            " tab('saved').getAttribute('class'),"
            " tab('overview').getAttribute('class'),"
            " P.overviewShown()]);")
        self.assertEqual(["saved", None, "",
                          "hc-overview-tab hc-overview-tab-on",
                          "hc-overview-tab", True], got)

    def test_going_back_to_the_overview_brings_the_project_card_back(self):
        got = self.open(
            "click(tab('saved')); click(tab('overview'));"
            "return JSON.stringify([P.overviewPage(),"
            " box.querySelector('.hc-overview-main').getAttribute('data-hc-on'),"
            " box.querySelector('.hc-saved').getAttribute('data-hc-on')]);")
        self.assertEqual(["overview", "", None], got)

    def test_goals_still_closes_the_whole_box(self):
        got = self.open(
            "click(tab('saved')); click(tab('goals'));"
            "return JSON.stringify(P.overviewShown());")
        self.assertFalse(got)

    def test_the_shelf_lists_what_the_project_saved(self):
        got = self.open(
            "click(tab('saved'));"
            "return JSON.stringify([texts(box, 'hc-saved-tag'),"
            " texts(box, 'hc-saved-name'), texts(box, 'hc-saved-ref'),"
            " rows().length]);")
        self.assertEqual([["FILE", "LINK"],
                          ["attention.pdf", "escapingflatland.substack.com"],
                          ["/Users/me/papers/attention.pdf",
                           "https://escapingflatland.substack.com/p/childhoods"],
                          2], got)

    def test_an_empty_shelf_says_that_nothing_here_is_context(self):
        got = self.open(
            "click(tab('saved'));"
            "return JSON.stringify([rows().length,"
            " texts(box, 'hc-saved-empty').join('').indexOf('no chat') >= 0]);",
            state=state_with([]))
        self.assertEqual([0, True], got)

    def test_the_page_says_out_loud_that_it_is_not_sent_anywhere(self):
        got = self.open(
            "click(tab('saved'));"
            "return JSON.stringify(texts(box, 'hc-saved-note'));")
        self.assertEqual(["kept for you · not sent to any chat"], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SavedWritingTests(Saved, BridgeTestCase):
    """Adding and removing, which is one op: the whole list, posted back."""

    def test_add_files_opens_the_chooser_and_saves_what_came_back(self):
        got = self.open(
            "click(tab('saved'));"
            "click(box.querySelector('[data-hc-saved-files]'));"
            "return later(function () { return JSON.stringify(["
            "  calls.filter(function (c) { return c[1] && c[1].op === 'pick_files'; })"
            "    .map(function (c) { return c[1].start; }),"
            "  meta().map(function (c) { return c[1].saved.map(function (r) {"
            "    return [r.kind, r.ref]; }); }),"
            "  texts(box, 'hc-saved-name')]); });")
        self.assertEqual([["/Users/me/work/myrepo"],
                          [[["file", "/Users/me/papers/attention.pdf"],
                            ["link", "https://escapingflatland.substack.com/p/childhoods"],
                            ["file", "/Users/me/papers/new.pdf"]]],
                          ["attention.pdf", "escapingflatland.substack.com",
                           "new.pdf"]], got)

    def test_a_file_already_on_the_shelf_is_not_saved_twice(self):
        got = self.open(
            "click(tab('saved'));"
            "click(box.querySelector('[data-hc-saved-files]'));"
            "return later(function () { return JSON.stringify(["
            "  meta().length,"
            "  box.querySelector('[data-hc-saved-say]').textContent]); });",
            picked={"ok": True, "files": [
                {"path": "/Users/me/papers/attention.pdf",
                 "name": "attention.pdf"}]})
        self.assertEqual([0, "already saved"], got)

    def test_a_cancelled_chooser_writes_nothing_and_says_nothing(self):
        got = self.open(
            "click(tab('saved'));"
            "click(box.querySelector('[data-hc-saved-files]'));"
            "return later(function () { return JSON.stringify(["
            "  meta().length,"
            "  box.querySelector('[data-hc-saved-say]').textContent]); });",
            picked={"ok": True, "cancelled": True})
        self.assertEqual([0, ""], got)

    def test_a_link_is_typed_and_added(self):
        got = self.open(
            "click(tab('saved'));"
            "box.querySelector('[data-hc-saved-link]').value = 'https://x.dev/p';"
            "click(box.querySelector('[data-hc-saved-add]'));"
            "return later(function () { return JSON.stringify(["
            "  meta().map(function (c) { return c[1].saved.length; }),"
            "  meta()[0][1].saved[2],"
            "  box.querySelector('[data-hc-saved-link]').value]); });")
        self.assertEqual([[3],
                          {"id": "v3-15", "kind": "link", "label": "",
                           "ref": "https://x.dev/p"}, ""], got)

    def test_a_link_written_the_short_way_is_still_a_link(self):
        got = self.open(
            "click(tab('saved'));"
            "var field = box.querySelector('[data-hc-saved-link]');"
            "field.value = 'arxiv.org/abs/1706.03762';"
            "key('Enter', field);"
            "return later(function () { return JSON.stringify("
            "  meta()[0][1].saved[2].ref); });")
        self.assertEqual("https://arxiv.org/abs/1706.03762", got)

    def test_an_empty_link_box_is_a_word_not_a_write(self):
        got = self.open(
            "click(tab('saved'));"
            "click(box.querySelector('[data-hc-saved-add]'));"
            "return later(function () { return JSON.stringify(["
            "  meta().length,"
            "  box.querySelector('[data-hc-saved-say]').textContent]); });")
        self.assertEqual([0, "paste a link first"], got)

    def test_removing_a_row_posts_the_shelf_without_it(self):
        got = self.open(
            "click(tab('saved'));"
            "click(rows()[0].querySelector('[data-hc-drop-saved]'));"
            "return later(function () { return JSON.stringify(["
            "  meta().map(function (c) { return c[1].saved.map(function (r) {"
            "    return r.ref; }); }),"
            "  texts(box, 'hc-saved-name')]); });")
        self.assertEqual([[["https://escapingflatland.substack.com/p/childhoods"]],
                          ["escapingflatland.substack.com"]], got)

    def test_the_shelf_never_travels_as_a_source(self):
        got = self.open(
            "click(tab('saved'));"
            "click(box.querySelector('[data-hc-saved-files]'));"
            "return later(function () { return JSON.stringify("
            "  meta().map(function (c) { return c[1].sources === undefined; })); });")
        self.assertEqual([True], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class BrowseForSourceTests(Saved, BridgeTestCase):
    """The same chooser, on the way a document becomes context: attaching a
    file used to mean typing its path."""

    def test_browse_shows_for_a_document_and_hides_for_the_others(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "var b = box.querySelector('[data-hc-src-browse]');"
            "var seen = [b.getAttribute('data-hc-off')];"
            "P.setSourceKind('doc'); seen.push(b.getAttribute('data-hc-off'));"
            "P.setSourceKind('chat'); seen.push(b.getAttribute('data-hc-off'));"
            "return JSON.stringify(seen);")
        self.assertEqual(["", None, ""], got)

    def test_what_was_picked_is_attached_without_being_typed(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "P.setSourceKind('doc');"
            "click(box.querySelector('[data-hc-src-browse]'));"
            "return later(function () { return JSON.stringify(["
            "  meta().map(function (c) { return c[1].sources.map(function (r) {"
            "    return [r.type, r.label]; }); }),"
            "  P.addSourceShown()]); });")
        self.assertEqual([[[["doc", "/Users/me/papers/new.pdf"]]], False], got)

    def test_a_cancelled_chooser_leaves_the_dialog_as_it_was(self):
        got = self.open(
            "click(box.querySelector('.hc-overview-addsrc'));"
            "P.setSourceKind('doc');"
            "click(box.querySelector('[data-hc-src-browse]'));"
            "return later(function () { return JSON.stringify(["
            "  meta().length, P.addSourceShown()]); });",
            picked={"ok": True, "cancelled": True})
        self.assertEqual([0, True], got)


if __name__ == "__main__":
    unittest.main()
