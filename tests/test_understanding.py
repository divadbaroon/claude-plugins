"""The scenario a goal's work is for, and the questions asked about it.

The rail's Understanding tab writes onto a goal what the work is actually
for, in the reader's words -- typed, with screenshots of it pasted beside
them -- and what they want answered about it. The scenario itself is shaped
into GIVEN / WHEN / THEN, with a question beside every line the reader's own
words did not fill; the questions about it are answered in ordinary prose and
they can follow up. The answers are kept with the questions. All of it
survives the artifact posting its own tree back, and all of it opens every
build of that goal's rows.
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


EMPTY = {"scenario": "", "shots": [], "questions": [],
         "evidence": [], "pending": {}}


class Engine:
    """A provider that answers once and remembers what it was asked."""

    def __init__(self, said="The later save wins: the tree is written whole,"
                            " so the second writer's copy replaces the first."):
        self.said, self.seen, self.dirs = said, "", None
        self.searched = None

    def generate_plain(self, prompt):
        self.seen = prompt
        return self.said

    def generate_reading(self, prompt, read_dirs=()):
        self.seen, self.dirs = prompt, list(read_dirs)
        return self.said

    def generate_searching(self, prompt, where="", read=None):
        self.seen, self.searched = prompt, where
        self.dirs = list(read) if read else None
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

    def generate_searching(self, prompt, where="", read=None):
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
                           "shots": [], "thread": []}], held["questions"])

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
    """A question about the scenario, answered the way a person would."""

    def tree(self):
        goal = GM.new_goal("g1", "Share a goal tree", None, origin="user")
        goal["notes"] = "Two machines, one document."
        goals = {"version": 1, "goals": [goal]}
        GM.sanitize(goals)
        return goals["goals"]

    def test_the_answer_comes_back_as_an_answer_and_not_as_a_form(self):
        # What this changes: the tab used to demand GIVEN / WHEN / THEN of
        # every answer, and a reader asking what happens to the second build
        # had to read the answer back out of three capitalised clauses.
        engine = Engine()
        out = ui.ask_scenario(self.tree(), "g1",
                              "Two people work one tree from two machines.",
                              "  Who   wins a conflict?  ", engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("Who wins a conflict?", out["asked"])
        self.assertEqual("The later save wins: the tree is written whole, so"
                         " the second writer's copy replaces the first.",
                         out["answer"])
        # Prose is asked for by name, and the form is not.
        self.assertIn("Answer in plain prose", engine.seen)
        self.assertNotIn("GIVEN <what is true before anything happens>",
                         engine.seen)
        # And the material to answer from is there: the scenario itself and
        # the goal it belongs to.
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
        self.assertIn("tool call", out["error"])
        # Asked twice before being refused, and told the second time what was
        # wrong with the first reply.
        self.assertEqual(2, len(engine.asked))
        self.assertIn("Your last reply was not an answer", engine.asked[1])
        self.assertNotIn("Your last reply was not an answer", engine.asked[0])

    def test_a_second_try_that_is_an_answer_is_the_answer(self):
        engine = Replies('{"command":"rg -n building"}',
                         "The second waits for the first to finish.")
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "What happens with the second todo?",
                              engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("The second waits for the first to finish.",
                         out["answer"])

    def test_an_answer_wrapped_whole_in_a_fence_is_unwrapped(self):
        # The fence is the model's packaging, not the reader's answer, and
        # what is kept here is drawn as it stands.
        engine = Engine(said="```\nThe later save wins.\n```")
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "Who wins?", engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("The later save wins.", out["answer"])

    def test_an_answer_that_quotes_code_keeps_the_code_it_quotes(self):
        # A fence around part of an answer is the code being shown, not
        # packaging: taking it off would run the quote into the prose.
        said = ("The later save wins -- the whole tree is written:\n\n"
                "```python\nwrite(tree)\n```\n\nSo the second copy replaces"
                " the first.")
        engine = Engine(said=said)
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "Who wins?", engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual(said, out["answer"])

    def test_an_answer_that_quotes_json_in_it_is_still_an_answer(self):
        # Only the opening line is weighed against a tool call. An answer
        # about this project may well quote a line of JSON out of it, and
        # refusing that would refuse the answers that went and looked.
        said = ('The name is fixed in the manifest:\n\n'
                '{"name": "engelbart", "version": "0.19.2"}\n\n'
                'so a rename there is a rename everywhere.')
        engine = Replies(said)
        out = ui.ask_scenario(self.tree(), "g1", "Two people work one tree.",
                              "Where does the name come from?", engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual(said, out["answer"])
        self.assertEqual(1, len(engine.asked))

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
        # What is still open is said after looking, not instead of it.
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

    def test_a_printed_tool_call_is_asked_again_without_being_called_off(self):
        # The second try in the project keeps the project: what was wrong
        # with the first reply was that the call was printed, not that it
        # went looking.
        engine = Replies('{"tool":"grep","input":{"pattern":"building"}}',
                         "The second build waits for the first.")
        out = ui.ask_scenario(self.tree(), "g1", "Two builds at once.",
                              "What happens with the second todo?",
                              cwd=str(ROOT), engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual(2, len(engine.asked))
        self.assertIn("You really can search this project", engine.asked[1])
        self.assertNotIn("You have no tools on this call", engine.asked[1])

    def test_a_plain_answer_from_the_code_is_kept_as_it_stands(self):
        # Prose that reads like a report is an answer: nothing is asked twice
        # for saying where it looked.
        engine = Replies("I read build.py: the second build waits"
                         " (src/build.py:212).")
        out = ui.ask_scenario(self.tree(), "g1", "Two builds at once.",
                              "What happens with the second todo?",
                              cwd=str(ROOT), engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual(1, len(engine.asked))

    def test_a_follow_up_carries_the_thread_it_follows(self):
        engine = Engine()
        ui.ask_scenario(
            self.tree(), "g1", "Two people work one tree.",
            "And if both are offline?",
            # The tab's own spelling of a turn, which is not the ask panel's.
            turns=[{"q": "Who wins a conflict?", "a": "The later save wins."},
                   {"q": "asked, never answered", "a": ""}],
            engine=engine)
        self.assertIn("Q: Who wins a conflict?", engine.seen)
        self.assertIn("A: The later save wins.", engine.seen)
        self.assertNotIn("asked, never answered", engine.seen)
        self.assertLess(engine.seen.index("A: The later save wins."),
                        engine.seen.index("And if both are offline?"))


class AskWithScreenshotsTests(unittest.TestCase):
    """A question with screenshots pasted onto it.

    Half of what anyone asks about a screen is quicker shown than described,
    so a question carries its own images -- not the scenario's -- and the call
    is handed the files rather than a description of them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope = Path(self.tmp.name) / "chat-a"
        self.shots = self.scope / "attachments"
        self.shots.mkdir(parents=True)
        self.shot = self.shots / "20260831-000000-aaaa.png"
        self.shot.write_bytes(b"\x89PNG\r\n")
        self.shots, self.shot = self.shots.resolve(), self.shot.resolve()

    def tree(self):
        goal = GM.new_goal("g1", "Share a goal tree", None, origin="user")
        goals = {"version": 1, "goals": [goal]}
        GM.sanitize(goals)
        return goals["goals"]

    def test_a_questions_screenshot_is_named_for_the_call_to_open(self):
        engine = Engine(said="The rail is showing the done band.")
        out = ui.ask_scenario(
            self.tree(), "g1", "Two people work one tree.",
            "What is this panel showing?", engine=engine,
            shots=[{"path": str(self.shot), "name": "the rail"}],
            trajdir=self.scope)
        self.assertTrue(out["ok"], out)
        self.assertIn(str(self.shot), engine.seen)
        self.assertIn("Screenshots the question is about", engine.seen)
        # Named files are of no use to a provider not allowed to open them.
        self.assertEqual([str(self.shots)], engine.dirs)

    def test_a_screenshot_is_opened_by_a_call_that_is_also_searching(self):
        # The common case: the project is on this machine, so the question is
        # answered out of the code -- and the image it is about lives in the
        # workspace's attachments, which is not under that project.
        engine = Engine(said="It is the rail, drawn by bridge.js.")
        out = ui.ask_scenario(
            self.tree(), "g1", "Two people work one tree.",
            "Which file draws this?", cwd=str(ROOT), engine=engine,
            shots=[{"path": str(self.shot), "name": "the rail"}],
            trajdir=self.scope)
        self.assertTrue(out["ok"], out)
        self.assertEqual(str(ROOT), engine.searched)
        self.assertEqual([str(self.shots)], engine.dirs)

    def test_a_path_outside_the_workspace_is_not_named_at_all(self):
        # The path arrives from a browser and decides what a subprocess may
        # open: only this workspace's own attachments survive.
        engine = Engine(said="I cannot see it.")
        out = ui.ask_scenario(
            self.tree(), "g1", "Two people work one tree.",
            "What is this?", engine=engine,
            shots=[{"path": "/etc/passwd", "name": "passwd"}],
            trajdir=self.scope)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("/etc/passwd", engine.seen)
        self.assertNotIn("Screenshots the question is about", engine.seen)
        self.assertIsNone(engine.dirs)

    def test_a_questions_screenshots_open_the_build_of_its_rows(self):
        goal = GM.new_goal("g1", "Share a goal tree", None, origin="user")
        goal["understanding"] = {
            "scenario": "Two people work one tree.",
            "questions": [{"id": "qaaaa0001", "text": "What is this panel?",
                           "shots": [{"path": str(self.shot),
                                      "name": "the rail"}]}]}
        lines = "\n".join(GM.render_understanding(goal))
        self.assertIn("- What is this panel?", lines)
        self.assertIn("  - screenshot: " + str(self.shot), lines)


