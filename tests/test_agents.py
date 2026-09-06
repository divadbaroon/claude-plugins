"""The agents behind the goal page: how a turn is routed.

Every interaction is written to a local event log; the Overseer is asked
only on the transitions the trigger policy names; each of the six roles
is a callable the orchestrator hands the turn to. The model is never in
these tests -- the agents are stood in for -- and neither is a build
process, except in the one test that runs a stub `claude` to show the
build's end reaching the agents through build.py itself.
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import build as BUILD  # noqa: E402
from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory.agents import chat as CHAT  # noqa: E402
from human_compact.trajectory.agents import context as CTX  # noqa: E402
from human_compact.trajectory.agents import events as EV  # noqa: E402
from human_compact.trajectory.agents import orchestrator as ORCH  # noqa: E402
from human_compact.trajectory.agents import overseer as OVERSEER  # noqa: E402
from human_compact.trajectory.agents import policy as POLICY  # noqa: E402
from human_compact.trajectory.agents import runtime as RT  # noqa: E402
from human_compact.trajectory.agents import trace as TRACE  # noqa: E402
from human_compact.trajectory.agents import verifier as VERIFIER  # noqa: E402

GOAL, PIECE = "g1", "g1.1"
ROWS = ("taaaa0001", "taaaa0002")


def _answer(say="", todos=(), needs=None):
    return {"ok": True, "say": say, "todos": list(todos),
            "needs": needs or {"kind": "", "question": ""}}


class Recorder:
    """A stand-in agent: answers in order, remembers what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, *args, **kw):
        self.calls.append((args, kw))
        if not self.answers:
            raise AssertionError("asked more than it had answers for")
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class FakeRuntime(RT.Runtime):
    """A runtime that only remembers. ``build`` marks the rows done on the
    tree, as a build that worked would; ``reopen`` leaves them."""

    kind = "fake"

    def __init__(self, session_id, root, cwd="", preview=None, discovery="the project is a Flask app; it runs with `python app.py` on port 5000"):
        super().__init__(cwd, root)
        self.session_id = session_id
        self.builds, self.reopens, self.discoveries = [], [], []
        self.preview = preview or {"ok": True, "status": "unconfigured"}
        self.discovery = discovery

    def discover(self, question=""):
        self.discoveries.append(question)
        return "Directory: %s\nTop level: app.py, templates/\n%s" % (self.cwd, self.discovery)

    def preview_state(self, session_id=""):
        return dict(self.preview)

    def build(self, session_id, root, goal_id, row_ids, quick=False):
        self.builds.append((goal_id, list(row_ids), quick))
        mark_rows(session_id, root, goal_id, row_ids, "done")
        return {"ok": True, "started": True, "rows": list(row_ids), "quick": quick,
                "claude_session_id": "run-%d" % len(self.builds)}

    def reopen(self, session_id, root, goal_id, row_id, note):
        self.reopens.append((goal_id, row_id, note))
        return {"ok": True, "row": row_id}


