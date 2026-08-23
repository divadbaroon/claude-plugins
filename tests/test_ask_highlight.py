"""Asking Claude about what is highlighted, in the browser.

Drag a cursor over anything in the workspace -- a goal's title in the tree,
a TODO row in the rail, a line of somebody's notes -- and a small "Ask
Claude" appears beside the selection. Taking it opens a panel that quotes
the passage and answers questions about it from that passage and the goal
it sits in -- and stays open afterwards, so the next question can be a
follow-up rather than a fresh start. Nothing is written back: the
conversation lives as long as the panel does.

The node harness from test_goal_ui_bridge holds that contract; the server
side of the same feature is in test_projects.
"""
import json
import unittest

from test_goal_ui_bridge import NODE, BridgeTestCase
from test_project_ui import PRELUDE, chat_state


# A highlight, the way a browser reports one: the range's own rectangle,
# and the node the cursor finished in so the goal can be read back off it.
def selection_js(text, node="tree", rect=None):
    rect = rect or {"left": 210, "right": 320, "top": 140, "bottom": 156,
                    "width": 110, "height": 16}
    return (
        "window.getSelection = function () {"
        "  return { isCollapsed: false, rangeCount: 1,"
        "    anchorNode: %s, focusNode: %s,"
        "    toString: function () { return %s; },"
        "    getRangeAt: function () {"
        "      return { getBoundingClientRect: function () { return %s; } }; } };"
        "};" % (node, node, json.dumps(text), json.dumps(rect)))


# The tree the artifact draws: a row carrying the goal it stands for, and a
# rail row that carries none, so the open goal has to stand in for it.
TREE = (
    "var row = document.createElement('div');"
    "row.className = 'hc-row';"
    "row.setAttribute('data-hc-goal', 'g1a');"
    "var title = document.createElement('span');"
    "title.className = 'hc-row-title';"
    "row.appendChild(title);"
    "app.appendChild(row);"
    "var rail = document.createElement('span');"
    "rail.className = 'hc-todo-line';"
    "app.appendChild(rail);"
    "var tree = title;"
)

ANSWERED = (
    "window.fetch = function (url, opts) {"
    "  calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
    "  return Promise.resolve({ ok: true, json: function () {"
    "    return Promise.resolve({ ok: true, asked: 'why?',"
    "      answer: 'Because **it** is the head of the family.' }); } });"
    "};"
)

# A server that answers every question with a different sentence, so a
# panel two questions deep can be told from one that redrew the first.
COUNTED = (
    "var nth = 0;"
    "window.fetch = function (url, opts) {"
    "  calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
    "  nth += 1;"
    "  var said = 'Answer ' + nth + '.';"
    "  return Promise.resolve({ ok: true, json: function () {"
    "    return Promise.resolve({ ok: true, asked: 'q', answer: said }); } });"
    "};"
)

