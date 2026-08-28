"""Brainstorming: setup's conversation, reopened inside a project.

The same four question shapes and the same cards as the cold-start setup, so
these tests are mostly about the two things that make it a different screen:
there is no sequence to walk, and nothing is written into the reader's tree
until they say so. The third is the project itself -- the goals and how far
their rows have got, carried into every turn, condensed once and cached so
the next brainstorm of the same project does not pay for it again.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from human_compact.trajectory import brainstorm as BS  # noqa: E402
from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import setup_chat as SC  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_chat_ui_server import browser_executable  # noqa: E402
from test_goal_ui_bridge import BridgeTestCase, NODE  # noqa: E402
from test_project_ui import PRELUDE, chat_state, fetch_js  # noqa: E402


def tree(*goals):
    doc = {"version": 1, "goals": list(goals)}
    GM.sanitize(doc)
    return doc


def goal(gid, title, parent=None, status="active", rows=()):
    made = GM.new_goal(gid, title, parent, origin="user")
    made["status"] = status
    made["todo_items"] = [dict(r) for r in rows]
    return made


class FormTests(unittest.TestCase):
    """What the model is told it may send, and what it is told not to do."""

    def test_the_prompt_names_every_card_the_panel_can_draw(self):
        form = "\n".join(BS.FORM)
        for card in ("questions", "focus", "goals", "todos", "offer"):
            self.assertIn(card, form)

    def test_the_prompt_says_there_is_no_order_to_walk(self):
        # Setup's whole shape is four cards in one order. This screen is the
        # same conversation without that, and a model that walks a stranger
        # through onboarding inside a project they already have is the
        # failure the sentence is there to prevent.
        form = "\n".join(BS.FORM).lower()
        self.assertIn("no fixed order", form)

    def test_the_prompt_forbids_writing_before_it_has_been_asked_for(self):
        form = "\n".join(BS.FORM).lower()
        self.assertIn("nothing you write is saved unless they ask", form)
        self.assertIn("offer", form)

    def test_the_project_goes_above_the_conversation_not_inside_it(self):
        lines = BS.compose([{"role": "you", "text": "what next"}],
                           "# Its goals\n- [active] Ship the rail")
        body = "\n".join(lines)
        self.assertLess(body.index("Ship the rail"), body.index("what next"))

    def test_a_conversation_is_bounded_from_the_oldest_end(self):
        many = [{"role": "you", "text": "turn %d" % i} for i in range(80)]
        body = "\n".join(BS.compose(many))
        self.assertIn("turn 79", body)
        self.assertNotIn("turn 0\n", body)


class CardTests(unittest.TestCase):
    """Whatever came back, as the one shape the panel draws."""

    def test_a_reply_with_nothing_in_it_is_a_card_of_none(self):
        self.assertEqual("none", BS.normalize_card({})["card"])
        self.assertEqual("none", BS.normalize_card("not an object")["card"])

    def test_a_card_it_does_not_know_is_none(self):
        self.assertEqual("none", BS.normalize_card({"card": "explode",
                                                    "say": "hi"})["card"])

    def test_prose_alone_is_a_card_of_none_that_still_says_something(self):
        out = BS.normalize_card({"card": "none", "say": "Tell me more."})
        self.assertEqual("none", out["card"])
        self.assertEqual("Tell me more.", out["say"])

    def test_every_question_shape_survives_with_what_it_needs(self):
        out = BS.normalize_card({"card": "questions", "questions": {
            "eyebrow": "two questions", "items": [
                {"id": "who", "type": "mcq", "title": "Who is it for?",
                 "options": [{"label": "you", "why": "fastest"},
                             {"label": "a team"}]},
                {"id": "why", "type": "open", "title": "What is in the way?",
                 "placeholder": "the constraint nobody wrote down"}]}})
        self.assertEqual("questions", out["card"])
        items = out["questions"]["items"]
        self.assertEqual(["mcq", "open"], [q["type"] for q in items])
        self.assertEqual("fastest", items[0]["options"][0]["why"])
        self.assertEqual("the constraint nobody wrote down",
                         items[1]["placeholder"])

    def test_a_choice_with_nothing_to_choose_from_becomes_a_box_to_type_in(self):
        # Setup's rule, inherited: a question the reader cannot answer is
        # worse than an open one.
        out = BS.normalize_card({"card": "questions", "questions": {"items": [
            {"id": "a", "type": "mcq", "title": "Which?", "options": []}]}})
        self.assertEqual("free", out["questions"]["items"][0]["type"])

    def test_a_focus_card_carries_its_options_and_their_arguments(self):
        out = BS.normalize_card({"card": "focus", "focus": {
            "title": "Which reading is it?",
            "options": [{"label": "the rail", "why": "it is what you touch"},
                        {"label": "the store"}]}})
        self.assertEqual("focus", out["card"])
        self.assertEqual("Which reading is it?", out["focus"]["title"])
        self.assertEqual("it is what you touch",
                         out["focus"]["options"][0]["why"])

    def test_a_focus_with_one_option_is_not_a_choice_and_is_refused(self):
        # One reading offered is the model telling the reader what it
        # decided, which is what prose is for.
        out = BS.normalize_card({"card": "focus", "say": "I think it is the rail.",
                                 "focus": {"options": [{"label": "the rail"}]}})
        self.assertEqual("none", out["card"])
        self.assertEqual("I think it is the rail.", out["say"])

    def test_goals_keep_the_pieces_they_break_into(self):
        out = BS.normalize_card({"card": "goals", "goals": [
            {"label": "Make the rail readable", "why": "you look at it most",
             "subgoals": ["Fold the context", {"label": "Widen the tabs"}]}]})
        self.assertEqual("goals", out["card"])
        self.assertEqual("Make the rail readable", out["goals"][0]["label"])
        self.assertEqual(["Fold the context", "Widen the tabs"],
                         [k["label"] for k in out["goals"][0]["subgoals"]])

    def test_an_offer_may_only_be_of_goals_or_of_rows(self):
        self.assertEqual("todos", BS.normalize_card(
            {"card": "offer", "offer": "todos", "say": "ready?"})["offer"])
        self.assertEqual("none", BS.normalize_card(
            {"card": "offer", "offer": "a plan", "say": "ready?"})["card"])

    def test_a_payload_sent_with_no_envelope_is_still_read(self):
        # Told to send one card, the model sometimes sends the payload at
        # the top level. It has answered; only the wrapper is missing.
        self.assertEqual("questions", BS.normalize_card(
            {"items": [{"id": "a", "type": "free", "title": "What?"}]})["card"])
        self.assertEqual("goals", BS.normalize_card(
            [{"label": "Ship it"}])["card"])
        self.assertEqual("todos", BS.normalize_card(
            ["Add the route", "Switch the client"])["card"])

    def test_rows_under_pieces_and_rows_on_their_own_both_arrive(self):
        out = BS.normalize_card({"card": "todos", "subgoals": [
            {"label": "The rail", "todos": ["Fold the context"]}]})
        self.assertEqual([["The rail", ["Fold the context"]]],
                         [[p["label"], p["todos"]] for p in out["subgoals"]])
        flat = BS.normalize_card({"card": "todos", "todos": ["Do the thing"]})
        self.assertEqual(["Do the thing"], flat["todos"])

    def test_a_piece_with_no_rows_under_it_is_a_heading_and_is_dropped(self):
        out = BS.normalize_card({"card": "todos", "todos": ["Do the thing"],
                                 "subgoals": [{"label": "Later", "todos": []}]})
        self.assertEqual([], out["subgoals"])


class NoSequenceTests(unittest.TestCase):
    """The difference from setup, held in one place."""

    class Engine:
        def __init__(self, reply):
            self.reply = reply
            self.asked = []

        def generate_json(self, prompt):
            self.asked.append(prompt)
            return self.reply

    def test_rows_on_the_very_first_turn_are_drawn_not_discarded(self):
        # Setup would refuse this: its first card must be questions. Here the
        # reader has the tree in front of them and the model's judgement
        # about what to send is the thing being asked for.
        engine = self.Engine({"card": "todos", "say": "here they are",
                              "todos": ["Fold the context"]})
        out = BS.ask([{"role": "you", "text": "break the rail work down"}],
                     engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("todos", out["card"])
        self.assertEqual(["Fold the context"], out["todos"])

    def test_a_round_that_came_back_with_nothing_says_so(self):
        out = BS.ask([], engine=self.Engine({}))
        self.assertFalse(out["ok"])
        self.assertIn("nothing", out["error"])

    def test_the_project_reaches_the_prompt_the_model_is_actually_sent(self):
        engine = self.Engine({"card": "none", "say": "noted"})
        BS.ask([{"role": "you", "text": "what next"}],
               "# Its goals\n- [in_progress] Refactor how the TODOs work",
               engine=engine)
        self.assertIn("Refactor how the TODOs work", engine.asked[0])

    def test_it_runs_on_the_readers_own_account_the_way_setup_does(self):
        # No key of ours: the same provider, named the same way, asking for
        # whichever sonnet this install's account is allowed.
        from unittest import mock
        from human_compact.trajectory import providers as PROVIDERS

        engine = self.Engine({"card": "none", "say": "noted"})
        with mock.patch.object(PROVIDERS, "make",
                               return_value=engine) as made:
            with mock.patch.object(SC, "setup_model", return_value="sonnet-x"):
                out = BS.ask([{"role": "you", "text": "hello"}])
        self.assertTrue(out["ok"], out)
        self.assertEqual(("claude", "synthesize", "sonnet-x"),
                         made.call_args[0])

    def test_a_provider_that_cannot_be_reached_is_reported_as_itself(self):
        from human_compact.trajectory import providers as PROVIDERS

        class Broken:
            def generate_json(self, prompt):
                raise PROVIDERS.ProviderError("claude is not on PATH")

        out = BS.ask([], engine=Broken())
        self.assertFalse(out["ok"])
        self.assertIn("not on PATH", out["error"])


class DigestTests(unittest.TestCase):
    """The project, as the model is given it."""

    def setUp(self):
        self.doc = tree(
            goal("g1", "Refactor how the TODOs work", status="in_progress",
                 rows=[{"id": "t1", "text": "Add back prompt editor",
                        "status": "done"},
                       {"id": "t2", "text": "Default hide the context",
                        "status": ""}]),
            goal("g11", "UI Changes", parent="g1"),
            goal("g2", "Onboarding"))

    def test_it_carries_the_tree_its_statuses_and_where_the_rows_got_to(self):
        said = BS.digest(self.doc, {"name": "engelbart",
                                    "objective": "plan better"})
        self.assertIn("engelbart", said)
        self.assertIn("plan better", said)
        self.assertIn("[in_progress] Refactor how the TODOs work", said)
        self.assertIn("Add back prompt editor", said)
        self.assertIn("1 done", said)
        self.assertIn("1 not sent", said)

    def test_the_tree_keeps_its_shape_so_a_subgoal_reads_as_one(self):
        said = BS.digest(self.doc, {})
        rows = [ln for ln in said.split("\n") if "UI Changes" in ln]
        self.assertTrue(rows[0].startswith("  - "), rows)

    def test_a_project_with_nothing_in_it_says_so_rather_than_nothing(self):
        self.assertIn("(none yet)", BS.digest(tree(), {}))

    def test_it_is_bounded_however_large_the_tree_is(self):
        many = tree(*[goal("g%d" % i, "Goal %d" % i,
                           rows=[{"id": "t%d-%d" % (i, n),
                                  "text": "row %d %d" % (i, n), "status": ""}
                                 for n in range(30)])
                      for i in range(1, 90)])
        said = BS.digest(many, {})
        self.assertLessEqual(len(said), BS.MAX_CTX_CHARS)

    def test_only_the_first_few_rows_of_a_long_list_are_named(self):
        long = tree(goal("g1", "Long", rows=[
            {"id": "t%d" % n, "text": "row %d" % n, "status": ""}
            for n in range(20)]))
        said = BS.digest(long, {})
        self.assertIn("… %d more" % (20 - BS.MAX_CTX_ROWS), said)


class CacheTests(unittest.TestCase):
    """Condensed once, and reused by every later brainstorm of the project."""

    class Engine:
        def __init__(self, said="a short project summary"):
            self.said = said
            self.calls = 0

        def generate_json(self, prompt):
            self.calls += 1
            return {"context": self.said}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = str(self.root / "app")
        Path(self.cwd).mkdir()

    def test_a_short_project_is_sent_as_it_stands(self):
        engine = self.Engine()
        raw = "# Its goals\n- [active] Ship it"
        self.assertEqual(raw, BS.project_context(self.root, self.cwd, raw,
                                                 engine))
        self.assertEqual(0, engine.calls)

    def test_a_long_one_is_condensed_once_and_read_from_disk_after(self):
        engine = self.Engine()
        raw = "x " * BS.CONDENSE_OVER
        first = BS.project_context(self.root, self.cwd, raw, engine)
        self.assertEqual("a short project summary", first)
        self.assertEqual(1, engine.calls)
        again = BS.project_context(self.root, self.cwd, raw, engine)
        self.assertEqual("a short project summary", again)
        self.assertEqual(1, engine.calls)

    def test_the_cache_is_the_projects_so_any_chat_of_it_reads_the_same_one(self):
        engine = self.Engine()
        raw = "x " * BS.CONDENSE_OVER
        BS.project_context(self.root, self.cwd, raw, engine)
        held = BS.load_cache(self.root, self.cwd)
        self.assertEqual("a short project summary", held["context"])
        self.assertTrue(str(BS._cache_path(self.root, self.cwd)).endswith(
            ".brainstorm.json"))

    def test_a_tree_that_changed_throws_the_condensation_away(self):
        engine = self.Engine()
        BS.project_context(self.root, self.cwd, "x " * BS.CONDENSE_OVER, engine)
        engine.said = "a different summary"
        out = BS.project_context(self.root, self.cwd,
                                 "y " * BS.CONDENSE_OVER, engine)
        self.assertEqual("a different summary", out)
        self.assertEqual(2, engine.calls)

    def test_a_model_that_cannot_be_reached_leaves_the_raw_digest(self):
        class Broken:
            def generate_json(self, prompt):
                raise RuntimeError("no")

        raw = "x " * BS.CONDENSE_OVER
        out = BS.project_context(self.root, self.cwd, raw, Broken())
        self.assertEqual(raw[:BS.CONDENSE_OVER], out)


class WriteTests(unittest.TestCase):
    """What approving a card puts in the tree."""

    def test_goals_land_as_roots_with_their_pieces_under_them(self):
        doc = tree(goal("g1", "Already here"))
        made = BS.apply_goals(doc, [
            {"label": "Make the rail readable", "why": "you look at it most",
             "subgoals": ["Fold the context", "Widen the tabs"]}])
        self.assertEqual(3, made)
        titles = [g["title"] for g in doc["goals"]]
        self.assertIn("Make the rail readable", titles)
        parent = [g for g in doc["goals"]
                  if g["title"] == "Make the rail readable"][0]
        self.assertIsNone(parent["parent_goal_id"])
        self.assertEqual("you look at it most", parent["description"])
        kids = [g["title"] for g in doc["goals"]
                if g["parent_goal_id"] == parent["id"]]
        self.assertEqual(["Fold the context", "Widen the tabs"], kids)

    def test_a_goal_the_reader_approved_is_theirs_not_the_machines(self):
        doc = tree()
        BS.apply_goals(doc, [{"label": "Ship it"}])
        self.assertEqual("user", doc["goals"][0]["origin"])

    def test_flat_rows_hang_on_the_goal_they_were_sent_to(self):
        doc = tree(goal("g1", "The rail"))
        written = BS.apply_todos(doc, "g1", ["Fold the context", "Widen tabs"])
        self.assertEqual(2, written)
        rows = GM.by_id(doc, "g1")["todo_items"]
        self.assertEqual(["Fold the context", "Widen tabs"],
                         [r["text"] for r in rows])

    def test_rows_arrive_unsent_because_writing_work_down_starts_none(self):
        doc = tree(goal("g1", "The rail"))
        BS.apply_todos(doc, "g1", ["Fold the context"])
        self.assertEqual("", GM.by_id(doc, "g1")["todo_items"][0]["status"])

    def test_pieces_become_children_carrying_their_own_rows(self):
        doc = tree(goal("g1", "The rail"))
        written = BS.apply_todos(doc, "g1", [], [
            {"label": "The tabs", "todos": ["Widen them", "Wrap them"]}])
        self.assertEqual(2, written)
        kid = [g for g in doc["goals"] if g["parent_goal_id"] == "g1"][0]
        self.assertEqual("The tabs", kid["title"])
        self.assertEqual(["Widen them", "Wrap them"],
                         [r["text"] for r in kid["todo_items"]])

    def test_rows_written_show_up_in_the_markdown_the_rail_reads(self):
        doc = tree(goal("g1", "The rail"))
        BS.apply_todos(doc, "g1", ["Fold the context"])
        self.assertIn("Fold the context", GM.by_id(doc, "g1")["todos_md"])

    def test_a_goal_that_is_not_there_is_written_to_by_nothing(self):
        doc = tree(goal("g1", "The rail"))
        self.assertEqual(0, BS.apply_todos(doc, "g99", ["Anything"]))
        self.assertEqual([], GM.by_id(doc, "g1")["todo_items"])


class OpTests(unittest.TestCase):
    """Through the workspace's own door, the way the panel asks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-brainstorm"
        paths = CS.paths(self.session, self.root)
        paths.session_dir.mkdir(parents=True)
        self.project = self.root / "app"
        self.project.mkdir()
        paths.manifest.write_text(json.dumps({"cwd": str(self.project)}))
        doc = tree(goal("g1", "Refactor how the TODOs work",
                        status="in_progress",
                        rows=[{"id": "t1", "text": "Add back prompt editor",
                               "status": "done"}]))
        paths.goals.write_text(json.dumps(doc))
        paths.important.write_text(json.dumps({"items": []}))
        paths.prompts.write_text(json.dumps({"prompts": []}))
        PS.save_project(self.root, str(self.project),
                        {"name": "engelbart", "objective": "plan better"})
        self.dir = paths.session_dir

    def op(self, **body):
        return ui._apply_locked(body, trajdir=self.dir, chat_scoped=True)

    def goals_now(self):
        return CS.load_goals(self.session, self.root)[0]

    def test_saying_something_runs_outside_the_state_lock(self):
        out = self.op(op="brainstorm_say", transcript=[])
        self.assertIn("__deferred__", out)
        self.assertEqual("brainstorm_say", out["__deferred__"][0])

    def test_the_deferred_call_carries_the_project_and_its_tree(self):
        out = self.op(op="brainstorm_say", transcript=[])
        kind, _sid, root, cwd, held = out["__deferred__"]
        # Resolved, the way the store spells it: /var is a symlink to
        # /private/var on macOS and the cache is keyed by the real path.
        self.assertEqual(str(self.project.resolve()), cwd)
        self.assertEqual(self.root.resolve(), root)
        self.assertIn("Refactor how the TODOs work", held["__digest__"])
        self.assertIn("plan better", held["__digest__"])

    def test_a_transcript_that_is_not_a_list_is_refused_at_the_door(self):
        out = self.op(op="brainstorm_say", transcript="words")
        self.assertFalse(out["ok"])
        self.assertIn("transcript", out["error"])

    def test_approved_goals_are_written_into_the_chats_own_tree(self):
        out = self.op(op="brainstorm_apply", goals=[
            {"label": "Make onboarding land", "subgoals": ["Write the copy"]}])
        self.assertTrue(out["ok"], out)
        self.assertEqual(2, out["goals"])
        titles = [g["title"] for g in self.goals_now()["goals"]]
        self.assertIn("Make onboarding land", titles)
        self.assertIn("Write the copy", titles)

    def test_approved_rows_are_hung_on_the_goal_the_panel_named(self):
        out = self.op(op="brainstorm_apply", goal_id="g1",
                      todos=["Remove the token counter"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(1, out["todos"])
        rows = GM.by_id(self.goals_now(), "g1")["todo_items"]
        self.assertIn("Remove the token counter", [r["text"] for r in rows])

    def test_rows_sent_to_a_goal_that_is_not_there_are_refused(self):
        out = self.op(op="brainstorm_apply", goal_id="g99", todos=["Anything"])
        self.assertFalse(out["ok"])
        self.assertIn("no such goal", out["error"])

    def test_a_write_with_nothing_in_it_is_refused_rather_than_saved(self):
        out = self.op(op="brainstorm_apply", goals=[], todos=[])
        self.assertFalse(out["ok"])
        self.assertIn("nothing", out["error"])

    def test_a_shape_that_is_not_a_list_is_refused_before_anything_is_written(self):
        out = self.op(op="brainstorm_apply", goals="Ship it")
        self.assertFalse(out["ok"])
        self.assertIn("goals must be a list", out["error"])

    def test_neither_op_is_offered_to_a_vault_with_no_chat_behind_it(self):
        for kind in ("brainstorm_say", "brainstorm_apply"):
            out = ui._apply_locked({"op": kind, "transcript": []},
                                   trajdir=None, chat_scoped=False)
            self.assertFalse(out.get("ok"), kind)
            self.assertIn("chat scope", out["error"])


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class PanelTests(BridgeTestCase):
    """The third view: what it shows, what it covers, and what it leaves.

    The workspace's own node harness, with the project chip in the header
    the way the project tests set it up -- the brainstorm is a view of a
    project, so a workspace with no project has nothing to open.
    """

    # A tree with a subgoal in it, so the picker that says where approved
    # rows land has more than one place to offer.
    TREE = [{"id": "g1", "title": "Refactor how the TODOs work",
             "status": "active",
             "children": [{"id": "g11", "title": "UI Changes",
                           "status": "active"}]}]

    def api(self, tail, project=True):
        state = chat_state(project)
        state["goals"] = json.loads(json.dumps(self.TREE))
        return self.run_js(
            PRELUDE + fetch_js()
            + "store[%s] = JSON.stringify({goals: %s, selId: 'g1'});"
              % (json.dumps("hc-vault-ui-v1"), json.dumps(self.TREE))
            + "P.acceptState(%s); P.renderProjectChip();" % json.dumps(state)
            + "var bs = P.brainstorm;" + tail,
            state=state)

    def test_the_tab_row_offers_three_views_not_two(self):
        out = self.api(
            "P.renderViewTabs();"
            "out = (document.querySelector('.hc-viewtabs').children || [])"
            "  .map(function (t) { return t.getAttribute('data-hc-viewtab'); });")
        self.assertEqual(["overview", "goals", "brainstorm"], out)

    def test_opening_it_marks_the_root_and_lights_its_tab(self):
        out = self.api(
            "bs.open();"
            "P.renderViewTabs();"
            "out = {on: bs.shown(),"
            " lit: (document.querySelector('.hc-viewtabs').children || [])"
            "   .filter(function (t) { return t.getAttribute('data-hc-on')"
            "     !== null; })"
            "   .map(function (t) { return t.getAttribute('data-hc-viewtab'); })};")
        self.assertTrue(out["on"])
        self.assertEqual(["brainstorm"], out["lit"])

    def test_it_opens_on_an_invitation_rather_than_on_a_blank_page(self):
        out = self.api("bs.open(); out = bs.state().msgs;")
        self.assertEqual(1, len(out))
        self.assertEqual("engelbart", out[0]["role"])
        self.assertIn("on your mind", out[0]["text"])

    def test_the_overview_and_the_brainstorm_cannot_both_be_open(self):
        out = self.api(
            "bs.open();"
            "P.openOverview();"
            "out = {bs: bs.shown(), over: P.overviewShown()};")
        self.assertFalse(out["bs"])
        self.assertTrue(out["over"])

    def test_clicking_its_tab_opens_it_and_clicking_goals_puts_it_away(self):
        out = self.api(
            "P.renderViewTabs();"
            "var tabs = document.querySelector('.hc-viewtabs');"
            "click(tabs.children[2]); var opened = bs.shown();"
            "click(tabs.children[1]);"
            "out = [opened, bs.shown()];")
        self.assertEqual([True, False], out)

    def test_saying_something_posts_the_whole_conversation(self):
        # The transcript lives in the browser and goes out whole on every
        # round: the server keeps no conversation of its own.
        out = self.api(
            "bs.open();"
            "bs.state().draft = 'the rail is unreadable';"
            "bs.send();"
            "out = calls.filter(function (c) {"
            "  return c[1] && c[1].op === 'brainstorm_say'; })"
            "  .map(function (c) { return c[1].transcript.map("
            "    function (m) { return m.role + ': ' + m.text; }); });")
        self.assertEqual(1, len(out))
        self.assertEqual("you: the rail is unreadable", out[0][-1])

    def test_the_composer_survives_a_redraw_of_the_conversation(self):
        # The reader is mid-sentence far more often than not: the column is
        # rebuilt on every card, and the box they are typing in is not in it.
        out = self.api(
            "bs.open();"
            "var box = bs.box().querySelector('[data-hc-bs-input]');"
            "bs.draw();"
            "out = bs.box().querySelector('[data-hc-bs-input]') === box;")
        self.assertTrue(out)

    def test_the_goals_it_can_hang_rows_on_are_the_trees_own(self):
        out = self.api("out = bs.goalRows();")
        self.assertEqual([["g1", 0], ["g11", 1]],
                         [[r["id"], r["depth"]] for r in out])

    def test_rows_land_on_the_selected_goal_unless_they_are_sent_elsewhere(self):
        self.assertEqual("g1", self.api("out = bs.target();"))
        self.assertEqual("g11", self.api(
            "bs.state().goalId = 'g11'; out = bs.target();"))

    def test_approving_rows_names_the_goal_they_go_under(self):
        out = self.api(
            "bs.open();"
            "bs.write({goal_id: 'g11', todos: ['Widen the tabs']}, 'Added.');"
            "out = calls.filter(function (c) {"
            "  return c[1] && c[1].op === 'brainstorm_apply'; })"
            "  .map(function (c) { return [c[1].goal_id, c[1].todos]; });")
        self.assertEqual([["g11", ["Widen the tabs"]]], out)

    # --- the picker that says where approved rows land ---------------------
    #
    # A native menu shows one line at a time, cannot be looked in, and drops
    # the indent that is the only thing telling two goals of the same name
    # apart. This is the goals rail's own search-then-choose instead.

    def todos(self, tail):
        return self.api(
            "bs.open();"
            "bs.state().card = {card: 'todos', todos: ['Widen the tabs']};"
            "bs.draw();"
            "var pick = bs.box().querySelector('.hc-bs-pick');"
            "var rows = function (only) {"
            "  return Array.prototype.slice.call("
            "    pick.querySelector('.hc-bs-pick-list').children)"
            "    .filter(function (r) {"
            "      return r.getAttribute('data-hc-bs-goalpick') !== null"
            "        && (!only || only(r)); })"
            "    .map(function (r) {"
            "      return r.getAttribute('data-hc-bs-goalpick'); });"
            "};" + tail)

    def test_where_rows_go_is_chosen_from_a_searched_list_not_a_menu(self):
        out = self.todos(
            "out = {menu: bs.box().querySelector('select') !== null,"
            " field: pick.querySelector('.hc-bs-pick-input') !== null,"
            " rows: rows(null)};")
        self.assertFalse(out["menu"])
        self.assertTrue(out["field"])
        self.assertEqual(["g1", "g11"], out["rows"])

    def test_typing_in_it_puts_away_the_goals_that_do_not_match(self):
        # In place: the field being typed in is inside the column a redraw
        # would throw away, caret and all.
        out = self.todos(
            "var field = pick.querySelector('.hc-bs-pick-input');"
            "field.value = 'ui'; fire('input', field);"
            "out = {same: bs.box().querySelector('.hc-bs-pick-input') === field,"
            " shown: rows(function (r) { return r.style.display !== 'none'; })};")
        self.assertTrue(out["same"])
        self.assertEqual(["g11"], out["shown"])

    def test_a_search_that_matches_nothing_says_so(self):
        out = self.todos(
            "var field = pick.querySelector('.hc-bs-pick-input');"
            "field.value = 'zzz'; fire('input', field);"
            "out = [rows(function (r) { return r.style.display !== 'none'; }),"
            " pick.querySelector('[data-hc-bs-goalnone]').style.display];")
        self.assertEqual([[], ""], out)

    def test_what_was_typed_is_still_there_after_the_column_is_redrawn(self):
        out = self.todos(
            "var field = pick.querySelector('.hc-bs-pick-input');"
            "field.value = 'ui'; fire('input', field);"
            "bs.draw();"
            "out = bs.box().querySelector('.hc-bs-pick-input').value;")
        self.assertEqual("ui", out)

    def test_the_goal_picked_in_it_is_the_one_rows_are_written_under(self):
        out = self.todos(
            "click(pick.querySelector('[data-hc-bs-goalpick=\"g11\"]'));"
            "out = {id: bs.state().goalId, target: bs.target(),"
            " marked: rows(function (r) {"
            "   return r.getAttribute('data-hc-on') === '1'; })};")
        self.assertEqual("g11", out["id"])
        self.assertEqual("g11", out["target"])
        self.assertEqual(["g11"], out["marked"])

    # --- a card sent from what was typed into it ---------------------------

    def asked(self, tail):
        return self.api(
            "bs.open();"
            "bs.state().card = {card: 'questions', questions: {items: ["
            "  {id: 'q1', type: 'open', title: 'What is it for?'}]}};"
            "bs.draw();"
            "var send = bs.box().querySelector('[data-hc-bs-act=\"answers\"]');"
            "var field = bs.box().querySelector('[data-hc-bs-field]');" + tail)

    def test_a_typed_answer_is_what_makes_the_card_sendable(self):
        # A picked answer redraws the card and the button comes back armed;
        # a typed one does not, and without arming it where it stands the
        # card can never be sent however much is written in it.
        out = self.asked(
            "var before = send.getAttribute('disabled');"
            "field.value = 'a rail nobody can read'; fire('input', field);"
            "out = [before, send.getAttribute('disabled')];")
        self.assertEqual(["disabled", None], out)

    def test_taking_the_answer_back_out_makes_it_unsendable_again(self):
        out = self.asked(
            "field.value = 'something'; fire('input', field);"
            "field.value = ''; fire('input', field);"
            "out = send.getAttribute('disabled');")
        self.assertEqual("disabled", out)

    def test_pressing_it_sends_what_was_typed(self):
        out = self.asked(
            "field.value = 'a rail nobody can read'; fire('input', field);"
            "click(send);"
            "out = calls.filter(function (c) {"
            "  return c[1] && c[1].op === 'brainstorm_say'; })"
            "  .map(function (c) {"
            "    var said = c[1].transcript;"
            "    return said[said.length - 1].text; });")
        self.assertEqual(1, len(out))
        self.assertIn("a rail nobody can read", out[0])

    def test_the_line_under_a_declined_offer_arms_its_send_the_same_way(self):
        out = self.api(
            "bs.open();"
            "bs.state().card = {card: 'offer', offer: 'goals'};"
            "bs.state().declined = true;"
            "bs.draw();"
            "var send = bs.box().querySelector('[data-hc-bs-act=\"no\"]');"
            "var before = send.getAttribute('disabled');"
            "var note = bs.box().querySelector('[data-hc-bs-note]');"
            "note.value = 'it has not read the rail'; fire('input', note);"
            "out = [before, send.getAttribute('disabled')];")
        self.assertEqual(["disabled", None], out)

    def test_closing_it_leaves_the_conversation_where_it_was(self):
        out = self.api(
            "bs.open();"
            "bs.state().msgs.push({role: 'you', text: 'keep this'});"
            "bs.close();"
            "bs.open();"
            "out = bs.state().msgs.map(function (m) { return m.text; });")
        self.assertIn("keep this", out)

    def test_a_workspace_with_no_project_has_nothing_to_brainstorm_about(self):
        self.assertFalse(self.api("out = bs.open();", project=False))

    def test_the_stylesheet_keeps_the_goals_rail_and_covers_the_rest(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        self.assertIn("--hc-bs-left:max(var(--hc-left),32vw)", css)
        self.assertIn("[data-hc-launch][data-hc-brainstorm] .hc-rail-left",
                      css)
        # The counts are fixed to the window's right edge and would float
        # over the conversation.
        self.assertIn("[data-hc-launch][data-hc-brainstorm] .hc-titlerow"
                      "{display:none!important}", css)

    def test_the_panel_starts_where_the_goals_rail_ends(self):
        out = self.api(
            "bs.open();"
            "out = document.getElementById('hc-project-style').textContent;")
        self.assertIn(".hc-brainstorm{display:none;position:fixed", out)
        self.assertIn("left:var(--hc-bs-left,300px)", out)


class LiveTests(unittest.TestCase):
    """The whole way through, in a browser, against a real server.

    The node harness holds the shapes; this holds the one thing it cannot:
    that a card approved on screen ends up in the reader's goal file.
    """

    def setUp(self):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright is not installed")
        self.chrome = browser_executable()
        if not self.chrome:
            self.skipTest("Chrome/Chromium is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-bs-live"
        paths = CS.paths(self.session, self.root)
        paths.session_dir.mkdir(parents=True)
        paths.goals.write_text(json.dumps(tree(
            goal("g1", "Refactor how the TODOs work", status="in_progress"),
            goal("g11", "UI Changes", parent="g1"))))
        paths.important.write_text(json.dumps({"items": []}))
        paths.prompts.write_text(json.dumps({"prompts": []}))
        paths.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        # Without a project the page opens on onboarding, whose shade sits
        # over everything and takes every click.
        CS.bind_project(self.session, str(self.root), root=self.root)
        self.trajdir = paths.session_dir

    def rows_on(self, gid):
        doc = CS.load_goals(self.session, self.root)[0]
        return [r["text"] for r in GM.by_id(doc, gid)["todo_items"]]

    def test_a_card_approved_on_screen_lands_in_the_goal_file(self):
        import threading
        from playwright.sync_api import sync_playwright

        server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
        ui._configure_server(server, self.trajdir, True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with sync_playwright() as play:
                browser = play.chromium.launch(executable_path=self.chrome)
                page = browser.new_context(
                    viewport={"width": 1400, "height": 900}).new_page()
                page.goto(url)
                page.wait_for_selector(".hc-viewtab", timeout=30000)
                page.get_by_text("Brainstorm", exact=True).first.click()
                page.wait_for_selector(".hc-brainstorm", state="visible",
                                       timeout=10000)
                # The card the model would have sent, put on screen by hand:
                # what is under test is the approval, not the round trip.
                page.evaluate(
                    "() => { var bs = window.__hcPromptUI.brainstorm;"
                    "  bs.state().card = {ok: true, card: 'todos',"
                    "    say: 'four rows', todos: [], subgoals: ["
                    "      {label: 'The tab row',"
                    "       todos: ['Wrap the tabs when the rail is narrow']}]};"
                    "  bs.draw(); }")
                page.get_by_text("ADD THESE TODOS").click()
                page.wait_for_selector(".hc-bs-say", timeout=10000)
                said = page.locator(".hc-bs-say").first.inner_text()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertIn("written into your goals", said)
        doc = CS.load_goals(self.session, self.root)[0]
        piece = [g for g in doc["goals"]
                 if g["title"] == "The tab row"][0]
        self.assertEqual("g1", piece["parent_goal_id"])
        self.assertEqual(["Wrap the tabs when the rail is narrow"],
                         self.rows_on(piece["id"]))


if __name__ == "__main__":
    unittest.main()
