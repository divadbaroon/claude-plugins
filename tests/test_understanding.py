"""The scenario a goal's work is for, and the questions asked about it.

The rail's Understanding tab writes onto a goal what the work is actually
for, in the reader's words -- typed, with screenshots of it pasted beside
them -- and what they want answered about it. Claude answers those
questions in GIVEN/WHEN/THEN and they can follow up; the answers are kept
with the questions. All of it survives the artifact posting its own tree
back, and all of it opens every build of that goal's rows.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import build as BUILD  # noqa: E402
from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_projects import server_for  # noqa: E402
from test_chat_ui_server import post_json  # noqa: E402


EMPTY = {"scenario": "", "shots": [], "questions": []}


class Engine:
    """A provider that answers once and remembers what it was asked."""

    def __init__(self, said="GIVEN a tree\nWHEN two edit it\nTHEN one wins"):
        self.said, self.seen, self.dirs = said, "", None
        self.searched = None

    def generate_plain(self, prompt):
        self.seen = prompt
        return self.said

    def generate_reading(self, prompt, read_dirs=()):
        self.seen, self.dirs = prompt, list(read_dirs)
        return self.said

    def generate_searching(self, prompt, where=""):
        self.seen, self.searched = prompt, where
        return self.said

    def generate(self, prompt):
        self.seen = prompt
        return self.said


class Replies:
    """A provider with an answer for each call, and every prompt it saw."""

    def __init__(self, *said):
        self.said, self.asked = list(said), []

    def generate_plain(self, prompt):
        self.asked.append(prompt)
        return self.said[min(len(self.asked), len(self.said)) - 1]

    def generate_searching(self, prompt, where=""):
        return self.generate_plain(prompt)

    generate = generate_plain


class UnderstandingModelTests(unittest.TestCase):
    def test_normalize_keeps_the_scenario_and_the_questions_with_words(self):
        out = GM.normalize_understanding({
            "scenario": "Two people share one goal tree.",
            "questions": [
                {"id": "qaaaa0001", "text": "Who resolves a conflict?"},
                {"id": "qaaaa0002", "text": "   "},
                {"id": "bad-id", "text": "Where do invites live?"},
                "a bare string",
                {"text": "  spaced   out  "},
                None,
            ]})
        self.assertEqual("Two people share one goal tree.", out["scenario"])
        texts = [q["text"] for q in out["questions"]]
        self.assertEqual(["Who resolves a conflict?", "Where do invites live?",
                          "a bare string", "spaced out"], texts)
        # An id the browser minted is kept; one that is not an id is replaced,
        # so a redraw does not re-key every box.
        self.assertEqual("qaaaa0001", out["questions"][0]["id"])
        self.assertTrue(GM._QUESTION_ID.match(out["questions"][1]["id"]))

    def test_normalize_answers_the_same_shape_for_junk(self):
        for junk in (None, [], "scenario", {"questions": "no"}):
            self.assertEqual(EMPTY, GM.normalize_understanding(junk))

    def test_normalize_caps_what_it_keeps(self):
        out = GM.normalize_understanding({
            "scenario": "x" * (GM.MAX_SCENARIO + 50),
            "questions": [{"text": "q%d" % i} for i in range(40)]})
        self.assertEqual(GM.MAX_SCENARIO, len(out["scenario"]))
        self.assertEqual(GM.MAX_QUESTIONS, len(out["questions"]))

    def test_sanitize_gives_every_goal_the_field_in_one_shape(self):
        # g2 is a goal written before this field existed: it has every other
        # field and not this one, which is what an older goals.json holds.
        older = GM.new_goal("g2", "Older")
        older.pop("understanding")
        goals = {"version": 1, "goals": [GM.new_goal("g1", "Ship it"), older]}
        GM.sanitize(goals)
        for gid in ("g1", "g2"):
            self.assertEqual(EMPTY, GM.by_id(goals, gid)["understanding"])

    def test_render_is_empty_until_something_is_written(self):
        self.assertEqual([], GM.render_understanding({}))
        self.assertEqual([], GM.render_understanding(
            {"understanding": {"scenario": "  ", "questions": []}}))
        lines = GM.render_understanding(
            {"understanding": {"scenario": "", "questions": [
                {"id": "qaaaa0001", "text": "Who wins a conflict?"}]}})
        self.assertIn("(not described)", lines)
        self.assertIn("- Who wins a conflict?", lines)

    def test_a_question_keeps_the_thread_it_was_answered_in(self):
        out = GM.normalize_understanding({"questions": [
            {"id": "qaaaa0001", "text": "Who wins a conflict?", "thread": [
                {"q": "Who wins a conflict?", "a": "GIVEN two\nTHEN one"},
                # Asked, never answered: not a turn. Kept, it would read as
                # an answer of nothing.
                {"q": "And offline?", "a": "   "},
                {"q": "  And   offline?  ", "a": "GIVEN nobody is on"},
                "not a turn at all"]}]})
        thread = out["questions"][0]["thread"]
        self.assertEqual([("Who wins a conflict?", "GIVEN two\nTHEN one"),
                          ("And offline?", "GIVEN nobody is on")],
                         [(t["q"], t["a"]) for t in thread])

    def test_a_thread_is_bounded_like_everything_else_kept(self):
        out = GM.normalize_understanding({"questions": [
            {"id": "qaaaa0001", "text": "Why?", "thread": [
                {"q": "q%d" % n, "a": "a%d" % n}
                for n in range(GM.MAX_TURNS + 5)]}]})
        self.assertEqual(GM.MAX_TURNS, len(out["questions"][0]["thread"]))

    def test_the_screenshots_a_scenario_was_made_from_are_kept_by_path(self):
        out = GM.normalize_understanding({"shots": [
            {"path": "/tmp/shots/a.png", "name": "the rail"},
            # A path repeated is one screenshot, and a bare string is a path
            # that names itself.
            {"path": "/tmp/shots/a.png", "name": "again"},
            "/tmp/shots/b.png",
            {"name": "no path at all"}]})
        self.assertEqual([("/tmp/shots/a.png", "the rail"),
                          ("/tmp/shots/b.png", "b.png")],
                         [(s["path"], s["name"]) for s in out["shots"]])

    def test_the_render_carries_the_shots_and_the_answers_already_given(self):
        lines = GM.render_understanding({"understanding": {
            "scenario": "Two people work one tree.",
            "shots": [{"path": "/tmp/shots/a.png", "name": "the rail"}],
            "questions": [{"id": "qaaaa0001", "text": "Who wins a conflict?",
                           "thread": [
                               {"q": "Who wins a conflict?",
                                "a": "GIVEN two writers\nTHEN the later wins"},
                               {"q": "And offline?",
                                "a": "GIVEN nobody is on"}]}]}})
        text = "\n".join(lines)
        # The file itself, so a build can open it.
        self.assertIn("- /tmp/shots/a.png", text)
        # The answers hang under the question they settled, and a follow-up
        # says what it was a follow-up to.
        self.assertIn("- Who wins a conflict?", text)
        self.assertIn("    GIVEN two writers", text)
        self.assertIn("  - and then: And offline?", text)
        self.assertLess(text.index("- Who wins a conflict?"),
                        text.index("GIVEN two writers"))

    def test_screenshots_alone_are_enough_to_have_a_scenario_at_all(self):
        lines = GM.render_understanding({"understanding": {
            "shots": [{"path": "/tmp/shots/a.png", "name": "a.png"}]}})
        self.assertIn("- /tmp/shots/a.png", lines)


class UnderstandingPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-scenario"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        self.trajdir = p.session_dir
        goal = GM.new_goal("g1", "Share a goal tree", None, origin="user")
        goal["todo_items"] = [
            {"id": "taaaa0001", "text": "Write the invite route", "depth": 0,
             "status": "", "question": ""}]
        goal["understanding"] = {
            "scenario": "Two people work the same tree from two machines.",
            "questions": [{"id": "qaaaa0001", "text": "Who wins a conflict?"},
                          {"id": "qaaaa0002", "text": "Where do invites live?"}]}
        goals = {"version": 1, "goals": [goal]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))

    def test_the_build_prompt_opens_on_the_scenario_above_the_work(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        rows = BUILD.picked_with_children(g["todo_items"], ["taaaa0001"])
        prompt = BUILD.compose_prompt(self.session, goals, important, [], g,
                                      rows, root=self.root)
        self.assertIn("# The scenario this goal is for", prompt)
        self.assertIn("Two people work the same tree from two machines.",
                      prompt)
        self.assertIn("- Who wins a conflict?", prompt)
        self.assertIn("- Where do invites live?", prompt)
        # It is what the work is FOR, so it stands above the work.
        self.assertLess(prompt.index("# The scenario this goal is for"),
                        prompt.index("# The work"))

    def test_a_goal_with_nothing_written_carries_no_heading(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        g["understanding"] = {"scenario": "", "questions": []}
        prompt = BUILD.compose_prompt(self.session, goals, important, [], g,
                                      [], root=self.root)
        self.assertNotIn("# The scenario", prompt)

    def test_the_rails_preview_prints_what_the_build_will_open_on(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        preview = BUILD.preview(self.session, self.root, goals, important, g,
                                [])
        self.assertIn("Two people work the same tree from two machines.",
                      preview)


class UnderstandingRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope = Path(self.tmp.name) / "chat-a"
        self.scope.mkdir()
        goals = {"version": 1, "goals": [GM.new_goal("g1", "Share a tree")]}
        GM.sanitize(goals)
        (self.scope / "goals.json").write_text(json.dumps(goals))
        (self.scope / "important.json").write_text(json.dumps({"items": []}))
        (self.scope / "prompts.json").write_text(json.dumps({"prompts": []}))

    def held(self):
        return json.loads((self.scope / "goals.json").read_text())["goals"][0]

    def write(self, scenario, questions):
        return ui._apply({"op": "set_understanding", "goal_id": "g1",
                          "scenario": scenario, "questions": questions},
                         self.scope, True)

    def test_the_op_writes_both_halves_together(self):
        self.assertEqual({"ok": True}, self.write(
            "Two machines, one tree.",
            [{"id": "qaaaa0001", "text": "Who wins a conflict?"},
             {"id": "qaaaa0002", "text": ""}]))
        held = self.held()["understanding"]
        self.assertEqual("Two machines, one tree.", held["scenario"])
        self.assertEqual([{"id": "qaaaa0001", "text": "Who wins a conflict?",
                           "thread": []}], held["questions"])

    def test_the_op_writes_the_answers_and_the_screenshots_too(self):
        out = ui._apply(
            {"op": "set_understanding", "goal_id": "g1",
             "scenario": "Two machines, one tree.",
             "shots": [{"path": "/tmp/shots/a.png", "name": "the rail"}],
             "questions": [{"id": "qaaaa0001", "text": "Who wins?", "thread": [
                 {"q": "Who wins?", "a": "GIVEN two\nWHEN both\nTHEN later"}]}]},
            self.scope, True)
        self.assertEqual({"ok": True}, out)
        held = self.held()["understanding"]
        self.assertEqual([{"path": "/tmp/shots/a.png", "name": "the rail"}],
                         held["shots"])
        self.assertEqual("GIVEN two\nWHEN both\nTHEN later",
                         held["questions"][0]["thread"][0]["a"])

    def test_the_op_names_a_goal_it_cannot_find(self):
        out = ui._apply({"op": "set_understanding", "goal_id": "nope",
                         "scenario": "x", "questions": []}, self.scope, True)
        self.assertFalse(out["ok"])
        self.assertIn("goal not found", out["error"])

    def test_the_artifacts_own_tree_does_not_erase_it(self):
        # The browser posts its whole tree back on every edit and the import
        # rebuilds each goal from a fixed field list. This field is not in
        # that tree, so it has to be carried across.
        self.write("Two machines, one tree.",
                   [{"id": "qaaaa0001", "text": "Who wins a conflict?"}])
        out = ui._import([{"id": "g1", "title": "Share a tree", "done": False,
                           "notes": "typed a moment later"}],
                         self.scope, True)
        self.assertTrue(out["ok"], out)
        held = self.held()
        self.assertEqual("typed a moment later", held["notes"])
        self.assertEqual("Two machines, one tree.",
                         held["understanding"]["scenario"])
        self.assertEqual(1, len(held["understanding"]["questions"]))


class AskAboutTheScenarioTests(unittest.TestCase):
    """A question about the scenario, answered in GIVEN / WHEN / THEN."""

    def tree(self):
        goal = GM.new_goal("g1", "Share a goal tree", None, origin="user")
        goal["notes"] = "Two machines, one document."
        goals = {"version": 1, "goals": [goal]}
        GM.sanitize(goals)
        return goals["goals"]

    def test_the_answer_is_asked_for_in_the_one_shape(self):
        engine = Engine()
        out = ui.ask_scenario(self.tree(), "g1",
                              "Two people work one tree from two machines.",
                              "  Who   wins a conflict?  ", engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("Who wins a conflict?", out["asked"])
        self.assertEqual("GIVEN a tree\nWHEN two edit it\nTHEN one wins",
                         out["answer"])
        # The shape is asked for, and the material to answer from is there:
        # the scenario itself and the goal it belongs to.
        self.assertIn("GIVEN <what is true before anything happens>",
                      engine.seen)
        self.assertIn("Two people work one tree from two machines.",
                      engine.seen)
        self.assertIn("Two machines, one document.", engine.seen)
        # The question being answered is the last thing said.
        self.assertLess(engine.seen.index("# The scenario"),
                        engine.seen.index("# The question"))

    def test_a_question_with_no_scenario_under_it_is_refused(self):
        engine = Engine()
        out = ui.ask_scenario(self.tree(), "g1", "   ", "Who wins?",
                              engine=engine)
        self.assertFalse(out["ok"])
        self.assertIn("describe the scenario first", out["error"])
        self.assertEqual("", engine.seen)

    def test_nothing_asked_is_refused_before_the_provider(self):
        engine = Engine()
        out = ui.ask_scenario(self.tree(), "g1", "A scenario.", "  ",
                              engine=engine)
        self.assertFalse(out["ok"])
        self.assertIn("ask something first", out["error"])
        self.assertEqual("", engine.seen)

    def test_the_provider_is_told_it_has_nothing_to_search_with(self):
        # What the tab sends is a question about a situation, not an agent
        # turn: a provider that goes looking for the project comes back with
        # a search instead of an answer.
        engine = Engine()
        ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                        "Who wins?", engine=engine)
        self.assertIn("You have no tools on this call", engine.seen)

    def test_a_tool_call_written_out_as_prose_is_not_kept_as_an_answer(self):
        # What this fixes: a reply that is a bash call rendered in markdown.
        # Kept, it is the answer on the goal for good and opens every build.
        blob = ('**Tool: bash**\n\nInput:\n```json\n'
                '{"command":"rg -n building","description":"look"}\n```')
        engine = Replies(blob, blob)
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "What happens with the second todo?",
                              engine=engine)
        self.assertFalse(out["ok"])
        self.assertIn("GIVEN / WHEN / THEN", out["error"])
        # Asked twice before being refused, and told the second time what was
        # wrong with the first reply.
        self.assertEqual(2, len(engine.asked))
        self.assertIn("Your last reply was not an answer", engine.asked[1])
        self.assertNotIn("Your last reply was not an answer", engine.asked[0])

    def test_a_second_try_in_the_shape_is_the_answer(self):
        engine = Replies('{"command":"rg -n building"}',
                         "GIVEN one build is running\nTHEN the second waits")
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "What happens with the second todo?",
                              engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN one build is running\nTHEN the second waits",
                         out["answer"])

    def test_a_preamble_the_form_asked_for_none_of_is_dropped(self):
        engine = Engine(said="Sure -- here is the answer:\n\n```\n"
                             "GIVEN a tree\nTHEN one wins\n```")
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "Who wins?", engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN a tree\nTHEN one wins", out["answer"])

    def test_a_keyword_set_in_bold_is_still_a_keyword(self):
        # The tab colours a keyword by finding it at the head of its line, so
        # markdown the form asked for none of comes off before it is kept.
        engine = Engine(said="- **GIVEN** a tree\n- **THEN** one wins")
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "Who wins?", engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN a tree\nTHEN one wins", out["answer"])

    def test_a_question_about_the_project_is_answered_out_of_the_project(self):
        # What this fixes: an answer kept on the goal that said it could not
        # check the code. When the project is on this machine, the call is
        # made in it and told to go and look.
        engine = Engine()
        out = ui.ask_scenario(self.tree(), "g1",
                              "A todo is building and I start another.",
                              "What happens with the second todo?",
                              cwd=str(ROOT), engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual(str(ROOT), engine.searched)
        self.assertIn(str(ROOT), engine.seen)
        self.assertIn("Grep for the names the scenario uses", engine.seen)
        # And is not told the opposite in the same breath.
        self.assertNotIn("You have no tools on this call", engine.seen)
        # The shape is still the shape -- that is what the tab draws.
        self.assertIn("GIVEN <what is true before anything happens>",
                      engine.seen)
        # UNCLEAR is what you write after looking, not instead of it.
        self.assertIn("never that you could not check the code", engine.seen)

    def test_a_directory_that_is_not_one_is_answered_from_the_words(self):
        # A shared workspace records a path on somebody else's disk. Nothing
        # is opened at that path here: the question is answered as it always
        # was, from the scenario and the goal.
        engine = Engine()
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "Who wins?", cwd=str(ROOT / "not-a-project"),
                              engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertIsNone(engine.searched)
        self.assertIn("You have no tools on this call", engine.seen)

    def test_a_reply_out_of_shape_is_asked_again_without_being_called_off(self):
        # The second try in the project keeps the project: what was wrong
        # with the first reply was its shape, not its looking.
        engine = Replies("Here is what I found in build.py.",
                         "GIVEN one build is running\nTHEN the second waits")
        out = ui.ask_scenario(self.tree(), "g1", "Two builds at once.",
                              "What happens with the second todo?",
                              cwd=str(ROOT), engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual(2, len(engine.asked))
        self.assertIn("it is only the shape that was", engine.asked[1])
        self.assertNotIn("You have no tools on this call", engine.asked[1])

    def test_a_follow_up_carries_the_thread_it_follows(self):
        engine = Engine()
        ui.ask_scenario(
            self.tree(), "g1", "Two people work one tree.",
            "And if both are offline?",
            # The tab's own spelling of a turn, which is not the ask panel's.
            turns=[{"q": "Who wins a conflict?", "a": "GIVEN two\nTHEN later"},
                   {"q": "asked, never answered", "a": ""}],
            engine=engine)
        self.assertIn("Q: Who wins a conflict?", engine.seen)
        self.assertIn("A: GIVEN two\nTHEN later", engine.seen)
        self.assertNotIn("asked, never answered", engine.seen)
        self.assertLess(engine.seen.index("A: GIVEN two"),
                        engine.seen.index("And if both are offline?"))


class DraftTheScenarioTests(unittest.TestCase):
    """The scenario written from screenshots, from rough words, or both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope = Path(self.tmp.name) / "chat-a"
        self.shots = self.scope / "attachments"
        self.shots.mkdir(parents=True)
        self.shot = self.shots / "20260824-000000-aaaa.png"
        self.shot.write_bytes(b"\x89PNG\r\n")
        # What a subprocess would be handed. A temporary directory on macOS
        # sits under a symlink, and the guard compares resolved paths.
        self.shots, self.shot = self.shots.resolve(), self.shot.resolve()

    def test_a_screenshot_is_named_for_the_provider_to_open(self):
        engine = Engine(said="Two people edit one goal tree at once.")
        out = ui.draft_scenario(
            self.scope, "  ", [{"path": str(self.shot), "name": "the rail"}],
            engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("Two people edit one goal tree at once.",
                         out["scenario"])
        self.assertIn(str(self.shot), engine.seen)
        # Named files are of no use to a provider not allowed to open them.
        self.assertEqual([str(self.shots)], engine.dirs)

    def test_words_alone_are_answered_without_opening_anything(self):
        engine = Engine(said="Two people edit one tree.")
        out = ui.draft_scenario(self.scope, "two ppl one tree, conflicts",
                                [], engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertIn("two ppl one tree, conflicts", engine.seen)
        # No files to open, so the plain round trip -- and no directory
        # handed to a subprocess for a call that reads nothing.
        self.assertIsNone(engine.dirs)

    def test_a_path_this_workspace_never_wrote_is_not_opened(self):
        # The paths arrive from a browser and decide what a subprocess may
        # read. A file elsewhere on the disk is dropped rather than opened,
        # even when it exists.
        outside = Path(self.tmp.name) / "secrets.png"
        outside.write_bytes(b"\x89PNG\r\n")
        engine = Engine(said="Something.")
        out = ui.draft_scenario(self.scope, "", [{"path": str(outside)}],
                                engine=engine)
        self.assertFalse(out["ok"])
        self.assertIn("paste a screenshot", out["error"])
        self.assertEqual("", engine.seen)

    def test_nothing_at_all_is_refused_before_the_provider(self):
        engine = Engine()
        out = ui.draft_scenario(self.scope, "   ", [], engine=engine)
        self.assertFalse(out["ok"])
        self.assertIn("paste a screenshot", out["error"])
        self.assertEqual("", engine.seen)


class ScenarioRouteTests(unittest.TestCase):
    """Both calls the tab makes, over the wire the browser makes them on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-scenario"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        self.trajdir = p.session_dir
        goal = GM.new_goal("g1", "Share a goal tree", None, origin="user")
        goals = {"version": 1, "goals": [goal]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))

    def test_the_ask_route_answers_and_keeps_nothing_itself(self):
        seen = {}

        def answer(lines, engine=None, read_dirs=None, search_dir=""):
            seen["prompt"] = "\n".join(lines)
            seen["where"] = search_dir
            return {"ok": True, "answer": "GIVEN a tree\nTHEN one wins"}

        with mock.patch.object(ui, "_answer", answer):
            with server_for(self.trajdir) as url:
                out = post_json(url + "/api/ask_scenario", {
                    "goal": "g1", "scenario": "Two people, one tree.",
                    "question": "Who wins a conflict?",
                    "turns": [{"q": "earlier?", "a": "GIVEN nothing"}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN a tree\nTHEN one wins", out["answer"])
        self.assertIn("Two people, one tree.", seen["prompt"])
        self.assertIn("Q: earlier?", seen["prompt"])
        # The project this chat works in, taken from its own manifest rather
        # than from anything the browser posted: the answer is looked for in
        # the code, and a browser does not get to name the directory.
        self.assertEqual(str(self.root.resolve()), seen["where"])
        # The route answers; the tab writes the answer down through the
        # ordinary op, so nothing has been written onto the goal here.
        goals, _ = chat_state.load_goals(self.session, self.root)
        self.assertEqual(EMPTY, GM.by_id(goals, "g1")["understanding"])

    def test_the_draft_route_writes_the_scenario_from_a_screenshot(self):
        shots = self.trajdir / "attachments"
        shots.mkdir()
        shot = shots / "20260824-000000-aaaa.png"
        shot.write_bytes(b"\x89PNG\r\n")
        shots, shot = shots.resolve(), shot.resolve()
        seen = {}

        def answer(lines, engine=None, read_dirs=None, search_dir=""):
            seen["prompt"] = "\n".join(lines)
            seen["dirs"] = read_dirs
            return {"ok": True, "answer": "Two people edit one tree."}

        with mock.patch.object(ui, "_answer", answer):
            with server_for(self.trajdir) as url:
                out = post_json(url + "/api/draft_scenario", {
                    "text": "two ppl one tree",
                    "shots": [{"path": str(shot), "name": "the rail"}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual("Two people edit one tree.", out["scenario"])
        self.assertEqual([{"path": str(shot), "name": "the rail"}],
                         out["shots"])
        self.assertIn(str(shot), seen["prompt"])
        self.assertEqual([str(shots)], seen["dirs"])


if __name__ == "__main__":
    unittest.main()
