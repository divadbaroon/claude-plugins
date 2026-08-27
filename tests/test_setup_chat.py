"""The cold start: a chat that has nothing to read a goal out of.

A reader who runs `npx engelbart-cli` and then `/bart` in a blank chat has
no transcript to infer anything from, so the goals screen opens empty and
says nothing about what to do with it. Setup is what stands in that place:
a conversation that asks what they are working on, proposes a plan they
approve, and writes the project, its goals and their TODO rows from what
they said.

Everything here is model output on its way into the reader's document, so
every field is bounded and every shape is coerced rather than trusted.
"""

import contextlib
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import setup_chat as SC  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402


class OpeningTests(unittest.TestCase):
    def test_the_opening_line_asks_for_their_own_words(self):
        # The blank screen said nothing; this says the one thing the reader
        # needs to know -- that describing it badly is enough to start.
        self.assertIn("own words", SC.OPEN)

    def test_the_prompt_names_every_card_it_may_ask_for(self):
        form = "\n".join(SC.FORM)
        for card in ("questions", "plan", "goals", "todos"):
            self.assertIn(card, form)

    def test_the_prompt_carries_the_conversation_so_far(self):
        lines = SC.compose([{"role": "you", "text": "uploads are slow"},
                            {"role": "engelbart", "text": "noted"}])
        body = "\n".join(lines)
        self.assertIn("uploads are slow", body)
        self.assertIn("noted", body)

    def test_a_transcript_is_bounded_from_the_oldest_end(self):
        # The newest turns are what the next card is drawn from; an opening
        # message from forty turns ago is not worth the deadline.
        many = [{"role": "you", "text": "turn %d" % i} for i in range(60)]
        body = "\n".join(SC.compose(many))
        self.assertIn("turn 59", body)
        self.assertNotIn("turn 0\n", body)


