"""The Builds tab behind the gear: which model a TODO build runs on, and at
what effort.

The choices are read off the installed Claude Code binary when the panel
opens (``/api/models``), so a CLI update is what adds new models; a change
is posted to the server (``set_build_settings``), which keeps it for every
build after. The controls are found by attribute, as the other tabs' are.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE
from test_project_ui import PRELUDE, chat_state
from test_share_in_settings import RECORD


MODELS = {"ok": True,
          "aliases": ["fable", "opus", "sonnet", "haiku"],
          "models": ["claude-fable-5", "claude-opus-5", "claude-opus-4-8",
                     "claude-sonnet-5"],
          "efforts": ["low", "medium", "high", "xhigh", "max"],
          "source": {"path": "/Users/me/.local/share/claude/versions/2.1.245",
                     "version": "2.1.245",
                     "scanned_at": "2026-08-25T06:00:00+00:00"},
          "settings": {"model": "claude-opus-5", "effort": "high"}}

SUPABASE = {"ok": True, "configured": False, "signed_in": False,
            "config_path": "/vault/supabase.json"}


def fetch_js(models=None):
    models = models if models is not None else MODELS
    return (
        # The server keeps what was set: a change to one key leaves the
        # other as it was, and answers with both.
        "var kept = Object.assign({}, %s.settings || {});"
        "fetch = function (url, opts) {"
        "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
        "  calls.push([String(url), sent]);"
        "  var u = String(url); var body;"
        "  if (u.indexOf('/api/models') >= 0) body = %s;"
        "  else if (u.indexOf('/api/project.json') >= 0) body = %s;"
        "  else if (u.indexOf('/api/supabase') >= 0) body = %s;"
        "  else if (sent && sent.op === 'set_build_settings') {"
        "    if (sent.model !== undefined) kept.model = sent.model;"
        "    if (sent.effort !== undefined) kept.effort = sent.effort;"
        "    body = { ok: true, settings: Object.assign({}, kept) };"
        "  } else body = { ok: true };"
        "  return Promise.resolve({ ok: true, json: function () {"
        "    return Promise.resolve(body); } });"
        "};" % (json.dumps(models), json.dumps(models), json.dumps(RECORD),
                json.dumps(SUPABASE)))


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class BuildTabTests(BridgeTestCase):

    def panel(self, tail, **fetch):
        return json.loads(self.run_js(
            PRELUDE + fetch_js(**fetch)
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(chat_state())
            + "P.gear.open(); P.gear.tab('builds');"
            + "var panel = P.gear.panel();"
            + "var values = function (pick) { return pick.children.map("
            "    function (o) { return o.value; }); };"
            + "later(function () { " + tail + " });"))

    def test_the_tab_offers_the_aliases_then_the_cli_s_models_and_marks_the_choice(self):
        got = self.panel(
            "var model = panel.querySelector('[data-hc-build-set=\"model\"]');"
            "var effort = panel.querySelector('[data-hc-build-set=\"effort\"]');"
            "var tab = panel.querySelector('[data-hc-settings-tab=\"builds\"]');"
            "var sec = panel.querySelector('[data-hc-settings-sec=\"builds\"]');"
            "return JSON.stringify(["
            " tab.textContent, tab.getAttribute('data-hc-on'), sec.getAttribute('data-hc-on'),"
            " values(model), model.value, values(effort), effort.value,"
            " panel.querySelector('[data-hc-build-say]').textContent]);")
        self.assertEqual(
            ["Builds", "", "",
             ["", "fable", "opus", "sonnet", "haiku", "claude-fable-5",
              "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"],
             "claude-opus-5",
             ["", "low", "medium", "high", "xhigh", "max"], "high",
             "models read from Claude Code 2.1.245; new ones appear when the"
             " CLI updates"], got)

    def test_a_change_is_posted_and_the_line_says_what_builds_run_on_now(self):
        got = self.panel(
            "var model = panel.querySelector('[data-hc-build-set=\"model\"]');"
            "model.value = 'sonnet'; fire('change', model);"
            "var effort = panel.querySelector('[data-hc-build-set=\"effort\"]');"
            "effort.value = ''; fire('change', effort);"
            "return later(function () { return JSON.stringify(["
            " calls.filter(function (c) { return c[1] && c[1].op === 'set_build_settings'; })"
            "   .map(function (c) { return [c[1].model, c[1].effort]; }),"
            " panel.querySelector('[data-hc-build-say]').textContent]); });")
        # One key per change -- the other is left as it was on the server.
        self.assertEqual([[["sonnet", None], [None, ""]],
                          "saved · builds run on sonnet at the CLI's default effort"],
                         got)

    def test_without_the_binary_the_aliases_still_stand_and_the_line_says_so(self):
        got = self.panel(
            "var model = panel.querySelector('[data-hc-build-set=\"model\"]');"
            "return JSON.stringify([values(model), model.value,"
            " panel.querySelector('[data-hc-build-say]').textContent]);",
            models={"ok": True, "aliases": ["fable", "opus", "sonnet", "haiku"],
                    "models": [], "efforts": ["low", "medium", "high"],
                    "source": None, "settings": {"model": "", "effort": ""}})
        self.assertEqual([["", "fable", "opus", "sonnet", "haiku"], "",
                          "the Claude Code binary was not found; the aliases"
                          " still work"], got)

    def test_a_chosen_model_the_list_does_not_name_is_kept_as_an_option(self):
        chosen = dict(MODELS, settings={"model": "claude-opus-4-1-20250805",
                                        "effort": ""})
        got = self.panel(
            "var model = panel.querySelector('[data-hc-build-set=\"model\"]');"
            "return JSON.stringify([values(model).slice(-1), model.value]);",
            models=chosen)
        self.assertEqual([["claude-opus-4-1-20250805"], "claude-opus-4-1-20250805"],
                         got)


if __name__ == "__main__":
    unittest.main()
