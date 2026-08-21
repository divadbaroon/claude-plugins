"""The search bar under GOALS: a few words find a goal wherever it sits.

The box ranks every goal by its title, notes, TODO rows and prompt,
forgiving a slip of spelling; hits stand in for the tree while there is a
query, and picking one opens its branch and selects it. These tests hold
the ranking as a pure function and the rail's behaviour in the node harness
from test_goal_ui_bridge.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE


def goal(gid, title, parent=None, **fields):
    base = {"id": gid, "title": title, "parent_goal_id": parent,
            "status": "active", "prompt_ids": [], "sources": [],
            "notes": "", "description": "", "todo_items": [],
            "todos_md": "", "prompt_md": "", "updated_at": "2026-08-10T00:00:00+00:00"}
    base.update(fields)
    return base


GOALS = [
    goal("g1", "Notifications for TODO builds",
         notes="a banner in the corner when a build finishes",
         updated_at="2026-08-12T00:00:00+00:00"),
    goal("g1a", "Bell in the header", parent="g1",
         todo_items=[{"id": "t1", "text": "count the unread ones", "depth": 0,
                      "status": ""}]),
    goal("g2", "Search the rail",
         prompt_md="use fuzzy matching so a typo still finds it",
         updated_at="2026-08-11T00:00:00+00:00"),
    goal("g3", "Linked chats", prompt_ids=["p1"],
         updated_at="2026-08-09T00:00:00+00:00"),
    goal("g4", "Gone", status="abandoned", notes="banner"),
    goal("g5", "Banner copy", updated_at="2026-08-13T00:00:00+00:00"),
]
PROMPTS = [{"id": "p1", "role": "user", "text": "let me pick several chats",
            "session_id": "abc", "created_at": "2026-08-01"}]


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SearchRankingTests(BridgeTestCase):
    def rank(self, query, goals=None, prompts=None):
        return self.run_js(
            "window.__hcPromptUI.search.rank(%s, %s, %s);"
            % (json.dumps(goals if goals is not None else GOALS),
               json.dumps(prompts if prompts is not None else PROMPTS),
               json.dumps(query)))

    def ids(self, query, **kw):
        return [h["id"] for h in self.rank(query, **kw)]

    def test_an_empty_query_finds_nothing(self):
        self.assertEqual([], self.rank(""))
        self.assertEqual([], self.rank("   "))

    def test_a_title_outranks_the_same_word_in_notes(self):
        # "banner" is g5's title and a word in g1's notes.
        self.assertEqual(["g5", "g1"], self.ids("banner"))

    def test_every_field_is_searched(self):
        self.assertEqual(["g1"], self.ids("corner"))          # notes
        self.assertEqual(["g1a"], self.ids("unread"))         # a TODO row
        self.assertEqual(["g2"], self.ids("fuzzy"))           # the goal's prompt
        self.assertEqual(["g3"], self.ids("several"))         # a linked prompt
        self.assertEqual(["g1a"], self.ids("bell"))           # a subgoal's title

    def test_a_slip_of_spelling_still_finds_the_goal(self):
        self.assertEqual(["g1"], self.ids("notifcations"))
        self.assertEqual(["g1"], self.ids("notif"))
        self.assertEqual(["g2"], self.ids("fuzy"))

    def test_short_words_must_be_exact(self):
        # Three letters are one edit from most of English.
        self.assertEqual([], self.ids("teh"))
        # Two titles and one note carry "the"; titles first, the later
        # edited of the two ahead.
        self.assertEqual(["g2", "g1a", "g1"], self.ids("the"))

    def test_every_word_of_the_query_has_to_land(self):
        self.assertEqual(["g1"], self.ids("banner build"))
        self.assertEqual([], self.ids("banner fuzzy"))

    def test_equal_scores_fall_to_the_goal_edited_last(self):
        goals = [goal("a", "Router", updated_at="2026-08-01T00:00:00+00:00"),
                 goal("b", "Router", updated_at="2026-08-03T00:00:00+00:00"),
                 goal("c", "Router", updated_at="2026-08-02T00:00:00+00:00")]
        self.assertEqual(["b", "c", "a"], self.ids("router", goals=goals))

    def test_an_abandoned_goal_is_never_a_hit(self):
        self.assertNotIn("g4", self.ids("banner"))

    def test_a_hit_says_where_it_sits_and_where_it_matched(self):
        hit = self.rank("unread")[0]
        self.assertEqual(["Notifications for TODO builds"], hit["trail"])
        self.assertEqual("todos", hit["where"])
        self.assertEqual("count the unread ones", hit["excerpt"])
        top = self.rank("banner")[0]
        self.assertEqual("title", top["where"])
        self.assertEqual("", top["excerpt"])

    def test_the_distance_counts_a_swapped_pair_once(self):
        d = self.run_js("[window.__hcPromptUI.search.distance('abcd', 'abdc'),"
                        " window.__hcPromptUI.search.distance('abcd', 'abcd'),"
                        " window.__hcPromptUI.search.distance('abcd', 'xyz', 2)];")
        self.assertEqual([1, 0, 3], d)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SearchRailTests(BridgeTestCase):
    PRELUDE = (
        "var S = window.__hcPromptUI.search;"
        "var rail = document.createElement('div'); rail.className = 'hc-rail-left';"
        "var head = document.createElement('div'); head.className = 'hc-rail-head';"
        "var box = document.createElement('div'); box.className = 'hc-search';"
        "var input = document.createElement('input'); input.className = 'hc-search-input';"
        "var hits = document.createElement('div'); hits.className = 'hc-search-hits';"
        "var tree = document.createElement('div'); tree.className = 'hc-tree';"
        "box.appendChild(input); box.appendChild(hits);"
        "rail.appendChild(head); rail.appendChild(box); rail.appendChild(tree);"
        "app.appendChild(rail);"
        "window.__hcPromptUI.acceptState({ goals: %s, prompts: %s, scope: %s,"
        "  session_id: 'abc' });"
        "S.render();"
        "var went = []; window.__hcSelectGoal = function (id) { went.push(['select', id]); };"
        "var fire = function (type, target, key) {"
        "  listeners.filter(function (l) { return l[0] === type; })"
        "    .forEach(function (l) { l[1]({ type: type, target: target, key: key || '',"
        "      preventDefault: function () {}, stopPropagation: function () {} }); }); };"
        "var type = function (q) { input.value = q; fire('input', input); };"
        "var drawn = function () { return hits.children.map(function (n) {"
        "  return [n.className, n.getAttribute('data-hc-goal'),"
        "          n.getAttribute('data-hc-hit-active') !== null]; }); };"
        "var searching = function () { return rail.getAttribute('data-hc-searching') !== null; };"
    )

    def rail(self, tail, scope="chat", goals=None):
        return self.run_js(
            (self.PRELUDE % (json.dumps(goals if goals is not None else GOALS),
                             json.dumps(PROMPTS), json.dumps(scope))) + tail)

    def test_typing_draws_hits_in_place_of_the_tree(self):
        out = self.rail(
            "S.render();"
            "var before = [searching(), drawn()];"
            "type('banner');"
            "[before, searching(), drawn()];")
        self.assertEqual([False, []], out[0])
        self.assertTrue(out[1])
        self.assertEqual([["hc-search-hit", "g5", True],
                          ["hc-search-hit", "g1", False]], out[2])

    def test_a_hit_shows_its_trail_title_and_the_matching_line(self):
        out = self.rail(
            "type('unread');"
            "var hit = hits.children[0];"
            "[hit.querySelector('.hc-search-hit-trail').textContent,"
            " hit.querySelector('.hc-search-hit-title').textContent,"
            " hit.querySelector('.hc-search-hit-where').children.map("
            "   function (n) { return n.textContent; })];")
        self.assertEqual(["Notifications for TODO builds", "Bell in the header",
                          ["TODO: ", "count the unread ones"]], out)

    def test_clearing_the_box_puts_the_tree_back(self):
        out = self.rail(
            "type('banner'); var mid = searching();"
            "type(''); [mid, searching(), drawn()];")
        self.assertEqual([True, False, []], out)

    def test_nothing_matching_says_so(self):
        out = self.rail(
            "type('zzzzqqq'); [searching(), hits.children.map(function (n) {"
            "  return [n.className, n.textContent]; })];")
        self.assertEqual([True, [["hc-search-none", "Nothing matches “zzzzqqq”."]]],
                         out)

    def test_down_and_enter_pick_the_lit_hit_and_clear_the_box(self):
        out = self.rail(
            "type('banner');"
            "fire('keydown', input, 'ArrowDown');"
            "var lit = drawn();"
            "fire('keydown', input, 'Enter');"
            "[lit, went, input.value, searching(), drawn()];")
        self.assertEqual([["hc-search-hit", "g5", False],
                          ["hc-search-hit", "g1", True]], out[0])
        self.assertEqual([["select", "g1"]], out[1])
        self.assertEqual("", out[2])
        self.assertFalse(out[3])
        self.assertEqual([], out[4])

    def test_the_artifacts_reveal_is_preferred_when_it_is_there(self):
        out = self.rail(
            "window.__hcRevealGoal = function (id) { went.push(['reveal', id]); return true; };"
            "type('fuzzy'); fire('keydown', input, 'Enter'); went;")
        self.assertEqual([["reveal", "g2"]], out)

    def test_clicking_a_hit_picks_it(self):
        out = self.rail(
            "type('several');"
            "fire('click', hits.children[0].querySelector('.hc-search-hit-title'));"
            "[went, input.value];")
        self.assertEqual([[["select", "g3"]], ""], out)

    def test_escape_clears_the_box(self):
        out = self.rail(
            "type('banner'); fire('keydown', input, 'Escape');"
            "[input.value, searching(), went];")
        self.assertEqual(["", False, []], out)

    def test_hits_follow_the_state_as_it_changes(self):
        out = self.rail(
            "type('banner'); var first = drawn().length;"
            "window.__hcPromptUI.acceptState({ goals: %s, prompts: [], scope: 'chat',"
            "  session_id: 'abc' });"
            "S.render(); [first, drawn().length];"
            % json.dumps(GOALS + [goal("g6", "Another banner")]))
        self.assertEqual([2, 3], out)

    def test_a_global_vault_has_no_search(self):
        out = self.rail("type('banner'); [searching(), drawn()];", scope="global")
        self.assertEqual([False, []], out)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class SearchPatchTests(BridgeTestCase):
    def test_the_box_sits_directly_under_the_goals_heading_in_a_chat(self):
        out = self.patched_bundle("out;", scope="chat")
        head = out.index('<span class="hc-rail-count">{{ goalCount }}</span></div>')
        box = out.index('<div class="hc-search">')
        self.assertEqual(head + len('<span class="hc-rail-count">{{ goalCount }}</span></div>'),
                         box, "nothing stands between the heading and the box")
        self.assertIn('<input class="hc-search-input" type="search"', out)
        self.assertIn('<div class="hc-search-hits"></div>', out)

    def test_a_global_vault_gets_no_box(self):
        out = self.patched_bundle("out;", scope="global")
        self.assertNotIn("hc-search", out)

    def test_the_artifact_learns_to_reveal_a_goal(self):
        out = self.patched_bundle(
            "[out.indexOf('window.__hcRevealGoal = (id) =>') >= 0,"
            " out.indexOf('window.__hcSelectGoal = (id) => this.set(') >= 0];",
            scope="chat")
        self.assertEqual([True, True], out)

    def test_no_rule_is_drawn_between_the_heading_and_the_box(self):
        css = self.run_js("window.__hcPromptUI.launchCss();")
        self.assertIn("[data-hc-launch] .hc-rail-left>.hc-rail-head{border-bottom:0", css)
        self.assertIn("[data-hc-launch] .hc-search{flex:none;", css)
        self.assertIn("border-bottom:1px solid var(--bd)}", css)
        # The tree moved down one child; its padding followed it.
        self.assertIn("[data-hc-launch] .hc-rail-left>div:nth-child(3){padding:6px 6px 0}", css)
        self.assertNotIn(".hc-rail-left>div:nth-child(2)", css)
        # While searching, only the heading and the box stay on screen.
        self.assertIn(".hc-rail-left[data-hc-searching]>:not(.hc-rail-head):not(.hc-search){display:none!important}", css)


if __name__ == "__main__":
    unittest.main()
