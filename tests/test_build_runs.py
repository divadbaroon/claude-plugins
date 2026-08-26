"""The rail's Build: picked TODO rows handed to a headless Claude session.

A stub `claude` on PATH stands in for the CLI: it prints stream-json the way
the real one does, asks a question on its first turn and finishes the row on
the resumed one. What is under test is the plumbing -- the prompt, the
statuses, the question thread, the answer going back into the same session --
never the model.
"""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import build as BUILD  # noqa: E402
from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402


STUB = r'''#!/usr/bin/env python3
import json, sys, os, time
# What the real CLI reports on its last line. The build reads it to learn what
# a row costs; every turn here says the same, so a test can do the arithmetic.
USAGE = {"input_tokens": 1000, "output_tokens": 200,
         "cache_read_input_tokens": 4000}
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
resume = "--resume" in args
log = os.environ["STUB_LOG"]
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if os.environ.get("STUB_FAIL_ONCE") == "1":
    marker = log + ".failed"
    with open(log, "a") as fh:
        fh.write(json.dumps({"args": args, "prompt": prompt, "resume": resume}) + "\n")
    if not os.path.exists(marker):
        open(marker, "w").write("1")
        print(json.dumps({"type": "result", "is_error": True,
                          "result": "API Error: 500 Internal server error"}))
        raise SystemExit(1)
    ids = []
    for line in open(log):
        p0 = json.loads(line)["prompt"]
        ids += [w.strip("[]") for w in p0.split() if w.startswith("[t") and w.endswith("]")]
    for i in dict.fromkeys(ids):
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": json.dumps({"id": i, "state": "DONE"})}]}}), flush=True)
    print(json.dumps({"type": "result", "is_error": False, "result": "done",
                      "usage": USAGE}))
    raise SystemExit(0)
if os.environ.get("STUB_FINISH") == "1" and not resume:
    with open(log, "a") as fh:
        fh.write(json.dumps({"args": args, "prompt": prompt}) + "\n")
    ids = [w.strip("[]") for w in prompt.split() if w.startswith("[t") and w.endswith("]")]
    for i in ids:
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": json.dumps({"id": i, "state": "DONE"})}]}}), flush=True)
    print(json.dumps({"type": "result", "is_error": False, "result": "done",
                      "usage": USAGE}))
    raise SystemExit(0)
with open(log, "a") as fh:
    fh.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(),
                         "resume": resume,
                         "api_key": os.environ.get("ANTHROPIC_API_KEY")}) + "\n")
def say(text):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}), flush=True)
if not resume:
    ids = [w.strip("[]") for w in prompt.split() if w.startswith("[t") and w.endswith("]")]
    say("Looking at the rows.")
    say('{"id": "%s", "question": "Which router file: src/a.ts or src/b.ts?"}' % ids[0])
    print(json.dumps({"type": "result", "is_error": False, "result": "asked",
                      "usage": USAGE}))
else:
    try:
        msg = json.loads(prompt)
    except ValueError:
        # Not an answer: a withdrawal, or any other prose. Acknowledge and end.
        say("Moving on.")
        print(json.dumps({"type": "result", "is_error": False, "result": "ok",
                          "usage": USAGE}))
        raise SystemExit(0)
    if "reopened" in msg:
        say("Fixing: %s" % msg["reopened"])
    else:
        say("Thanks: %s" % msg["answer"])
    say('{"id": "%s", "state": "DONE"}' % msg["id"])
    print(json.dumps({"type": "result", "is_error": False, "result": "done",
                      "usage": USAGE}))
'''


def goal(gid, title, **fields):
    g = GM.new_goal(gid, title, origin="user")
    g.update(fields)
    return g


class BuildRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-build"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        goals = {"version": 1, "goals": [goal(
            "g1", "Ship the router",
            todo_items=[
                {"id": "taaaa0001", "text": "Add the route", "depth": 0},
                {"id": "taaaa0002", "text": "and its test", "depth": 1},
                {"id": "taaaa0003", "text": "Update the docs", "depth": 0},
            ])]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        # a stub claude ahead of the real one
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "claude"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "stub.log"
        self.old_env = dict(os.environ)
        os.environ["PATH"] = str(self.bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["STUB_LOG"] = str(self.log)
        os.environ["HC_BUILD_MODE"] = "headless"
        # An API key in the server's shell must not reach the build: the
        # reader's subscription pays for the reader's button.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-should-not-leak"
        os.environ.pop("HC_USE_API_KEY", None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
        BUILD._RUNS.clear()
        # Registered last, so it runs first: a run's reader thread writes the
        # run record and its log until the stub exits, and a temp directory
        # removed under it is "Directory not empty" on the way out -- the
        # one failure CI has had in this file, on both platforms.
        self.addCleanup(self._drain_runs)

    @staticmethod
    def _drain_runs(seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            threads = [run.thread for run in list(BUILD._RUNS.values())
                       if getattr(run, "thread", None) is not None
                       and run.thread.is_alive()]
            if not threads:
                break
            for thread in threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        BUILD._RUNS.clear()

    def rows(self):
        goals, _ = chat_state.load_goals(self.session, self.root)
        return {r["id"]: (r["status"], r["question"])
                for r in GM.by_id(goals, "g1")["todo_items"]}

    def wait_for(self, predicate, seconds=8):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_the_directive_parser_reads_only_the_protocol(self):
        text = ('prose {"id": "taaaa0001", "question": "Which?"} more '
                '{"id": "taaaa0003", "state": "DONE"} {"id": "x", "junk": 1} '
                '{"id": "taaaa0002", "state": "WIP"}')
        self.assertEqual(
            [{"id": "taaaa0001", "question": "Which?"},
             {"id": "taaaa0003", "state": "DONE"}],
            BUILD.directives(text))

    def test_the_prompt_carries_the_tree_the_rows_and_the_protocol(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        rows = BUILD.picked_with_children(g["todo_items"], ["taaaa0001"])
        prompt = BUILD.compose_prompt(self.session, goals, important, [], g, rows)
        self.assertIn("# Current goals for this Claude chat", prompt)
        self.assertIn("Ship the router", prompt)
        # the picked parent carries its id; its child rides along without one
        self.assertIn("- Add the route [taaaa0001]", prompt)
        self.assertIn("  - and its test\n", prompt)
        self.assertNotIn("Update the docs", prompt.split("# The work")[1])
        self.assertIn('{"id": "<row id>", "question": "<under 100 characters>"}', prompt)
        self.assertIn('{"id": "<row id>", "state": "DONE"}', prompt)

    def test_the_prompt_names_the_project_it_will_run_in(self):
        # The tree names goals; it never says the place they are for. That is
        # the record the workspace already keeps for the directory the chat
        # was started in -- what the reader called it and what they wrote it
        # is for -- so the session opens knowing which repository it is in.
        from human_compact.trajectory import project_store as PS
        PS.save_project(self.root, str(self.root),
                        {"name": "Router", "objective": "one router, no forks"})
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        rows = BUILD.picked_with_children(g["todo_items"], ["taaaa0001"])
        prompt = BUILD.compose_prompt(self.session, goals, important, [], g,
                                      rows, root=self.root)
        head = prompt.split("# Current goals")[0]
        self.assertIn("# Project", head)
        self.assertIn(f"Router · {self.root}", head)
        self.assertIn("Objective: one router, no forks", head)

    def test_a_preview_is_the_prompt_a_build_would_open_on(self):
        # What the rail's Prompt tab prints above the reader's own words. With
        # nothing picked it previews every row still to do, since that is the
        # build the reader is standing in front of.
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        every = BUILD.preview(self.session, self.root, goals, important, g, [])
        work = every.split("# The work")[1]
        self.assertIn("- Add the route [taaaa0001]", work)
        self.assertIn("- Update the docs [taaaa0003]", work)
        # and the picked rows alone once there are any
        one = BUILD.preview(self.session, self.root, goals, important, g,
                            ["taaaa0003"]).split("# The work")[1]
        self.assertIn("- Update the docs [taaaa0003]", one)
        self.assertNotIn("Add the route", one)

    def test_the_context_a_preview_carries_is_that_string_without_the_rows(self):
        # What the rail prices a TODO row against: the same prompt with no
        # rows in it, counted the way the Prompt tab counts the whole of it.
        # Anything less -- the goal-context file alone, which is what `cost`
        # measures -- reads low beside the number the tab prints.
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        bare = BUILD.compose_prompt(
            self.session, goals, important,
            chat_state.load_prompts(self.session, self.root), g, [],
            root=self.root)
        got = BUILD.preview_context_tokens(self.session, self.root, goals,
                                           important, g)
        self.assertEqual(len(bare) // 4, got)
        self.assertGreater(
            got, BUILD.cost(self.session, self.root)["context_tokens"])

    def test_a_build_asks_then_finishes_on_the_answer_in_the_same_session(self):
        started = BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(started["ok"], started)
        # building the moment it is submitted
        self.assertEqual("building", self.rows()["taaaa0001"][0])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "asking"))
        self.assertEqual("Which router file: src/a.ts or src/b.ts?",
                         self.rows()["taaaa0001"][1])
        # the unpicked rows were never touched
        self.assertEqual(("", ""), self.rows()["taaaa0003"])
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: not run.alive()))
        # The reader thread writes the record's verdict a beat after the
        # process is gone; wait for the word rather than for the exit.
        self.assertTrue(self.wait_for(
            lambda: BUILD.load_run(self.session, self.root, "g1")["status"]
            == "waiting"), BUILD.load_run(self.session, self.root, "g1"))
        record = BUILD.load_run(self.session, self.root, "g1")
        self.assertEqual("waiting", record["status"])

        answered = BUILD.answer(self.session, self.root, "g1", "taaaa0001", "src/a.ts")
        self.assertTrue(answered["ok"], answered)
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done"))
        self.assertEqual("", self.rows()["taaaa0001"][1])
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(2, len(calls))
        first, second = calls
        self.assertIn("--session-id", first["args"])
        self.assertEqual(self.root.resolve(), Path(first["cwd"]).resolve(),
                         "the chat's own directory")
        self.assertIn("--resume", second["args"])
        sid = first["args"][first["args"].index("--session-id") + 1]
        self.assertEqual(sid, second["args"][second["args"].index("--resume") + 1],
                         "the answer goes back into the same session")
        self.assertEqual({"id": "taaaa0001", "answer": "src/a.ts"},
                         json.loads(second["prompt"]))
        self.assertIn("--permission-mode", first["args"])
        # Subscription, not the key the server inherited.
        self.assertIsNone(first["api_key"])
        self.assertIsNone(second["api_key"])

    def test_the_op_route_reaches_the_runner_and_refuses_bad_picks(self):
        trajdir = chat_state.paths(self.session, self.root).session_dir
        out = ui._apply({"op": "build_todos", "goal_id": "g1", "ids": ["nope"]},
                        trajdir, True)
        self.assertFalse(out["ok"])
        out = ui._apply({"op": "build_todos", "goal_id": "g1", "ids": []},
                        trajdir, True)
        self.assertFalse(out["ok"])
        out = ui._apply({"op": "build_todos", "goal_id": "g1",
                         "ids": ["taaaa0003"]}, trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0003"][0] == "asking"))
        # A stale browser copy posting the tree does not undo the run's state.
        goals, _ = chat_state.load_goals(self.session, self.root)
        nested = [{"id": "g1", "title": "Ship the router", "children": [],
                   "todo_items": [
                       {"id": "taaaa0003", "text": "Update the docs, please",
                        "depth": 0, "status": "", "question": ""}]}]
        ui._import(nested, trajdir, True)
        self.assertEqual(("asking", "Which router file: src/a.ts or src/b.ts?"),
                         self.rows()["taaaa0003"])
        goals, _ = chat_state.load_goals(self.session, self.root)
        self.assertEqual("Update the docs, please",
                         GM.by_id(goals, "g1")["todo_items"][0]["text"])


class ReopenTests(BuildRunTests):
    """A row came back done and the reader disagrees."""

    def history(self, row_id="taaaa0001"):
        goals, _ = chat_state.load_goals(self.session, self.root)
        row = next(r for r in GM.by_id(goals, "g1")["todo_items"]
                   if r["id"] == row_id)
        return row.get("history") or []

    def finish(self, row_id="taaaa0001"):
        os.environ["STUB_FINISH"] = "1"
        out = BUILD.start(self.session, self.root, "g1", [row_id])
        self.assertTrue(out["ok"], out)
        self.assertTrue(self.wait_for(
            lambda: self.rows()[row_id][0] == "done"), self.rows())

    def test_a_reopened_row_goes_back_into_the_same_session_with_the_note(self):
        self.finish()
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: not run.alive()))
        out = BUILD.reopen(self.session, self.root, "g1", "taaaa0001",
                           "  truncation still happens in the subagent path ")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["resumed"], out)
        self.assertEqual(2, out["run"], "the run now happening")
        # The run that ended keeps its verdict and what the reader said.
        self.assertEqual(
            [{"state": "done",
              "note": "truncation still happens in the subagent path"}],
            self.history())
        # And the row is out again, then done again -- history intact.
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done"), self.rows())
        self.assertEqual(1, len(self.history()))
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(2, len(calls))
        self.assertIn("--resume", calls[1]["args"],
                      "the session that did run 1 is the one told what was wrong")
        self.assertEqual(
            {"id": "taaaa0001",
             "reopened": "truncation still happens in the subagent path"},
            json.loads(calls[1]["prompt"]))
        # The goal it belongs to is working again, not finished.
        goals, _ = chat_state.load_goals(self.session, self.root)
        self.assertEqual("in_progress", GM.by_id(goals, "g1")["status"])

    def test_a_row_not_finished_and_a_note_that_is_blank_are_both_refused(self):
        blank = BUILD.reopen(self.session, self.root, "g1", "taaaa0001", "   ")
        self.assertFalse(blank["ok"])
        self.assertIn("went wrong", blank["error"])
        # taaaa0003 has never run: there is nothing to disagree with yet.
        fresh = BUILD.reopen(self.session, self.root, "g1", "taaaa0003", "no")
        self.assertFalse(fresh["ok"])
        self.assertIn("finished", fresh["error"])
        missing = BUILD.reopen(self.session, self.root, "g1", "tzzzz9999", "no")
        self.assertFalse(missing["ok"])
        self.assertEqual([], self.history())

    def test_with_no_session_left_a_fresh_run_opens_on_the_whole_history(self):
        self.finish()
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: not run.alive()))
        # The reader thread writes the record's verdict a beat after the
        # process is gone; a record removed before that write comes back.
        self.assertTrue(self.wait_for(
            lambda: BUILD.load_run(self.session, self.root, "g1")["status"]
            == "idle"))
        if run.thread is not None:
            run.thread.join(timeout=5)
        # The record of run 1 is gone -- a restart, an older build.
        BUILD._RUNS.clear()
        BUILD._run_path(self.session, self.root, "g1").unlink()
        os.environ["STUB_FINISH"] = "1"
        out = BUILD.reopen(self.session, self.root, "g1", "taaaa0001",
                           "you removed the cap but it still truncates")
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["resumed"], out)
        self.assertTrue(self.wait_for(
            lambda: len(self.log.read_text().splitlines()) == 2))
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertNotIn("--resume", calls[1]["args"])
        self.assertIn("- Add the route [taaaa0001]", calls[1]["prompt"])
        self.assertIn('(run 1 ended done; the user reopened it: "you removed'
                      ' the cap but it still truncates")', calls[1]["prompt"])

    def test_the_op_route_reaches_reopen(self):
        self.finish()
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: not run.alive()))
        trajdir = chat_state.paths(self.session, self.root).session_dir
        out = ui._apply({"op": "reopen_todo", "goal_id": "g1",
                         "id": "taaaa0001", "note": "not good enough"},
                        trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual([{"state": "done", "note": "not good enough"}],
                         self.history())
        # A stale browser copy posting the tree does not erase the history.
        ui._import([{"id": "g1", "title": "Ship the router", "children": [],
                     "todo_items": [{"id": "taaaa0001", "text": "Add the route",
                                     "depth": 0, "status": "", "question": ""}]}],
                   trajdir, True)
        self.assertEqual([{"state": "done", "note": "not good enough"}],
                         self.history())


class QueueBehindARunTests(BuildRunTests):
    """Picks made while a build is out wait their turn, then go."""

    def test_more_picks_queue_and_start_when_the_run_ends(self):
        os.environ["STUB_FINISH"] = "1"
        os.environ["STUB_SLEEP"] = "0.8"
        first = BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(first["ok"], first)
        second = BUILD.start(self.session, self.root, "g1", ["taaaa0003"])
        self.assertTrue(second["ok"], second)
        self.assertTrue(second.get("queued") and second.get("after_run"), second)
        self.assertEqual("queued", self.rows()["taaaa0003"][0])
        # Both done: the second went out by itself when the first ended.
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done"
            and self.rows()["taaaa0003"][0] == "done", seconds=12),
            self.rows())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(2, len(calls))
        self.assertIn("[taaaa0003]", calls[1]["prompt"])


