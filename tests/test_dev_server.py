"""The workspace starting a project's own dev server.

A stub `npm` on PATH stands in for the package manager: it prints what a dev
server prints, then actually listens on the port it announced, so the parts
under test are the real ones -- the address is read back off stdout, the port
is probed, the process group is signalled. Nothing here builds anything.
"""

import json
import os
import socket
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import dev_server as DEV  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402


STUB = r'''#!/usr/bin/env python3
"""Prints the way a dev server does, then serves what it announced."""
import os, socket, sys, time
args = sys.argv[1:]
log = os.environ.get("STUB_NPM_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(args) + "\n")
if os.environ.get("STUB_NPM_FAIL") == "1":
    print("Failed to compile.")
    print("./app/page.tsx:3:1  Type error: nope", flush=True)
    raise SystemExit(1)
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", int(os.environ.get("STUB_NPM_PORT", "0"))))
sock.listen(8)
port = sock.getsockname()[1]
print("> dev")
print("   Next.js 15.0.0")
time.sleep(float(os.environ.get("STUB_NPM_DELAY", "0")))
print("  - Local:        http://localhost:%d" % port, flush=True)
print("  Ready in 900ms", flush=True)
while True:
    try:
        conn, _ = sock.accept()
        conn.close()
    except OSError:
        break
'''


