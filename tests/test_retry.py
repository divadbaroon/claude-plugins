"""Retrying provider calls, and the shared gate that makes concurrency safe.

The point of this module is not speed: it is that a transient failure must not
cost a conversation. Before it existed, one rate-limited call meant that
conversation was dropped from synthesis and the goal tree was quietly built
from less evidence than the vault held.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import retry as R  # noqa: E402


class ClassificationTests(unittest.TestCase):
    """Retrying a permanent failure just wastes the user's quota."""

    def test_rate_limits_are_transient(self):
        for text in ("rate limit exceeded", "HTTP 429", "Too Many Requests",
                     "model overloaded", "503 Service Unavailable",
                     "claude CLI timed out after 180s", "connection reset"):
            self.assertTrue(R.is_transient(RuntimeError(text)), text)

    def test_a_malformed_response_is_not(self):
        for text in ("did not return parseable JSON",
                     "claude CLI not found on PATH",
                     "goal synthesis response is missing the goals array"):
            self.assertFalse(R.is_transient(RuntimeError(text)), text)


class CallTests(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.clock = {"t": 0.0}

    def gate(self):
        """A gate whose clock moves when it sleeps, so waiting terminates."""
        def sleep(seconds):
            self.slept.append(round(seconds, 3))
            self.clock["t"] += seconds
        return R.Gate(sleep=sleep, now=lambda: self.clock["t"])

    def test_a_transient_failure_is_retried_until_it_works(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("429 rate limit")
            return "extracted"

        got = R.call(flaky, gate=self.gate(), jitter=lambda a, b: 0)
        self.assertEqual("extracted", got)
        self.assertEqual(3, len(calls))

    def test_backoff_grows(self):
        clock, slept = {"t": 0.0}, []

        def sleep(seconds):
            slept.append(round(seconds, 3))
            clock["t"] += seconds

        gate = R.Gate(sleep=sleep, now=lambda: clock["t"])

        def always():
            raise RuntimeError("overloaded")

        with self.assertRaises(RuntimeError):
            R.call(always, gate=gate, jitter=lambda a, b: 0)
        self.assertEqual([2.0, 4.0, 8.0], slept)

    def test_a_permanent_failure_is_raised_at_once(self):
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError("did not return parseable JSON")

        with self.assertRaises(RuntimeError):
            R.call(broken, gate=self.gate())
        self.assertEqual(1, len(calls))
        self.assertEqual([], self.slept)

    def test_it_gives_up_rather_than_retrying_forever(self):
        calls = []

        def always():
            calls.append(1)
            raise RuntimeError("timed out")

        with self.assertRaises(RuntimeError):
            R.call(always, gate=self.gate(), jitter=lambda a, b: 0)
        self.assertEqual(R.MAX_ATTEMPTS, len(calls))

    def test_the_original_error_survives(self):
        def always():
            raise RuntimeError("429 slow down please")

        with self.assertRaises(RuntimeError) as caught:
            R.call(always, gate=self.gate(), jitter=lambda a, b: 0)
        self.assertIn("slow down please", str(caught.exception))


class GateTests(unittest.TestCase):
    """One worker being throttled is news about the account, not the call."""

    def test_a_hold_makes_every_caller_wait(self):
        clock = {"t": 0.0}
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock["t"] += seconds

        gate = R.Gate(sleep=sleep, now=lambda: clock["t"])
        gate.hold(10)
        gate.wait()
        self.assertEqual([10], slept)

    def test_an_expired_hold_does_not_wait(self):
        clock = {"t": 100.0}
        slept = []
        gate = R.Gate(sleep=slept.append, now=lambda: clock["t"])
        gate.hold(5)
        clock["t"] = 200.0
        gate.wait()
        self.assertEqual([], slept)

    def test_one_workers_rate_limit_slows_the_others(self):
        # The failure mode this prevents: eight workers all retrying into the
        # same limit, turning one throttle into eight dropped conversations.
        clock = {"t": 0.0}
        gate = R.Gate(sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                      now=lambda: clock["t"])
        seen = []

        def limited():
            raise RuntimeError("429")

        with self.assertRaises(RuntimeError):
            R.call(limited, gate=gate, jitter=lambda a, b: 0)
        # A second worker arriving now is held rather than firing immediately.
        gate.wait()
        seen.append(gate.waits)
        self.assertGreater(seen[0], 0)


if __name__ == "__main__":
    unittest.main()
