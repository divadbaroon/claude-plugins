"""Retry for provider calls, shared across concurrent extractions.

Without this, one flaky call costs a conversation permanently: extract_all
collects the failure, synthesis skips that conversation, and the goal tree is
built from less evidence than the vault holds — silently, because nothing on
screen distinguishes "not analyzed" from "failed once".

Concurrency makes that worse rather than better, since the first thing to fail
under load is the rate limiter. So the gate is shared: when one call is told to
slow down, every worker waits, instead of all of them retrying into the same
wall.
"""
import random
import re
import threading
import time

# Transient means "the same call could succeed later": the model was busy, the
# account was throttled, the subprocess ran out of time. A response that parsed
# into the wrong shape will parse the same way forever.
_TRANSIENT = re.compile(
    r"rate.?limit|429|too many requests|overloaded|503|502|504|"
    r"timed out|timeout|temporarily unavailable|connection reset",
    re.I)

MAX_ATTEMPTS = 4
BASE_DELAY = 2.0
MAX_DELAY = 60.0


def is_transient(error) -> bool:
    return bool(_TRANSIENT.search(str(error)))


class Gate:
    """A cooldown every worker respects.

    One worker hitting a rate limit is evidence about the account, not about
    that conversation. Holding the others back turns a burst of failures into
    a slower run instead of a shorter goal tree.
    """

    def __init__(self, sleep=time.sleep, now=time.monotonic):
        self._sleep = sleep
        self._now = now
        self._lock = threading.Lock()
        self._until = 0.0
        self.waits = 0

    def wait(self):
        # One sleep, not a spin: a caller that under-sleeps is caught by the
        # next wait, and a loop here would never end if the clock and the
        # sleep ever disagree.
        with self._lock:
            remaining = min(self._until - self._now(), MAX_DELAY)
            if remaining <= 0:
                return
            self.waits += 1
        self._sleep(remaining)

    def hold(self, seconds):
        with self._lock:
            self._until = max(self._until, self._now() + seconds)


def call(fn, *, gate=None, attempts=MAX_ATTEMPTS, sleep=time.sleep,
         jitter=random.uniform, on_retry=None):
    """Run fn(), retrying transient failures with backoff behind the gate."""
    gate = gate if gate is not None else Gate(sleep=sleep)
    last = None
    for attempt in range(1, attempts + 1):
        gate.wait()          # the gate owns the waiting; call() never sleeps
        try:
            return fn()
        except Exception as error:                       # noqa: BLE001
            last = error
            if attempt >= attempts or not is_transient(error):
                raise
            delay = min(MAX_DELAY, BASE_DELAY * (2 ** (attempt - 1)))
            delay += jitter(0, delay / 2)                # spread the retries
            gate.hold(delay)
            if on_retry:
                on_retry(attempt, delay, error)
    raise last                                           # unreachable