def mark_rows(session_id, root, goal_id, row_ids, status):
    with CS.session_lock(session_id, root, wait_s=5):
        goals, important = CS.load_goals(session_id, root)
        goal = GM.by_id(goals, goal_id)
        for row in goal.get("todo_items") or []:
            if row["id"] in row_ids:
                row["status"] = status
        GM.sanitize(goals)
        CS.save_goals(session_id, goals, important, root)


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat"
        env = mock.patch.dict(os.environ, {"HUMAN_COMPACT_HOME": str(self.root / "hc-home"),
                                           "HC_AUTOSYNC_SECONDS": "0"})
        env.start()
        self.addCleanup(env.stop)
        p = CS.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        top = GM.new_goal(GOAL, "Create an interface to import the dataset", origin="user")
        piece = GM.new_goal(PIECE, "Save the dataset locally", GOAL, origin="user")
        piece["todo_items"] = [{"id": ROWS[0], "text": "Write the file", "depth": 0},
                               {"id": ROWS[1], "text": "Name it after the dataset", "depth": 0}]
        goals = {"version": 1, "goals": [top, piece]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        self.project = self.root / "project"
        self.project.mkdir()
        p.manifest.write_text(json.dumps({"cwd": str(self.project)}))
        self.runtime = FakeRuntime(self.session, self.root, str(self.project))
        self.tracer = TRACE.Tracer(self.session, self.root)

    def orchestrator(self, **agents):
        return ORCH.Orchestrator(self.session, self.root, cwd=str(self.project),
                                 runtime=self.runtime, agents=agents, tracer=self.tracer)

    def held(self):
        return {"root": self.root, "cwd": str(self.project), "digest": "",
                "session": self.session, "goal": "Create an interface to import the dataset",
                "subgoal": "Save the dataset locally", "subgoal_id": PIECE}

    def events(self, *types):
        return [e["type"] for e in EV.read(self.session, self.root)
                if not types or e["type"] in types]

    def routed(self):
        """(event, action) for every time the Overseer was asked."""
        return [(e["payload"]["event"], e["payload"]["action"])
                for e in EV.read(self.session, self.root, types=[POLICY.OVERSEER_ROUTED])]


class BartRoutingTests(AgentCase):
    def test_an_ordinary_message_goes_to_chat_not_brainstorm(self):
        chat = Recorder(_answer("Parquet keeps the types; good call.", ["Write the file as parquet"]))
        brainstorm = Recorder()
        orch = self.orchestrator(chat=chat, brainstorm=brainstorm)
        out = orch.bart_message(self.held(), [{"role": "you", "text": "I want to save the file as parquet"}])
        self.assertTrue(out["ok"], out)
        self.assertEqual([("text", "Parquet keeps the types; good call."),
                          ("proposal", "Write the file as parquet")],
                         [(r["kind"], r["text"]) for r in out["replies"]])
        self.assertEqual(1, len(chat.calls))
        self.assertEqual([], brainstorm.calls)
        self.assertEqual([("bart.message", "chat")], self.routed())
        self.assertEqual(1, orch.overseer_calls)
        # The chat got the conversation, where it is, and no card grammar.
        args, kw = chat.calls[0]
        self.assertEqual("I want to save the file as parquet", args[0][0]["text"])
        self.assertIn('"Save the dataset locally"', "\n".join(kw["focus"]))
        self.assertNotIn("`todos`", "\n".join(kw["focus"]))
        # The log has the turn whole; the trace has its shape.
        self.assertEqual(["bart.message", "overseer.routed", "chat.replied"], self.events())
        self.assertEqual(["bart.message", "bart.message > overseer.route",
                          "bart.message > chat.reply"], self.tracer.tree())

    def test_asking_for_options_goes_to_brainstorm(self):
        chat = Recorder()
        brainstorm = Recorder({"ok": True, "say": "Three ways.", "card": "focus", "replies": [
            {"kind": "text", "text": "Three ways."},
            {"kind": "text", "text": "Which?\n- parquet -- typed\n- csv -- plain"}]})
        orch = self.orchestrator(chat=chat, brainstorm=brainstorm)
        out = orch.bart_message(self.held(), [
            {"role": "you", "text": "Can you brainstorm some options for the file format?"}])
        self.assertTrue(out["ok"], out)
        self.assertEqual("brainstorm", out["route"])
        self.assertEqual([], chat.calls)
        self.assertEqual(1, len(brainstorm.calls))
        self.assertIn("`todos`", "\n".join(brainstorm.calls[0][1]["focus"]))
        self.assertEqual([("bart.message", "brainstorm")], self.routed())
        self.assertEqual(["bart.message", "overseer.routed", "brainstorm.replied"], self.events())

    def test_asking_for_a_plan_goes_to_the_path_agent(self):
        path = Recorder({"ok": True, "say": "", "card": "todos", "replies": [
            {"kind": "proposal", "text": "Pick the format"},
            {"kind": "proposal", "text": "Write the file"}]})
        orch = self.orchestrator(chat=Recorder(), brainstorm=Recorder(), path=path)
        out = orch.bart_message(self.held(), [{"role": "you", "text": "break this down into steps"}])
        self.assertEqual("replan", out["route"])
        self.assertEqual(2, sum(1 for r in out["replies"] if r["kind"] == "proposal"))
        self.assertEqual([("bart.message", "replan")], self.routed())

    def test_a_choice_only_the_reader_can_make_goes_to_brainstorm(self):
        chat = Recorder(_answer("It depends on who reads the file after.",
                                needs={"kind": "human_preference",
                                       "question": "Should the export be for people or for pandas?"}))
        brainstorm = Recorder({"ok": True, "say": "", "card": "questions", "replies": [
            {"kind": "text", "text": "Who reads it? (pick one)\n- people -- csv\n- pandas -- parquet"}]})
        orch = self.orchestrator(chat=chat, brainstorm=brainstorm)
        out = orch.bart_message(self.held(), [{"role": "you", "text": "export it somehow"}])
        self.assertTrue(out["ok"], out)
        # The chat's word first, then the question put properly.
        self.assertEqual([("text", "It depends on who reads the file after."),
                          ("text", "Who reads it? (pick one)\n- people -- csv\n- pandas -- parquet")],
                         [(r["kind"], r["text"]) for r in out["replies"]])
        self.assertEqual("Should the export be for people or for pandas?",
                         brainstorm.calls[0][1]["question"])
        self.assertEqual([("bart.message", "chat"), ("chat.needs_human", "brainstorm")],
                         self.routed())
        self.assertEqual(2, orch.overseer_calls)

    def test_a_fact_of_the_project_is_discovered_not_asked(self):
        chat = Recorder(
            _answer("", needs={"kind": "environment",
                               "question": "which web framework the project uses"}),
            _answer("It is a Flask app, so the route goes in app.py.", ["Add the /export route to app.py"]))
        brainstorm = Recorder()
        orch = self.orchestrator(chat=chat, brainstorm=brainstorm)
        out = orch.bart_message(self.held(), [{"role": "you", "text": "where does the export route go?"}])
        self.assertTrue(out["ok"], out)
        self.assertEqual("chat", out["route"])
        self.assertEqual([("text", "It is a Flask app, so the route goes in app.py."),
                          ("proposal", "Add the /export route to app.py")],
                         [(r["kind"], r["text"]) for r in out["replies"]])
        # Nobody asked the reader: the brainstorm never ran, and no reply
        # carries the question.
        self.assertEqual([], brainstorm.calls)
        self.assertFalse(any("framework" in r["text"] for r in out["replies"]))
        # The runtime was asked, and the chat's second turn got the answer.
        self.assertEqual(["which web framework the project uses"], self.runtime.discoveries)
        self.assertEqual(2, len(chat.calls))
        self.assertIn("Flask", chat.calls[1][1]["discovered"])
        self.assertEqual("", chat.calls[0][1]["discovered"])
        self.assertEqual([("bart.message", "chat"), ("chat.needs_discovery", "chat")],
                         self.routed())
        self.assertEqual(["bart.message", "overseer.routed", "chat.needs_discovery",
                          "overseer.routed", "discover.done", "chat.replied"], self.events())
        # And what was found is kept for the next agent.
        kinds = [u["kind"] for u in CTX.load(self.session, self.root)]
        self.assertEqual(["discovered_dependency"], kinds)
        self.assertEqual(["bart.message", "bart.message > overseer.route",
                          "bart.message > chat.reply", "bart.message > overseer.route",
                          "bart.message > discover", "bart.message > chat.reply"],
                         self.tracer.tree())

    def test_a_model_that_could_not_answer_is_reported(self):
        orch = self.orchestrator(chat=Recorder({"ok": False, "error": "claude CLI not found on PATH"}),
                                 brainstorm=Recorder())
        out = orch.bart_message(self.held(), [{"role": "you", "text": "hi"}])
        self.assertFalse(out["ok"])
        self.assertIn("claude CLI not found", out["error"])
        self.assertNotIn("replies", out)


class BuildLoopTests(AgentCase):
    def test_rows_handed_to_the_build_go_to_the_build_agent(self):
        orch = self.orchestrator(chat=Recorder(), brainstorm=Recorder())
        out = orch.build_requested(PIECE, list(ROWS))
        self.assertTrue(out["ok"], out)
        self.assertEqual(list(ROWS), out["rows"])
        self.assertEqual([(PIECE, list(ROWS), False)], self.runtime.builds)
        self.assertEqual([("todo.build_requested", "build")], self.routed())
        self.assertEqual(["todo.build_requested", "overseer.routed", "build.started"], self.events())
        self.assertEqual(["todo.build", "todo.build > overseer.route",
                          "todo.build > build.agent"], self.tracer.tree()[:3])

    def test_a_finished_build_is_verified(self):
        verify = Recorder({"passed": True, "reason": "looks right", "evidence": {}})
        orch = self.orchestrator(verify=verify)
        orch.build_requested(PIECE, list(ROWS))
        repaired = orch.build_finished(PIECE, "idle", list(ROWS), run_id="run-1")
        self.assertFalse(repaired)
        self.assertEqual(1, len(verify.calls))
        self.assertEqual((self.session, self.root, PIECE, list(ROWS)), verify.calls[0][0][:4])
        self.assertIn(("build.completed", "verify"), self.routed())

    def test_a_pass_completes_the_work_and_the_overseer_hears_it(self):
        # The real Verifier, on rows the fake build marked done and a run
        # record that ended clean.
        orch = self.orchestrator()
        orch.build_requested(PIECE, list(ROWS))
        BUILD._save_run(self.session, self.root, {"goal_id": PIECE, "status": "idle",
                                                  "exit_code": 0, "error": ""})
        orch.build_finished(PIECE, "idle", list(ROWS), run_id="run-1")
        self.assertEqual([("todo.build_requested", "build"), ("build.completed", "verify"),
                          ("verify.passed", "none")], self.routed())
        self.assertIn("verify.passed", self.events())
        self.assertEqual([], self.runtime.reopens)
        kinds = [(u["kind"], u["todoId"]) for u in CTX.load(self.session, self.root)]
        self.assertEqual([("verification_result", ""), ("todo_status", ROWS[0]),
                          ("todo_status", ROWS[1])], kinds)
        self.assertEqual(0, orch.state()["attempts"].get(PIECE, 0))
        # The Terminal saw it.
        said = [l["text"] for l in BUILD.load_activity(self.session, self.root, PIECE)]
        self.assertEqual(["verifying the build",
                          "verified: every row is done, the run ended clean"], said)
        self.assertEqual(["build.finished", "build.finished > overseer.route",
                          "build.finished > verifier.agent",
                          "build.finished > overseer.route"], self.tracer.tree()[-4:])

    def test_a_fail_repairs_up_to_the_limit_then_asks_the_reader(self):
        verify = Recorder({"passed": False, "reason": "the page returns 500", "evidence": {}})
        orch = self.orchestrator(verify=verify)
        orch.build_requested(PIECE, list(ROWS))
        # First fail: the row goes back out with the reason as its note.
        self.assertTrue(orch.build_finished(PIECE, "idle", list(ROWS), run_id="run-1"))
        self.assertEqual(1, len(self.runtime.reopens))
        goal_id, row_id, note = self.runtime.reopens[0]
        self.assertEqual((PIECE, ROWS[0]), (goal_id, row_id))
        self.assertIn("Verification failed: the page returns 500", note)
        self.assertEqual(1, orch.state()["attempts"][PIECE])
        # Second fail: once more.
        self.assertTrue(orch.build_finished(PIECE, "idle", list(ROWS), run_id="run-1"))
        self.assertEqual(2, len(self.runtime.reopens))
        # Third fail: the limit is spent; the reader is told, in the
        # piece's conversation and the Terminal, and nothing is rebuilt.
        self.assertFalse(orch.build_finished(PIECE, "idle", list(ROWS), run_id="run-1"))
        self.assertEqual(2, len(self.runtime.reopens))
        routed = self.routed()
        self.assertEqual([("verify.failed", "build"), ("verify.failed", "build"),
                          ("verify.failed", "chat")],
                         [r for r in routed if r[0] == "verify.failed"])
        self.assertIn("verify.escalated", self.events())
        chat = CS.load_bart_chats(self.session, self.root)[PIECE]
        self.assertEqual("bart", chat[-1]["who"])
        self.assertIn("3 times and it still does not check out: the page returns 500", chat[-1]["text"])
        self.assertEqual(0, orch.state()["attempts"][PIECE])
        lines = [l["text"] for l in BUILD.load_activity(self.session, self.root, PIECE)]
        self.assertIn("repair 1 of 2: the page returns 500", lines)
        self.assertIn("repair 2 of 2: the page returns 500", lines)
        self.assertIn("verification failed 3 times; asking you", lines)

    def test_a_build_that_failed_or_asked_is_left_to_the_page(self):
        orch = self.orchestrator(verify=Recorder())
        orch.build_finished(PIECE, "failed", list(ROWS), error="API Error: 500")
        orch.build_finished(PIECE, "waiting", list(ROWS))
        self.assertEqual([("build.failed", "none"), ("build.question", "none")], self.routed())
        self.assertEqual([], self.runtime.reopens)
        self.assertEqual(["run_result"], [u["kind"] for u in CTX.load(self.session, self.root)])

    def test_the_real_verifier_reads_the_rows_the_run_and_the_preview(self):
        mark_rows(self.session, self.root, PIECE, list(ROWS), "done")
        BUILD._save_run(self.session, self.root, {"goal_id": PIECE, "status": "idle",
                                                  "exit_code": 0, "error": ""})
        self.assertTrue(VERIFIER.verify(self.session, self.root, PIECE, ROWS, self.runtime)["passed"])
        self.runtime.preview = {"ok": True, "status": "failed", "exit_code": 1}
        verdict = VERIFIER.verify(self.session, self.root, PIECE, ROWS, self.runtime)
        self.assertFalse(verdict["passed"])
        self.assertIn("run failed after the build (exit 1)", verdict["reason"])
        self.runtime.preview = {"ok": True, "status": "running", "healthy": True}
        verdict = VERIFIER.verify(self.session, self.root, PIECE, ROWS, self.runtime)
        self.assertTrue(verdict["passed"])
        self.assertIn("the preview answers", verdict["reason"])
        mark_rows(self.session, self.root, PIECE, [ROWS[1]], "failed")
        verdict = VERIFIER.verify(self.session, self.root, PIECE, ROWS, self.runtime)
        self.assertIn("1 row failed: Name it after the dataset", verdict["reason"])
        mark_rows(self.session, self.root, PIECE, list(ROWS), "done")
        checks = [lambda rt: (False, "GET /export answered 404")]
        verdict = VERIFIER.verify(self.session, self.root, PIECE, ROWS, self.runtime, checks)
        self.assertEqual("GET /export answered 404", verdict["reason"])


class TriggerPolicyTests(AgentCase):
    def test_minor_events_are_recorded_and_never_routed(self):
        for op in ({"op": "add_todo_row", "goal_id": PIECE, "text": "write the tests"},
                   {"op": "set_todo_text", "goal_id": PIECE, "id": ROWS[0], "text": "save as parquet"},
                   {"op": "set_todo_done", "goal_id": PIECE, "id": ROWS[0], "done": True},
                   {"op": "set_notes", "goal_id": PIECE, "text": "a note"},
                   {"op": "remove_todo_row", "goal_id": PIECE, "id": ROWS[1]}):
            ORCH.note_op(self.session, self.root, op, {"ok": True})
        self.assertEqual(["todo.added", "todo.text_edited", "todo.done_toggled",
                          "notes.edited", "todo.removed"], self.events())
        self.assertEqual([], self.routed())
        # And the log's lines have the shape.
        [first] = EV.read(self.session, self.root, limit=1, types=["todo.added"])
        self.assertEqual({"id", "projectId", "timestamp", "type", "source", "payload",
                          "subgoalId", "todoId", "runId"}, set(first))
        self.assertEqual(("user", PIECE, str(self.project)),
                         (first["source"], first["subgoalId"], first["projectId"]))
        # A minor event handed to the orchestrator directly goes nowhere.
        orch = self.orchestrator(chat=Recorder(), brainstorm=Recorder())
        for kind in ("chat.saved", "preview.op", "todo.text_edited", "build.started"):
            self.assertIsNone(orch.handle(orch.emit(kind, EV.USER, {}, subgoal_id=PIECE)))
        self.assertEqual(0, orch.overseer_calls)

    def test_meaningful_transitions_invoke_the_overseer_once_each(self):
        self.assertEqual({"bart.message", "todo.build_requested", "plan.requested",
                          "build.completed", "build.failed", "build.question",
                          "verify.passed", "verify.failed",
                          "chat.needs_human", "chat.needs_discovery"}, set(POLICY.MEANINGFUL))
        orch = self.orchestrator(chat=Recorder(), brainstorm=Recorder(),
                                 verify=Recorder({"passed": True, "reason": "ok", "evidence": {}}))
        for kind in ("build.question", "build.failed", "plan.requested"):
            with self.subTest(kind):
                before = orch.overseer_calls
                event = orch.emit(kind, EV.SYSTEM, {}, subgoal_id=PIECE)
                if kind == "plan.requested":
                    orch.agents["path"] = Recorder({"ok": True, "say": "", "card": "todos", "replies": []})
                orch.handle(event, {"transcript": [{"role": "you", "text": "plan"}]})
                self.assertEqual(before + 1, orch.overseer_calls)
        self.assertEqual(["build.question", "build.failed", "plan.requested"],
                         [r[0] for r in self.routed()])

    def test_the_readers_words_are_read_conservatively(self):
        self.assertTrue(POLICY.wants_brainstorm("brainstorm this with me"))
        self.assertTrue(POLICY.wants_brainstorm("what are my options here?"))
        self.assertTrue(POLICY.wants_brainstorm("give me a few alternatives"))
        self.assertFalse(POLICY.wants_brainstorm("the parquet option is fine"))
        self.assertFalse(POLICY.wants_brainstorm("thanks"))
        self.assertTrue(POLICY.wants_plan("break it down into steps"))
        self.assertFalse(POLICY.wants_plan("the plan is fine"))
        self.assertEqual(("chat", "an ordinary message: answered, not brainstormed"),
                         POLICY.bart_intent("I want to save the file as parquet"))

    def test_the_overseer_is_rules_the_reader_can_read(self):
        state = {"attempts": {PIECE: 0}}
        fail = EV.new_event("verify.failed", "system", {"reason": "500"}, subgoal_id=PIECE)
        self.assertEqual("build", OVERSEER.route(fail, state)["action"])
        self.assertEqual("build", OVERSEER.route(fail, {"attempts": {PIECE: 1}})["action"])
        self.assertEqual("chat", OVERSEER.route(fail, {"attempts": {PIECE: 2}})["action"])
        done = EV.new_event("build.completed", "system", {}, subgoal_id=PIECE)
        self.assertEqual("verify", OVERSEER.route(done, state)["action"])
        passed = OVERSEER.route(EV.new_event("verify.passed", "system",
                                             {"reason": "ok", "rows": list(ROWS)}, subgoal_id=PIECE), state)
        self.assertEqual("none", passed["action"])
        self.assertEqual(["verification_result", "todo_status", "todo_status"],
                         [u["kind"] for u in passed["contextUpdates"]])
        self.assertEqual({"action", "reason", "targetSubgoalId", "targetTodoId", "contextUpdates"},
                         set(passed))


class RuntimeTests(AgentCase):
    def test_local_is_the_runtime_and_daytona_is_named_but_not_here(self):
        local = RT.make(cwd=str(self.project), root=self.root)
        self.assertIsInstance(local, RT.LocalRuntime)
        self.assertEqual({"kind": "local", "cwd": str(self.project)}, local.describe())
        with mock.patch.dict(os.environ, {"HC_AGENT_RUNTIME": "daytona"}):
            with self.assertRaisesRegex(RuntimeError, "DaytonaRuntime is not here yet"):
                RT.make(cwd=str(self.project), root=self.root)
        with self.assertRaises(ValueError):
            RT.make("cloud", cwd=str(self.project))
        # The Build agent reaches build.start through it, as the op did.
        with mock.patch.object(BUILD, "start", return_value={"ok": True, "rows": ["r"]}) as start:
            self.assertEqual({"ok": True, "rows": ["r"]},
                             local.build(self.session, self.root, PIECE, ["r"], quick=True))
        start.assert_called_once_with(self.session, self.root, PIECE, ["r"], quick=True)

    def test_the_local_runtime_stays_inside_the_project_and_reads_what_it_is(self):
        (self.project / "package.json").write_text('{"name": "app", "scripts": {"dev": "vite"}}')
        (self.project / "src").mkdir()
        local = RT.LocalRuntime(str(self.project), self.root)
        local.write_file("src/a.txt", "hello")
        self.assertEqual("hello", local.read_file("src/a.txt"))
        with self.assertRaises(ValueError):
            local.read_file("../outside")
        found = local.discover("which framework")
        self.assertIn("Top level: package.json, src/", found)
        self.assertIn('"dev": "vite"', found)
        ran = local.run([sys.executable, "-c", "print('hi')"])
        self.assertEqual((True, "hi\n"), (ran["ok"], ran["out"]))
        tracer = TRACE.Tracer()
        TRACE.use(tracer)
        try:
            local.read_file("src/a.txt")
            local.run(["definitely-not-a-command-xyz"])
        finally:
            TRACE.use(None)
        self.assertEqual(["file.read", "command.exec"], tracer.names())
        self.assertEqual("error", tracer.spans[1]["status"])


class EventLogTests(AgentCase):
    def test_the_log_is_one_line_per_event_and_stays_bounded(self):
        orch = self.orchestrator()
        for n in range(5):
            orch.emit("chat.saved", EV.USER, {"n": n}, subgoal_id=PIECE)
        spot = EV.path(self.session, self.root)
        self.assertEqual(5, len(spot.read_text().splitlines()))
        self.assertEqual([0, 1, 2, 3, 4], [e["payload"]["n"] for e in EV.read(self.session, self.root)])
        self.assertEqual([3, 4], [e["payload"]["n"] for e in EV.read(self.session, self.root, limit=2)])
        with mock.patch.object(EV, "KEEP_BYTES", 10), mock.patch.object(EV, "KEEP_LINES", 2):
            orch.emit("chat.saved", EV.USER, {"n": 5}, subgoal_id=PIECE)
        self.assertEqual([4, 5], [e["payload"]["n"] for e in EV.read(self.session, self.root)])
        with self.assertRaises(ValueError):
            EV.new_event("x", "robot")
        # The trace went beside it.
        orch.bart_message(self.held(), [{"role": "you", "text": "hi"}]) if False else None
        self.assertTrue(spot.with_name("agent_events.jsonl").exists())

    def test_hc_agents_0_is_the_old_path(self):
        with mock.patch.dict(os.environ, {"HC_AGENTS": "0"}):
            self.assertFalse(ORCH.enabled())
            self.assertIsNone(ORCH.note_op(self.session, self.root, {"op": "add_todo_row"}))
            self.assertFalse(ORCH.build_finished(self.session, self.root, PIECE, "idle", ROWS))
        self.assertEqual([], EV.read(self.session, self.root))


STUB = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
ids = [w.strip("[]") for w in prompt.split() if w.startswith("[t") and w.endswith("]")]
def say(text):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}), flush=True)
for i in ids:
    say(json.dumps({"id": i, "state": "DONE"}))
