"""A workspace that serves the plugin, and outlives its own code.

The page is drawn from the plugin's source and answered by a process that
loaded that source at start. A build of this project edits it, and from
then on every control the edit added fails against the running server --
which the page says out loud, once, on the way in. This watches for the
same condition and becomes the new version instead.

Three things hold a restart back, and each has cost somebody a workspace:
a request in flight, a build with a process out, and a run of edits that
has not settled.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import ui  # noqa: E402


class FakeServer:
    def __init__(self, busy=0, chat_scoped=False):
        self.activity_lock = threading.Lock()
        self.active_requests = busy
        self.chat_scoped = chat_scoped
        self.trajdir = Path("/nowhere")
        self.follow_stop = threading.Event()
        self.closed = False
        self.stopped = False

    def server_close(self):
        self.closed = True

    def shutdown(self):
        self.stopped = True


class WatchCodeTests(unittest.TestCase):
    """The decision to re-exec, without ever making one."""

    def setUp(self):
        self.execs = []
        self.stop = threading.Event()
        # execv never returns in the real thing. Here it records and sets
        # the stop, so the watcher's own loop ends on its next wait rather
        # than deciding twice.
        def fake_exec(path, argv):
            self.execs.append(argv[1:])
            self.stop.set()
        self.exec_patch = mock.patch.object(ui.os, "execv", fake_exec)
        self.exec_patch.start()
        self.addCleanup(self.exec_patch.stop)

    def run_watch(self, server, stamps, stale=True, builds=False,
                  moving=False):
        """Drive the watcher with a scripted sequence of code stamps.

        `moving` never lets the stamp settle, standing in for an editor
        part-way through writing a package.
        """
        import itertools
        ticker = itertools.count(stamps[-1] + 1.0)
        seen = iter(stamps)
        stamp = (lambda: next(ticker)) if moving else (
            lambda: next(seen, stamps[-1]))
        with mock.patch.object(ui, "_server_is_stale", lambda: stale), \
             mock.patch.object(ui, "_code_stamp", stamp), \
             mock.patch.object(ui, "_builds_running", lambda s: builds):
            thread = threading.Thread(
                target=ui._watch_code, args=(server, self.stop),
                kwargs={"interval": 0.01}, daemon=True)
            thread.start()
            self.stop.wait(1.0)
            self.stop.set()
            thread.join(timeout=1.0)

    def test_a_settled_edit_on_an_idle_server_becomes_the_new_version(self):
        server = FakeServer()
        self.run_watch(server, [100.0])
        self.assertEqual(1, len(self.execs))
        # Same arguments, so the replacement serves the same chat and port.
        self.assertEqual(["-m", "human_compact.cli"] + sys.argv[1:],
                         self.execs[0])
        # The socket is given up first: two processes must not hold the port.
        self.assertTrue(server.closed)
        self.assertTrue(server.follow_stop.is_set())

    def test_code_that_has_not_stopped_moving_is_not_restarted_onto(self):
        # An editor part-way through writing a package is not a version.
        server = FakeServer()
        self.run_watch(server, [100.0], moving=True)
        self.assertEqual([], self.execs)
        self.assertFalse(server.closed)

    def test_a_request_in_flight_holds_it_back(self):
        server = FakeServer(busy=1)
        self.run_watch(server, [100.0])
        self.assertEqual([], self.execs)

    def test_a_build_with_a_process_out_holds_it_back(self):
        # The lesson of a run that lost its reader mid-flight and left its
        # rows saying "building" for ever.
        server = FakeServer()
        self.run_watch(server, [100.0], builds=True)
        self.assertEqual([], self.execs)

    def test_code_that_has_not_moved_is_left_alone(self):
        server = FakeServer()
        self.run_watch(server, [100.0], stale=False)
        self.assertEqual([], self.execs)


class BuildsRunningTests(unittest.TestCase):
    """Both halves of "is a build out": this process's, and a dead one's."""

    def test_a_live_run_in_this_process_counts(self):
        from human_compact.trajectory import build as BUILD
        live = mock.Mock()
        live.alive.return_value = True
        with mock.patch.dict(BUILD._RUNS, {"s:g": live}, clear=True):
            self.assertTrue(ui._builds_running(FakeServer()))

    def test_a_finished_run_in_this_process_does_not(self):
        from human_compact.trajectory import build as BUILD
        done = mock.Mock()
        done.alive.return_value = False
        with mock.patch.dict(BUILD._RUNS, {"s:g": done}, clear=True):
            self.assertFalse(ui._builds_running(FakeServer()))

    def test_a_record_left_by_an_earlier_server_counts_while_its_pid_lives(self):
        import json
        import tempfile
        from human_compact.trajectory import build as BUILD
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder/"g1.json").write_text(json.dumps(
                {"status": "running", "pid": 4242}))
            # later.json and usage.json are not runs and must not be read
            # as one; a run whose pid has gone is not out either.
            (folder/"later.json").write_text(json.dumps({"g1": ["t1"]}))
            (folder/"g2.json").write_text(json.dumps(
                {"status": "running", "pid": 999999}))
            server = FakeServer(chat_scoped=True)
            with mock.patch.dict(BUILD._RUNS, {}, clear=True), \
                 mock.patch.object(ui, "_chat_identity", lambda d: ("s", None)), \
                 mock.patch.object(BUILD, "_builds_dir", lambda s, r: folder), \
                 mock.patch.object(ui, "_pid_alive", lambda p: int(p) == 4242):
                self.assertTrue(ui._builds_running(server))
                with mock.patch.object(ui, "_pid_alive", lambda p: False):
                    self.assertFalse(ui._builds_running(server))


if __name__ == "__main__":
    unittest.main()
