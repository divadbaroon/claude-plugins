"""The Archive page: what deleting a goal has always actually done.

Deleting has never erased. The record stays in goals.json with its status set
to archived, and the tree is defined as not drawing it -- which left the only
way back to a deleted goal being to open the file and read it. This page is
that way back: everything archived, the TODO rows that went down with it, and
the two things a reader can want from a deleted goal -- put it back, or
really delete it.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import supabase_client as SB  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_goal_ui_bridge import BridgeTestCase, NODE  # noqa: E402
from test_project_ui import PRELUDE, chat_state, PROJECTS, CHATS  # noqa: E402


GOALS = [
    {"id": "g1", "title": "Ship the rail", "parent_goal_id": None,
     "status": "in_progress", "updated_at": "2026-08-20T00:00:00+00:00"},
    {"id": "g2", "title": "The old rail", "parent_goal_id": None,
     "status": "archived", "updated_at": "2026-08-22T00:00:00+00:00",
     "todo_items": [{"id": "taaaa0001", "text": "sand the edges",
                     "status": "done"},
                    {"id": "taaaa0002", "text": "", "status": ""}]},
    {"id": "g2a", "title": "Its one subgoal", "parent_goal_id": "g2",
     "status": "archived", "updated_at": "2026-08-21T00:00:00+00:00"},
    {"id": "g3", "title": "Deleted before the rename", "parent_goal_id": None,
     "status": "abandoned", "updated_at": "2026-08-19T00:00:00+00:00"},
]


def state_with(goals=None):
    state = chat_state()
    state["goals"] = json.loads(json.dumps(GOALS if goals is None else goals))
    state["prompts"] = []
    state["project"]["saved"] = []
    state["project"]["sources"] = []
    return state


def fetch_js(state):
    """Answer the routes this page touches; record what was posted."""
    return (
        "fetch = function (url, opts) {"
        "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
        "  calls.push([String(url), sent]);"
        "  var u = String(url); var body;"
        "  if (u.indexOf('/api/state') >= 0) body = %s;"
        "  else if (u.indexOf('/api/chats') >= 0) body = %s;"
        "  else if (u.indexOf('/api/projects') >= 0) body = %s;"
        "  else body = { ok: true };"
        "  return Promise.resolve({ ok: true, json: function () {"
        "    return Promise.resolve(body); } });"
        "};" % (json.dumps(state), json.dumps(CHATS), json.dumps(PROJECTS)))


class Archive:
    """The overview open on its Archive page."""

    def open(self, tail, state=None, served=None):
        shown = state if state is not None else state_with()
        return json.loads(self.run_js(
            PRELUDE
            + fetch_js(served if served is not None else shown)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(shown)
            + "P.openOverview();"
            + "var box = document.querySelector('.hc-overview');"
            + "var tab = function (name) { var out = null;"
            + "  (function walk(n) { (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-overview-tab') === name) out = c;"
            + "    walk(c); }); })(box); return out; };"
            + "var rows = function () { var out = [];"
            + "  (function walk(n) { (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-archive-row') !== null) out.push(c);"
            + "    walk(c); }); })(box); return out; };"
            + "var act = function (id, what) { var out = null;"
            + "  (function walk(n) { (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-archive-' + what) === id) out = c;"
            + "    walk(c); }); })(box); return out; };"
            + "var says = function () { return String(box.querySelector("
            + "  '[data-hc-archive-say]').textContent || ''); };"
            + "var dialog = function () { var out = null;"
            + "  (function walk(n) { (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-ask-confirm') !== null) out = c;"
            + "    walk(c); }); })(document.body); return out; };"
            + "var press = function (what) { var out = null;"
            + "  (function walk(n) { (n.children || []).forEach(function (c) {"
            + "    if (c.getAttribute('data-hc-ask-' + what) !== null) out = c;"
            + "    walk(c); }); })(document.body);"
            + "  if (out && out.onclick) out.onclick({}); return !!out; };"
            + "var ops = function () { return calls.filter(function (c) {"
            + "  return c[1] && c[1].op; }).map(function (c) { return c[1]; }); };"
            + "later(function () { " + tail + " });"))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ArchivePageTests(Archive, BridgeTestCase):

    def test_the_archive_tab_swaps_the_page_under_the_tabs(self):
        got = self.open(
            "click(tab('archive'));"
            "return JSON.stringify([P.overviewPage(),"
            " box.querySelector('.hc-overview-main').getAttribute('data-hc-on'),"
            " box.querySelector('.hc-archive').getAttribute('data-hc-on'),"
            " tab('archive').getAttribute('class'),"
            " P.overviewShown()]);")
        self.assertEqual(["archive", None, "",
                          "hc-overview-tab hc-overview-tab-on", True], got)

    def test_it_lists_every_archived_goal_and_subgoal_and_nothing_live(self):
        got = self.open(
            "click(tab('archive'));"
            "return JSON.stringify(texts(box, 'hc-archive-name'));")
        # Newest first, and the goal still spelt the old way is in the list:
        # a store written before the rename is the common case, not an edge.
        self.assertEqual(["The old rail", "Its one subgoal",
                          "Deleted before the rename"], got)

    def test_a_subgoal_says_where_it_sat(self):
        got = self.open(
            "click(tab('archive'));"
            "return JSON.stringify(texts(box, 'hc-archive-path'));")
        self.assertEqual(["The old rail"], got)

    def test_the_rows_a_goal_took_down_with_it_are_shown(self):
        got = self.open(
            "click(tab('archive'));"
            "return JSON.stringify([texts(box, 'hc-archive-todotext'),"
            " texts(box, 'hc-archive-state')]);")
        # The blank row is not a row: it was never written.
        self.assertEqual([["sand the edges"], ["done"]], got)

    def test_a_subgoal_is_told_its_parent_is_archived_too(self):
        got = self.open(
            "click(tab('archive'));"
            "return JSON.stringify(texts(box, 'hc-archive-warn').length);")
        self.assertEqual(1, got)

    def test_an_empty_archive_says_what_would_put_something_here(self):
        clean = state_with([GOALS[0]])
        got = self.open(
            "click(tab('archive'));"
            "return JSON.stringify([rows().length,"
            " texts(box, 'hc-archive-empty').length]);",
            state=clean)
        self.assertEqual([0, 1], got)

    def test_every_row_offers_both_a_restore_and_a_delete(self):
        got = self.open(
            "click(tab('archive'));"
            "return JSON.stringify([rows().length,"
            " texts(box, 'hc-archive-btn')]);")
        self.assertEqual(3, got[0])
        self.assertEqual(["Restore", "Delete"] * 3, got[1])


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class RestoreTests(Archive, BridgeTestCase):
    """Putting one back: on disk, and in this browser's own memory."""

    def test_restore_asks_for_the_restore_not_a_bare_status(self):
        # set_status would leave a subgoal active under an archived parent,
        # which the tree walks to from nowhere: restored and invisible.
        got = self.open(
            "click(tab('archive')); click(act('g2', 'restore'));"
            "return JSON.stringify(ops());")
        self.assertEqual([{"op": "restore_goal", "goal_id": "g2"}], got)

    def test_one_lifted_out_from_under_an_archived_parent_says_so(self):
        got = self.open(
            "fetch = function (url, opts) {"
            "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "  calls.push([String(url), sent]);"
            "  var body = sent && sent.op === 'restore_goal'"
            "    ? { ok: true, lifted: true } : { ok: true };"
            "  return Promise.resolve({ ok: true, json: function () {"
            "    return Promise.resolve(body); } }); };"
            "click(tab('archive')); click(act('g2a', 'restore'));"
            "return later(function () { return JSON.stringify(says()); });")
        self.assertIn("back at the top of the tree", got)

    def test_it_also_forgets_this_browser_deleted_it(self):
        # The merge honours this side's memory over the server's status, so
        # a restore that left the tombstone here would land on disk and then
        # be undone by the next poll.
        got = self.open(
            "localStorage.setItem('hc-deleted-goals-v1',"
            "  JSON.stringify({ g2: 1, g9: 1 }));"
            "click(tab('archive')); click(act('g2', 'restore'));"
            "return later(function () {"
            "  return JSON.stringify(Object.keys(JSON.parse("
            "    localStorage.getItem('hc-deleted-goals-v1')))); });")
        self.assertEqual(["g9"], got)

    def test_the_row_goes_when_the_state_that_follows_says_it_is_active(self):
        after = state_with([dict(GOALS[0]), dict(GOALS[1], status="active"),
                            GOALS[2], GOALS[3]])
        got = self.open(
            "click(tab('archive')); click(act('g2', 'restore'));"
            "return later(function () { return later(function () {"
            "  return JSON.stringify([texts(box, 'hc-archive-name'), says()]);"
            "}); });",
            served=after)
        self.assertEqual(["Its one subgoal", "Deleted before the rename"],
                         got[0])
        self.assertIn("back in the tree", got[1])

    def test_a_refused_restore_says_so_and_keeps_the_row(self):
        got = self.open(
            "fetch = function (url, opts) {"
            "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "  calls.push([String(url), sent]);"
            "  var body = sent && sent.op"
            "    ? { ok: false, error: 'that goal is not yours to change' }"
            "    : { ok: true };"
            "  return Promise.resolve({ ok: true, json: function () {"
            "    return Promise.resolve(body); } }); };"
            "click(tab('archive')); click(act('g2', 'restore'));"
            "return later(function () {"
            "  return JSON.stringify([says(), rows().length]); });")
        self.assertEqual("that goal is not yours to change", got[0])
        self.assertEqual(3, got[1])


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class PurgeTests(Archive, BridgeTestCase):
    """Erasing one: asked for by name, and only after a question."""

    def test_delete_asks_before_anything_is_written(self):
        got = self.open(
            "click(tab('archive')); click(act('g2', 'purge'));"
            "return later(function () {"
            "  return JSON.stringify([!!dialog(),"
            "    texts(document.body, 'hc-ask-title'),"
            "    texts(document.body, 'hc-ask-body'), ops().length]); });")
        self.assertTrue(got[0], "the question must be asked")
        self.assertEqual(["Delete \u201cThe old rail\u201d for good?"], got[1])
        # What goes is named: the subgoal under it and the row on it.
        self.assertIn("1 subgoal", got[2][0])
        self.assertIn("1 TODO row", got[2][0])
        self.assertIn("nothing restores it", got[2][0])
        self.assertEqual(0, got[3], "and nothing is written before the answer")

    def test_cancelling_writes_nothing_and_keeps_the_row(self):
        got = self.open(
            "click(tab('archive')); click(act('g2', 'purge'));"
            "return later(function () { press('no');"
            "  return later(function () {"
            "    return JSON.stringify([ops().length, rows().length,"
            "      says(), !!dialog()]); }); });")
        self.assertEqual(0, got[0])
        self.assertEqual(3, got[1])
        self.assertEqual("Nothing was deleted.", got[2])
        self.assertFalse(got[3], "the question closes behind it")

    def test_confirming_posts_the_purge(self):
        got = self.open(
            "click(tab('archive')); click(act('g2', 'purge'));"
            "return later(function () { press('yes');"
            "  return later(function () { return JSON.stringify(ops()); }); });")
        self.assertEqual([{"op": "purge_goal", "goal_id": "g2"}], got)

    def test_what_was_erased_is_remembered_as_deleted_on_this_side(self):
        # Without this the merge reads the absence as a stale writer's loss
        # and puts the local copy back -- a delete that undoes itself.
        got = self.open(
            "fetch = function (url, opts) {"
            "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "  calls.push([String(url), sent]);"
            "  var body = sent && sent.op === 'purge_goal'"
            "    ? { ok: true, deleted: ['g2', 'g2a'] } : { ok: true };"
            "  return Promise.resolve({ ok: true, json: function () {"
            "    return Promise.resolve(body); } }); };"
            "click(tab('archive')); click(act('g2', 'purge'));"
            "return later(function () { press('yes');"
            "  return later(function () {"
            "    return JSON.stringify(Object.keys(JSON.parse("
            "      localStorage.getItem('hc-deleted-goals-v1') || '{}')).sort());"
            "  }); });")
        self.assertEqual(["g2", "g2a"], got)

    def test_an_account_copy_left_behind_is_said_out_loud(self):
        got = self.open(
            "fetch = function (url, opts) {"
            "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "  calls.push([String(url), sent]);"
            "  var body = sent && sent.op === 'purge_goal'"
            "    ? { ok: true, deleted: ['g2'], cloud: 'not signed in' }"
            "    : { ok: true };"
            "  return Promise.resolve({ ok: true, json: function () {"
            "    return Promise.resolve(body); } }); };"
            "click(tab('archive')); click(act('g2', 'purge'));"
            "return later(function () { press('yes');"
            "  return later(function () { return JSON.stringify([says(),"
            "    box.querySelector('[data-hc-archive-say]')"
            "      .getAttribute('data-hc-bad')]); }); });")
        self.assertIn("not signed in", got[0])
        self.assertEqual("", got[1], "and said as a problem, not as progress")

    def test_a_refused_purge_says_so_and_keeps_the_row(self):
        got = self.open(
            "fetch = function (url, opts) {"
            "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "  calls.push([String(url), sent]);"
            "  var body = sent && sent.op"
            "    ? { ok: false, error: 'only an archived goal can be deleted"
            " for good' } : { ok: true };"
            "  return Promise.resolve({ ok: true, json: function () {"
            "    return Promise.resolve(body); } }); };"
            "click(tab('archive')); click(act('g2', 'purge'));"
            "return later(function () { press('yes');"
            "  return later(function () {"
            "    return JSON.stringify([says(), rows().length]); }); });")
        self.assertEqual("only an archived goal can be deleted for good", got[0])
        self.assertEqual(3, got[1])