class CardTests(unittest.TestCase):
    """What comes back, coerced into the one shape the rail can draw."""

    def test_a_reply_with_nothing_in_it_is_a_card_of_none(self):
        self.assertEqual("none", SC.normalize_card({})["card"])
        self.assertEqual("none", SC.normalize_card("not an object")["card"])

    def test_what_it_says_is_kept_and_bounded(self):
        out = SC.normalize_card({"say": "x" * 5000, "card": "none"})
        self.assertEqual(SC.MAX_SAY, len(out["say"]))

    def test_a_card_it_does_not_know_is_none(self):
        self.assertEqual("none", SC.normalize_card({"card": "explode"})["card"])

    def test_every_kind_of_question_keeps_what_it_needs_to_be_asked(self):
        out = SC.normalize_card({
            "card": "questions",
            "questions": {"eyebrow": "three questions", "items": [
                {"id": "kind", "type": "mcq", "title": "Who for?",
                 "options": ["me", "a team"]},
                {"id": "have", "type": "select_all", "title": "What is there?",
                 "subtitle": "pick any", "options": ["an API"]},
                {"id": "done", "type": "free", "title": "Done means?",
                 "placeholder": "uploads never touch the API"},
                {"id": "story", "type": "open", "title": "What happened?"},
                {"id": "which", "type": "mcq", "title": "Which is right?",
                 "options": [{"label": "signed URLs", "why": "no proxy"},
                             {"label": "proxy route"}]}]}})
        items = out["questions"]["items"]
        self.assertEqual(["mcq", "select_all", "free", "open", "mcq"],
                         [q["type"] for q in items])
        self.assertEqual(["me", "a team"],
                         [o["label"] for o in items[0]["options"]])
        self.assertEqual("pick any", items[1]["subtitle"])
        self.assertEqual("uploads never touch the API", items[2]["placeholder"])
        # An option may carry the argument for it -- that is how one shape
        # asks both "which is true" and "which of these is right".
        self.assertEqual([{"label": "signed URLs", "why": "no proxy"},
                          {"label": "proxy route", "why": ""}],
                         items[4]["options"])
        # A plain choice carries labels and no arguments.
        self.assertEqual(["", ""], [o["why"] for o in items[0]["options"]])
        # And the kinds that are not choices carry no options at all.
        self.assertEqual([], items[3]["options"])

    def test_proposals_written_under_candidates_are_still_askable(self):
        # A model that fills the key the old shape used has still asked a
        # question the reader can answer.
        out = SC.normalize_card({"card": "questions", "questions": {
            "items": [{"id": "w", "type": "mcq", "title": "Which?",
                       "candidates": [{"label": "a", "why": "cheap"}]}]}})
        q = out["questions"]["items"][0]
        self.assertEqual([{"label": "a", "why": "cheap"}], q["options"])

    def test_a_question_of_no_known_kind_is_asked_as_one_line(self):
        # Better a box to type in than a question the reader cannot answer.
        out = SC.normalize_card({"card": "questions", "questions": {
            "items": [{"id": "q", "type": "oracle", "title": "?"}]}})
        self.assertEqual("free", out["questions"]["items"][0]["type"])

    def test_a_choice_with_nothing_to_choose_from_becomes_one_too(self):
        for kind in ("mcq", "select_all"):
            out = SC.normalize_card({"card": "questions", "questions": {
                "items": [{"id": "q", "type": kind, "title": "?",
                           "options": []}]}})
            self.assertEqual("free", out["questions"]["items"][0]["type"], kind)

    def test_a_question_with_no_title_is_dropped(self):
        out = SC.normalize_card({"card": "questions", "questions": {
            "items": [{"id": "a", "type": "free", "title": ""},
                      {"id": "b", "type": "free", "title": "real"}]}})
        self.assertEqual(1, len(out["questions"]["items"]))

    def test_questions_with_nothing_left_in_them_are_not_a_card(self):
        out = SC.normalize_card({"card": "questions",
                                 "questions": {"items": []}})
        self.assertEqual("none", out["card"])

    def test_every_question_is_given_an_id_it_can_be_answered_by(self):
        out = SC.normalize_card({"card": "questions", "questions": {
            "items": [{"type": "free", "title": "one"},
                      {"type": "free", "title": "two"}]}})
        ids = [q["id"] for q in out["questions"]["items"]]
        self.assertEqual(2, len(set(ids)))
        self.assertTrue(all(ids))

    def test_the_plan_is_prose_and_the_doubts_under_it(self):
        out = SC.normalize_card({"card": "plan", "plan": {
            "description": "Move uploads off the API.\n\nDone when the"
                           " client PUTs straight to storage.",
            "unsure": ["which bucket", "who signs the URL"]}})
        # Paragraphs survive; a form's labels were never the reader's words.
        self.assertIn("\n\n", out["plan"]["description"])
        self.assertEqual(["which bucket", "who signs the URL"],
                         out["plan"]["unsure"])

    def test_a_plan_still_written_as_rows_is_folded_into_the_prose(self):
        # Half a plan is worse than a clumsy one, so a model writing the
        # older shape is read rather than dropped.
        out = SC.normalize_card({"card": "plan", "plan": {
            "lines": [{"k": "the work", "v": "direct to storage"},
                      {"k": "constraint", "v": "under 200ms"}]}})
        self.assertIn("direct to storage", out["plan"]["description"])
        self.assertIn("under 200ms", out["plan"]["description"])

    def test_a_plan_that_says_nothing_is_not_a_card(self):
        out = SC.normalize_card({"card": "plan",
                                 "plan": {"unsure": ["everything"]}})
        self.assertEqual("none", out["card"])

    def test_goals_carry_the_reason_each_one_is_offered(self):
        out = SC.normalize_card({"card": "goals", "goals": [
            {"label": "Signed uploads in staging", "why": "proves the shape"},
            {"label": "p95 under 200ms"}]})
        self.assertEqual(2, len(out["goals"]))
        self.assertEqual("proves the shape", out["goals"][0]["why"])
        self.assertEqual("", out["goals"][1]["why"])

    def test_a_goal_with_no_label_is_dropped(self):
        out = SC.normalize_card({"card": "goals",
                                 "goals": [{"why": "no label"},
                                           {"label": "real"}]})
        self.assertEqual(["real"], [g["label"] for g in out["goals"]])

    def test_todos_are_lines_of_text_however_they_arrive(self):
        out = SC.normalize_card({"card": "todos", "todos": [
            "Add POST /uploads/sign", {"text": "Switch the client"}, "  ", 7]})
        self.assertEqual(["Add POST /uploads/sign", "Switch the client"],
                         out["todos"])

    def test_the_lists_are_capped(self):
        out = SC.normalize_card({"card": "goals",
                                 "goals": [{"label": "g%d" % i}
                                           for i in range(50)]})
        self.assertEqual(SC.MAX_GOALS, len(out["goals"]))
        out = SC.normalize_card({"card": "todos",
                                 "todos": ["t%d" % i for i in range(200)]})
        self.assertEqual(SC.MAX_TODOS, len(out["todos"]))


