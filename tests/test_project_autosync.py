"""When a change becomes a request, and when it does not.

The clock here is a variable and the network is a list. Everything this
module decides is a function of time, so a test that used the real one would
either take half a minute or assert nothing; instead the clock is moved by
hand and the pump is woken and waited for at each step.
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import project_autosync as AS  # noqa: E402

CWD = "/tmp/a-project"
OTHER = "/tmp/another-project"


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.t

    def advance(self, seconds):
        with self.lock:
            self.t += seconds


class Network:
    """A stand-in for supabase_client.sync_project."""

    def __init__(self):
        self.calls = []
        self.files_asked = []
        self.fail_with = None
        self.overlaps = 0
        self._busy = 0
        self.lock = threading.Lock()
        self.gate = None            # set to an Event to hold a sync open

    def __call__(self, root, cwd, files=None):
        with self.lock:
            self._busy += 1
            if self._busy > 1:
                self.overlaps += 1
            self.calls.append(cwd)
            self.files_asked.append(files)
        try:
            if self.gate is not None:
                self.gate.wait(timeout=5)
            if self.fail_with:
                raise RuntimeError(self.fail_with)
            return {"ok": True}
        finally:
            with self.lock:
                self._busy -= 1

    @property
    def count(self):
        with self.lock:
            return len(self.calls)


class AutosyncTest(unittest.TestCase):
    def setUp(self):
        AS.reset()
        self.clock = Clock()
        self.net = Network()
        AS._now = self.clock
        AS._sync = self.net
        self.addCleanup(self.restore)

    def restore(self):
        AS.reset()
        AS._now = time.monotonic
        AS._sync = None

    # The pump is a thread; a test that asserted immediately after moving the
    # clock would be racing it. Wake it and give it a bounded moment to act.
    def settle(self, want=None, timeout=2.0):
        deadline = time.monotonic() + timeout
        # A grace period even when nothing is expected: "the pump did not
        # sync" is only worth asserting once the pump has had the chance to.
        grace = time.monotonic() + 0.15
        with AS._CV:
            AS._CV.notify_all()
        while time.monotonic() < deadline:
            if (want is None or self.net.count >= want) \
                    and time.monotonic() >= grace:
                # Even when the count is right, let an in-flight settle land.
                if not any(s.in_flight for s in AS._STATES.values()):
                    return
            time.sleep(0.01)
            with AS._CV:
                AS._CV.notify_all()

    def test_a_change_does_not_sync_immediately(self):
        AS.mark_dirty(None, CWD)
        self.settle()
        self.assertEqual(self.net.count, 0)

    def test_it_syncs_once_the_changes_stop(self):
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        self.assertEqual(self.net.calls, [CWD])

    def test_a_burst_of_changes_is_one_request(self):
        for _ in range(25):
            AS.mark_dirty(None, CWD)
            self.clock.advance(0.2)          # still inside the quiet window
        self.settle()
        self.assertEqual(self.net.count, 0, "typing must not sync")
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        self.assertEqual(self.net.count, 1)

    def test_a_change_during_the_window_resets_the_timer(self):
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS - 0.5)
        AS.mark_dirty(None, CWD)             # the timer starts again here
        self.clock.advance(AS.QUIET_SECONDS - 0.5)
        self.settle()
        self.assertEqual(self.net.count, 0)
        self.clock.advance(1.0)
        self.settle(want=1)
        self.assertEqual(self.net.count, 1)

    def test_continuous_change_still_syncs_at_the_ceiling(self):
        AS.mark_dirty(None, CWD)
        # Never quiet for long enough, for far longer than the ceiling.
        for _ in range(int(AS.MAX_DIRTY_SECONDS / 2) + 4):
            self.clock.advance(2.0)
            AS.mark_dirty(None, CWD)
            self.settle()
        self.assertGreaterEqual(self.net.count, 1)
        self.assertLessEqual(self.net.count, 3, "the ceiling is not a metronome")

    def test_the_ceiling_is_measured_from_the_last_send(self):
        first_change = self.clock.t
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        sent_at = self.clock.t
        # Changes now never stop, so only the ceiling can fire -- and the
        # question is which moment it counts from. Run past the point where
        # a ceiling measured from the first change ever made would have
        # fired, and require that nothing has gone out.
        while self.clock.t < sent_at + AS.MAX_DIRTY_SECONDS - 2:
            self.clock.advance(2.0)
            AS.mark_dirty(None, CWD)
            self.settle()
        self.assertGreater(self.clock.t, first_change + AS.MAX_DIRTY_SECONDS)
        self.assertEqual(self.net.count, 1, "the clock restarts at each send")
        # It runs from the first change AFTER a send -- "nothing stays dirty
        # for more than MAX_DIRTY_SECONDS" -- so the second send lands a
        # little after the naive sent_at + MAX.
        self.clock.advance(6.0)
        AS.mark_dirty(None, CWD)
        self.settle(want=2)
        self.assertEqual(self.net.count, 2)
        state = AS._STATES[AS._key(None, CWD)]
        self.assertIsNotNone(state.last_ok_at)

    def test_two_syncs_never_overlap(self):
        gate = threading.Event()
        self.net.gate = gate
        AS.mark_dirty(None, CWD)
        AS.mark_dirty(None, OTHER)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        with AS._CV:
            AS._CV.notify_all()
        time.sleep(0.15)                     # one is in flight, held open
        self.assertEqual(self.net.count, 1)
        gate.set()
        self.net.gate = None
        self.settle(want=2)
        self.assertEqual(self.net.overlaps, 0)
        self.assertEqual(sorted(self.net.calls), sorted([CWD, OTHER]))

    def test_nothing_clean_is_ever_sent(self):
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        self.clock.advance(AS.MAX_DIRTY_SECONDS * 3)
        self.settle()
        self.assertEqual(self.net.count, 1)

    def test_a_change_during_a_send_is_not_lost(self):
        gate = threading.Event()
        self.net.gate = gate
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        with AS._CV:
            AS._CV.notify_all()
        time.sleep(0.15)
        self.assertEqual(self.net.count, 1)
        AS.mark_dirty(None, CWD)             # lands mid-flight
        gate.set()
        self.net.gate = None
        self.settle(want=1)
        state = AS._STATES[AS._key(None, CWD)]
        self.assertTrue(state.dirty, "a write during a send is still unsent")
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=2)
        self.assertEqual(self.net.count, 2)

    def test_a_failure_keeps_the_project_dirty_and_retries(self):
        self.net.fail_with = "could not reach Supabase: timed out"
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        state = AS._STATES[AS._key(None, CWD)]
        self.assertTrue(state.dirty)
        self.assertEqual(state.attempt, 1)
        # It does not hammer: the backoff has to pass first.
        self.settle()
        self.assertEqual(self.net.count, 1)
        self.net.fail_with = None
        self.clock.advance(AS.RETRY_SCHEDULE[0] + 0.1)
        self.settle(want=2)
        self.assertEqual(self.net.count, 2)
        self.assertFalse(state.dirty)

    def test_a_refusal_backs_off_further_than_a_dropped_connection(self):
        state = AS._Project(None, CWD)
        AS._settle(state, 1, False, "could not reach Supabase: [Errno 8]",
                   1000.0)
        transient = state.next_attempt_at
        cold = AS._Project(None, CWD)
        AS._settle(cold, 1, False, "sign in first: an invitation is joined",
                   1000.0)
        self.assertLess(transient, cold.next_attempt_at)
        self.assertEqual(cold.next_attempt_at, 1000.0 + AS.COLD_SECONDS)

    def test_editing_during_an_outage_does_not_reset_the_backoff(self):
        # The opposite rule would be defensible right up until you notice it
        # means a request every few seconds for as long as the network is
        # down and the reader keeps typing.
        self.net.fail_with = "could not reach Supabase: down"
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        self.net.fail_with = None
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle()
        self.assertEqual(self.net.count, 1, "the backoff still governs")
        # And when it expires, the retry carries the newer change with it.
        self.clock.advance(AS.RETRY_SCHEDULE[0])
        self.settle(want=2)
        self.assertEqual(self.net.count, 2)
        self.assertFalse(AS._STATES[AS._key(None, CWD)].dirty)

    def test_a_boundary_skips_the_quiet_window(self):
        AS.mark_dirty(None, CWD)
        AS.flush_soon(None, CWD, "build finished")
        self.settle(want=1)
        self.assertEqual(self.net.count, 1, "a boundary does not wait")

    def test_a_boundary_asks_for_the_file_half_and_a_routine_send_does_not(self):
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        # Routine: let the file provenance keep its own slower clock.
        self.assertEqual(self.net.files_asked, [None])
        AS.flush_soon(None, CWD, "build finished")
        self.settle(want=2)
        self.assertEqual(self.net.files_asked[-1], True)

    def test_a_boundary_sends_even_when_nothing_local_changed(self):
        AS.flush_soon(None, CWD, "closing")
        self.settle(want=1)
        self.assertEqual(self.net.count, 1)

    def test_drain_sends_what_is_still_dirty_and_stops_the_pump(self):
        AS.mark_dirty(None, CWD)
        AS.mark_dirty(None, OTHER)
        out = AS.drain(timeout=5.0)
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(out["sent"]), sorted([CWD, OTHER]))
        self.assertEqual(self.net.overlaps, 0)
        self.assertFalse(AS._STATES[AS._key(None, CWD)].dirty)

    def test_drain_reports_what_it_could_not_send_and_keeps_it_dirty(self):
        self.net.fail_with = "could not reach Supabase"
        AS.mark_dirty(None, CWD)
        out = AS.drain(timeout=5.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed"], [CWD])
        self.assertTrue(AS._STATES[AS._key(None, CWD)].dirty)

    def test_the_manual_button_settles_the_schedule(self):
        AS.mark_dirty(None, CWD)
        AS.note_external_sync(None, CWD)     # the button just sent it
        self.clock.advance(AS.MAX_DIRTY_SECONDS * 2)
        self.settle()
        self.assertEqual(self.net.count, 0)

    def test_a_project_without_a_directory_is_never_scheduled(self):
        AS.mark_dirty(None, "")
        AS.flush_soon(None, "")
        self.clock.advance(AS.MAX_DIRTY_SECONDS * 2)
        self.settle()
        self.assertEqual(self.net.count, 0)
        self.assertEqual(AS._STATES, {})

    def test_a_sync_that_raises_does_not_kill_the_pump(self):
        self.net.fail_with = "boom"
        AS.mark_dirty(None, CWD)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=1)
        self.net.fail_with = None
        AS.mark_dirty(None, OTHER)
        self.clock.advance(AS.QUIET_SECONDS + 0.1)
        self.settle(want=2)
        self.assertIn(OTHER, self.net.calls)

    def test_status_says_what_is_waiting(self):
        AS.mark_dirty(None, CWD, "goals")
        row = AS.status(None, CWD)["projects"][0]
        self.assertTrue(row["dirty"])
        self.assertEqual(row["last_reason"], "goals")
        self.assertLessEqual(row["due_in"], AS.QUIET_SECONDS)


class WiringTest(unittest.TestCase):
    """The scheduler is only useful if the workspace actually calls it.

    Everything above drives the module directly, which would pass just as
    happily if nothing in the workspace had been wired to it at all.
    """

    def setUp(self):
        import json
        import os
        import tempfile
        from unittest import mock

        AS.reset()
        self.clock = Clock()
        self.net = Network()
        AS._now = self.clock
        AS._sync = self.net
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "chat-sessions").mkdir(parents=True)
        self.project = Path(self.tmp.name) / "work"
        self.project.mkdir()
        self.env = mock.patch.dict(
            os.environ, {"CLAUDE_VAULT_DIR": str(self.vault)}, clear=False)
        self.env.start()
        self.session = "wired-session"
        directory = self.vault / "chat-sessions" / self.session
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "session_id": self.session,
            "cwd": str(self.project), "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z"}))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.env.stop()
        self.tmp.cleanup()
        AS.reset()
        AS._now = time.monotonic
        AS._sync = None

    def test_saving_the_goal_tree_marks_the_project_dirty(self):
        from human_compact.trajectory import chat_state as CS

        CS.save_goals(self.session,
                      {"goals": [{"id": "g1", "title": "a goal"}]},
                      {"items": []}, None)
        rows = AS.status()["projects"]
        self.assertTrue(rows, "no project was scheduled at all")
        self.assertTrue(rows[0]["dirty"])
        self.assertEqual(rows[0]["cwd"], str(self.project))
        # And it waits rather than syncing on the save itself.
        self.assertEqual(self.net.count, 0)

    def test_the_project_a_save_belongs_to_is_the_chat_s_own(self):
        from human_compact.trajectory import project_autosync as mod

        self.assertEqual(mod.project_of(self.session, None),
                         str(self.project))


if __name__ == "__main__":
    unittest.main()
