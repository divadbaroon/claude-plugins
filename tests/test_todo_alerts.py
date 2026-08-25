"""What the workspace says when the builder changes a TODO row.

A row handed to the builder comes back done, failed, or asking. The bridge
diffs row status between two accepted states and says so once: a banner in
the top-right corner (clickable, dismissable, timed, and switchable), a bell
in the header counting what has not been seen, and a center listing all of
it. These tests hold that contract in the node harness from
test_goal_ui_bridge.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE


def goals(rows_by_goal):
    """{goal_id: [(row_id, text, status, question?), ...]} -> state goals."""
    out = []
    for gid, rows in rows_by_goal.items():
        out.append({
            "id": gid, "title": "Goal " + gid, "parent_goal_id": None,
            "status": "active", "prompt_ids": [], "sources": [],
            "todo_items": [
                {"id": r[0], "text": r[1], "depth": 0, "status": r[2],
                 "question": r[3] if len(r) > 3 else ""}
                for r in rows]})
    return out


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class TodoAlertTests(BridgeTestCase):
    SID = "7f3a1b2c-4d5e-4f60-8a9b-0c1d2e3f4a5b"

    PRELUDE = (
        "var A = window.__hcPromptUI.alerts;"
        "var slot = document.createElement('span'); slot.className = 'hc-alerts';"
        "header.appendChild(slot);"
        "var accept = function (gs) { return window.__hcPromptUI.acceptState("
        "  { goals: gs, prompts: [], scope: %s, session_id: %s }); };"
        "var fire = function (type, target, related) {"
        "  listeners.filter(function (l) { return l[0] === type; })"
        "    .forEach(function (l) { l[1]({ type: type, target: target,"
        "      relatedTarget: related || null, preventDefault: function () {},"
        "      stopPropagation: function () {} }); }); };"
        "var stack = function () {"
        "  var node = A.stack(); return node ? node.children : []; };"
        "var drawn = function () { return stack().map(function (n) {"
        "  return [n.getAttribute('data-hc-alert-kind'),"
        "          n.querySelector('.hc-alert-title').textContent,"
        "          n.querySelector('.hc-alert-detail').textContent,"
        "          n.querySelector('.hc-alert-goal').textContent]; }); };"
        "var bell = function () { A.renderBell(); return slot.querySelector('.hc-bell'); };"
        "var badge = function () { var b = bell(); return b ? [b.getAttribute('data-hc-unread'),"
        "  b.querySelector('.hc-bell-count').textContent] : null; };"
    )

    def alerts(self, tail, scope="chat", defer=True):
        return self.run_js(
            (self.PRELUDE % (json.dumps(scope), json.dumps(self.SID))) + tail,
            extra_env={"HC_DEFER_TIMEOUT": "1"} if defer else None)

    # --- the banner ----------------------------------------------------------

    def test_the_first_state_is_a_baseline_and_reports_nothing(self):
        # A page opening on a chat whose build finished an hour ago has
        # nothing new to say about it.
        out = self.alerts(
            "accept(%s); drawn();"
            % json.dumps(goals({"g1": [("t1", "wire it", "done"),
                                       ("t2", "ask me", "asking", "which?")]})))
        self.assertEqual([], out)

    def test_a_row_that_finishes_fails_or_asks_gets_a_banner(self):
        out = self.alerts(
            "accept(%s); accept(%s); drawn();"
            % (json.dumps(goals({"g1": [("t1", "wire it", "building"),
                                        ("t2", "test it", "queued"),
                                        ("t3", "ship it", "building")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "done"),
                                        ("t2", "test it", "failed"),
                                        ("t3", "ship it", "asking",
                                         "Which registry?")]}))))
        self.assertEqual(
            [["done", "TODO finished", "wire it", "Goal g1"],
             ["failed", "TODO failed", "test it", "Goal g1"],
             ["asking", "Claude has a question", "Which registry?", "Goal g1"]],
            out)

    def test_the_same_transition_is_reported_once(self):
        out = self.alerts(
            "var a = %s, b = %s;"
            "accept(a); accept(b); accept(b); accept(b);"
            "[stack().length, A.log().length];"
            % (json.dumps(goals({"g1": [("t1", "wire it", "building")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "done")]}))))
        self.assertEqual([1, 1], out)

    def test_ticking_a_row_done_by_hand_is_not_a_build_finishing(self):
        # Only a row that was out with the builder finishing is news. A row
        # the reader ticked off themselves went from nothing to done.
        out = self.alerts(
            "accept(%s); accept(%s); drawn();"
            % (json.dumps(goals({"g1": [("t1", "wire it", "")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "done")]}))))
        self.assertEqual([], out)

    def test_a_row_this_page_handed_off_counts_even_if_the_poll_misses_building(self):
        # Build, then done before the next 1.5s poll: the server never showed
        # "building". todoBuild marks the row out first, so it still reads
        # as a finish.
        out = self.alerts(
            "accept(%s); A.noteOut(['t1']); accept(%s); drawn();"
            % (json.dumps(goals({"g1": [("t1", "wire it", "")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "done")]}))))
        self.assertEqual([["done", "TODO finished", "wire it", "Goal g1"]], out)

    def test_a_global_vault_says_nothing(self):
        out = self.alerts(
            "accept(%s); accept(%s); [drawn(), A.log().length];"
            % (json.dumps(goals({"g1": [("t1", "wire it", "building")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "done")]}))),
            scope="global")
        self.assertEqual([[], 0], out)

    def test_the_banner_goes_away_on_its_own_after_the_set_seconds(self):
        out = self.alerts(
            "var delays = [];"
            "var real = setTimeout;"
            "setTimeout = function (f, ms) { delays.push(ms); return real(f, ms); };"
            "A.setSettings({ seconds: 9 });"
            "accept(%s); accept(%s);"
            "var before = stack().length; fireTimers();"
            "[before, stack().length, delays, A.log().length];"
            % (json.dumps(goals({"g1": [("t1", "wire it", "building")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "done")]}))))
        self.assertEqual([1, 0, [9000], 1], out)

    def test_the_close_marks_it_read_and_keeps_it_in_the_center(self):
        out = self.alerts(
            "accept(%s); accept(%s);"
            "var box = stack()[0];"
            "fire('click', box.querySelector('.hc-alert-close'));"
            "[stack().length, A.log().map(function (e) { return [e.kind, e.read]; }),"
            " A.unread(), badge()];"
            % (json.dumps(goals({"g1": [("t1", "wire it", "building")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "done")]}))))
        self.assertEqual([0, [["done", True]], 0, [None, "0"]], out)

    def test_clicking_the_banner_goes_to_the_goal(self):
        out = self.alerts(
            "var went = [];"
            "window.__hcSelectGoal = function (id) { went.push(id); };"
            "accept(%s); accept(%s);"
            "var box = stack()[0];"
            "fire('click', box.querySelector('.hc-alert-detail'));"
            "[went, stack().length, A.log()[0].read,"
            " window.__hcPromptUI.todoState().tab];"
            % (json.dumps(goals({"g2": [("t1", "wire it", "building")]})),
               json.dumps(goals({"g2": [("t1", "wire it", "done")]}))))
        self.assertEqual([["g2"], 0, True, "todos"], out)

    # --- settings --------------------------------------------------------------

    def test_banners_can_be_turned_off_and_the_center_still_records(self):
        out = self.alerts(
            "A.setSettings({ banners: false });"
            "accept(%s); accept(%s);"
            "[drawn(), A.log().length, A.unread(), badge(),"
            " JSON.parse(store['hc-alerts-settings-v1']).banners];"
            % (json.dumps(goals({"g1": [("t1", "wire it", "building")]})),
               json.dumps(goals({"g1": [("t1", "wire it", "failed")]}))))
        self.assertEqual([[], 1, 1, ["1", "1"], False], out)

    def test_settings_persist_and_seconds_are_clamped(self):
        out = self.alerts(
            "A.setSettings({ seconds: 0 }); var lo = A.settings().seconds;"
            "A.setSettings({ seconds: 999 }); var hi = A.settings().seconds;"
            "A.setSettings({ seconds: '12' });"
            "[lo, hi, A.settings(), JSON.parse(store['hc-alerts-settings-v1'])];")
        self.assertEqual([1, 120, {"banners": True, "seconds": 12},
                          {"banners": True, "seconds": 12}], out)

    # The controls live behind the header gear, not at the foot of the
    # center: the center lists what happened, the gear is where the page is
    # set.

    SETTINGS_INPUTS = (
        "var inputsIn = function (root) { var out = [];"
        "  (function walk(n) { ((n && n.children) || []).forEach(function (c) {"
        "    if (c.getAttribute('data-hc-alert-set') !== null) out.push(c);"
        "    walk(c); }); })(root); return out; };")

    def test_the_gear_settings_controls_write_the_settings(self):
        out = self.alerts(
            self.SETTINGS_INPUTS +
            "var G = window.__hcPromptUI.gear; G.open();"
            "var inputs = inputsIn(G.panel());"
            "var before = inputs.map(function (i) { return [i.getAttribute('data-hc-alert-set'), i.checked, i.value]; });"
            "inputs[0].checked = false; fire('change', inputs[0]);"
            "inputs[1].value = '20'; fire('change', inputs[1]);"
            "[before, A.settings(),"
            " inputs.map(function (i) { return [i.checked, i.value]; })];")
        self.assertEqual([[["banners", True, ""], ["seconds", None, "6"]],
                          {"banners": False, "seconds": 20},
                          [[False, ""], [None, "20"]]], out)

    def test_the_center_carries_no_settings_controls(self):
        out = self.alerts(
            self.SETTINGS_INPUTS +
            "A.open();"
            "[inputsIn(A.center()).length,"
            " A.center().querySelector('.hc-alert-settings') === null];")
        self.assertEqual([0, True], out)

    def test_the_gear_sits_in_the_header_and_toggles_the_panel(self):
        # Drawn by the sweep once a chat state has arrived; one of the gear's
        # panel and the bell's center is up at a time, and a click anywhere
        # else takes the panel down.
        out = self.alerts(
            "var G = window.__hcPromptUI.gear;"
            "var gslot = document.createElement('span'); gslot.className = 'hc-settings';"
            "header.appendChild(gslot);"
            "var before = [G.render(), gslot.querySelector('.hc-gear')];"
            "accept([]);"
            "G.render();"
            "var gear = gslot.querySelector('.hc-gear');"
            "fire('click', gear);"
            "var up = [G.panel() !== null, gear.getAttribute('data-hc-gear-open'),"
            "          G.panel().querySelector('.hc-settings-sec-head').textContent];"
            "fire('click', bell());"
            "var swapped = [G.panel() !== null, A.center() !== null];"
            "fire('click', gear);"
            "var back = [G.panel() !== null, A.center() !== null];"
            "fire('click', document.body);"
            "[before, up, swapped, back, [G.panel() !== null, gear.getAttribute('data-hc-gear-open')]];")
        self.assertEqual([[False, None],
                          [True, "", "Notifications"],
                          [False, True],
                          [True, False],
                          [False, None]], out)

    # --- the bell and the center ------------------------------------------------

    def test_the_bell_counts_what_has_not_been_seen(self):
        # The bell is a chat-scope control: it is drawn once a chat state
        # has been accepted, and reads 0 until something happens.
        out = self.alerts(
            "accept(%s); var none = badge();"
            "accept(%s);"
            "var two = badge();"
            "A.markRead(A.log()[0].id, true);"
            "[none, two, badge()];"
            % (json.dumps(goals({"g1": [("t1", "a", "building"),
                                        ("t2", "b", "building")]})),
               json.dumps(goals({"g1": [("t1", "a", "done"),
                                        ("t2", "b", "failed")]}))))
        self.assertEqual([[None, "0"], ["2", "2"], ["1", "1"]], out)

    def test_the_bell_opens_the_center_newest_first_and_marks_unread(self):
        out = self.alerts(
            "accept(%s); accept(%s); accept(%s);"
            "fire('click', bell());"
            "var rows = A.center().querySelector('.hc-alert-center-list').children;"
            "var listed = rows.map(function (r) { return [r.getAttribute('data-hc-alert-kind'),"
            "  r.getAttribute('data-hc-alert-unread') !== null,"
            "  r.querySelector('.hc-alert-detail').textContent]; });"
            "fire('click', bell());"
            "[listed, A.center() === null];"
            % (json.dumps(goals({"g1": [("t1", "first", "building"),
                                        ("t2", "second", "building")]})),
               json.dumps(goals({"g1": [("t1", "first", "done"),
                                        ("t2", "second", "building")]})),
               json.dumps(goals({"g1": [("t1", "first", "done"),
                                        ("t2", "second", "asking", "why?")]}))))
        self.assertEqual([[["asking", True, "why?"], ["done", True, "first"]], True],
                         out)

    def test_mark_all_read_and_clear_from_the_center(self):
        out = self.alerts(
            "accept(%s); accept(%s);"
            "A.open();"
            "var acts = {};"
            "(function walk(n) { (n.children || []).forEach(function (c) {"
            "  var k = c.getAttribute('data-hc-alert-act'); if (k) acts[k] = c;"
            "  walk(c); }); })(A.center());"
            "fire('click', acts['read-all']);"
            "var afterRead = [A.unread(), A.log().length];"
            "fire('click', acts['clear']);"
            "[afterRead, A.log().length, stack().length];"
            % (json.dumps(goals({"g1": [("t1", "a", "building"),
                                        ("t2", "b", "building")]})),
               json.dumps(goals({"g1": [("t1", "a", "done"),
                                        ("t2", "b", "failed")]}))))
        self.assertEqual([[0, 2], 0, 0], out)

    def test_a_cleared_bell_counts_the_next_alert_from_one(self):
        out = self.alerts(
            "accept(%s); accept(%s);"
            "var before = badge();"
            "A.clear();"
            "var cleared = badge();"
            "accept(%s);"
            "[before, cleared, badge(), A.log().length];"
            % (json.dumps(goals({"g1": [("t1", "a", "building"),
                                        ("t2", "b", "building"),
                                        ("t3", "c", "building")]})),
               json.dumps(goals({"g1": [("t1", "a", "done"),
                                        ("t2", "b", "failed"),
                                        ("t3", "c", "building")]})),
               json.dumps(goals({"g1": [("t1", "a", "done"),
                                        ("t2", "b", "failed"),
                                        ("t3", "c", "done")]}))))
        self.assertEqual([["2", "2"], [None, "0"], ["1", "1"], 1], out)

    def test_a_clear_in_another_page_is_not_undone_by_this_one(self):
        # Two workspace pages on one store. The other one cleared the log
        # while this one was holding a copy of it; the alert that lands here
        # next is added to what is stored, not to what was remembered, and
        # the bell reads 1 rather than what it read before the clear plus 1.
        out = self.alerts(
            "accept(%s); accept(%s);"
            "var mine = badge();"
            # The other page's clear, as this one's storage listener finds it.
            "store['hc-alerts-log-v1'] = '[]';"
            "A.changedElsewhere();"
            "var after = [badge(), A.log().length, stack().length];"
            "accept(%s);"
            "[mine, after, badge(), A.log().length];"
            % (json.dumps(goals({"g1": [("t1", "a", "building"),
                                        ("t2", "b", "building"),
                                        ("t3", "c", "building")]})),
               json.dumps(goals({"g1": [("t1", "a", "done"),
                                        ("t2", "b", "failed"),
                                        ("t3", "c", "building")]})),
               json.dumps(goals({"g1": [("t1", "a", "done"),
                                        ("t2", "b", "failed"),
                                        ("t3", "c", "done")]}))))
        self.assertEqual([["2", "2"], [[None, "0"], 0, 0], ["1", "1"], 1], out)

    def test_the_log_survives_a_reload(self):
        # Second harness run on the same localStorage: the center still
        # lists what happened, unread as it was left.
        out = self.alerts(
            "store['hc-alerts-log-v1'] = JSON.stringify([{id: 'a1', kind: 'failed',"
            "  goalId: 'g1', goalTitle: 'Goal g1', rowId: 't1', text: 'a',"
            "  question: '', at: 1700000000000, read: false}]);"
            "accept([]);"
            "[A.unread(), A.log()[0].kind, badge()];")
        self.assertEqual([1, "failed", ["1", "1"]], out)

    def test_a_banner_that_was_up_at_reload_comes_back_for_the_time_it_had_left(self):
        # A state change from the builder is what reconcileState reloads the
        # page for, so a banner's first second is often its last before the
        # reload. It comes back from the log once the header (the first
        # live DOM) is drawn -- unless it was read, dismissed, or is past
        # its time.
        out = self.alerts(
            "var delays = [];"
            "var real = setTimeout;"
            "setTimeout = function (f, ms) { delays.push(ms); return real(f, ms); };"
            "var now = Date.now();"
            "store['hc-alerts-log-v1'] = JSON.stringify(["
            "  {id: 'a1', kind: 'done', goalId: 'g1', goalTitle: 'Goal g1', rowId: 't1',"
            "   text: 'fresh', question: '', at: now - 2000, read: false},"
            "  {id: 'a2', kind: 'failed', goalId: 'g1', goalTitle: 'Goal g1', rowId: 't2',"
            "   text: 'seen', question: '', at: now - 1000, read: true},"
            "  {id: 'a3', kind: 'asking', goalId: 'g1', goalTitle: 'Goal g1', rowId: 't3',"
            "   text: 'old', question: 'q', at: now - 60000, read: false}]);"
            "accept([]); bell(); bell();"
            "[drawn().map(function (d) { return d[2]; }),"
            " delays.length === 1 && delays[0] > 3500 && delays[0] <= 4000];")
        self.assertEqual([["fresh"], True], out)

    def test_the_artifact_publishes_the_selection_hook_the_bridge_calls(self):
        out = self.patched_bundle(
            "out.indexOf('window.__hcSelectGoal = (id) => this.set(') >= 0;",
            scope="chat")
        self.assertTrue(out)


if __name__ == "__main__":
    unittest.main()