class StageTests(unittest.TestCase):
    """The order is not the model's to choose.

    Left to its own judgement it sometimes wrote a plan from one sentence,
    or offered goals nobody had agreed the shape of. The four steps are the
    product -- ask, then say what you think this is, then which one first,
    then the rows -- so the stage is worked out from what has actually been
    produced and the model is told which card it is writing, not asked.
    """

    def test_a_conversation_with_nothing_in_it_asks_questions(self):
        self.assertEqual("questions", SC.stage_of([], []))

    def test_questions_come_before_a_plan(self):
        # One round of answers is enough to write from; none is not. The
        # floor is one because two was a floor the model fought: it would
        # have enough, try to move on, be discarded, and the reader would
        # sit in front of a card that never came.
        self.assertEqual("questions", SC.stage_of([], []))
        self.assertEqual("plan", SC.stage_of([], ["questions"]))

    def test_the_plan_is_not_skipped_however_much_was_said(self):
        many = [{"role": "you", "text": "a very full description"}] * 8
        self.assertEqual("questions", SC.stage_of(many, []))

    def test_goals_come_after_the_plan_was_shown(self):
        self.assertEqual("goals", SC.stage_of([], ["questions", "plan"]))

    def test_todos_come_last(self):
        self.assertEqual("todos", SC.stage_of(
            [], ["questions", "plan", "goals"]))

    def test_after_the_rows_there_is_nothing_left_to_ask_for(self):
        self.assertEqual("none", SC.stage_of(
            [], ["questions", "plan", "goals", "todos"]))

    def test_a_card_out_of_turn_is_replaced_by_the_one_that_is_due(self):
        # The model wrote a plan when the stage was questions. What it said
        # is kept -- it is talking to the reader -- but the card is not
        # drawn, because drawing it is what would skip the step.
        class Eager:
            def generate_json(self, prompt):
                return {"say": "Here is the plan.", "card": "plan",
                        "plan": {"description": "Move uploads"}}
        out = SC.ask([{"role": "you", "text": "uploads are slow"}],
                     engine=Eager(), shown=[])
        self.assertTrue(out["ok"])
        self.assertEqual("none", out["card"])
        self.assertEqual("Here is the plan.", out["say"])

    def test_a_reply_that_was_only_the_wrong_card_is_asked_again(self):
        # Discarding the card would leave nothing at all, and a silent round
        # reads to the reader as a tool that broke.
        tries = []
        class Eager:
            def generate_json(self, prompt):
                tries.append(prompt)
                if len(tries) == 1:
                    return {"card": "plan", "plan": {"description": "early"}}
                return {"say": "Two questions.", "card": "questions",
                        "questions": {"items": [
                            {"id": "a", "type": "free", "title": "Who for?"}]}}
        out = SC.ask([{"role": "you", "text": "x"}], engine=Eager(), shown=[])
        self.assertEqual(2, len(tries))
        self.assertIn("was discarded", tries[1])
        self.assertTrue(out["ok"])
        self.assertEqual("questions", out["card"])

    def test_the_card_that_is_due_is_named_in_the_prompt(self):
        seen = {}
        class Stub:
            def generate_json(self, prompt):
                seen["prompt"] = prompt
                return {"say": "ok", "card": "plan",
                        "plan": {"description": "h"}}
        SC.ask([{"role": "you", "text": "x"}], engine=Stub(),
               shown=["questions"])
        self.assertIn("write the plan", seen["prompt"])

    def test_the_card_that_is_due_is_kept(self):
        class Stub:
            def generate_json(self, prompt):
                return {"say": "ok", "card": "plan",
                        "plan": {"description": "h"}}
        out = SC.ask([{"role": "you", "text": "x"}], engine=Stub(),
                     shown=["questions"])
        self.assertEqual("plan", out["card"])