def free_port():
    """A port nothing is on, so a test never reads the machine's own :3000."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(check, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


class DevServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-dev"
        paths = chat_state.paths(self.session, self.root)
        paths.session_dir.mkdir(parents=True)
        self.project = self.root / "app"
        (self.project / "node_modules").mkdir(parents=True)
        # Pinned to a port this machine is not using: the default 3000 may
        # well be taken by whatever the reader has running, and a test must
        # not read that as this project's server.
        self.hint = free_port()
        self.write_package({"dev": f"next dev -p {self.hint}"},
                           {"next": "15.0.0"})
        paths.manifest.write_text(json.dumps({"cwd": str(self.project)}))
        goals = {"version": 1, "goals": [
            {"id": "g1", "title": "Ship the interface", "status": "active"}]}
        GM.sanitize(goals)
        paths.goals.write_text(json.dumps(goals))
        paths.important.write_text(json.dumps({"items": []}))
        paths.prompts.write_text(json.dumps({"prompts": []}))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("npm", "pnpm"):
            stub = self.bin / name
            stub.write_text(STUB)
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.old_env = dict(os.environ)
        os.environ["PATH"] = str(self.bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["STUB_NPM_LOG"] = str(self.root / "npm.log")
        self.addCleanup(self.restore_env)
        self.addCleanup(self.stop_everything)

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    def stop_everything(self):
        try:
            DEV.stop(self.session, self.root, str(self.project), timeout=3.0)
        except Exception:  # noqa: BLE001 - teardown never fails the test
            pass
        DEV._RUNS.clear()

    def write_package(self, scripts, deps=None):
        (self.project / "package.json").write_text(json.dumps(
            {"name": "app", "scripts": scripts,
             "dependencies": deps or {}}))

    def status(self):
        return DEV.status(self.session, self.root, str(self.project))

    def start(self, **kw):
        return DEV.start(self.session, self.root, str(self.project), **kw)

    def running(self):
        return self.status()["status"] == "running"


class DetectTests(DevServerTests):
    def test_a_next_project_is_recognised_with_its_default_port(self):
        self.write_package({"dev": "next dev"}, {"next": "15.0.0"})
        plan = DEV.detect(str(self.project))
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(["npm", "run", "dev"], plan["command"])
        self.assertEqual("Next.js", plan["framework"])
        self.assertEqual(3000, plan["port_hint"])

    def test_the_lockfile_chooses_the_package_manager(self):
        (self.project / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
        self.assertEqual(["pnpm", "run", "dev"],
                         DEV.detect(str(self.project))["command"])

    def test_a_declared_package_manager_wins_over_the_lockfile(self):
        (self.project / "package-lock.json").write_text("{}")
        (self.project / "package.json").write_text(json.dumps(
            {"name": "app", "packageManager": "pnpm@9.1.0",
             "scripts": {"dev": "vite"}}))
        self.assertEqual(["pnpm", "run", "dev"],
                         DEV.detect(str(self.project))["command"])

    def test_a_port_pinned_in_the_script_is_the_hint(self):
        self.write_package({"dev": "next dev --port=4321"})
        self.assertEqual(4321, DEV.detect(str(self.project))["port_hint"])

    def test_start_stands_in_where_there_is_no_dev(self):
        self.write_package({"start": "node server.js"})
        plan = DEV.detect(str(self.project))
        self.assertEqual("start", plan["script"])

    def test_a_project_with_neither_script_says_so(self):
        self.write_package({"build": "next build"})
        plan = DEV.detect(str(self.project))
        self.assertFalse(plan["ok"])
        self.assertIn("no dev or start script", plan["error"])

    def test_missing_dependencies_are_reported_never_installed(self):
        for child in (self.project / "node_modules").iterdir():
            child.unlink()
        (self.project / "node_modules").rmdir()
        plan = DEV.detect(str(self.project))
        self.assertFalse(plan["ok"])
        self.assertIn("dependencies are not installed", plan["error"])
        # And nothing was run to fix it.
        self.assertFalse(Path(os.environ["STUB_NPM_LOG"]).exists())

    def test_a_directory_that_is_not_a_project_is_not_one(self):
        plan = DEV.detect(str(self.root / "nowhere"))
        self.assertFalse(plan["ok"])
        self.assertIn("no longer exists", plan["error"])


class StartStopTests(DevServerTests):
    def test_starting_serves_and_reports_the_address_it_printed(self):
        out = self.start()
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["started"], out)
        self.assertTrue(wait_for(self.running), self.status())
        state = self.status()
        port = int(state["url"].rsplit(":", 1)[1].strip("/"))
        # The address is the process's, not the framework default we guessed.
        self.assertNotEqual(3000, port)
        self.assertTrue(DEV._answers(port))
        self.assertEqual(["run", "dev"],
                         Path(os.environ["STUB_NPM_LOG"]).read_text().split())

    def test_a_second_start_does_not_start_a_second_server(self):
        self.assertTrue(self.start()["started"])
        self.assertTrue(wait_for(self.running))
        again = self.start()
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["started"], again)
        self.assertEqual(1, len(
            Path(os.environ["STUB_NPM_LOG"]).read_text().strip().splitlines()))

    def test_stopping_takes_the_port_with_it(self):
        self.start()
        self.assertTrue(wait_for(self.running))
        port = int(self.status()["url"].rsplit(":", 1)[1].strip("/"))
        out = DEV.stop(self.session, self.root, str(self.project))
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["stopped"], out)
        self.assertEqual("stopped", out["status"])
        self.assertTrue(wait_for(lambda: not DEV._answers(port)))
        # A server the reader stopped is not a server that failed.
        self.assertFalse(self.status().get("error"))

    def test_stopping_what_is_not_running_is_not_an_error(self):
        out = DEV.stop(self.session, self.root, str(self.project))
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["stopped"], out)

    def test_a_server_that_dies_reports_its_log_not_a_spinner(self):
        os.environ["STUB_NPM_FAIL"] = "1"
        self.start()
        self.assertTrue(wait_for(
            lambda: self.status()["status"] == "stopped"), self.status())
        state = self.status()
        self.assertIn("exited before it served", state["error"])
        self.assertIn("Type error: nope",
                      "\n".join(DEV.log(self.session, self.root,
                                        str(self.project))["lines"]))

    def test_the_log_carries_what_the_server_printed(self):
        self.start()
        self.assertTrue(wait_for(self.running))
        lines = DEV.log(self.session, self.root, str(self.project))["lines"]
        self.assertIn("$ npm run dev", lines[0])
        self.assertTrue(any("Local:" in ln for ln in lines), lines)


class AdoptionTests(DevServerTests):
    """The workspace was closed and opened again; the server was not."""

    def test_a_server_from_a_previous_workspace_is_found_again(self):
        self.start()
        self.assertTrue(wait_for(self.running))
        url = self.status()["url"]
        DEV._RUNS.clear()          # everything this process remembered, gone
        state = self.status()
        self.assertEqual("running", state["status"])
        self.assertEqual(url, state["url"])

    def test_and_can_be_stopped_from_the_record_alone(self):
        self.start()
        self.assertTrue(wait_for(self.running))
        port = int(self.status()["url"].rsplit(":", 1)[1].strip("/"))
        DEV._RUNS.clear()
        out = DEV.stop(self.session, self.root, str(self.project))
        self.assertTrue(out["stopped"], out)
        self.assertTrue(wait_for(lambda: not DEV._answers(port)))

    def test_a_recycled_pid_is_not_mistaken_for_the_server(self):
        record = {"cwd": str(self.project), "manager": "npm",
                  "status": "running", "pid": os.getpid(),
                  "url": "http://127.0.0.1:65500/"}
        # This process is alive and is not a group leader running npm, which
        # is exactly the case the identity check exists to refuse.
        self.assertIsNone(DEV._identity(record))


class PortInUseTests(DevServerTests):
    def setUp(self):
        super().setUp()
        self.held = socket.socket()
        self.held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.held.bind(("127.0.0.1", 0))
        self.held.listen(4)
        self.addCleanup(self.held.close)
        self.port = self.held.getsockname()[1]
        self.write_package({"dev": f"next dev -p {self.port}"},
                           {"next": "15.0.0"})

    def test_a_busy_port_stops_a_second_server_going_up_beside_it(self):
        state = self.status()
        self.assertEqual("in_use", state["status"])
        self.assertEqual(f"http://127.0.0.1:{self.port}/", state["other_url"])
        out = self.start()
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["started"], out)
        self.assertFalse(Path(os.environ["STUB_NPM_LOG"]).exists())

    def test_but_the_reader_can_say_start_anyway(self):
        os.environ["STUB_NPM_PORT"] = "0"     # the framework picks another
        out = self.start(force=True)
        self.assertTrue(out["started"], out)
        self.assertTrue(wait_for(self.running), self.status())
        self.assertNotEqual(f"http://127.0.0.1:{self.port}/",
                            self.status()["url"])


class OpTests(DevServerTests):
    """Through the workspace's own door, the way the panel asks."""

    def op(self, **body):
        return ui._apply(body, trajdir=chat_state.paths(
            self.session, self.root).session_dir, chat_scoped=True)

    def test_status_resolves_the_chats_project_directory(self):
        out = self.op(op="dev_status", goal_id="g1")
        self.assertTrue(out["ok"], out)
        self.assertEqual(str(self.project), out["cwd"])
        self.assertEqual("npm run dev", out["command"])
        self.assertEqual("stopped", out["status"])

    def test_start_and_stop_go_through_the_same_door(self):
        out = self.op(op="dev_start", goal_id="g1")
        self.assertTrue(out["started"], out)
        self.assertTrue(wait_for(self.running), self.status())
        opened = self.op(op="dev_log", goal_id="g1")
        self.assertTrue(any("Local:" in ln for ln in opened["lines"]))
        self.assertTrue(self.op(op="dev_stop", goal_id="g1")["stopped"])

    def test_a_goal_that_names_its_own_project_is_served_from_there(self):
        other = self.root / "other"
        (other / "node_modules").mkdir(parents=True)
        (other / "package.json").write_text(json.dumps(
            {"name": "other", "scripts": {"dev": "vite"}}))
        paths = chat_state.paths(self.session, self.root)
        goals = json.loads(paths.goals.read_text())
        goals["goals"][0]["project_cwd"] = str(other)
        paths.goals.write_text(json.dumps(goals))
        out = self.op(op="dev_status", goal_id="g1")
        self.assertEqual(str(other), out["cwd"])

    def test_a_global_workspace_has_no_project_to_serve(self):
        out = ui._apply({"op": "dev_start"}, trajdir=chat_state.paths(
            self.session, self.root).session_dir, chat_scoped=False)
        self.assertFalse(out["ok"])
        self.assertIn("chat scope", out["error"])


if __name__ == "__main__":
    unittest.main()
