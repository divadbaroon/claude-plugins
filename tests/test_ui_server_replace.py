"""One scope, one server.

Launching the goal UI twice used to leave the first process serving whatever
code it started with, on a port the browser might still be pointed at. These
tests hold the replacement contract — and, just as importantly, the limits on
what it is willing to signal.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import ui  # noqa: E402


class RegistryGuardTests(unittest.TestCase):
    """What the launcher refuses to touch matters more than what it stops."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trajdir = Path(self.temp.name) / "vault" / "trajectory"
        self.trajdir.mkdir(parents=True)

    def write(self, record):
        ui._registry_path(self.trajdir).write_text(json.dumps(record))

    def test_no_registry_stops_nothing(self):
        self.assertIsNone(ui.stop_existing(self.trajdir))

    def test_a_stale_registry_is_cleared_not_signalled(self):
        self.write({"pid": 999999999, "url": "http://127.0.0.1:8765/"})
        self.assertIsNone(ui.stop_existing(self.trajdir))
        self.assertFalse(ui._registry_path(self.trajdir).exists())

    def test_a_live_pid_that_is_not_our_server_is_left_alone(self):
        # A pid outlives its process and is recycled. This one is alive and
        # ours to inspect, but nothing answers /api/health on that port.
        self.write({"pid": os.getpid(), "url": "http://127.0.0.1:1/"})
        self.assertIsNone(ui.stop_existing(self.trajdir))

    def test_a_non_loopback_url_is_never_probed_or_signalled(self):
        for url in ("http://example.com:8765/", "https://127.0.0.1:8765/",
                    "http://10.0.0.5:8765/"):
            with self.subTest(url=url):
                self.write({"pid": os.getpid(), "url": url})
                self.assertIsNone(ui.stop_existing(self.trajdir))

    def test_a_corrupt_registry_is_survivable(self):
        ui._registry_path(self.trajdir).write_text("{not json")
        self.assertIsNone(ui.stop_existing(self.trajdir))


class ReplaceLaunchTests(unittest.TestCase):
    """End to end: two launches, one survivor."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        (self.vault / "trajectory").mkdir(parents=True)
        self.trajdir = self.vault / "trajectory"
        self.children = []
        self.addCleanup(self.reap)

    def reap(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()

    def launch(self, *extra):
        env = os.environ.copy()
        env["CLAUDE_VAULT_DIR"] = str(self.vault)
        # The global browser UI is experimental in this release; these are its
        # contracts for when it is switched on.
        env["HC_EXPERIMENTAL"] = "1"
        env["PYTHONPATH"] = str(HC_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        child = subprocess.Popen(
            [sys.executable, "-m", "human_compact.cli", "ui", "--no-open",
             "--port", "0", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        self.children.append(child)
        return child

    def wait_for_registry(self, pid, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = ui._read_registry(self.trajdir)
            if isinstance(record, dict) and record.get("pid") == pid:
                return record
            time.sleep(0.05)
        self.fail(f"server {pid} never registered")

    def test_the_second_launch_replaces_the_first(self):
        first = self.launch()
        record = self.wait_for_registry(first.pid)
        self.assertEqual("global", record["scope"])
        self.assertTrue(record["url"].startswith("http://127.0.0.1:"))

        second = self.launch()
        self.wait_for_registry(second.pid)
        self.assertIsNotNone(first.wait(timeout=10),
                             "the first server should have been stopped")
        self.assertIsNone(second.poll(), "the replacement should still be up")

    def test_no_replace_leaves_the_first_running(self):
        first = self.launch()
        self.wait_for_registry(first.pid)
        second = self.launch("--no-replace")
        self.wait_for_registry(second.pid)
        self.assertIsNone(first.poll(), "--no-replace must not stop anything")

    def test_shutdown_clears_the_registry_it_owns(self):
        child = self.launch()
        self.wait_for_registry(child.pid)
        child.terminate()
        child.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not ui._registry_path(self.trajdir).exists():
                return
            time.sleep(0.05)
        self.fail("registry outlived the server that owned it")


if __name__ == "__main__":
    unittest.main()