print(json.dumps({"type": "result", "is_error": False, "result": "done",
                  "usage": {"input_tokens": 10, "output_tokens": 2}}))
'''


class BuildWiringTests(AgentCase):
    """The build's end reaches the agents through build.py itself."""

    def test_a_real_build_ending_tells_the_agents(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "claude"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
               "HC_BUILD_MODE": "headless", "HC_BUILD_RESTART_CHECK": "0"}
        heard = []

        def finished(session_id, root, goal_id, ended, row_ids, run_id="", error=""):
            heard.append((session_id, goal_id, ended, list(row_ids), bool(run_id), error))
            return False

        BUILD._RUNS.clear()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ORCH, "build_finished", finished):
            out = BUILD.start(self.session, self.root, PIECE, list(ROWS))
            self.assertTrue(out["ok"], out)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not heard:
                time.sleep(0.05)
            run = BUILD._run_for(self.session, self.root, PIECE)
            if run and run.thread:
                run.thread.join(timeout=10)
        self.assertEqual([(self.session, PIECE, "idle", list(ROWS), True, "")], heard)
        goals, _ = CS.load_goals(self.session, self.root)
        rows = {r["id"]: r.get("status") for r in GM.by_id(goals, PIECE)["todo_items"]}
        self.assertEqual({ROWS[0]: "done", ROWS[1]: "done"}, rows)


if __name__ == "__main__":
    unittest.main()