class AccountTests(unittest.TestCase):
    """Whose Claude answers, and by what name the model is asked for."""

    def test_an_unconnected_reader_is_asked_for_by_the_plain_alias(self):
        # Their own claude.ai login understands "sonnet"; nothing else has
        # told us what else to call it.
        with mock.patch.object(SC, "SETUP_MODEL", "sonnet"), \
             mock.patch("human_compact.engelbart_auth.status",
                        return_value={"models": []}):
            self.assertEqual("sonnet", SC.setup_model())

    def test_a_connected_reader_is_asked_for_the_model_their_key_allows(self):
        # bart auth pins enforceAvailableModels to the list the issued key
        # may use, so the bare alias is a model the gateway need not answer.
        with mock.patch("human_compact.engelbart_auth.status",
                        return_value={"models": ["claude-opus-4-1",
                                                 "claude-sonnet-4-5"]}):
            self.assertEqual("claude-sonnet-4-5", SC.setup_model())

    def test_an_account_that_cannot_be_read_falls_back_to_the_alias(self):
        with mock.patch("human_compact.engelbart_auth.status",
                        side_effect=OSError("no vault")):
            self.assertEqual("sonnet", SC.setup_model())

    def test_a_provider_that_cannot_be_reached_is_reported_as_itself(self):
        # A setup that silently says nothing is the blank screen this whole
        # surface exists to replace.
        class Dead:
            def generate_json(self, prompt):
                raise RuntimeError("no CLI")
        out = SC.ask([{"role": "you", "text": "hi"}], engine=Dead())
        self.assertFalse(out["ok"])
        self.assertIn("Claude", out["error"])

    def test_a_reply_with_neither_words_nor_a_card_is_not_an_answer(self):
        class Empty:
            def generate_json(self, prompt):
                return {}
        out = SC.ask([{"role": "you", "text": "hi"}], engine=Empty())
        self.assertFalse(out["ok"])

    def test_what_comes_back_is_the_card_the_model_named(self):
        class Stub:
            def generate_json(self, prompt):
                assert "mcq" in prompt and "select_all" in prompt
                return {"say": "Two questions.", "card": "questions",
                        "questions": {"items": [
                            {"id": "a", "type": "mcq", "title": "Who for?",
                             "options": ["me", "a team"]}]}}
        out = SC.ask([{"role": "you", "text": "uploads are slow"}],
                     engine=Stub())
        self.assertTrue(out["ok"])
        self.assertEqual("questions", out["card"])
        self.assertEqual("mcq", out["questions"]["items"][0]["type"])