class BuildCostTests(BuildRunTests):
    """What a row will cost to build: measured where it can be, estimated
    where it cannot, and never invented.

    The stub reports 5,200 tokens on every turn it ends (see USAGE), so the
    arithmetic below is exact.
    """

    TURN = 5200

    def row(self, row_id):
        goals, _ = chat_state.load_goals(self.session, self.root)
        return next(r for r in GM.by_id(goals, "g1")["todo_items"]
                    if r["id"] == row_id)

    def test_a_chat_that_has_built_nothing_says_so_with_the_default(self):
        cost = BUILD.cost(self.session, self.root)
        self.assertEqual(0, cost["samples"])
        self.assertEqual(BUILD.DEFAULT_ROW_TOKENS, cost["row_tokens"])
        self.assertEqual(BUILD.DEFAULT_ROW_CHARS, cost["row_chars"])

    def test_the_context_every_build_opens_on_is_measured_not_guessed(self):
        text = chat_state.write_goal_context(self.session, root=self.root)
        cost = BUILD.cost(self.session, self.root)
        self.assertEqual(len(text) // 4, cost["context_tokens"])
        self.assertGreater(cost["context_tokens"], 0)

    def test_usage_counts_the_cache_as_well_as_the_prompt(self):
        self.assertEqual(5200, BUILD._usage_tokens(
            {"type": "result", "usage": {"input_tokens": 1000,
                                         "output_tokens": 200,
                                         "cache_read_input_tokens": 4000}}))
        # A CLI that reports nothing contributes nothing, rather than a zero
        # that would drag the median down.
        self.assertEqual(0, BUILD._usage_tokens({"type": "result"}))

    def test_the_estimate_is_the_median_of_the_runs_it_has_seen(self):
        for tokens in (10000, 90000, 20000):
            BUILD.record_usage(self.session, self.root, 1, 40, tokens)
        cost = BUILD.cost(self.session, self.root)
        self.assertEqual(3, cost["samples"])
        self.assertEqual(20000, cost["row_tokens"], "the middle run, not the mean")
        self.assertEqual(40, cost["row_chars"])

    def test_only_the_last_runs_are_kept(self):
        for i in range(BUILD.USAGE_KEEP + 5):
            BUILD.record_usage(self.session, self.root, 1, 40, 1000 + i)
        self.assertEqual(BUILD.USAGE_KEEP, BUILD.cost(self.session, self.root)["samples"])

    def test_a_finished_build_of_one_row_leaves_the_real_number_on_it(self):
        os.environ["STUB_FINISH"] = "1"
        self.assertTrue(BUILD.start(self.session, self.root, "g1", ["taaaa0003"])["ok"])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0003"][0] == "done"))
        self.assertTrue(self.wait_for(
            lambda: "tokens" in self.row("taaaa0003")))
        self.assertEqual(self.TURN, self.row("taaaa0003")["tokens"])
        cost = BUILD.cost(self.session, self.root)
        self.assertEqual(1, cost["samples"])
        self.assertEqual(self.TURN, cost["row_tokens"])
        self.assertEqual(len("Update the docs"), cost["row_chars"])
        self.assertEqual(self.TURN,
                         BUILD.load_run(self.session, self.root, "g1")["tokens"])

    def test_a_build_of_two_rows_is_priced_per_row_and_marks_neither(self):
        os.environ["STUB_FINISH"] = "1"
        self.assertTrue(BUILD.start(self.session, self.root, "g1",
                                    ["taaaa0001", "taaaa0003"])["ok"])
        self.assertTrue(self.wait_for(
            lambda: BUILD.cost(self.session, self.root)["samples"] == 1))
        cost = BUILD.cost(self.session, self.root)
        self.assertEqual(self.TURN // 2, cost["row_tokens"])
        # A number that cannot be attributed to one row is not written onto
        # one: the rail keeps estimating those rows.
        self.assertNotIn("tokens", self.row("taaaa0001"))
        self.assertNotIn("tokens", self.row("taaaa0003"))

    def test_a_question_and_its_answer_are_one_build_banked_once(self):
        self.assertTrue(BUILD.start(self.session, self.root, "g1", ["taaaa0001"])["ok"])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "asking"))
        # Nothing is banked while the build is still waiting on the reader.
        self.assertEqual(0, BUILD.cost(self.session, self.root)["samples"])
        self.assertTrue(BUILD.answer(self.session, self.root, "g1",
                                     "taaaa0001", "src/a.ts")["ok"])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done"))
        self.assertTrue(self.wait_for(
            lambda: "tokens" in self.row("taaaa0001")))
        self.assertEqual(1, BUILD.cost(self.session, self.root)["samples"],
                         "one build, not one per turn")
        # Both turns: the one that asked and the one that finished.
        self.assertEqual(self.TURN * 2, self.row("taaaa0001")["tokens"])

    def test_normalize_keeps_only_a_number_there_is_a_reason_for(self):
        out = GM.normalize_todo_items([
            {"id": "taaaa0001", "text": "a", "depth": 0, "tokens": 5200},
            {"id": "taaaa0002", "text": "b", "depth": 0, "tokens": 0},
            {"id": "taaaa0003", "text": "c", "depth": 0, "tokens": "junk"},
            {"id": "taaaa0004", "text": "d", "depth": 0},
        ])
        self.assertEqual(5200, out[0]["tokens"])
        for row in out[1:]:
            self.assertNotIn("tokens", row, "no key, so the two sides compare equal")

    def test_the_measured_number_survives_the_browser_s_next_save(self):
        # The browser owns text, depth and order; the server owns what a build
        # spent. A page saving an edit does not send that number back, and a
        # page that sends one anyway is not believed.
        previous = [{"id": "taaaa0001", "text": "Add the route", "depth": 0,
                     "status": "done", "tokens": 5200}]
        out = ui._merge_todo_items(
            [{"id": "taaaa0001", "text": "Add the route, carefully",
              "depth": 0, "status": "", "tokens": 99}], previous)
        self.assertEqual("Add the route, carefully", out[0]["text"])
        self.assertEqual("done", out[0]["status"])
        self.assertEqual(5200, out[0]["tokens"])
        fresh = ui._merge_todo_items(
            [{"id": "taaaa0009", "text": "new", "depth": 0, "tokens": 42}],
            previous)
        self.assertNotIn("tokens", fresh[0])

    def test_a_row_out_with_the_builder_is_not_blanked_by_a_stale_page(self):
        # A page whose import was composed before the reader finished typing
        # -- and which lands after the build that carried the finished text
        # -- posts the row blank. Taking that leaves a row saying "building"
        # with nothing written on it. While a row is with the builder its
        # text is what was sent; a blank over it is not an edit.
        out = ui._merge_todo_items(
            [{"id": "taaaa0001", "text": "", "depth": 0}],
            [{"id": "taaaa0001", "text": "Add the route", "depth": 0,
              "status": "building"}])
        self.assertEqual("Add the route", out[0]["text"])
        # A row nobody is building is the reader's: clearing it clears it.
        for state in ("", "done", "failed"):
            cleared = ui._merge_todo_items(
                [{"id": "taaaa0001", "text": "", "depth": 0}],
                [{"id": "taaaa0001", "text": "Add the route", "depth": 0,
                  "status": state}])
            self.assertEqual("", cleared[0]["text"], state)
        # And a real edit while it builds is still the reader's.
        edited = ui._merge_todo_items(
            [{"id": "taaaa0001", "text": "Add the route, carefully", "depth": 0}],
            [{"id": "taaaa0001", "text": "Add the route", "depth": 0,
              "status": "building"}])
        self.assertEqual("Add the route, carefully", edited[0]["text"])

    def test_a_build_the_reader_cut_short_is_not_a_sample(self):
        os.environ["STUB_FINISH"] = "1"
        os.environ["STUB_SLEEP"] = "5"
        self.assertTrue(BUILD.start(self.session, self.root, "g1", ["taaaa0001"])["ok"])
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: run.alive()))
        BUILD.cancel(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(self.wait_for(lambda: not run.alive(), seconds=12))
        time.sleep(0.3)
        self.assertEqual(0, BUILD.cost(self.session, self.root)["samples"])
        self.assertNotIn("tokens", self.row("taaaa0001"))


class TransientRetryTests(BuildRunTests):
    """A provider 500 is retried in the same session, not a verdict."""

    def test_a_500_is_retried_and_the_rows_finish(self):
        os.environ["STUB_FAIL_ONCE"] = "1"
        BUILD.RETRY_DELAY_S, held = 0.05, BUILD.RETRY_DELAY_S
        self.addCleanup(lambda: setattr(BUILD, "RETRY_DELAY_S", held))
        out = BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(out["ok"], out)
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done", seconds=12),
            self.rows())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(2, len(calls))
        self.assertFalse(calls[0]["resume"])
        self.assertTrue(calls[1]["resume"], "the retry resumes the same session")
        # The reader thread writes the record's verdict a beat after the
        # rows change; wait for the word rather than for the rows.
        self.assertTrue(self.wait_for(
            lambda: BUILD.load_run(self.session, self.root, "g1")["status"]
            == "idle"), BUILD.load_run(self.session, self.root, "g1"))
        record = BUILD.load_run(self.session, self.root, "g1")
        self.assertEqual("idle", record["status"])
        self.assertEqual(1, record.get("retry"))


