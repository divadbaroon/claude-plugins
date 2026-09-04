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


class StoreTests(unittest.TestCase):
    """The conversations themselves, kept the way the goals are kept.

    A brainstorm used to live in the browser and nowhere else: closing the
    tab was the end of it, and the thinking that had gone into an idea was
    gone before the idea was written down. It goes to a file beside the tree
    it argues with now -- one per project, not one per chat, for the same
    reason the goals are stored that way.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-store"
        self.paths = CS.paths(self.session, self.root)
        self.paths.session_dir.mkdir(parents=True)
        self.paths.manifest.write_text(json.dumps({"cwd": str(self.root)}))

    def said(self, *turns):
        return [{"role": role, "text": text} for role, text in turns]

    def talk(self):
        return self.said(("engelbart", "What's on your mind?"),
                         ("you", "the rail is unreadable"))

    def test_a_conversation_is_written_beside_the_goals_of_its_chat(self):
        held = CS.save_brainstorm(self.session, "", self.talk(), self.root)
        self.assertTrue(self.paths.brainstorms.exists())
        self.assertEqual(str(self.paths.session_dir / "brainstorms.json"),
                         str(self.paths.brainstorms))
        stored = CS.load_brainstorms(self.session, self.root)
        self.assertEqual(1, len(stored))
        self.assertEqual(held["id"], stored[0]["id"])
        self.assertEqual(["What's on your mind?", "the rail is unreadable"],
                         [m["text"] for m in stored[0]["messages"]])

    def test_a_longer_version_replaces_the_conversation_it_grew_out_of(self):
        first = CS.save_brainstorm(self.session, "", self.talk(), self.root)
        CS.save_brainstorm(self.session, first["id"],
                           self.talk() + self.said(("engelbart", "which part")),
                           self.root)
        stored = CS.load_brainstorms(self.session, self.root)
        self.assertEqual(1, len(stored))
        self.assertEqual(3, len(stored[0]["messages"]))

    def test_a_conversation_with_no_id_starts_another_one_beside_it(self):
        first = CS.save_brainstorm(self.session, "", self.talk(), self.root)
        second = CS.save_brainstorm(
            self.session, "", self.said(("you", "what about onboarding")),
            self.root)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(2, len(CS.load_brainstorms(self.session, self.root)))

    def test_the_one_touched_last_is_the_one_read_first(self):
        CS.save_brainstorm(self.session, "", self.talk(), self.root)
        CS.save_brainstorm(self.session, "",
                           self.said(("you", "what about onboarding")),
                           self.root)
        stored = CS.load_brainstorms(self.session, self.root)
        self.assertEqual("what about onboarding", stored[0]["title"])

    def test_a_screen_opened_and_left_is_not_a_conversation(self):
        # The invitation is the same sentence every time. A file full of
        # them would be a list of the times somebody looked at the screen.
        self.assertIsNone(CS.save_brainstorm(self.session, "", [], self.root))
        self.assertIsNone(CS.save_brainstorm(self.session, "", "words",
                                             self.root))
        self.assertEqual([], CS.load_brainstorms(self.session, self.root))

    def test_a_conversation_is_named_by_the_first_thing_the_reader_said(self):
        held = CS.save_brainstorm(self.session, "", self.talk(), self.root)
        self.assertEqual("the rail is unreadable", held["title"])

    def test_a_turn_from_nobody_this_screen_has_is_dropped(self):
        held = CS.save_brainstorm(
            self.session, "",
            self.said(("you", "keep me"), ("system", "drop me"))
            + [{"role": "you", "text": "   "}, "not even a turn"],
            self.root)
        self.assertEqual(["keep me"], [m["text"] for m in held["messages"]])

    def test_the_file_holds_the_last_conversations_not_every_one_ever(self):
        for i in range(CS.BRAINSTORM_LIMIT + 5):
            CS.save_brainstorm(self.session, "",
                               self.said(("you", "idea %d" % i)), self.root)
        stored = CS.load_brainstorms(self.session, self.root)
        self.assertEqual(CS.BRAINSTORM_LIMIT, len(stored))
        self.assertEqual("idea %d" % (CS.BRAINSTORM_LIMIT + 4),
                         stored[0]["title"])

    def test_a_file_somebody_edited_by_hand_reads_as_what_is_left_of_it(self):
        self.paths.brainstorms.write_text(json.dumps(
            {"version": 1, "chats": ["not a chat", {"messages": []},
                                     {"id": "b1", "messages": [
                                         {"role": "you", "text": "kept"}]}]}))
        stored = CS.load_brainstorms(self.session, self.root)
        self.assertEqual(["b1"], [row["id"] for row in stored])
        self.assertEqual("kept", stored[0]["title"])

    def test_two_chats_of_one_project_brainstorm_into_the_same_file(self):
        # The tree is the project's, so the arguments about it are too:
        # picking up yesterday's thinking must not depend on reopening the
        # same terminal window it happened in.
        home = self.root / "app"
        home.mkdir()
        other = "chat-store-two"
        CS.paths(other, self.root).session_dir.mkdir(parents=True)
        CS.paths(other, self.root).manifest.write_text(json.dumps({}))
        CS.bind_project(self.session, str(home), self.root)
        CS.bind_project(other, str(home), self.root)
        CS.save_brainstorm(self.session, "", self.talk(), self.root)
        self.assertEqual(["the rail is unreadable"],
                         [row["title"]
                          for row in CS.load_brainstorms(other, self.root)])


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
        for kind in ("brainstorm_say", "brainstorm_apply",
                     "brainstorm_chats", "brainstorm_save"):
            out = ui._apply_locked({"op": kind, "transcript": [],
                                    "messages": []},
                                   trajdir=None, chat_scoped=False)
            self.assertFalse(out.get("ok"), kind)
            self.assertIn("chat scope", out["error"])

    # --- the conversations themselves --------------------------------------

    def test_a_round_that_landed_is_written_down_and_read_back(self):
        out = self.op(op="brainstorm_save", id="", messages=[
            {"role": "you", "text": "the rail is unreadable"},
            {"role": "engelbart", "text": "which part of it"}])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["id"])
        self.assertEqual("the rail is unreadable", out["title"])
        back = self.op(op="brainstorm_chats")
        self.assertEqual([out["id"]], [row["id"] for row in back["chats"]])
        self.assertEqual(2, len(back["chats"][0]["messages"]))

    def test_a_later_round_of_the_same_conversation_replaces_it(self):
        first = self.op(op="brainstorm_save", id="", messages=[
            {"role": "you", "text": "the rail is unreadable"}])
        self.op(op="brainstorm_save", id=first["id"], messages=[
            {"role": "you", "text": "the rail is unreadable"},
            {"role": "engelbart", "text": "which part of it"}])
        back = self.op(op="brainstorm_chats")["chats"]
        self.assertEqual(1, len(back))
        self.assertEqual(2, len(back[0]["messages"]))

    def test_a_conversation_that_is_not_a_list_is_refused_at_the_door(self):
        out = self.op(op="brainstorm_save", messages="words")
        self.assertFalse(out["ok"])
        self.assertIn("messages", out["error"])

    def test_nothing_said_yet_is_refused_rather_than_stored(self):
        out = self.op(op="brainstorm_save", messages=[])
        self.assertFalse(out["ok"])
        self.assertIn("nothing said", out["error"])

    def test_a_workspace_that_has_never_brainstormed_says_so_with_a_list(self):
        self.assertEqual([], self.op(op="brainstorm_chats")["chats"])


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

    def test_the_tab_row_offers_the_four_workspace_views(self):
        out = self.api(
            "P.renderViewTabs();"
            "out = (document.querySelector('.hc-viewtabs').children || [])"
            "  .map(function (t) { return t.getAttribute('data-hc-viewtab'); });")
        self.assertEqual(["overview", "goals", "brainstorm", "docs"], out)

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

    def test_clicking_its_tab_opens_the_page_and_goals_docks_it(self):
        # Goals does not end the conversation, it puts it back where it
        # belongs on that view: along the foot of the tree, folded to a
        # line. The page view is what goes away.
        out = self.api(
            "P.renderViewTabs();"
            "var tabs = document.querySelector('.hc-viewtabs');"
            "click(tabs.children[2]);"
            "var opened = [bs.shown(), bs.docked()];"
            "click(tabs.children[1]);"
            "out = [opened, [bs.shown(), bs.docked(), bs.dockOpen()]];")
        self.assertEqual([True, False], out[0], "the whole page")
        self.assertEqual([True, True, False], out[1],
                         "the same panel, docked and folded")

    def test_the_page_view_sheds_the_dock_it_was_opened_from(self):
        # Or the full page would be laid out as a one-line strip.
        out = self.api(
            "bs.openDock(); bs.dockShow(true); bs.open();"
            "var root = document.documentElement;"
            "out = [bs.shown(), bs.docked(),"
            " root.getAttribute('data-hc-bs-open') !== null];")
        self.assertEqual([True, False, False], out)

    def test_saying_something_posts_the_whole_conversation(self):
        # The transcript lives in the browser and goes out whole on every
        # round -- the server holds a copy of it, but never the argument
        # about what to say next.
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

    # --- the conversation, kept -------------------------------------------
    #
    # Closing the tab used to be the end of a brainstorm. It is written down
    # after every round now, so reopening the screen is picking a thought
    # back up rather than starting one over.

    HELD = [{"id": "b1", "title": "the rail is unreadable", "messages": [
        {"role": "you", "text": "the rail is unreadable"},
        {"role": "engelbart", "text": "which part of it"}]}]

    def served(self, chats=None, tail=""):
        """The panel against a server that already has conversations in it."""
        return self.api(
            "fetch = function (url, opts) {"
            "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "  calls.push([url, sent]);"
            "  var body = {ok: true};"
            "  if (sent && sent.op === 'brainstorm_chats')"
            "    body = {ok: true, chats: %s};"
            "  if (sent && sent.op === 'brainstorm_save')"
            "    body = {ok: true, id: 'b9', title: 'saved'};"
            "  return Promise.resolve({ok: true, json: function () {"
            "    return Promise.resolve(body); }});"
            "};" % json.dumps(self.HELD if chats is None else chats) + tail)

    def sent(self, op):
        return ("calls.filter(function (c) { return c[1] && c[1].op === '%s'; })"
                "  .map(function (c) { return c[1]; })" % op)

    def test_opening_it_picks_up_where_the_last_conversation_left_off(self):
        out = self.served(tail=(
            "bs.open();"
            "later(function () { return {"
            "  said: bs.state().msgs.map(function (m) { return m.text; }),"
            "  id: bs.state().id,"
            "  drawn: texts(bs.box(), 'hc-bs-body')}; });"))
        self.assertEqual(["the rail is unreadable", "which part of it"],
                         out["said"])
        self.assertEqual("b1", out["id"])
        self.assertEqual(["the rail is unreadable", "which part of it"],
                         out["drawn"])

    def test_a_project_with_no_conversations_opens_on_the_invitation(self):
        out = self.served(chats=[], tail=(
            "bs.open();"
            "later(function () { return bs.state().msgs.map("
            "  function (m) { return m.role; }); });"))
        self.assertEqual(["engelbart"], out)

    def test_the_file_is_read_once_however_often_the_screen_is_opened(self):
        out = self.served(tail=(
            "bs.open(); bs.close(); bs.open();"
            "later(function () { bs.close(); bs.open();"
            "  return later(function () { return %s.length; }); });"
            % self.sent("brainstorm_chats")))
        self.assertEqual(1, out)

    def test_a_read_that_never_landed_is_asked_again_next_time(self):
        # An empty file and an unreachable server look the same to the
        # reader; only one of them is worth giving up on.
        out = self.api(
            "fetch = function (url, opts) {"
            "  calls.push([url, opts && opts.body ? JSON.parse(opts.body)"
            "    : null]);"
            "  return Promise.resolve({ok: true, json: function () {"
            "    return Promise.resolve({ok: false, error: 'down'}); }});"
            "};"
            "bs.open();"
            "later(function () { bs.close(); bs.open();"
            "  return later(function () { return %s.length; }); });"
            % self.sent("brainstorm_chats"))
        self.assertEqual(2, out)

    def test_a_restore_that_lands_late_does_not_take_their_words_back_out(self):
        # They opened the screen and started typing before the file came
        # back. What they said outranks what was on disk.
        out = self.served(tail=(
            "bs.open();"
            "bs.state().draft = 'forget the rail, onboarding is the thing';"
            "bs.send();"
            "later(function () { return bs.state().msgs.map("
            "  function (m) { return m.text; }); });"))
        self.assertIn("forget the rail, onboarding is the thing", out)
        self.assertNotIn("which part of it", out)

    def test_a_round_that_lands_is_written_down_whole(self):
        out = self.served(chats=[], tail=(
            "bs.open();"
            "bs.state().draft = 'the rail is unreadable';"
            "bs.send();"
            "later(function () { return {saved: %s.map(function (s) {"
            "  return [s.id, s.messages.map(function (m) { return m.text; })];"
            "}), id: bs.state().id}; });" % self.sent("brainstorm_save")))
        self.assertEqual(1, len(out["saved"]))
        # No id on the first save: the server mints one and the panel keeps
        # it, so the next round extends this conversation instead of
        # starting a second one beside it.
        self.assertEqual("", out["saved"][0][0])
        self.assertIn("the rail is unreadable", out["saved"][0][1])
        self.assertEqual("b9", out["id"])

    def test_a_round_that_failed_still_keeps_what_they_said(self):
        out = self.served(chats=[], tail=(
            "fetch = function (url, opts) {"
            "  var sent = opts && opts.body ? JSON.parse(opts.body) : null;"
            "  calls.push([url, sent]);"
            "  var body = sent && sent.op === 'brainstorm_say'"
            "    ? {ok: false, error: 'no credit'} : {ok: true, id: 'b9'};"
            "  return Promise.resolve({ok: true, json: function () {"
            "    return Promise.resolve(body); }});"
            "};"
            "bs.open();"
            "bs.state().draft = 'the rail is unreadable';"
            "bs.send();"
            "later(function () { return %s.map(function (s) {"
            "  return s.messages.map(function (m) { return m.text; }); }); });"
            % self.sent("brainstorm_save")))
        self.assertEqual(1, len(out))
        self.assertIn("the rail is unreadable", out[0])

    def test_a_screen_opened_and_left_is_not_written_down(self):
        out = self.served(chats=[], tail=(
            "bs.open();"
            "later(function () { return bs.store().then(function () {"
            "  return %s.length; }); });" % self.sent("brainstorm_save")))
        self.assertEqual(0, out)

    def test_a_new_brainstorm_starts_beside_the_one_that_was_on_screen(self):
        out = self.served(tail=(
            "bs.open();"
            "later(function () {"
            "  var was = bs.state().id;"
            "  click(bs.box().querySelector('[data-hc-bs-act=\"new\"]'));"
            "  return {was: was, id: bs.state().id,"
            "    said: bs.state().msgs.map(function (m) { return m.text; }),"
            "    saves: %s.length}; });" % self.sent("brainstorm_save")))
        self.assertEqual("b1", out["was"])
        # Nothing is written by starting one: what it replaces on screen is
        # already in the file, and the new one is stored on its first round.
        self.assertEqual("", out["id"])
        self.assertEqual(0, out["saves"])
        self.assertEqual(1, len(out["said"]))
        self.assertIn("on your mind", out["said"][0])

    def test_the_stylesheet_keeps_the_goals_rail_and_covers_the_rest(self):
        css = self.run_js("out = window.__hcPromptUI.launchCss();")
        self.assertIn("--hc-bs-left:max(var(--hc-left),32vw)", css)
        # Both are the FULL-PAGE view's doing, and say so: the dock stands
        # under the tree rather than beside it, so it neither narrows the
        # rail nor takes the counts off the screen.
        self.assertIn("[data-hc-launch][data-hc-brainstorm]"
                      ":not([data-hc-bs-dock]) .hc-rail-left", css)
        # The counts are fixed to the window's right edge and would float
        # over the conversation.
        self.assertIn("[data-hc-launch][data-hc-brainstorm]"
                      ":not([data-hc-bs-dock]) .hc-titlerow"
                      "{display:none!important}", css)

    def test_the_panel_starts_where_the_goals_rail_ends(self):
        out = self.api(
            "bs.open();"
            "out = document.getElementById('hc-project-style').textContent;")
        self.assertIn(".hc-brainstorm{display:none;position:fixed", out)
        self.assertIn("left:var(--hc-bs-left,300px)", out)


class LiveTests(unittest.TestCase):
    """The whole way through, in a browser, against a real server.

    The node harness holds the shapes; this holds the two things it cannot:
    that a card approved on screen ends up in the reader's goal file, and
    that the conversation itself survives the page it was had on.
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

    def serving(self):
        """The workspace, on a port, for as long as the `with` block runs."""
        import contextlib
        import threading

        @contextlib.contextmanager
        def run():
            server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
            ui._configure_server(server, self.trajdir, True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield "http://127.0.0.1:%d" % server.server_address[1]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        return run()

    def test_a_conversation_outlives_the_page_it_was_had_on(self):
        from playwright.sync_api import sync_playwright

        def brainstorm(page, url=None):
            if url:
                page.goto(url)
            page.wait_for_selector(".hc-viewtab", timeout=30000)
            page.get_by_text("Brainstorm", exact=True).first.click()
            page.wait_for_selector(".hc-brainstorm", state="visible",
                                   timeout=10000)

        with self.serving() as url:
            with sync_playwright() as play:
                browser = play.chromium.launch(executable_path=self.chrome)
                page = browser.new_context(
                    viewport={"width": 1400, "height": 900}).new_page()
                brainstorm(page, url)
                # The round the model would have answered, put on screen by
                # hand: what is under test is what happens to it afterwards.
                page.evaluate(
                    "() => { var bs = window.__hcPromptUI.brainstorm;"
                    "  bs.state().msgs.push({role: 'you',"
                    "    text: 'the rail is unreadable'});"
                    "  bs.state().msgs.push({role: 'engelbart',"
                    "    text: 'which part of it'});"
                    "  bs.draw(); return bs.store(); }")
                page.reload()
                brainstorm(page)
                page.wait_for_function(
                    "() => window.__hcPromptUI.brainstorm.state()"
                    "  .msgs.length > 1", timeout=10000)
                said = page.evaluate(
                    "() => window.__hcPromptUI.brainstorm.state().msgs"
                    "  .map(function (m) { return m.text; })")
                browser.close()
        held = CS.load_brainstorms(self.session, self.root)
        self.assertEqual(1, len(held))
        # The invitation the screen opens on is part of the conversation and
        # is kept with it; the title is not, since every one starts that way.
        self.assertEqual(["the rail is unreadable", "which part of it"],
                         [m["text"] for m in held[0]["messages"]][1:])
        self.assertEqual("the rail is unreadable", held[0]["title"])
        # And what the file holds is what the second page opened on.
        self.assertEqual([m["text"] for m in held[0]["messages"]], said)

    def test_a_re_render_does_not_swallow_what_they_were_typing(self):
        """The composer is drawn from the draft, not built once and left.

        The artifact re-renders its own document whenever its state changes,
        which takes this panel with it and rebuilds the whole thing. The
        composer used to come back empty while the words stayed in state, so
        the reader watched their sentence vanish and the Send beside it go
        dead -- and no amount of pressing it did anything, because what
        armed it was the field that no longer existed.
        """
        from playwright.sync_api import sync_playwright

        with self.serving() as url:
            with sync_playwright() as play:
                browser = play.chromium.launch(executable_path=self.chrome)
                page = browser.new_context(
                    viewport={"width": 1400, "height": 900}).new_page()
                page.goto(url)
                page.wait_for_selector(".hc-viewtab", timeout=30000)
                page.get_by_text("Brainstorm", exact=True).first.click()
                page.wait_for_selector(".hc-brainstorm", state="visible",
                                       timeout=10000)
                box = page.locator("[data-hc-bs-input]")
                box.click()
                box.type("half an idea I want to keep")
                page.wait_for_timeout(300)
                self.assertIsNone(page.evaluate(
                    "() => document.querySelector('[data-hc-bs-act=\"send\"]')"
                    "  .getAttribute('disabled')"))
                # What a re-render does to this panel, done to it directly.
                page.evaluate(
                    "() => { var n = document.querySelector('.hc-brainstorm');"
                    "  n.parentNode.removeChild(n); }")
                page.wait_for_selector(".hc-brainstorm", state="visible",
                                       timeout=10000)
                page.wait_for_timeout(1200)
                after = page.evaluate(
                    "() => [document.querySelector('[data-hc-bs-input]').value,"
                    "  document.querySelector('[data-hc-bs-act=\"send\"]')"
                    "    .getAttribute('disabled')]")
                browser.close()
        self.assertEqual(["half an idea I want to keep", None], after)

    def test_send_takes_the_answer_typed_into_the_card(self):
        """The big button at the bottom sends whatever is standing.

        A reader with a question card on screen answers it in the card and
        then presses the button that says Send, because that is the button
        that says Send. It used to do nothing at all, which reads as broken
        rather than as a rule about which box belongs to which button.
        """
        from playwright.sync_api import sync_playwright

        card = {"ok": True, "card": "questions", "say": "One question.",
                "questions": {"eyebrow": "one question", "items": [
                    {"id": "what", "type": "open", "options": [],
                     "title": "What does it stop you doing?",
                     "subtitle": "", "placeholder": "the thing…"}]}}
        with self.serving() as url:
            with sync_playwright() as play:
                browser = play.chromium.launch(executable_path=self.chrome)
                page = browser.new_context(
                    viewport={"width": 1400, "height": 900}).new_page()
                page.goto(url)
                page.wait_for_selector(".hc-viewtab", timeout=30000)
                page.get_by_text("Brainstorm", exact=True).first.click()
                page.wait_for_selector(".hc-brainstorm", state="visible",
                                       timeout=10000)
                page.evaluate(
                    "(card) => { var bs = window.__hcPromptUI.brainstorm;"
                    "  bs.state().card = card; bs.state().thinking = false;"
                    "  bs.draw(); }", card)
                page.wait_for_selector("textarea[data-hc-bs-field]",
                                       timeout=10000)
                field = page.locator("textarea[data-hc-bs-field]")
                field.click()
                field.type("it stops me reading the rail")
                page.wait_for_timeout(300)
                # Nothing is in the composer, and its Send is live anyway --
                # what it would send is the answer above it.
                armed = page.evaluate(
                    "() => [window.__hcPromptUI.brainstorm.state().draft,"
                    "  document.querySelector('[data-hc-bs-act=\"send\"]')"
                    "    .getAttribute('disabled')]")
                page.locator("[data-hc-bs-act=\"send\"]").click()
                page.wait_for_timeout(800)
                said = page.evaluate(
                    "() => window.__hcPromptUI.brainstorm.state().msgs"
                    "  .slice(-1)[0].text")
                browser.close()
        self.assertEqual(["", None], armed)
        self.assertEqual(
            "What does it stop you doing?: it stops me reading the rail", said)

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


class DockTests(BridgeTestCase):
    """The same brainstorm, docked at the foot of the goal tree.

    On the Goals page thinking out loud happens beside the thing being
    thought about, so the panel is a line under the workspace that grows
    into the conversation when it is written in. It is the SAME panel: the
    same messages, the same cards, the same round, the same writes. Only
    where it stands and how much of it is drawn.
    """

    TREE = PanelTests.TREE
    api = PanelTests.api

    def test_docking_opens_the_one_brainstorm_rather_than_a_second_one(self):
        out = self.api(
            "bs.openDock();"
            "out = {on: bs.shown(), docked: bs.docked(),"
            " msgs: bs.state().msgs.length,"
            " panels: document.querySelectorAll('.hc-brainstorm').length};")
        self.assertTrue(out["on"], "it is the brainstorm, shown")
        self.assertTrue(out["docked"])
        self.assertEqual(1, out["msgs"], "the same invitation, not a new one")
        self.assertEqual(1, out["panels"], "one panel, in a second place")

    def test_it_opens_folded_and_grows_when_it_is_written_in(self):
        out = self.api(
            "bs.openDock(); var shut = bs.dockOpen();"
            "var field = document.querySelector('[data-hc-bs-input]');"
            "fire('focusin', field);"
            "out = [shut, bs.dockOpen()];")
        self.assertEqual([False, True], out)

    def test_hide_folds_it_back_without_ending_the_conversation(self):
        out = self.api(
            "bs.openDock(); bs.dockShow(true);"
            "var field = document.querySelector('[data-hc-bs-input]');"
            "field.value = 'half a thought'; fire('input', field);"
            "click(document.querySelector('[data-hc-bs-act=\"dockhide\"]'));"
            "out = {open: bs.dockOpen(), on: bs.shown(),"
            " draft: bs.state().draft};")
        self.assertFalse(out["open"], "folded")
        self.assertTrue(out["on"], "but not closed")
        self.assertEqual("half a thought", out["draft"],
                         "what was typed is still theirs")

    def test_escape_folds_it_and_keeps_what_was_typed(self):
        out = self.api(
            "bs.openDock(); bs.dockShow(true);"
            "var field = document.querySelector('[data-hc-bs-input]');"
            "field.value = 'half a thought'; fire('input', field);"
            "key('Escape', field);"
            "out = {open: bs.dockOpen(), draft: bs.state().draft,"
            " msgs: bs.state().msgs.length};")
        self.assertFalse(out["open"])
        self.assertEqual("half a thought", out["draft"])
        self.assertEqual(1, out["msgs"], "escape does not send")

    def test_the_placeholder_names_what_is_selected(self):
        out = self.api(
            "bs.openDock();"
            "out = [bs.dockSubject(),"
            " document.querySelector('[data-hc-bs-input]')"
            "   .getAttribute('placeholder')];")
        self.assertEqual("“Refactor how the TODOs work”", out[0])
        self.assertEqual("Brainstorm about “Refactor how the TODOs"
                         " work”…", out[1])

    def test_the_full_page_view_keeps_its_own_placeholder(self):
        # Opened as a page rather than a dock, it is not about the selected
        # goal in particular and does not claim to be.
        out = self.api(
            "bs.open();"
            "out = [bs.docked(),"
            " document.querySelector('[data-hc-bs-input]')"
            "   .getAttribute('placeholder')];")
        self.assertFalse(out[0])
        self.assertEqual("say anything — or answer the card above",
                         out[1])

    def test_closing_it_takes_the_dock_off_the_root(self):
        # Or the next full-page brainstorm would open wearing the dock's
        # layout, which is a panel one line tall over the whole window.
        out = self.api(
            "bs.openDock(); bs.dockShow(true); bs.close();"
            "var root = document.documentElement;"
            "out = [bs.shown(), bs.docked(),"
            " root.getAttribute('data-hc-bs-open') !== null];")
        self.assertEqual([False, False, False], out)
