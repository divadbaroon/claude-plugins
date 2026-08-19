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
import json, sys, os
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
resume = "--resume" in args
log = os.environ["STUB_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(),
                         "resume": resume}) + "\n")
def say(text):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}), flush=True)
if not resume:
    ids = [w.strip("[]") for w in prompt.split() if w.startswith("[t") and w.endswith("]")]
    say("Looking at the rows.")
    say('{"id": "%s", "question": "Which router file: src/a.ts or src/b.ts?"}' % ids[0])
    print(json.dumps({"type": "result", "is_error": False, "result": "asked"}))
else:
    msg = json.loads(prompt)
    say("Thanks: %s" % msg["answer"])
    say('{"id": "%s", "state": "DONE"}' % msg["id"])
    print(json.dumps({"type": "result", "is_error": False, "result": "done"}))
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
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
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


if __name__ == "__main__":
    unittest.main()
