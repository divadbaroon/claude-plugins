"""How the system works, read from the system rather than drawn once.

A workspace made of a hook, a vault, an analyser, a server and a browser
has five places for a version to drift and, until now, no page that showed
any of them. Every confusion this project has cost a day to was one of
those: a server older than its code, hooks running an installed copy that
had never heard of a switch, a build whose process had gone.

The point of the page is that its warnings are facts, not decoration -- so
these tests are mostly about when a stage warns and what it then says.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE

HEALTHY = {
    "ok": True,
    "server": {"pid": 1, "stale": False, "auto_reload": True,
               "source": "/repo/hc/src/human_compact/trajectory"},
    "vault": {"base": "/vault", "chats": 10, "projects": 6,
              "session_id": "c088cc9e-7b02-4950-aba7-8c57fbd2c51d"},
    "hooks": {"installed": True, "events": ["SessionStart", "Stop"],
              "runs": "/repo/hc", "runtime": "", "is_repo": True,
              "missing": []},
    "builds": {"live": 1, "records": 27, "stale": 0},
    "inference": {"on": True, "why": "", "status": "idle",
                  "analyzed": 3900, "requested": 3900, "error": ""},
    "project": {"name": "claude-plugins", "cwd": "/repo"},
}


def report(**over):
    out = json.loads(json.dumps(HEALTHY))
    for key, value in over.items():
        out[key].update(value) if isinstance(value, dict) else None
    return out


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SystemStageTests(BridgeTestCase):

    def stages(self, payload):
        return json.loads(self.run_js(
            "JSON.stringify(window.__hcPromptUI.systemStages(%s)"
            "  .map(function (s) { return [s.name, !!s.warn, s.note || '',"
            "    s.facts.map(function (f) { return f[1]; })]; }));"
            % json.dumps(payload)))

    def names(self, payload):
        return [row[0] for row in self.stages(payload)]

    def warned(self, payload):
        return [row[0] for row in self.stages(payload) if row[1]]

    def note_of(self, payload, name):
        for row in self.stages(payload):
            if row[0] == name:
                return row[2]
        return None

    def test_the_stages_are_the_path_a_turn_takes(self):
        self.assertEqual(
            ["This chat", "Hooks", "The vault", "The analyser",
             "This workspace", "Builds"], self.names(HEALTHY))

    def test_a_healthy_system_warns_about_nothing(self):
        self.assertEqual([], self.warned(HEALTHY))

    def test_hooks_running_an_installed_copy_are_the_warning_they_are(self):
        # The fault that made an off switch useless: the hooks run their own
        # copy of the plugin, which had never heard of it.
        got = report(hooks={"is_repo": False, "runtime": "0.19.0-acbd596"})
        self.assertIn("Hooks", self.warned(got))
        self.assertIn("installed plugin 0.19.0-acbd596",
                      json.dumps(self.stages(got)))
        self.assertIn("not the code in this repository",
                      self.note_of(got, "Hooks"))

    def test_hooks_that_are_not_installed_at_all_warn(self):
        got = report(hooks={"installed": False, "events": [], "is_repo": True})
        self.assertIn("Hooks", self.warned(got))
        self.assertIn("none", json.dumps(self.stages(got)))

    def test_inference_that_is_off_says_so_and_says_why(self):
        got = report(inference={"on": False, "why": "a file in this chat's"
                                                    " vault directory"})
        self.assertIn("The analyser", self.warned(got))
        note = self.note_of(got, "The analyser")
        self.assertIn("Off for this chat", note)
        self.assertIn("only what you typed", note)

    def test_a_backlog_warns_with_the_number_in_it(self):
        got = report(inference={"analyzed": 3382, "requested": 3913})
        self.assertIn("The analyser", self.warned(got))
        self.assertIn("531 turns have not been read",
                      self.note_of(got, "The analyser"))

    def test_a_small_backlog_is_ordinary_and_does_not_warn(self):
        got = report(inference={"analyzed": 3900, "requested": 3950})
        self.assertEqual([], self.warned(got))

    def test_a_server_older_than_its_code_warns(self):
        got = report(server={"stale": True})
        self.assertIn("This workspace", self.warned(got))
        self.assertIn("older code", self.note_of(got, "This workspace"))

    def test_build_records_whose_process_has_gone_warn_with_the_count(self):
        # Five of these accumulated unnoticed across one afternoon.
        got = report(builds={"live": 0, "records": 27, "stale": 5})
        self.assertIn("Builds", self.warned(got))
        self.assertIn("5 say they are running", self.note_of(got, "Builds"))

    def test_every_warning_carries_a_line_saying_what_is_wrong(self):
        # A red dot with nothing to read is decoration.
        got = report(hooks={"is_repo": False}, server={"stale": True},
                     builds={"stale": 2},
                     inference={"on": False, "why": "by request"})
        rows = [row for row in self.stages(got) if row[1]]
        self.assertEqual(4, len(rows))
        for name, _warn, note, _facts in rows:
            self.assertTrue(note.strip(), name + " warns and says nothing")


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SystemViewTests(BridgeTestCase):

    def open(self, tail, payload=None):
        return json.loads(self.run_js(
            "var P = window.__hcPromptUI;"
            "var calls = [];"
            "fetch = function (u) { calls.push(String(u));"
            "  return Promise.resolve({ ok: true, json: function () {"
            "    return Promise.resolve(%s); } }); };"
            "P.acceptState({ goals: [], prompts: [], scope: 'chat',"
            "  session_id: 'c088cc9e', project: { cwd: '/repo',"
            "  name: 'claude-plugins' } });"
            "var later = function (fn) { var p = Promise.resolve();"
            "  for (var i = 0; i < 12; i++) p = p.then(function () {});"
            "  return p.then(fn); };"
            "var deepText = function (n) { return String(n.textContent || '') +"
            "  (n.children || []).map(deepText).join(''); };"
            "P.openSystem();"
            "var box = document.querySelector('.hc-system');"
            "later(function () { " % json.dumps(payload or HEALTHY)
            + tail + " });"))

    def test_it_opens_reads_the_system_and_draws_a_stage_for_each(self):
        got = self.open(
            "var cards = []; (function walk(n) { (n.children || []).forEach("
            "  function (c) { if (String(c.className) === 'hc-system-stage')"
            "    cards.push(c); walk(c); }); })(box);"
            "return JSON.stringify([P.systemShown(),"
            " calls.filter(function (u) { return u.indexOf('/api/system') >= 0; }).length,"
            " cards.length,"
            " deepText(box.querySelector('.hc-overview-name'))]);")
        self.assertEqual([True, 1, 6, "How this works"], got)

    def test_the_goals_tab_puts_it_away(self):
        got = self.open(
            "P.closeSystem();"
            "return JSON.stringify([P.systemShown(),"
            " document.documentElement.getAttribute('data-hc-system')]);")
        self.assertEqual([False, None], got)

    def test_a_system_that_cannot_be_read_says_so_rather_than_drawing_nothing(self):
        got = self.open(
            "return JSON.stringify([box.querySelector('.hc-overview-empty')"
            "  .textContent]);", payload={"ok": False})
        self.assertEqual(["could not read the system"], got)


if __name__ == "__main__":
    unittest.main()