REFUSED = (
    "window.fetch = function (url, opts) {"
    "  calls.push([String(url), opts && opts.body ? JSON.parse(opts.body) : null]);"
    "  return Promise.resolve({ ok: true, json: function () {"
    "    return Promise.resolve({ ok: false, error: 'ask something first' }); } });"
    "};"
)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class HighlightOfferTests(BridgeTestCase):
    """The offer beside a live highlight."""

    def offer(self, tail, text="build the router", node="tree", extra=""):
        return json.loads(self.run_js(
            PRELUDE + TREE
            + "P.acceptState(%s);" % json.dumps(chat_state())
            + "P.askSelection.watch();"
            + selection_js(text, node) + extra
            + "P.askSelection.renderPill();"
            + tail))

    def test_a_highlight_offers_to_be_asked_about(self):
        got = self.offer(
            "var pill = P.askSelection.pill();"
            "JSON.stringify([!!pill, deepText(pill), pill.__hcWhat.text,"
            " pill.__hcWhat.goal, pill.style.left, pill.style.top,"
            " pill.parentNode === document.body]);")
        # Placed under the highlight, carrying which goal it landed in, and
        # parented outside the subtree the artifact redraws.
        self.assertEqual([True, "✦Ask Claude", "build the router", "g1a",
                          "210px", "162px", True], got)

    def test_nothing_is_offered_for_a_highlight_too_small_to_be_a_question(self):
        got = self.offer(
            "JSON.stringify([P.askSelection.pill() === null]);", text=" x ")
        self.assertEqual([True], got)

    def test_a_collapsed_selection_takes_the_offer_away_again(self):
        got = self.offer(
            "var had = !!P.askSelection.pill();"
            "window.getSelection = function () { return { isCollapsed: true }; };"
            "P.askSelection.renderPill();"
            "JSON.stringify([had, P.askSelection.pill() === null]);")
        self.assertEqual([True, True], got)

    def test_a_highlight_outside_a_goal_row_stands_for_the_open_goal(self):
        # The rail and the panes draw no row of their own; the goal they are
        # all showing is the one the question is about.
        got = self.offer(
            "JSON.stringify([P.askSelection.pill().__hcWhat.goal]);",
            node="rail",
            extra="store['hc-vault-ui-v1'] = JSON.stringify({selId: 'g1'});")
        self.assertEqual(["g1"], got)

    def test_an_open_panel_holds_the_passage_it_was_opened_on(self):
        # Clicking into the question field moves the highlight, and the
        # panel would reopen on whatever it moved to if the offer still
        # answered while one was up.
        got = self.offer(
            "P.askSelection.open(P.askSelection.pill().__hcWhat);"
            "var was = P.askSelection.box();"
            + selection_js("something else entirely") +
            "var again = P.askSelection.renderPill();"
            "JSON.stringify([again, P.askSelection.pill() === null,"
            " P.askSelection.box() === was,"
            " was.querySelector('.hc-sel-quote').textContent]);")
        self.assertEqual([False, True, True, "build the router"], got)


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class HighlightPanelTests(BridgeTestCase):
    """The panel, once the offer is taken."""

    def panel(self, tail, fetch=ANSWERED, question="why this one?"):
        return json.loads(self.run_js(
            PRELUDE + TREE + fetch
            + "P.acceptState(%s);" % json.dumps(chat_state())
            + "P.askSelection.watch();"
            + selection_js("build the router")
            + "P.askSelection.renderPill();"
            + "P.askSelection.open(P.askSelection.pill().__hcWhat);"
            + "var box = P.askSelection.box();"
            + "box.querySelector('[data-hc-sel-field]').value = %s;"
              % json.dumps(question)
            + tail))

    def test_the_panel_quotes_the_passage_it_was_opened_on(self):
        got = self.panel(
            "JSON.stringify([!!box, box.querySelector('.hc-sel-title').textContent,"
            " box.querySelector('.hc-sel-quote').textContent,"
            " box.querySelector('.hc-sel-field').getAttribute('placeholder'),"
            " box.querySelector('.hc-sel-go').textContent,"
            " P.askSelection.pill() === null,"
            " box.parentNode === document.body]);")
        self.assertEqual([True, "Ask Claude", "build the router",
                          "Ask about this — ⌘↩ to send", "Ask", True,
                          True], got)

    def test_the_question_carries_the_passage_and_its_goal_to_the_server(self):
        got = self.panel(
            "P.askSelection.run();"
            "later(function () {"
            "  var sent = calls.filter(function (c) {"
            "    return c[0].indexOf('/api/ask_selection') >= 0; });"
            "  return JSON.stringify([sent.length, sent[0][1],"
            "    texts(box, 'hc-sel-q'),"
            "    deepText(box.querySelector('.hc-sel-a')),"
            "    box.querySelector('[data-hc-sel-say]').textContent]);"
            "});")
        # The question stays above the answer it got: a panel is read as a
        # conversation, so nothing in it is anonymous.
        self.assertEqual(
            [1, {"text": "build the router", "goal": "g1a",
                 "question": "why this one?", "turns": []},
             ["why this one?"],
             "Because it is the head of the family.", ""], got)

    def test_a_question_with_no_words_never_leaves_the_page(self):
        got = self.panel(
            "var ran = P.askSelection.run();"
            "JSON.stringify([ran, calls.filter(function (c) {"
            "  return c[0].indexOf('/api/ask_selection') >= 0; }).length,"
            " box.querySelector('[data-hc-sel-say]').textContent,"
            " box.querySelector('[data-hc-sel-say]').getAttribute('data-hc-bad')]);",
            question="   ")
        self.assertEqual([False, 0, "ask something first", ""], got)

    def test_what_the_server_refuses_is_said_where_the_answer_would_be(self):
        got = self.panel(
            "P.askSelection.run();"
            "later(function () {"
            "  return JSON.stringify(["
            "    box.querySelector('[data-hc-sel-say]').textContent,"
            "    box.querySelector('[data-hc-sel-say]').getAttribute('data-hc-bad'),"
            "    deepText(box.querySelector('[data-hc-sel-out]'))]);"
            "});", fetch=REFUSED)
        self.assertEqual(["ask something first", "", ""], got)

    def test_an_answer_leaves_the_panel_open_for_the_next_question(self):
        got = self.panel(
            "P.askSelection.run();"
            "later(function () {"
            "  box.querySelector('[data-hc-sel-field]').value = 'and then?';"
            "  P.askSelection.run();"
            "  return later(function () {"
            "    var sent = calls.filter(function (c) {"
            "      return c[0].indexOf('/api/ask_selection') >= 0; });"
            "    return JSON.stringify([P.askSelection.box() === box,"
            "      sent.length, sent[1][1].question, sent[1][1].turns,"
            "      texts(box, 'hc-sel-q'), texts(box, 'hc-sel-a')]);"
            "  });"
            "});", fetch=COUNTED)
        # The second question is a follow-up: it goes out with what the
        # panel has already been told, and both turns stay on screen.
        self.assertEqual(
            [True, 2, "and then?",
             [{"question": "why this one?", "answer": "Answer 1."}],
             ["why this one?", "and then?"], ["Answer 1.", "Answer 2."]], got)

    def test_an_answered_question_empties_the_field_for_the_next(self):
        got = self.panel(
            "P.askSelection.run();"
            "later(function () {"
            "  return JSON.stringify(["
            "    box.querySelector('[data-hc-sel-field]').value,"
            "    box.querySelector('.hc-sel-field')"
            "      .getAttribute('placeholder')]);"
            "});")
        self.assertEqual(["", "Ask a follow-up — ⌘↩ to send"], got)

    def test_a_refused_question_goes_back_where_it_was_typed(self):
        # Nothing to retype, and no half turn left in the conversation to be
        # quoted back at the model as one it had already had.
        got = self.panel(
            "P.askSelection.run();"
            "later(function () {"
            "  return JSON.stringify(["
            "    box.querySelector('[data-hc-sel-field]').value,"
            "    P.askSelection.turns().length,"
            "    box.querySelector('[data-hc-sel-say]').textContent]);"
            "});", fetch=REFUSED)
        self.assertEqual(["why this one?", 0, "ask something first"], got)

    def test_a_click_in_the_workspace_spares_a_conversation(self):
        # An empty panel is one highlight away from coming back; a panel
        # with an answer in it is not, so reading the tree beside it must
        # not be what takes it away.
        got = self.panel(
            "fire('mousedown', tree);"
            "var gone = P.askSelection.box() === null;"
            "P.askSelection.open({ text: 'build the router', goal: 'g1a' });"
            "var again = P.askSelection.box();"
            "again.querySelector('[data-hc-sel-field]').value = 'why?';"
            "P.askSelection.run();"
            "later(function () {"
            "  fire('mousedown', tree);"
            "  return JSON.stringify([gone, P.askSelection.box() === again,"
            "    texts(again, 'hc-sel-a')]);"
            "});")
        self.assertEqual(
            [True, True, ["Because it is the head of the family."]], got)

    def test_closing_the_panel_takes_the_answer_with_it(self):
        # A reading aid, not a transcript: nothing about it is kept, and an
        # answer still in flight is not drawn into a panel that has gone.
        got = self.panel(
            "P.askSelection.run();"
            "P.askSelection.close();"
            "later(function () {"
            "  return JSON.stringify([P.askSelection.box() === null,"
            "    P.askSelection.held() === null,"
            "    box.parentNode === null]);"
            "});")
        self.assertEqual([True, True, True], got)


if __name__ == "__main__":
    unittest.main()
