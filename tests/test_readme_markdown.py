"""The README pane, read rather than transcribed.

A project's front page is markdown, and a pane that showed the source made
the reader do the parsing. This renders the part of markdown a README
actually uses -- headings, code, lists, quotes, rules, links -- as DOM
nodes, so nothing in the file can become markup on the page.
"""
import json
import unittest

from test_goal_ui_bridge import BridgeTestCase, NODE


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class MarkdownTests(BridgeTestCase):

    def md(self, text, tail="shape(node)"):
        return json.loads(self.run_js(
            "var P = window.__hcPromptUI;"
            "var shape = function (n) { return (n.children || []).map(function (c) {"
            "  return [String(c.className || c.tagName), deepText(c)]; }); };"
            "var deepText = function (n) { return String(n.textContent || '') +"
            "  (n.children || []).map(deepText).join(''); };"
            "var node = P.markdownNode(%s);"
            "JSON.stringify(%s);" % (json.dumps(text), tail)))

    def test_headings_paragraphs_and_rules(self):
        got = self.md("# Engelbart\n\n### Tools for steering agents.\n\n---\n\ntext")
        self.assertEqual([["hc-md-h hc-md-h1", "Engelbart"],
                          ["hc-md-h hc-md-h3", "Tools for steering agents."],
                          ["hc-md-rule", ""],
                          ["hc-md-p", "text"]], got)

    def test_a_wrapped_paragraph_is_one_paragraph(self):
        got = self.md("one\ntwo\n\nthree")
        self.assertEqual([["hc-md-p", "one two"], ["hc-md-p", "three"]], got)

    def test_raw_html_lines_are_dropped_not_shown(self):
        # A README's <div align="center"> is layout for GitHub and noise
        # here -- and must never be interpreted as markup.
        got = self.md('<div align="center">\n\n# Engelbart\n\n</div>')
        self.assertEqual([["hc-md-h hc-md-h1", "Engelbart"]], got)

    def test_a_fenced_block_is_kept_whole_with_its_language(self):
        got = self.md("```python\nif x:\n    go()\n```\nafter",
                      "[shape(node), node.children[0].getAttribute('data-hc-lang')]")
        self.assertEqual([[["hc-md-pre", "if x:\n    go()"],
                           ["hc-md-p", "after"]], "python"], got)

    def test_an_unterminated_fence_does_not_eat_the_page(self):
        got = self.md("```\nstuck")
        self.assertEqual([["hc-md-pre", "stuck"]], got)

    def test_bullets_and_numbers_become_one_list(self):
        got = self.md("- one\n- two\n\n1. first\n2. second",
                      "node.children.map(function (l) { return (l.children || [])"
                      "  .map(function (i) { return (i.children || [])"
                      "    .map(function (s) { return deepText(s); }); }); })")
        self.assertEqual([[["•", "one"], ["•", "two"]],
                          [["1.", "first"], ["2.", "second"]]], got)

    def test_quotes_and_tables_read_as_lines(self):
        got = self.md("> mind you\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual([["hc-md-quote", "mind you"],
                          ["hc-md-row", "a  ·  b"],
                          ["hc-md-row", "1  ·  2"]], got)

    def test_the_inline_run_of_code_bold_italic_and_links(self):
        got = self.md("run `hc chat-ui` for **goals** and *todos*",
                      "(node.children[0].children || []).map(function (c) {"
                      "  return [String(c.className || c.tagName), deepText(c)]; })")
        self.assertEqual([["span", "run "], ["hc-md-code", "hc chat-ui"],
                          ["span", " for "], ["strong", "goals"],
                          ["span", " and "], ["em", "todos"]], got)

    def test_links_carry_their_target_and_a_bare_url_becomes_one(self):
        got = self.md("see [the docs](https://example.com/x) and https://example.com/y",
                      "(node.children[0].children || []).filter(function (c) {"
                      "  return String(c.tagName) === 'a'; }).map(function (a) {"
                      "  return [deepText(a), a.getAttribute('href'),"
                      "          a.getAttribute('rel')]; })")
        self.assertEqual([["the docs", "https://example.com/x", "noreferrer noopener"],
                          ["https://example.com/y", "https://example.com/y",
                           "noreferrer noopener"]], got)

    def test_a_link_to_a_scheme_the_pane_will_not_follow_is_plain_text(self):
        # javascript: in a file on disk is the one thing a renderer must not
        # hand to the browser.
        got = self.md("[click](javascript:alert(1))",
                      "(node.children[0].children || []).map(function (c) {"
                      "  return [String(c.tagName), deepText(c)]; })")
        # The target closes at the first ")", so the one inside alert(1)
        # ends it and the last paren is left as text. What matters is that
        # no anchor is built.
        self.assertEqual([["span", "click"], ["span", ")"]], got)

    def test_an_image_is_named_not_fetched(self):
        # The pane reads a file from disk; it has no business pulling
        # anything over the network.
        got = self.md("![a diagram](https://example.com/a.png)",
                      "(node.children[0].children || []).map(function (c) {"
                      "  return [String(c.className || c.tagName), deepText(c)]; })")
        self.assertEqual([["hc-md-img", "a diagram"]], got)


if __name__ == "__main__":
    unittest.main()
