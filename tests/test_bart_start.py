"""`bart start`: the vault's Projects page, from a terminal.

The page in question is not a project's page. It lists every project the
vault knows, and it is drawn by whichever workspace server happens to be up
-- so the question this command answers is "is one up?", not "is MINE up?".
And it never answers by making a project: a viewer that mints a row for the
directory it was run in turns `cd ~/Downloads && bart start` into a project
called Downloads, and the vault fills with them.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact import cli  # noqa: E402
from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402


class BartStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "chat-state"
        env = mock.patch.dict(os.environ, {"HC_CHAT_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.opened = []
        browser = mock.patch("webbrowser.open", self.opened.append)
        browser.start()
        self.addCleanup(browser.stop)

    def sessions(self):
        try:
            return sorted(p.name for p in self.state.iterdir() if p.is_dir())
        except OSError:
            return []

    def project(self, name, session=None, objective=""):
        """A project on disk, optionally with a store to serve it."""
        home = Path(self.tmp.name) / name
        home.mkdir(parents=True, exist_ok=True)
        PS.save_project(None, str(home), {"objective": objective or name})
        if session:
            p = CS.paths(session)
            p.session_dir.mkdir(parents=True, exist_ok=True)
            p.manifest.write_text(json.dumps({
                "schema_version": 1,
                "session_id": session,
                "cwd": str(home),
                "origin": "workspace",
            }))
            PS.set_tree_session(None, str(home), session)
        return PS._resolved(str(home))

    def run_start(self, argv=None, cwd=None):
        said = io.StringIO()
        with mock.patch("os.getcwd", return_value=cwd or self.tmp.name):
            with redirect_stdout(said):
                code = cli.bart_main(["start"] + list(argv or []))
        return code, said.getvalue()

    # -- nothing to show -------------------------------------------------

    def test_a_vault_with_no_projects_says_so_and_makes_nothing(self):
        before = self.sessions()
        with self.assertRaises(SystemExit) as stop:
            self.run_start()
        self.assertNotEqual(0, stop.exception.code)
        self.assertEqual(before, self.sessions())
        self.assertEqual([], PS.list_projects(None))

    def test_running_it_in_an_unknown_directory_makes_no_project(self):
        """The wedge this design turns on: a viewer must not create rows."""
        here = Path(self.tmp.name) / "downloads"
        here.mkdir()
        self.project("acme", session="hcws-" + "a" * 24)
        with mock.patch.object(cli, "chat_ui_main",
                               side_effect=self._serve("http://127.0.0.1:7001/")):
            code, out = self.run_start(cwd=str(here))
        self.assertEqual(0, code)
        self.assertEqual(["acme"], [r["name"] for r in PS.list_projects(None)])
        self.assertIn("#projects", out)

    # -- reuse -----------------------------------------------------------

    def test_a_healthy_server_anywhere_in_the_vault_is_reused(self):
        sid = "hcws-" + "b" * 24
        home = self.project("acme", session=sid)
        PS.set_server_record(None, home, {
            "schema_version": 1, "session_id": sid,
            "pid": os.getpid(), "url": "http://127.0.0.1:7002/",
            "started_at": 0,
        })
        with mock.patch.object(cli, "_healthy_chat_server", return_value=True):
            with mock.patch.object(cli, "chat_ui_main") as launch:
                code, out = self.run_start()
        self.assertEqual(0, code)
        launch.assert_not_called()
        self.assertEqual("http://127.0.0.1:7002/#projects", out.strip())
        self.assertEqual(["http://127.0.0.1:7002/#projects"], self.opened)

    def test_the_project_underfoot_is_preferred_to_the_newest_one(self):
        old = self.project("acme", session="hcws-" + "c" * 24)
        new = self.project("widget", session="hcws-" + "d" * 24)
        for home, port in ((old, 7003), (new, 7004)):
            PS.set_server_record(None, home, {
                "schema_version": 1,
                "session_id": PS.tree_session(None, home),
                "pid": os.getpid(),
                "url": f"http://127.0.0.1:{port}/",
                "started_at": 0,
            })
        self.assertEqual("widget", PS.list_projects(None)[0]["name"])
        with mock.patch.object(cli, "_healthy_chat_server", return_value=True):
            code, out = self.run_start(cwd=old)
        self.assertEqual(0, code)
        self.assertEqual("http://127.0.0.1:7003/#projects", out.strip())

    # -- launch ----------------------------------------------------------

    def _serve(self, url):
        def launch(argv):
            print(url)
            return 0
        return launch

    def test_with_nothing_running_it_starts_a_server_on_an_existing_store(self):
        sid = "hcws-" + "e" * 24
        home = self.project("acme", session=sid)
        before = self.sessions()
        seen = {}

        def launch(argv):
            seen["argv"] = list(argv)
            print("http://127.0.0.1:7005/")
            return 0

        with mock.patch.object(cli, "_healthy_chat_server", return_value=False):
            with mock.patch.object(cli, "chat_ui_main", side_effect=launch):
                code, out = self.run_start()
        self.assertEqual(0, code)
        self.assertEqual("http://127.0.0.1:7005/#projects", out.strip())
        self.assertIn("--session", seen["argv"])
        self.assertEqual(sid, seen["argv"][seen["argv"].index("--session") + 1])
        self.assertIn("--no-open", seen["argv"])
        # No store was minted to serve a page that lists what already exists.
        self.assertEqual(before, self.sessions())

    def test_a_project_with_no_store_is_skipped_for_one_that_has_a_store(self):
        self.project("empty")
        sid = "hcws-" + "f" * 24
        self.project("acme", session=sid)
        seen = {}

        def launch(argv):
            seen["argv"] = list(argv)
            print("http://127.0.0.1:7006/")
            return 0

        with mock.patch.object(cli, "_healthy_chat_server", return_value=False):
            with mock.patch.object(cli, "chat_ui_main", side_effect=launch):
                code, _ = self.run_start()
        self.assertEqual(0, code)
        self.assertEqual(sid, seen["argv"][seen["argv"].index("--session") + 1])

    def test_no_open_prints_the_page_without_opening_a_browser(self):
        sid = "hcws-" + "0" * 24
        home = self.project("acme", session=sid)
        PS.set_server_record(None, home, {
            "schema_version": 1, "session_id": sid, "pid": os.getpid(),
            "url": "http://127.0.0.1:7007/", "started_at": 0,
        })
        with mock.patch.object(cli, "_healthy_chat_server", return_value=True):
            code, out = self.run_start(["--no-open"])
        self.assertEqual(0, code)
        self.assertEqual("http://127.0.0.1:7007/#projects", out.strip())
        self.assertEqual([], self.opened)

    def test_a_server_that_will_not_start_is_reported_not_swallowed(self):
        self.project("acme", session="hcws-" + "9" * 24)

        def launch(argv):
            print("something went wrong")
            return 0

        with mock.patch.object(cli, "_healthy_chat_server", return_value=False):
            with mock.patch.object(cli, "chat_ui_main", side_effect=launch):
                with self.assertRaises(SystemExit) as stop:
                    self.run_start()
        self.assertNotEqual(0, stop.exception.code)
        self.assertEqual([], self.opened)


class BartAccountCompatibilityTests(unittest.TestCase):
    """The installed launcher reads, but never rivals, npm device auth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.managed = Path(self.tmp.name)
        environment = mock.patch.dict(
            os.environ, {"HUMAN_COMPACT_HOME": str(self.managed)})
        environment.start()
        self.addCleanup(environment.stop)

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.bart_main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_token_reads_the_device_flow_record_for_legacy_helpers(self):
        (self.managed / "auth.json").write_text(json.dumps({
            "token": "egb_machine",
            "email": "member@example.com",
            "claude": {"apiKey": "sk-issued", "budgetUsd": 25,
                       "spendUsd": 3},
        }), encoding="utf-8")
        code, out, err = self.call("token")
        self.assertEqual(0, code)
        self.assertEqual("sk-issued", out.strip())
        self.assertEqual("", err)

    def test_account_mutation_points_to_the_only_auth_flow(self):
        code, out, err = self.call("auth")
        self.assertEqual(2, code)
        self.assertEqual("", out)
        self.assertIn("npx engelbart-cli auth", err)


if __name__ == "__main__":
    unittest.main()
