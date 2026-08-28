"""Sending the project up, and letting someone in, from the settings panel.

Both need the Supabase account that is signed into two fields above them,
and neither is about the goals -- so they live behind the gear rather than
on the overview, where they used to sit. The controls are found by
attribute, so the panel styles them its own way without the handlers
caring.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE
from test_project_ui import PRELUDE, chat_state


RECORD = {"ok": True, "path": "/vault/projects/abc123.json",
          "written": True, "truncated": False,
          "text": '{\n "project": {\n  "name": "myrepo"\n }\n}\n'}


def fetch_js(supabase=None, share=None, shares=None, record=None):
    record = record if record is not None else RECORD
    supabase = supabase if supabase is not None else {
        "ok": True, "configured": True, "signed_in": True,
        "email": "dbarron410@vt.edu", "url": "https://ref.supabase.co",
        "anon_key": "eyJabc", "display_name": "David",
        "config_path": "/vault/supabase.json"}
    share = share if share is not None else {
        "ok": True, "code": "hcjoin1_deadbeef", "role": "reader"}
    shares = shares if shares is not None else {
        "ok": True, "shares": [
            {"id": "s1", "role": "editor", "uses": 0,
             "expires_at": "2026-09-21T00:00:00Z"},
            {"id": "s2", "role": "reader", "uses": 2,
             "expires_at": None, "revoked_at": "2026-08-01T00:00:00Z"}]}
    return (
        "fetch = function (url, opts) {"
        "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
        "  calls.push([String(url), sent]);"
        "  var u = String(url); var body;"
        "  if (u.indexOf('/api/project.json') >= 0) body = %s;"
        "  else if (u.indexOf('/api/supabase') >= 0) body = %s;"
        "  else if (sent && sent.op === 'create_share') body = %s;"
        "  else if (sent && sent.op === 'list_shares') body = %s;"
        "  else body = { ok: true };"
        "  return Promise.resolve({ ok: true, json: function () {"
        "    return Promise.resolve(body); } });"
        "};" % (json.dumps(record), json.dumps(supabase), json.dumps(share),
                json.dumps(shares)))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class ShareInSettingsTests(BridgeTestCase):

    def panel(self, tail, state=None, **fetch):
        return json.loads(self.run_js(
            PRELUDE + fetch_js(**fetch)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(state or chat_state())
            + "P.gear.open();"
            + "var panel = P.gear.panel();"
            + "later(function () { " + tail + " });"))

    def test_the_panel_carries_the_controls_and_the_overview_no_longer_does(self):
        got = self.panel(
            "P.openOverview();"
            "var over = document.querySelector('.hc-overview');"
            "var has = function (root, sel) { return root.querySelector(sel) !== null; };"
            "return JSON.stringify(["
            " [has(panel, '[data-hc-sync]'), has(panel, '[data-hc-share]'),"
            "  has(panel, '[data-hc-share-role]'), has(panel, '[data-hc-code-box]'),"
            "  has(panel, '[data-hc-shares]')],"
            " [has(over, '[data-hc-sync]'), has(over, '[data-hc-share]'),"
            "  has(over, '[data-hc-share-role]'), has(over, '[data-hc-code-box]'),"
            "  has(over, '[data-hc-shares]')]]);")
        self.assertEqual([[True] * 5, [False] * 5], got)

    def test_a_signed_in_account_turns_both_buttons_on_and_says_whose(self):
        got = self.panel(
            "return JSON.stringify(["
            " panel.querySelector('[data-hc-sync-say]').textContent,"
            " panel.querySelector('[data-hc-sync]').getAttribute('data-hc-off'),"
            " panel.querySelector('[data-hc-share]').getAttribute('data-hc-off')]);")
        self.assertEqual(
            ["signed in as dbarron410@vt.edu · sends this project's goals,"
             " TODO rows and notes", None, None], got)

    def test_without_a_sign_in_neither_button_can_be_pressed(self):
        got = self.panel(
            "return JSON.stringify(["
            " panel.querySelector('[data-hc-sync-say]').textContent,"
            " panel.querySelector('[data-hc-sync]').getAttribute('data-hc-off'),"
            " panel.querySelector('[data-hc-share]').getAttribute('data-hc-off')]);",
            supabase={"ok": True, "configured": True, "signed_in": False,
                      "config_path": "/vault/supabase.json"})
        self.assertEqual(["connected, not signed in · run `hc supabase login`",
                          "", ""], got)

    def test_the_role_chip_flips_and_the_invite_is_minted_with_it(self):
        got = self.panel(
            "var chip = panel.querySelector('[data-hc-share-role]');"
            "click(chip);"
            "var flipped = [chip.getAttribute('data-hc-share-role'), chip.textContent];"
            "click(panel.querySelector('[data-hc-share]'));"
            "return later(function () { return JSON.stringify([flipped,"
            " calls.filter(function (c) { return c[1] && c[1].op === 'create_share'; })"
            "   .map(function (c) { return c[1].role; }),"
            " panel.querySelector('[data-hc-code-box]').value,"
            " panel.querySelector('[data-hc-code]').getAttribute('data-hc-on')]); });")
        self.assertEqual([["editor", "as editor"], ["editor"],
                          "hcjoin1_deadbeef", ""], got)

    def test_the_open_invites_are_listed_and_a_revoked_one_is_not(self):
        got = self.panel(
            "return JSON.stringify(["
            " texts(panel, 'hc-settings-share-row'),"
            " (function () { var x = panel.querySelector('[data-hc-share-revoke]');"
            "    click(x);"
            "    return calls.filter(function (c) { return c[1] && c[1].op === 'revoke_share'; })"
            "      .map(function (c) { return c[1].id; }); })()]);")
        self.assertEqual([["×joins as editornot redeemed yetexpires 2026-09-21"],
                          ["s1"]], got)

    def test_the_copy_button_reaches_the_box_in_the_panel(self):
        got = self.panel(
            "click(panel.querySelector('[data-hc-share]'));"
            "return later(function () {"
            "  var copied = null;"
            "  navigator.clipboard = { writeText: function (t) { copied = t;"
            "    return Promise.resolve(); } };"
            "  click(panel.querySelector('[data-hc-code-copy]'));"
            "  return JSON.stringify([copied]); });")
        self.assertEqual(["hcjoin1_deadbeef"], got)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SettingsTabTests(BridgeTestCase):
    """Four sections in one column was a scroll.

    The account and what is shared from it are what somebody opens this for,
    and both were below the fold under a banner timeout. One tab at a time,
    the account first because nothing else in here works until it is signed
    into.
    """

    def panel(self, tail, **fetch):
        return json.loads(self.run_js(
            PRELUDE + fetch_js(**fetch)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "P.gear.open();"
            + "var panel = P.gear.panel();"
            + "var tabs = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            + "  if (c.getAttribute('data-hc-settings-tab') !== null) tabs.push(c);"
            + "  walk(c); }); })(panel);"
            + "var secs = function () { var out = {};"
            + "  (panel.children || []).forEach(function (c) {"
            + "    var k = c.getAttribute('data-hc-settings-sec');"
            + "    if (k) out[k] = c.getAttribute('data-hc-on') !== null; });"
            + "  return out; };"
            + "later(function () { " + tail + " });"))

    ALL = ("notifications", "supabase", "project", "shared", "data")

    def only(self, *on):
        return dict((name, name in on) for name in self.ALL)

    def test_it_opens_on_the_account_with_the_others_put_away(self):
        got = self.panel(
            "return JSON.stringify([tabs.map(function (t) { return t.textContent; }),"
            " tabs.map(function (t) { return t.getAttribute('data-hc-on'); }),"
            " secs()]);")
        self.assertEqual([["Account", "Alerts", "Sharing", "Data"],
                          ["", None, None, None],
                          self.only("supabase")], got)

    def test_data_is_the_record_this_machine_keeps(self):
        # Named and copied, not shown: a real project's record runs to
        # hundreds of kilobytes, past what the route hands a pane, and a
        # dump that stops a quarter of the way in reads as nothing. The
        # pane says where the file is and offers the copy.
        got = self.panel(
            "click(tabs[3]);"
            "var pane = panel.querySelector('[data-hc-record]');"
            "return JSON.stringify([secs(), deepText(pane),"
            " pane.querySelector('.hc-settings-record') === null,"
            " calls.filter(function (c) { return String(c[0]).indexOf("
            "   '/api/project.json') >= 0; }).length]);")
        self.assertEqual([self.only("data"),
                          "/vault/projects/abc123.jsonCopy project record",
                          True, 1], got)

    def copies(self, tail, record=None):
        # Timers held: "copied ✓" clears itself after a moment, and the
        # test reads it before that moment.
        return json.loads(self.run_js(
            PRELUDE + fetch_js(record=record)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "P.gear.open(); P.gear.tab('data');"
            + "var panel = P.gear.panel();"
            + "var copied = [];"
            + "navigator.clipboard = { writeText: function (t) { copied.push(t);"
            + "  return Promise.resolve(); } };"
            + "var said = function () { var b = panel.querySelector('.hc-record-btn');"
            + "  return b ? [b.getAttribute('data-hc-record-copy'),"
            + "    b.querySelector('.hc-record-said').textContent] : null; };"
            + "later(function () { " + tail + " });",
            extra_env={"HC_DEFER_TIMEOUT": "1"}))

    def test_the_copy_button_fetches_the_file_whole_and_says_copied(self):
        got = self.copies(
            "click(panel.querySelector('.hc-record-btn'));"
            "var seen = [said()];"
            "return later(function () {"
            "  seen.push([said(), copied, calls.filter(function (c) {"
            "    return String(c[0]).indexOf('/api/project.json?full=1') >= 0; }).length]);"
            "  fireTimers(); seen.push(said());"
            "  return JSON.stringify(seen); });")
        self.assertEqual(
            [["busy", "reading…"],
             [["copied", "copied ✓"], [RECORD["text"]], 1],
             [None, ""]],
            got)

    def test_an_older_server_that_still_cuts_the_read_is_said_so(self):
        cut = dict(RECORD, truncated=True)
        got = self.copies(
            "return P.record.copy().then(function (ok) {"
            "  return JSON.stringify([ok, said(), copied]); });",
            record=cut)
        self.assertEqual([True, ["cut", "copied · cut short"], [RECORD["text"]]],
                         got)

    def test_a_chat_without_a_directory_offers_no_copy(self):
        got = self.copies(
            "return JSON.stringify([said(),"
            " deepText(panel.querySelector('[data-hc-record]'))]);",
            record={"ok": False, "error": "no project"})
        self.assertEqual([None, "This chat has no project directory, so there"
                                " is no record to read."], got)

    def test_sharing_carries_this_project_and_the_workspaces_joined(self):
        got = self.panel(
            "click(tabs[2]);"
            "return JSON.stringify([tabs.map(function (t) { return t.getAttribute('data-hc-on'); }),"
            " secs()]);")
        self.assertEqual([[None, None, "", None],
                          self.only("project", "shared")], got)

    def test_alerts_is_where_the_banner_settings_went(self):
        got = self.panel(
            "click(tabs[1]);"
            "return JSON.stringify([secs(),"
            " panel.querySelector('[data-hc-alert-set=\"banners\"]') !== null]);")
        self.assertEqual([self.only("notifications"), True], got)

    def test_without_a_project_the_account_says_it_is_not_connected(self):
        got = self.panel(
            "return JSON.stringify([panel.querySelector('[data-hc-sb-say]').textContent,"
            " panel.querySelector('[data-hc-sb-say]').getAttribute('data-hc-bad')]);",
            supabase={"ok": True, "configured": False, "signed_in": False,
                      "config_path": "/vault/supabase.json"})
        self.assertEqual(["not connected · no Supabase project is configured",
                          None], got)

    def test_the_tab_it_was_left_on_is_the_tab_it_comes_back_to(self):
        got = self.panel(
            "click(tabs[3]); P.gear.close(); P.gear.open();"
            "var back = P.gear.panel();"
            "var on = []; (function walk(n) { (n.children || []).forEach(function (c) {"
            "  if (c.getAttribute('data-hc-settings-tab') !== null"
            "      && c.getAttribute('data-hc-on') !== null) on.push(c.textContent);"
            "  walk(c); }); })(back);"
            "return JSON.stringify([on, P.gear.tab('data'),"
            " back.querySelector('[data-hc-settings-sec=\"data\"]')"
            "   .getAttribute('data-hc-on')]);")
        self.assertEqual([["Data"], True, ""], got)

    def test_the_close_button_still_closes_rather_than_switching(self):
        got = self.panel(
            "click(panel.querySelector('.hc-settings-act'));"
            "return JSON.stringify([P.gear.panel() === null]);")
        self.assertEqual([True], got)


class ChildrenAreNotArraysTests(unittest.TestCase):
    """A guard the node harness cannot be: its nodes hold real arrays.

    ``array()`` refuses anything Array.isArray refuses, and a browser's
    ``.children`` is an HTMLCollection -- so ``array(node.children)`` reads
    as empty on a real page and as the full list under test. The settings
    tabs and the add-context kind pills both shipped with that bug and both
    passed their tests. ``kids()`` exists for this; the only place allowed
    to call array() on a ``children`` is the goal tree, whose nodes are
    plain objects.
    """

    def test_no_dom_children_are_read_through_array(self):
        import re
        from pathlib import Path as _Path
        source = (_Path(__file__).resolve().parents[1] / "hc" / "src"
                  / "human_compact" / "trajectory" / "web" / "bridge.js").read_text()
        found = re.findall(r"array\(([A-Za-z.]*)\.children\)", source)
        # `node` here is a goal, not an element: its children are a list in
        # the state the server sent.
        self.assertEqual(["node"], found)