class SubgoalTests(unittest.TestCase):
    """The breakdown: pieces of the chosen goal, with rows under each.

    A goal worth starting on is rarely one row of work, and a flat list of
    twelve rows is a list nobody reads. The last card names the pieces and
    puts the rows under the piece they belong to, which is also the shape
    the workspace's own tree holds -- so the reader gets a goal with
    subgoals rather than a goal with a wall.
    """

    def test_the_pieces_come_back_with_their_rows(self):
        out = SC.normalize_card({"card": "todos", "subgoals": [
            {"label": "Signing route", "todos": ["Add POST /uploads/sign",
                                                 "Scope the token"]},
            {"label": "Client", "todos": ["PUT straight to storage"]}]})
        self.assertEqual("todos", out["card"])
        self.assertEqual(["Signing route", "Client"],
                         [g["label"] for g in out["subgoals"]])
        self.assertEqual(["Add POST /uploads/sign", "Scope the token"],
                         out["subgoals"][0]["todos"])

    def test_a_piece_with_no_rows_under_it_is_dropped(self):
        # A subgoal is a place to put work. One with none is a heading.
        out = SC.normalize_card({"card": "todos", "subgoals": [
            {"label": "Empty", "todos": []},
            {"label": "Real", "todos": ["do it"]}]})
        self.assertEqual(["Real"], [g["label"] for g in out["subgoals"]])

    def test_a_flat_list_of_rows_is_still_a_card(self):
        # The shape before this one, and what a model gives when the work
        # genuinely does not break down.
        out = SC.normalize_card({"card": "todos", "todos": ["one", "two"]})
        self.assertEqual(["one", "two"], out["todos"])
        self.assertEqual([], out["subgoals"])

    def test_the_pieces_become_subgoals_under_the_one_chosen(self):
        goals = SC.to_goals([{"label": "Signed uploads"}], "Signed uploads",
                            [], [{"label": "Signing route",
                                  "todos": ["Add the route"]},
                                 {"label": "Client",
                                  "todos": ["PUT to storage", "Drop the proxy"]}])
        parent = [g for g in goals if g["title"] == "Signed uploads"][0]
        kids = [g for g in goals if g.get("parent_goal_id") == parent["id"]]
        self.assertEqual(["Signing route", "Client"],
                         [k["title"] for k in kids])
        self.assertEqual(["PUT to storage", "Drop the proxy"],
                         [r["text"] for r in kids[1]["todo_items"]])
        # The rows live on the piece they belong to, not on the parent.
        self.assertEqual([], parent["todo_items"])

    def test_without_pieces_the_rows_stay_on_the_goal(self):
        goals = SC.to_goals([{"label": "a"}], "a", ["one"], [])
        self.assertEqual(["one"], [r["text"] for r in goals[0]["todo_items"]])

    def test_a_subgoal_is_in_progress_and_its_parent_is_too(self):
        goals = SC.to_goals([{"label": "a"}], "a", [],
                            [{"label": "piece", "todos": ["row"]}])
        self.assertEqual("in_progress", goals[0]["status"])
        kid = [g for g in goals if g.get("parent_goal_id")][0]
        self.assertEqual("in_progress", kid["status"])

    def test_the_goals_not_chosen_get_no_pieces(self):
        goals = SC.to_goals([{"label": "a"}, {"label": "b"}], "a", [],
                            [{"label": "piece", "todos": ["row"]}])
        parents = [g for g in goals if not g.get("parent_goal_id")]
        self.assertEqual(["a", "b"], [g["title"] for g in parents])
        self.assertEqual(1, len([g for g in goals if g.get("parent_goal_id")]))


class AnswerTests(unittest.TestCase):
    """What the reader picked, on its way back into the conversation."""

    def test_answers_read_back_as_the_turn_the_reader_took(self):
        said = SC.answers_as_said(
            {"items": [{"id": "kind", "type": "mcq", "title": "Who for?"},
                       {"id": "have", "type": "select_all",
                        "title": "What is there?"},
                       {"id": "done", "type": "free", "title": "Done means?"}]},
            {"kind": "a team", "have": ["an API", "storage"],
             "done": "uploads never touch the API"})
        self.assertIn("Who for?: a team", said)
        self.assertIn("What is there?: an API · storage", said)
        self.assertIn("Done means?: uploads never touch the API", said)

    def test_a_question_nobody_answered_is_left_out(self):
        said = SC.answers_as_said(
            {"items": [{"id": "a", "type": "free", "title": "asked"},
                       {"id": "b", "type": "free", "title": "skipped"}]},
            {"a": "answered"})
        self.assertIn("asked", said)
        self.assertNotIn("skipped", said)

    def test_answering_nothing_at_all_says_so(self):
        self.assertEqual(SC.SKIPPED, SC.answers_as_said({"items": []}, {}))


