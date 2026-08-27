"""The send nobody has to remember: four seconds after the last edit.

Nothing here reaches the network. What matters is which edits arm the
timer, that a burst of them is one send rather than many, and that a
workspace with nowhere to send stays quiet instead of failing once per
keystroke.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import autosync as AS  # noqa: E402
from human_compact.trajectory import chat_state, goals as GM  # noqa: E402
from human_compact.trajectory import ui as UI  # noqa: E402

SIGNED_IN = {"configured": True, "signed_in": True, "email": "m@example.com"}
# Short enough that the suite does not wait on wall-clock, long enough that
# two schedules in a row are genuinely coalesced rather than racing.
TICK = 0.05


def settle(seconds=0.35):
    """Wait for whatever timers are in flight, without a fixed sleep in the
    body of a test."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        with AS._GUARD:
            quiet = not AS._TIMERS and not AS._SENDING
        if quiet:
            return
        time.sleep(0.005)


class AutosyncTests(unittest.TestCase):
    def setUp(self):
        with AS._GUARD:
            for timer in AS._TIMERS.values():
                timer.cancel()
            AS._TIMERS.clear()
            AS._SENDING.clear()
            AS._AGAIN.clear()
            AS._LAST.clear()
        self.sent = []
        self.gate = threading.Event()

        def fake_sync(root, cwd):
            self.sent.append(cwd)
            self.gate.set()
            return {"ok": True, "project_id": "p", "sent": {"goals": 3}}

        patch = mock.patch.multiple(
            "human_compact.trajectory.supabase_client",
            sync_project=fake_sync,
            status=lambda root=None: dict(SIGNED_IN))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(settle)

    # ------------------------------------------------------------ arming

    def test_every_goal_edit_arms_the_send(self):
        """The set the dispatcher uses to decide a goal was edited is the
        same set that decides the project should go up. A new goal
        operation added to one and not the other is the bug this catches."""
        self.assertTrue(UI.GOAL_OPS <= AS.WRITE_OPS)

    def test_reading_the_workspace_never_sends_it(self):
        for kind in ("list_shares", "shared_projects", "prompt_preview",
                     "dev_log", "build_log", "open_project", "sync_supabase"):
            self.assertNotIn(kind, AS.WRITE_OPS)

    def test_a_saved_edit_arms_the_send_and_nothing_else_does(self):
        armed = []
        with mock.patch.object(UI, "_scope", lambda t: t), \
             mock.patch.object(UI, "_chat_identity", lambda t: ("sess", None)), \
             mock.patch.object(UI, "_project_identity",
                               lambda *a: {"cwd": "/work/proj"}), \
             mock.patch.object(UI.AUTOSYNC, "schedule",
                               lambda root, cwd: armed.append(cwd)):
            UI._arm_autosync({"op": "set_notes"}, {"ok": True}, None, True)
            # A refused edit changed nothing, so there is nothing to send.
            UI._arm_autosync({"op": "set_notes"}, {"ok": False}, None, True)
            # A read is not an edit.
            UI._arm_autosync({"op": "list_shares"}, {"ok": True}, None, True)
            # A workspace not scoped to a chat has no project to send.
            UI._arm_autosync({"op": "set_notes"}, {"ok": True}, None, False)
        self.assertEqual(armed, ["/work/proj"])

    def test_a_project_it_cannot_name_is_not_armed(self):
        self.assertFalse(AS.schedule(None, ""))
        self.assertFalse(AS.schedule(None, None))

    # ---------------------------------------------------------- debounce

    def test_a_burst_of_edits_is_one_send(self):
        for _ in range(6):
            AS.schedule(None, "/work/proj", _delay=TICK)
        self.assertTrue(self.gate.wait(2))
        settle()
        self.assertEqual(self.sent, ["/work/proj"])

    def test_a_timer_already_waking_cannot_send_after_rearm(self):
        entered = threading.Event()
        release = threading.Event()
        real_fire = AS._fire

        def slow_fire(*args):
            entered.set()
            release.wait(2)
            real_fire(*args)

        with mock.patch.object(AS, "_fire", slow_fire):
            AS.schedule(None, "/work/proj", _delay=0.001)
            self.assertTrue(entered.wait(2))
            AS.schedule(None, "/work/proj", _delay=TICK)
            release.set()
            settle()
        self.assertEqual(self.sent, ["/work/proj"])

    def test_two_projects_each_get_their_own(self):
        AS.schedule(None, "/work/one", _delay=TICK)
        AS.schedule(None, "/work/two", _delay=TICK)
        settle()
        self.assertEqual(sorted(self.sent), ["/work/one", "/work/two"])

    def test_the_send_can_be_disarmed(self):
        AS.schedule(None, "/work/proj", _delay=TICK)
        self.assertTrue(AS.cancel(None, "/work/proj"))
        settle()
        self.assertEqual(self.sent, [])

    def test_turning_it_off_leaves_the_button(self):
        with mock.patch.dict(os.environ, {"HC_AUTOSYNC_SECONDS": "0"}):
            self.assertFalse(AS.enabled())
            self.assertFalse(AS.schedule(None, "/work/proj"))
        settle()
        self.assertEqual(self.sent, [])

    def test_the_delay_is_four_seconds_unless_told_otherwise(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HC_AUTOSYNC_SECONDS", None)
            self.assertEqual(AS.delay(), 4.0)
        with mock.patch.dict(os.environ, {"HC_AUTOSYNC_SECONDS": "not a number"}):
            self.assertEqual(AS.delay(), 4.0)

    # ------------------------------------------------------- overlapping

    def test_the_button_and_the_timer_do_not_send_at_once(self):
        """``hc_sync_project`` prunes the rows its payload lacks, so a timer
        firing inside a hand-pressed send could delete what that send is
        still writing."""
        with mock.patch.dict(os.environ, {"HC_AUTOSYNC_SECONDS": str(TICK)}):
            with AS.hold(None, "/work/proj"):
                AS.schedule(None, "/work/proj", _delay=TICK)
                time.sleep(TICK * 3)
                self.assertEqual(self.sent, [])
            settle()
        # The edit that arrived mid-send is not dropped: it goes up after.
        self.assertEqual(self.sent, ["/work/proj"])

    # ------------------------------------------------------ nowhere to go

    def test_a_workspace_with_no_account_stays_quiet(self):
        with mock.patch("human_compact.trajectory.supabase_client.status",
                        lambda root=None: {"configured": True,
                                           "signed_in": False}):
            AS.schedule(None, "/work/proj", _delay=TICK)
            settle()
        self.assertEqual(self.sent, [])
        last = AS.state(None, "/work/proj")["last"]
        self.assertTrue(last.get("waiting"))

    def test_a_failed_send_is_a_sentence_not_a_traceback(self):
        def boom(root, cwd):
            raise RuntimeError("the network said no " + "x" * 400)

        with mock.patch("human_compact.trajectory.supabase_client.sync_project",
                        boom):
            AS.schedule(None, "/work/proj", _delay=TICK)
            settle()
        last = AS.state(None, "/work/proj")["last"]
        self.assertFalse(last["ok"])
        self.assertLessEqual(len(last["error"]), 200)
        self.assertIn("the network said no", last["error"])

    # -------------------------------------------------------- what it says

    def test_the_panel_can_read_what_the_send_did(self):
        AS.schedule(None, "/work/proj", _delay=TICK)
        settle()
        said = AS.state(None, "/work/proj")
        self.assertTrue(said["enabled"])
        self.assertFalse(said["pending"])
        self.assertTrue(said["last"]["ok"])
        self.assertEqual(said["last"]["sent"], {"goals": 3})


class EditedGoalTests(unittest.TestCase):
    """The whole way through: a note typed into a real chat, and the send
    armed for the project that chat belongs to."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "checkout"
        self.cwd.mkdir()
        session = "chat-autosync"
        p = chat_state.paths(session, self.root)
        p.session_dir.mkdir(parents=True)
        rows = {"version": 1, "goals": [
            {"id": "g1", "title": "Ship the router", "status": "active",
             "priority": "normal", "notes": "", "origin": "user"}]}
        GM.sanitize(rows)
        p.goals.write_text(json.dumps(rows))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))
        p.manifest.write_text(json.dumps({"cwd": str(self.cwd)}))
        self.trajdir = p.session_dir
        self.armed = []
        patch = mock.patch.object(
            AS, "schedule", lambda root, cwd: self.armed.append(str(cwd)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_typing_a_note_arms_the_send_for_this_project(self):
        out = UI._apply({"op": "set_notes", "goal_id": "g1",
                         "notes": "# what this is for"}, self.trajdir, True)
        self.assertTrue(out.get("ok"), out)
        # Resolved on both sides: the project identity resolves symlinks, and
        # on macOS a temp dir is /var -> /private/var.
        self.assertEqual([str(Path(c).resolve()) for c in self.armed],
                         [str(self.cwd.resolve())])

    def test_asking_a_question_of_the_workspace_arms_nothing(self):
        UI._apply({"op": "list_shares"}, self.trajdir, True)
        self.assertEqual(self.armed, [])


if __name__ == "__main__":
    unittest.main()