class DraftTheScenarioTests(unittest.TestCase):
    """The scenario mapped onto GIVEN / WHEN / THEN from what they had.

    Screenshots, rough words, or both -- and a question back for every line
    their words did not fill, rather than a plausible line written in for
    them.
    """

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
        engine = Engine(said="GIVEN two people share one goal tree\n"
                             "WHEN both save\nTHEN the later one wins")
        out = ui.draft_scenario(
            self.scope, "  ", [{"path": str(self.shot), "name": "the rail"}],
            engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN two people share one goal tree\n"
                         "WHEN both save\nTHEN the later one wins",
                         out["scenario"])
        self.assertEqual([], out["asks"])
        self.assertIn(str(self.shot), engine.seen)
        # Named files are of no use to a provider not allowed to open them.
        self.assertEqual([str(self.shots)], engine.dirs)

    def test_words_alone_are_mapped_without_opening_anything(self):
        engine = Engine(said="GIVEN two people share one tree\nWHEN both save"
                             "\nTHEN the later one wins")
        out = ui.draft_scenario(self.scope, "two ppl one tree, conflicts",
                                [], engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertIn("two ppl one tree, conflicts", engine.seen)
        # The form the words are being put into is asked for by name.
        self.assertIn("Map what they wrote onto GIVEN / WHEN / THEN",
                      engine.seen)
        # No files to open, so the plain round trip -- and no directory
        # handed to a subprocess for a call that reads nothing.
        self.assertIsNone(engine.dirs)

    def test_a_line_their_words_do_not_fill_comes_back_as_a_question(self):
        # The point of the mapping: an empty THEN stays an empty THEN, with
        # the question that would fill it beside it. This field opens every
        # build of the goal's rows -- a plausible line invented here is a
        # line nobody knows to check.
        engine = Engine(said="GIVEN two people share one tree\nWHEN both save"
                             "\nTHEN\nASK: which save should win?")
        out = ui.draft_scenario(self.scope, "two ppl one tree", [],
                                engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN two people share one tree\nWHEN both save"
                         "\nTHEN", out["scenario"])
        self.assertEqual(["which save should win?"], out["asks"])

    def test_words_that_map_onto_nothing_come_back_as_questions_only(self):
        engine = Engine(said="ASK who is doing this?\nASK what are they"
                             " looking at?")
        out = ui.draft_scenario(self.scope, "the thing is broken", [],
                                engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("", out["scenario"])
        self.assertEqual(["who is doing this?", "what are they looking at?"],
                         out["asks"])

    def test_markdown_the_form_asked_for_none_of_comes_off(self):
        engine = Engine(said="- **GIVEN** one tree\n- **THEN** one wins")
        out = ui.draft_scenario(self.scope, "one tree", [], engine=engine)
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN one tree\nTHEN one wins", out["scenario"])

    def test_a_reply_with_no_shape_in_it_leaves_the_box_alone(self):
        # Refused rather than emptied: what the reader typed is still the
        # best scenario anybody has.
        engine = Engine(said="Sure! Here is a lovely scenario for you.")
        out = ui.draft_scenario(self.scope, "two ppl one tree", [],
                                engine=engine)
        self.assertFalse(out["ok"])
        self.assertIn("did not map", out["error"])

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
            return {"ok": True, "answer": "The later save wins."}

        with mock.patch.object(ui, "_answer", answer):
            with server_for(self.trajdir) as url:
                out = post_json(url + "/api/ask_scenario", {
                    "goal": "g1", "scenario": "Two people, one tree.",
                    "question": "Who wins a conflict?",
                    "turns": [{"q": "earlier?", "a": "Nothing yet."}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual("The later save wins.", out["answer"])
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

    def test_the_draft_route_shapes_the_scenario_from_a_screenshot(self):
        shots = self.trajdir / "attachments"
        shots.mkdir()
        shot = shots / "20260824-000000-aaaa.png"
        shot.write_bytes(b"\x89PNG\r\n")
        shots, shot = shots.resolve(), shot.resolve()
        seen = {}

        def answer(lines, engine=None, read_dirs=None, search_dir=""):
            seen["prompt"] = "\n".join(lines)
            seen["dirs"] = read_dirs
            return {"ok": True, "answer": "GIVEN two people edit one tree"
                                          "\nWHEN both save\nTHEN"
                                          "\nASK which save should win?"}

        with mock.patch.object(ui, "_answer", answer):
            with server_for(self.trajdir) as url:
                out = post_json(url + "/api/draft_scenario", {
                    "text": "two ppl one tree",
                    "shots": [{"path": str(shot), "name": "the rail"}]})
        self.assertTrue(out["ok"], out)
        self.assertEqual("GIVEN two people edit one tree\nWHEN both save"
                         "\nTHEN", out["scenario"])
        self.assertEqual(["which save should win?"], out["asks"])
        self.assertEqual([{"path": str(shot), "name": "the rail"}],
                         out["shots"])
        self.assertIn(str(shot), seen["prompt"])
        self.assertEqual([str(shots)], seen["dirs"])


if __name__ == "__main__":
    unittest.main()