@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class RowControlTests(BridgeTestCase):
    """The control on every goal row: what it says it does."""

    def test_it_is_an_archive_box_and_says_archive(self):
        # The op behind it never changed -- it has always kept the record.
        # What changed is that the row now says so.
        got = self.patched_bundle(
            "[out.indexOf('title=\"Archive goal\"') >= 0,"
            " out.indexOf('title=\"Delete goal\"') >= 0,"
            " out.indexOf('hc-row-archive') >= 0,"
            " out.indexOf('{{ row.del }}') >= 0];", scope="chat")
        self.assertEqual([True, False, True, True], got)

    def test_a_read_only_workspace_hides_the_one_that_is_there(self):
        # The rule names the control by its title, so a title that moved on
        # without it would leave a guest a button that can only fail.
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        css = "".join(css) if isinstance(css, list) else css
        self.assertIn(
            '[data-hc-launch][data-hc-readonly] [title="Archive goal"],', css
        )
        self.assertIn('[title="Delete goal"]{display:none!important}', css)

    def test_the_guest_keeps_the_archive_page_but_not_its_buttons(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        css = "".join(css) if isinstance(css, list) else css
        self.assertIn(".hc-archive-acts{display:none!important}", css)
        self.assertNotIn(".hc-archive{display:none!important}", css)



# --- the op that erases ------------------------------------------------------

class PurgeOpTests(unittest.TestCase):
    """`purge_goal`: the only write in the workspace that loses something."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-archive"
        paths = CS.paths(self.session, self.root)
        paths.session_dir.mkdir(parents=True)
        tree = {"version": 1, "goals": [
            GM.new_goal("g1", "Ship it", origin="user"),
            GM.new_goal("g2", "The old rail", origin="user",
                        status="archived"),
            GM.new_goal("g2a", "Its subgoal", "g2", origin="user",
                        status="archived"),
            GM.new_goal("g2b", "Its other subgoal", "g2a", origin="user",
                        status="archived"),
        ]}
        GM.add_todo_row(tree["goals"][1], "sand the edges")
        GM.sanitize(tree)
        paths.goals.write_text(json.dumps(tree))
        paths.important.write_text(json.dumps({"items": []}))
        paths.prompts.write_text(json.dumps({"prompts": []}))
        paths.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        self.trajdir = paths.session_dir

    def apply(self, op):
        return ui._apply(op, self.trajdir, True)

    def ids(self):
        goals, _ = GM.load(self.trajdir)
        return [g["id"] for g in goals["goals"]]

    def test_restoring_one_puts_it_back_active(self):
        out = self.apply({"op": "restore_goal", "goal_id": "g2"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["lifted"])
        goals, _ = GM.load(self.trajdir)
        self.assertEqual("active", GM.by_id(goals, "g2")["status"])
        # Its subgoals are separately theirs to restore.
        self.assertEqual("archived", GM.by_id(goals, "g2a")["status"])

    def test_one_under_an_archived_parent_comes_back_at_the_top(self):
        # Left where it was it would be active and drawn by nobody: the tree
        # is walked down from the roots, and its parent is not in it.
        out = self.apply({"op": "restore_goal", "goal_id": "g2a"})
        self.assertTrue(out["lifted"], out)
        goals, _ = GM.load(self.trajdir)
        self.assertIsNone(GM.by_id(goals, "g2a")["parent_goal_id"])

    def test_a_goal_already_in_the_tree_is_not_restored(self):
        out = self.apply({"op": "restore_goal", "goal_id": "g1"})
        self.assertFalse(out["ok"])
        self.assertIn("not archived", out["error"])

    def test_it_takes_the_goal_and_everything_under_it(self):
        out = self.apply({"op": "purge_goal", "goal_id": "g2"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(["g1"], self.ids())
        self.assertEqual(["g2", "g2a", "g2b"], sorted(out["deleted"]))

    def test_the_rows_go_with_it(self):
        self.apply({"op": "purge_goal", "goal_id": "g2"})
        held = json.loads((self.trajdir / "todos.json").read_text())
        self.assertEqual({}, held["todos"])

    def test_a_goal_still_in_the_tree_cannot_be_erased(self):
        out = self.apply({"op": "purge_goal", "goal_id": "g1"})
        self.assertFalse(out["ok"])
        self.assertIn("archived", out["error"])
        self.assertIn("g1", self.ids())

    def test_a_goal_that_is_not_there_is_not_a_delete(self):
        out = self.apply({"op": "purge_goal", "goal_id": "g9"})
        self.assertFalse(out["ok"])
        self.assertEqual(4, len(self.ids()))

    def test_one_deleted_the_old_way_can_still_be_erased(self):
        # A store written before the rename says "abandoned"; sanitize reads
        # it as archived, and this op has to agree with sanitize.
        goals, important = GM.load(self.trajdir)
        GM.by_id(goals, "g2")["status"] = "abandoned"
        GM.save(self.trajdir, goals, important)
        out = self.apply({"op": "purge_goal", "goal_id": "g2"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(["g1"], self.ids())

    def test_the_account_copy_is_asked_for_too(self):
        seen = []

        def fake(root, cwd, session_id, local_id):
            seen.append((str(cwd), session_id, local_id))
            return {"ok": True, "deleted": 3}

        with mock.patch.object(SB, "delete_goal", fake):
            out = self.apply({"op": "purge_goal", "goal_id": "g2"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(
            [(str(Path(str(self.root)).resolve()), self.session, "g2")],
            [(str(Path(cwd).resolve()), who, gid) for cwd, who, gid in seen])
        self.assertNotIn("cloud", out)

    def test_an_account_that_refuses_does_not_undo_the_local_delete(self):
        def boom(root, cwd, session_id, local_id):
            raise SB.SupabaseError("not signed in")

        with mock.patch.object(SB, "delete_goal", boom):
            out = self.apply({"op": "purge_goal", "goal_id": "g2"})
        self.assertTrue(out["ok"], out)
        self.assertEqual("not signed in", out["cloud"])
        self.assertEqual(["g1"], self.ids())


if __name__ == "__main__":
    unittest.main()