class TreeTests(unittest.TestCase):
    """The approved conversation, as goals the workspace can hold."""

    def test_the_chosen_goal_carries_the_todo_rows_under_it(self):
        goals = SC.to_goals([{"label": "Signed uploads", "why": "first"},
                             {"label": "Under 200ms"}],
                            "Signed uploads",
                            ["Add the signing route", "Switch the client"])
        chosen = [g for g in goals if g["title"] == "Signed uploads"][0]
        self.assertEqual(["Add the signing route", "Switch the client"],
                         [r["text"] for r in chosen["todo_items"]])
        self.assertEqual("in_progress", chosen["status"])

    def test_the_goals_not_chosen_are_kept_and_left_alone(self):
        goals = SC.to_goals([{"label": "a"}, {"label": "b"}], "a", ["row"])
        other = [g for g in goals if g["title"] == "b"][0]
        self.assertEqual([], other["todo_items"])
        self.assertEqual("active", other["status"])

    def test_the_reason_a_goal_was_offered_becomes_its_description(self):
        goals = SC.to_goals([{"label": "a", "why": "proves the shape"}],
                            "a", [])
        self.assertEqual("proves the shape", goals[0]["description"])

    def test_a_goal_the_reader_typed_themselves_is_the_chosen_one(self):
        # "None of these" -- the label is theirs and is not in the offer.
        goals = SC.to_goals([{"label": "a"}], "my own thing", ["row"])
        titles = [g["title"] for g in goals]
        self.assertIn("my own thing", titles)
        mine = [g for g in goals if g["title"] == "my own thing"][0]
        self.assertEqual(["row"], [r["text"] for r in mine["todo_items"]])

    def test_every_row_starts_unsent(self):
        # The rail's whole model of a row: no status is "not yet sent to a
        # build", and setup has run nothing.
        goals = SC.to_goals([{"label": "a"}], "a", ["one", "two"])
        self.assertEqual(["", ""],
                         [r.get("status", "") for r in goals[0]["todo_items"]])

    def test_the_plan_becomes_the_projects_objective_and_notes(self):
        held = SC.to_project("uploads-api", {
            "description": "Move uploads off the API.\nDone when the client"
                           " PUTs straight to storage."})
        self.assertEqual("uploads-api", held["name"])
        # One line, because every chat in the project reads it.
        self.assertEqual("Move uploads off the API.", held["objective"])
        self.assertIn("PUTs straight to storage", held["description"])