class CancelTests(BuildRunTests):
    """A row taken back from the build returns to active, wherever it was."""

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_a_row_waiting_behind_a_run_is_simply_dropped(self):
        os.environ["STUB_FINISH"] = "1"
        os.environ["STUB_SLEEP"] = "0.8"
        self.assertTrue(BUILD.start(self.session, self.root, "g1", ["taaaa0001"])["ok"])
        second = BUILD.start(self.session, self.root, "g1", ["taaaa0003"])
        self.assertTrue(second.get("after_run"), second)
        out = BUILD.cancel(self.session, self.root, "g1", ["taaaa0003"])
        self.assertEqual(["taaaa0003"], out["cancelled"])
        self.assertEqual(("", ""), self.rows()["taaaa0003"])
        self.assertEqual({}, BUILD._load_later(self.session, self.root))
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done", seconds=12))
        time.sleep(0.3)
        self.assertEqual(1, len(self.calls()), "nothing went out for the cancelled row")
        self.assertEqual(("", ""), self.rows()["taaaa0003"])

    def test_cancelling_everything_a_run_is_doing_ends_the_process(self):
        os.environ["STUB_FINISH"] = "1"
        os.environ["STUB_SLEEP"] = "5"
        self.assertTrue(BUILD.start(self.session, self.root, "g1", ["taaaa0001"])["ok"])
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: run.alive()))
        out = BUILD.cancel(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(out.get("stopped"), out)
        self.assertEqual(("", ""), self.rows()["taaaa0001"])
        self.assertTrue(self.wait_for(lambda: not run.alive()))
        self.assertTrue(self.wait_for(
            lambda: (BUILD.load_run(self.session, self.root, "g1") or {}).get("status")
            == "cancelled"), BUILD.load_run(self.session, self.root, "g1"))
        self.assertEqual(("", ""), self.rows()["taaaa0001"], "not failed: cancelled")

    def test_cancelling_one_row_leaves_the_run_on_the_others(self):
        os.environ["STUB_FINISH"] = "1"
        os.environ["STUB_SLEEP"] = "0.8"
        self.assertTrue(BUILD.start(self.session, self.root, "g1",
                                    ["taaaa0001", "taaaa0003"])["ok"])
        out = BUILD.cancel(self.session, self.root, "g1", ["taaaa0003"])
        self.assertFalse(out.get("stopped"), out)
        self.assertEqual(("", ""), self.rows()["taaaa0003"])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done", seconds=12))
        # The process still printed DONE for the cancelled row; it is not taken.
        self.assertEqual(("", ""), self.rows()["taaaa0003"])
        self.assertTrue(self.wait_for(
            lambda: (BUILD.load_run(self.session, self.root, "g1") or {}).get("status")
            == "idle"), BUILD.load_run(self.session, self.root, "g1"))

    def test_withdrawing_a_question_resumes_the_run_on_the_rest(self):
        self.assertTrue(BUILD.start(self.session, self.root, "g1",
                                    ["taaaa0001", "taaaa0003"])["ok"])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "asking"))
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: not run.alive()))
        out = BUILD.cancel(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(out.get("resumed"), out)
        self.assertEqual(("", ""), self.rows()["taaaa0001"])
        self.assertTrue(self.wait_for(lambda: len(self.calls()) == 2))
        second = self.calls()[1]
        self.assertTrue(second["resume"], "the same session, told to move on")
        self.assertIn("withdrew", second["prompt"])
        self.assertIn("taaaa0001", second["prompt"])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0003"][0] == "done"))
        self.assertEqual(("", ""), self.rows()["taaaa0001"])

    def test_done_rows_are_left_alone_and_failed_ones_come_back(self):
        BUILD._set_row(self.session, self.root, "g1", "taaaa0001", status="done")
        BUILD._set_row(self.session, self.root, "g1", "taaaa0003", status="failed")
        out = BUILD.cancel(self.session, self.root, "g1", ["taaaa0001", "taaaa0003"])
        self.assertEqual(["taaaa0003"], out["cancelled"])
        self.assertEqual("done", self.rows()["taaaa0001"][0])
        self.assertEqual(("", ""), self.rows()["taaaa0003"])

    def test_the_op_route_reaches_cancel(self):
        trajdir = chat_state.paths(self.session, self.root).session_dir
        out = ui._apply({"op": "cancel_todos", "goal_id": "g1", "ids": ["nope"]},
                        trajdir, True)
        self.assertFalse(out["ok"])
        BUILD._set_row(self.session, self.root, "g1", "taaaa0003", status="queued")
        out = ui._apply({"op": "cancel_todos", "goal_id": "g1",
                         "ids": ["taaaa0003"]}, trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(("", ""), self.rows()["taaaa0003"])


class SessionBuildTests(unittest.TestCase):
    """The default: the build is handed to the connected session by the hooks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-hand"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        goals = {"version": 1, "goals": [goal(
            "g1", "Ship the router",
            todo_items=[
                {"id": "taaaa0001", "text": "Add the route", "depth": 0},
                {"id": "taaaa0003", "text": "Update the docs", "depth": 0},
            ])]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.root)}))
        self.old_env = dict(os.environ)
        os.environ["HC_BUILD_MODE"] = "session"
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text("")

    def rows(self):
        goals, _ = chat_state.load_goals(self.session, self.root)
        return {r["id"]: (r["status"], r["question"])
                for r in GM.by_id(goals, "g1")["todo_items"]}

    def say(self, text):
        with self.transcript.open("a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": text}]}}) + "\n")

    def test_a_reopened_row_queues_and_the_hook_tells_the_session_what_was_wrong(self):
        BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        BUILD.deliver(self.session, self.root, "Stop")
        self.say('{"id": "taaaa0001", "state": "DONE"}')
        BUILD.scan_transcript(self.session, self.root, str(self.transcript))
        self.assertEqual("done", self.rows()["taaaa0001"][0])

        out = BUILD.reopen(self.session, self.root, "g1", "taaaa0001",
                           "the cap is gone but it still truncates")
        self.assertTrue(out["ok"] and out["queued"], out)
        self.assertEqual("queued", self.rows()["taaaa0001"][0])
        text = BUILD.deliver(self.session, self.root, "Stop")
        self.assertIn("reopened a row you had already marked DONE", text)
        self.assertIn('{"id": "taaaa0001", "reopened": "the cap is gone but'
                      ' it still truncates"}', text)
        self.assertEqual("building", self.rows()["taaaa0001"][0])
        # And it finishes again, with the run that ended kept under it.
        self.say('{"id": "taaaa0001", "state": "DONE"}')
        BUILD.scan_transcript(self.session, self.root, str(self.transcript))
        self.assertEqual("done", self.rows()["taaaa0001"][0])
        goals, _ = chat_state.load_goals(self.session, self.root)
        row = next(r for r in GM.by_id(goals, "g1")["todo_items"]
                   if r["id"] == "taaaa0001")
        self.assertEqual([{"state": "done",
                           "note": "the cap is gone but it still truncates"}],
                         row["history"])

    def test_build_queues_and_the_stop_hook_hands_it_to_the_session(self):
        out = BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(out["ok"] and out["queued"], out)
        self.assertEqual("queued", self.rows()["taaaa0001"][0])
        self.assertEqual(1, len(BUILD.pending(self.session, self.root)))
        self.assertEqual(1, BUILD.session_state(self.session, self.root)["queued"])
        # Nothing was spawned: no run record, no process.
        self.assertIsNone(BUILD.load_run(self.session, self.root, "g1"))

        text = BUILD.deliver(self.session, self.root, "Stop")
        self.assertIn("The user pressed Build", text)
        self.assertIn("- Add the route [taaaa0001]", text)
        self.assertIn('{"id": "<row id>", "state": "DONE"}', text)
        self.assertNotIn("Update the docs", text.split("# The work")[1])
        # Taken: building now, and the queue is empty, so the next Stop
        # says nothing.
        self.assertEqual("building", self.rows()["taaaa0001"][0])
        self.assertEqual("", BUILD.deliver(self.session, self.root, "Stop"))

    def test_the_transcript_moves_the_rows_and_an_answer_queues_again(self):
        BUILD.start(self.session, self.root, "g1", ["taaaa0001", "taaaa0003"])
        BUILD.deliver(self.session, self.root, "Stop")
        self.say("Looking at it.")
        self.say('{"id": "taaaa0001", "question": "src/a.ts or src/b.ts?"}')
        applied = BUILD.scan_transcript(self.session, self.root, str(self.transcript))
        self.assertEqual(1, applied)
        self.assertEqual(("asking", "src/a.ts or src/b.ts?"), self.rows()["taaaa0001"])
        self.assertEqual(("building", ""), self.rows()["taaaa0003"])
        # Already read: a second scan applies nothing new.
        self.assertEqual(0, BUILD.scan_transcript(self.session, self.root, str(self.transcript)))

        out = BUILD.answer(self.session, self.root, "g1", "taaaa0001", "src/a.ts")
        self.assertTrue(out["ok"] and out["queued"], out)
        self.assertEqual("queued", self.rows()["taaaa0001"][0])
        text = BUILD.deliver(self.session, self.root, "UserPromptSubmit")
        self.assertIn("after answering their message", text)
        self.assertIn('{"id": "taaaa0001", "answer": "src/a.ts"}', text)
        self.assertEqual("building", self.rows()["taaaa0001"][0])

        self.say('done {"id": "taaaa0001", "state": "DONE"} and {"id": "taaaa0003", "state": "DONE"}')
        self.assertEqual(2, BUILD.scan_transcript(self.session, self.root, str(self.transcript)))
        self.assertEqual({"taaaa0001": ("done", ""), "taaaa0003": ("done", "")}, self.rows())

    def test_cancelling_a_queued_row_takes_it_out_of_the_waiting_build(self):
        BUILD.start(self.session, self.root, "g1", ["taaaa0001", "taaaa0003"])
        out = BUILD.cancel(self.session, self.root, "g1", ["taaaa0001"])
        self.assertEqual(["taaaa0001"], out["cancelled"])
        self.assertEqual(("", ""), self.rows()["taaaa0001"])
        self.assertEqual("queued", self.rows()["taaaa0003"][0])
        queue = BUILD.pending(self.session, self.root)
        self.assertEqual(1, len(queue))
        self.assertEqual(["taaaa0003"], queue[0]["row_ids"])
        work = queue[0]["prompt"].split("# The work")[1]
        self.assertNotIn("[taaaa0001]", work, "the prompt is recomposed without it")
        self.assertIn("[taaaa0003]", work)
        # The last row goes too: nothing is left for the hook to deliver.
        BUILD.cancel(self.session, self.root, "g1", ["taaaa0003"])
        self.assertEqual([], BUILD.pending(self.session, self.root))
        self.assertEqual("", BUILD.deliver(self.session, self.root, "Stop"))
        self.assertEqual(("", ""), self.rows()["taaaa0003"])

    def test_a_queued_answer_is_withdrawn_with_its_row(self):
        BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        BUILD.deliver(self.session, self.root, "Stop")
        self.say('{"id": "taaaa0001", "question": "src/a.ts or src/b.ts?"}')
        BUILD.scan_transcript(self.session, self.root, str(self.transcript))
        BUILD.answer(self.session, self.root, "g1", "taaaa0001", "src/a.ts")
        self.assertEqual(1, len(BUILD.pending(self.session, self.root)))
        BUILD.cancel(self.session, self.root, "g1", ["taaaa0001"])
        self.assertEqual([], BUILD.pending(self.session, self.root))
        self.assertEqual(("", ""), self.rows()["taaaa0001"])

    def test_the_hook_blocks_stop_with_the_build_and_rides_along_on_a_prompt(self):
        import io
        from human_compact import cli
        BUILD.start(self.session, self.root, "g1", ["taaaa0001"])

        def hook(event, **extra):
            payload = {"hook_event_name": event, "session_id": self.session,
                       "transcript_path": str(self.transcript), "cwd": str(self.root)}
            payload.update(extra)
            out = io.StringIO()
            with unittest.mock.patch.dict(os.environ, {"HC_CHAT_STATE_DIR": str(self.root)}):
                cli.chat_hook_main([], stdin=io.StringIO(json.dumps(payload)), stdout=out)
            return out.getvalue()

        import unittest.mock
        stopped = hook("Stop", stop_hook_active=False)
        self.assertTrue(stopped.strip(), "the Stop hook must answer")
        answer = json.loads(stopped)
        self.assertEqual("block", answer["decision"])
        self.assertIn("- Add the route [taaaa0001]", answer["reason"])
        self.assertEqual("building", self.rows()["taaaa0001"][0])
        # Nothing left: the next Stop lets Claude stop.
        self.assertEqual("", hook("Stop", stop_hook_active=True).strip())

        # Idle session, second build: it rides along with the next prompt.
        BUILD.start(self.session, self.root, "g1", ["taaaa0003"])
        prompted = hook("UserPromptSubmit", prompt="unrelated")
        self.assertTrue(prompted.strip())
        context = json.loads(prompted)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("after answering their message", context)
        self.assertIn("- Update the docs [taaaa0003]", context)
        self.assertEqual("building", self.rows()["taaaa0003"][0])

        # SessionEnd marks the session gone; a later hook brings it back.
        hook("SessionEnd")
        self.assertTrue(BUILD.session_state(self.session, self.root)["ended_at"])
        hook("SessionStart", source="resume")
        self.assertFalse(BUILD.session_state(self.session, self.root)["ended_at"])


class WatchingABuildTests(BuildRunTests):
    """What a build says about itself while it works.

    "building" and nothing else is a build the reader has to take on faith.
    A run keeps a log of what it did, the state says how far in it is, and a
    terminal can be opened on it -- following the log while it runs, resuming
    its session once it has stopped.
    """

    def test_the_events_a_run_prints_become_phrases_not_contents(self):
        self.assertEqual(
            [("say", "Looking at the router."), ("tool", "read a.ts"),
             ("tool", "ran pytest -q")],
            BUILD.stream_activity({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Looking at the router.\nand more"},
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/repo/src/a.ts"}},
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "pytest -q"}}]}}))

    def test_a_protocol_line_is_not_reported_as_work(self):
        # The row's own badge already says what it did; saying it twice, in
        # the shape the reader was never meant to see, is noise.
        self.assertEqual([], BUILD.stream_activity(
            {"type": "assistant", "message": {"content": [
                {"type": "text",
                 "text": '{"id": "taaaa0001", "state": "DONE"}'}]}}))

    def test_the_log_is_bounded_and_does_not_repeat_itself(self):
        for i in range(BUILD.ACTIVITY_KEEP + 5):
            BUILD.note_activity(self.session, self.root, "g1", "tool",
                                "read a-%d.ts" % i)
        lines = BUILD.load_activity(self.session, self.root, "g1")
        self.assertEqual(BUILD.ACTIVITY_KEEP, len(lines))
        self.assertEqual("read a-%d.ts" % (BUILD.ACTIVITY_KEEP + 4),
                         lines[-1]["text"])
        self.assertFalse(BUILD.note_activity(self.session, self.root, "g1",
                                             "tool", lines[-1]["text"]))
        # And a terminal can follow the same lines without parsing anything.
        followed = BUILD.watch_log(self.session, self.root, "g1").read_text()
        self.assertIn("read a-0.ts", followed)

    def settled(self):
        """Wait for the run to have written its own last word."""
        return self.wait_for(
            lambda: (BUILD.load_run(self.session, self.root, "g1")
                     or {}).get("status") == "waiting")

    def test_a_run_logs_what_it_did_and_where_it_stopped(self):
        BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(self.settled())
        said = [line["text"] for line
                in BUILD.load_activity(self.session, self.root, "g1")]
        self.assertIn("started on 1 row", said)
        self.assertIn("Looking at the rows.", said)
        self.assertIn("waiting on your answer", said)

    def test_the_state_says_how_far_in_the_build_is_and_what_it_last_did(self):
        BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(self.settled())
        live = BUILD.live(self.session, self.root)["g1"]
        self.assertEqual("waiting", live["status"])
        self.assertFalse(live["running"])
        self.assertEqual(1, live["rows"])
        self.assertIsInstance(live["elapsed_s"], int)
        # An estimate is for a build that is still going; this one is not.
        self.assertIsNone(live["eta_s"])
        self.assertEqual("waiting on your answer", live["last"]["text"])
        self.assertTrue(live["can_open"], "its session can be resumed")

    def test_how_long_a_row_takes_is_measured_like_what_it_costs(self):
        empty = BUILD.cost(self.session, self.root)
        self.assertEqual(BUILD.DEFAULT_ROW_SECONDS, empty["row_seconds"])
        self.assertEqual(0, empty["time_samples"])
        BUILD.record_usage(self.session, self.root, 1, 80, 1000, 120)
        BUILD.record_usage(self.session, self.root, 2, 80, 1000, 600)
        priced = BUILD.cost(self.session, self.root)
        self.assertEqual(210, priced["row_seconds"], "the median of 120 and 300")
        self.assertEqual(2, priced["time_samples"])

    def test_a_finished_build_leaves_how_long_it_took_behind_it(self):
        os.environ["STUB_FINISH"] = "1"
        os.environ["STUB_SLEEP"] = "1.1"
        BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(self.wait_for(
            lambda: self.rows()["taaaa0001"][0] == "done"))
        run = BUILD._run_for(self.session, self.root, "g1")
        self.assertTrue(self.wait_for(lambda: not run.alive()))
        self.assertTrue(self.wait_for(
            lambda: BUILD.cost(self.session, self.root)["time_samples"] == 1))
        self.assertGreaterEqual(
            BUILD.cost(self.session, self.root)["row_seconds"], 1)

    def test_the_op_route_reaches_the_log(self):
        trajdir = chat_state.paths(self.session, self.root).session_dir
        BUILD.note_activity(self.session, self.root, "g1", "tool", "read a.ts")
        out = ui._apply({"op": "build_log", "goal_id": "g1"}, trajdir, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(["read a.ts"], [l["text"] for l in out["lines"]])

    def test_a_goal_with_no_build_has_no_terminal_to_open(self):
        trajdir = chat_state.paths(self.session, self.root).session_dir
        out = ui._apply({"op": "watch_build", "goal_id": "g1"}, trajdir, True)
        self.assertFalse(out["ok"])
        self.assertIn("no build", out["error"])

    def test_the_workspace_state_carries_every_build_it_knows_of(self):
        trajdir = chat_state.paths(self.session, self.root).session_dir
        BUILD.start(self.session, self.root, "g1", ["taaaa0001"])
        self.assertTrue(self.settled())
        state = ui._payload(trajdir, True)
        self.assertIn("g1", state["build_runs"])
        self.assertEqual("waiting", state["build_runs"]["g1"]["status"])


if __name__ == "__main__":
    unittest.main()