class CommitTests(unittest.TestCase):
    """What setup leaves behind: a project, and nothing attached to it.

    The setup conversation happens on a web page, not in a Claude Code chat,
    so there is no chat to bind. The project is made from the name the reader
    typed while the goals were being written, and its goal tree lives in a
    workspace this vault minted rather than in any chat's store.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def commit(self, name="Uploads API", chosen="Signed uploads"):
        return SC.commit(
            self.root, name,
            {"head": "Move uploads off the API",
             "lines": [{"k": "the work", "v": "direct to storage"}]},
            [{"label": "Signed uploads", "why": "proves the shape"},
             {"label": "Under 200ms"}],
            chosen, ["Add the signing route", "Switch the client"])

    def test_the_project_is_made_from_the_name_the_reader_typed(self):
        out = self.commit()
        self.assertTrue(out["ok"], out)
        record = PS.load_project(self.root, out["cwd"])
        self.assertEqual("Uploads API", record["name"])
        self.assertEqual("Move uploads off the API", record["objective"])

    def test_its_goals_live_in_a_workspace_no_chat_owns(self):
        # "hcws-" is this vault's own minting; a chat's id is Claude's.
        out = self.commit()
        self.assertTrue(out["tree_session"].startswith("hcws-"))
        self.assertEqual(out["tree_session"],
                         PS.tree_session(self.root, out["cwd"]))

    def test_no_chat_is_bound_to_it(self):
        # The web page is not a chat. Binding the one the reader happened to
        # have open would attach the project to a conversation that had no
        # part in it.
        out = self.commit()
        self.assertEqual([], CS.chats_in_project(out["cwd"], self.root))

    def test_the_tree_holds_every_goal_with_rows_under_the_chosen_one(self):
        out = self.commit()
        goals, _important = CS.load_goals(out["tree_session"], self.root)
        titles = [g["title"] for g in goals["goals"]]
        self.assertIn("Signed uploads", titles)
        self.assertIn("Under 200ms", titles)
        chosen = [g for g in goals["goals"]
                  if g["title"] == "Signed uploads"][0]
        self.assertEqual(["Add the signing route", "Switch the client"],
                         [r["text"] for r in chosen["todo_items"]])

    def test_a_name_that_cannot_become_a_folder_is_refused(self):
        out = SC.commit(self.root, "///", {"description": "h"},
                        [{"label": "a"}],
                        "a", [])
        self.assertFalse(out["ok"])
        self.assertIn("name", out["error"])

    def test_a_name_already_taken_is_reported_rather_than_reused(self):
        first = self.commit()
        self.assertTrue(first["ok"])
        again = self.commit()
        self.assertFalse(again["ok"])
        self.assertIn("already", again["error"])

    def test_nothing_to_save_is_refused_before_a_project_is_made(self):
        out = SC.commit(self.root, "Empty", {"description": ""}, [], "", [])
        self.assertFalse(out["ok"])
        self.assertEqual([], PS.list_projects(self.root))


class RouteTests(unittest.TestCase):
    """The two ops, and where they run."""

    def test_saying_something_runs_outside_the_state_lock(self):
        # `say` spawns a claude subprocess and waits on it. A request that
        # held the state lock for three minutes is a workspace nobody else
        # can save into, which is why the build ops escape the same way.
        out = ui._apply_locked({"op": "setup_say", "transcript": []},
                               trajdir=None, chat_scoped=False)
        self.assertIn("__deferred__", out)
        self.assertEqual("setup_say", out["__deferred__"][0])

    def test_a_transcript_that_is_not_a_list_is_refused_at_the_door(self):
        out = ui._apply_locked({"op": "setup_say", "transcript": "words"},
                               trajdir=None, chat_scoped=False)
        self.assertFalse(out["ok"])
        self.assertIn("transcript", out["error"])

    def test_committing_runs_outside_the_lock_as_well(self):
        # It writes goals, and save_goals takes the same lock for itself.
        out = ui._apply_locked({"op": "setup_commit", "name": "x"},
                               trajdir=None, chat_scoped=False)
        self.assertEqual("setup_commit", out["__deferred__"][0])


class TerminalTests(unittest.TestCase):
    """Opening one for them, and refusing to open anything else."""

    def test_only_a_plain_command_is_ever_opened(self):
        # This exists to type `claude` into a window. Anything carrying a
        # shell metacharacter is a request to run something else.
        for bad in ("claude; rm -rf ~", "claude && curl x", "claude `id`",
                    "claude > /tmp/x", "claude | sh"):
            out = SC.open_terminal(bad)
            self.assertFalse(out["ok"], bad)

    def test_nothing_at_all_is_refused(self):
        self.assertFalse(SC.open_terminal("")["ok"])

    def test_a_machine_with_no_terminal_says_so_rather_than_failing(self):
        # The copy row beside the button is still the answer, so this is a
        # quiet no, not an error the reader has to deal with.
        with mock.patch.object(SC, "__name__", SC.__name__), \
             mock.patch("sys.platform", "linux"), \
             mock.patch("shutil.which", return_value=None):
            out = SC.open_terminal("claude")
        self.assertFalse(out["ok"])
        self.assertIn("no terminal", out["error"])


class PageTests(unittest.TestCase):
    """The page itself, served by the workspace that answers its ops."""

    @contextlib.contextmanager
    def server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        srv = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
        ui._configure_server(srv, Path(tmp.name), False)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:%d" % srv.server_address[1]
        finally:
            srv.follow_stop.set()
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)

    def get(self, url):
        with urllib.request.urlopen(url, timeout=10) as answer:
            return answer.status, answer.read().decode("utf-8")

    def test_the_page_is_served_and_asks_for_its_own_script(self):
        with self.server() as base:
            status, body = self.get(base + "/setup")
            self.assertEqual(200, status)
            self.assertIn("setup.js", body)
            # The workspace's own palette, so the page is not a second look.
            self.assertIn("--acc:#0000ee", body)

    def test_the_script_is_served_beside_it(self):
        with self.server() as base:
            status, body = self.get(base + "/setup.js")
            self.assertEqual(200, status)
            self.assertIn("setup_commit", body)

    def test_the_first_screen_asks_new_or_existing_before_anything_else(self):
        # No model call and no spinner: they have just installed and have
        # not asked for anything yet.
        _status, body = None, None
        with self.server() as base:
            _status, body = self.get(base + "/setup.js")
        self.assertIn("Start a new project", body)
        self.assertIn("Resume an existing one", body)

    def test_the_resume_path_hands_over_both_commands_to_copy(self):
        with self.server() as base:
            _status, body = self.get(base + "/setup.js")
        self.assertIn("claude -r", body)
        self.assertIn("/bart", body)


if __name__ == "__main__":
    unittest.main()
