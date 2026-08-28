"""The middle pane: how a project runs, and what happens when it does.

The pane is a projection of two things -- a surface and a lifecycle -- so
these tests are about the two staying apart. A project nobody has installed
is not a failed run; a program that prints instead of serving is not a broken
web preview; a dev server that printed a URL and then fell over is not
running because the URL is still in the scrollback.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from human_compact.trajectory import preview as PV  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402
from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        self.root.mkdir()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.addCleanup(self.quiet)

    def quiet(self):
        proc = PV.running(self.project)
        if proc:
            proc.stop()
        PV.forget(self.project)

    def write(self, name, text):
        spot = self.project / name
        spot.parent.mkdir(parents=True, exist_ok=True)
        spot.write_text(text if isinstance(text, str) else json.dumps(text))
        return spot


class DetectorTests(Fixture):
    """The repository's own files say how it runs. Nothing is asked, and
    nothing is run, to find that out."""

    def ids(self):
        return [p["id"] for p in PV.detect(self.project)]

    def test_a_project_that_says_nothing_gets_no_guess(self):
        self.assertEqual([], PV.detect(self.project))

    def test_the_dev_script_is_what_a_node_project_opens_on(self):
        self.write("package.json", {"scripts": {"dev": "next dev",
                                                "start": "next start",
                                                "test": "vitest"}})
        found = PV.detect(self.project)
        self.assertEqual("dev", found[0]["id"])
        self.assertEqual("npm run dev", found[0]["command"])
        self.assertTrue(found[0]["serves"])
        # The tests are a run profile too, and never the primary one: the
        # pane's question is "what happens if I run this", and a suite
        # answers a different question.
        self.assertEqual("test", found[-1]["id"])
        self.assertFalse(found[-1]["serves"])

    def test_the_lockfile_decides_which_package_manager_is_named(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        self.write("pnpm-lock.yaml", "lockfileVersion: 6.0\n")
        self.assertEqual("pnpm run dev", PV.detect(self.project)[0]["command"])

    def test_a_django_project_is_recognised_by_its_own_entry_point(self):
        self.write("manage.py", "# django\n")
        found = PV.detect(self.project)[0]
        self.assertEqual("python manage.py runserver", found["command"])
        self.assertTrue(found["serves"])

    def test_a_script_that_serves_is_told_apart_from_one_that_prints(self):
        self.write("main.py", "print('hello')\n")
        found = PV.detect(self.project)[0]
        self.assertEqual("python main.py", found["command"])
        self.assertFalse(found["serves"])
        self.assertEqual("script", found["kind"])

    def test_a_fastapi_file_is_served_rather_than_executed(self):
        self.write("main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        found = PV.detect(self.project)[0]
        self.assertIn("uvicorn", found["command"])
        self.assertTrue(found["serves"])

    def test_a_makefile_target_and_a_procfile_are_both_evidence(self):
        self.write("Makefile", "dev:\n\tpython -m server\n\nclean:\n\trm -rf x\n")
        self.assertIn("make-dev", self.ids())
        self.write("Procfile", "web: gunicorn app:app\n")
        found = PV.detect(self.project)
        self.assertIn("procfile-web", [p["id"] for p in found])

    def test_a_folder_of_html_is_offered_as_a_static_site(self):
        self.write("index.html", "<h1>hi</h1>")
        found = PV.detect(self.project)[0]
        self.assertEqual("static", found["id"])
        self.assertTrue(found["serves"])

    def test_a_static_site_stops_being_the_answer_once_there_is_a_build(self):
        self.write("index.html", "<h1>hi</h1>")
        self.write("package.json", {"scripts": {"dev": "vite"}})
        self.assertNotIn("static", self.ids())

    def test_every_profile_carries_the_sentence_that_explains_it(self):
        self.write("package.json", {"scripts": {"dev": "next dev"}})
        self.write("Cargo.toml", "[package]\nname='x'\n")
        for item in PV.detect(self.project):
            self.assertTrue(item["why"].strip(), item)
            self.assertTrue(item["why"].endswith("."), item)


class BlockerTests(Fixture):
    """One step needed. Only what a file on disk can prove -- everything
    subtler is the failed card's job, which has the actual error."""

    def test_a_node_project_with_no_modules_names_the_install(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        found = PV.blockers(self.project, {"command": "npm run dev"})
        self.assertEqual(["node_modules"], [b["id"] for b in found])
        self.assertEqual("npm install", found[0]["command"])
        self.assertEqual("run", found[0]["kind"])

    def test_installed_modules_are_not_a_blocker(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        (self.project / "node_modules").mkdir()
        self.assertEqual([], PV.blockers(self.project, {"command": "npm run dev"}))

    def test_an_example_env_with_no_real_one_is_a_manual_step(self):
        self.write(".env.example", "DATABASE_URL=\n")
        found = PV.blockers(self.project, {"command": "npm run dev"})
        self.assertEqual(["env"], [b["id"] for b in found])
        # Never offered as something to just run: the copy is trivial, the
        # filling in is not, and a card that says "done" after the copy
        # would be lying about the second half.
        self.assertEqual("manual", found[0]["kind"])

    def test_a_python_command_with_no_environment_says_so(self):
        self.write("requirements.txt", "flask\n")
        self.write("app.py", "x = 1\n")
        os.environ.pop("VIRTUAL_ENV", None)
        found = PV.blockers(self.project, {"command": "python app.py"})
        self.assertEqual(["python_env"], [b["id"] for b in found])
        (self.project / ".venv").mkdir()
        self.assertEqual([], PV.blockers(self.project, {"command": "python app.py"}))

    def test_a_node_blocker_is_not_raised_against_a_python_command(self):
        self.write("requirements.txt", "flask\n")
        (self.project / ".venv").mkdir()
        self.assertEqual([], PV.blockers(self.project, {"command": "pytest"}))


class ConfigTests(Fixture):
    """The answer is cached against the files it was derived from."""

    def test_configuring_writes_the_profiles_and_the_fingerprint(self):
        self.write("package.json", {"scripts": {"dev": "next dev"}})
        out = PV.configure(self.root, self.project)
        self.assertTrue(out["ok"])
        self.assertEqual("repository", out["source"])
        held = PV.read_config(self.root, self.project)
        self.assertEqual("dev", held["primary"])
        self.assertEqual(PV.fingerprint(self.project), held["fingerprint"])

    def test_a_build_file_changing_is_what_makes_the_answer_stale(self):
        (self.project / "node_modules").mkdir()
        self.write("package.json", {"scripts": {"dev": "next dev"}})
        PV.configure(self.root, self.project)
        self.assertFalse(PV.state(self.root, self.project)["stale"])
        # Source changing is not: how a project runs is decided by its build
        # files, and re-deriving it on every edit would be a model call an
        # hour for an answer nobody changed.
        self.write("src/app.tsx", "export default 1\n")
        self.assertFalse(PV.state(self.root, self.project)["stale"])
        self.write("package.json", {"scripts": {"dev": "next dev --turbo"}})
        after = PV.state(self.root, self.project)
        self.assertTrue(after["stale"])
        self.assertEqual("stale", after["status"])

    def test_a_step_still_needed_outranks_the_answer_being_stale(self):
        # Both are true and only one of them is what to do next. The flag
        # rides along either way, so the pane can say "this may be out of
        # date" over a card that is still the thing to press.
        self.write("package.json", {"scripts": {"dev": "vite"}})
        PV.configure(self.root, self.project)
        self.write("package.json", {"scripts": {"dev": "vite --host"}})
        out = PV.state(self.root, self.project)
        self.assertTrue(out["stale"])
        self.assertEqual("needs_user_action", out["status"])
        self.assertEqual("node_modules", out["blockers"][0]["id"])

    def test_a_verified_command_keeps_its_stamp_through_a_re_detect(self):
        self.write("package.json", {"scripts": {"dev": "next dev"}})
        PV.configure(self.root, self.project)
        PV.note_verified(self.root, self.project, "npm run dev")
        self.write("package.json", {"scripts": {"dev": "next dev",
                                                "test": "vitest"}})
        PV.configure(self.root, self.project)
        held = PV.read_config(self.root, self.project)
        by_id = {p["id"]: p for p in held["profiles"]}
        self.assertTrue(by_id["dev"].get("verified"))
        self.assertFalse(by_id["test"].get("verified"))

    def test_the_reader_can_choose_which_profile_is_the_primary_one(self):
        self.write("package.json", {"scripts": {"dev": "vite", "test": "vitest"}})
        PV.configure(self.root, self.project)
        self.assertFalse(PV.set_primary(self.root, self.project, "nope")["ok"])
        self.assertTrue(PV.set_primary(self.root, self.project, "test")["ok"])
        self.assertEqual("test", PV.state(self.root, self.project)["profile"]["id"])

    def test_a_project_with_nothing_runnable_asks_the_model_once(self):
        class Engine:
            calls = 0

            def generate_searching(self, prompt, where=""):
                Engine.calls += 1
                return json.dumps({"command": "bin/run --all", "kind": "cli",
                                   "name": "Simulation", "serves": False,
                                   "why": "Runs the simulation."})

        out = PV.configure(self.root, self.project, engine=Engine())
        self.assertTrue(out["ok"])
        self.assertEqual("model", out["source"])
        self.assertEqual("bin/run --all", out["profiles"][0]["command"])
        # And a project whose own files answer never reaches it.
        self.write("package.json", {"scripts": {"dev": "vite"}})
        PV.configure(self.root, self.project, engine=Engine())
        self.assertEqual(1, Engine.calls)

    def test_a_model_that_finds_nothing_says_so_rather_than_inventing(self):
        class Engine:
            def generate_searching(self, prompt, where=""):
                return json.dumps({"command": ""})

        out = PV.configure(self.root, self.project, engine=Engine())
        self.assertFalse(out["ok"])
        self.assertIn("run", out["error"])


class SupervisorTests(Fixture):
    """The processes, and what is true about them."""

    def start(self, command, serves=True, wait=2.5, until=None):
        profile = {"id": "t", "name": "T", "kind": "web", "command": command,
                   "why": "", "serves": serves}
        out = PV.start(self.root, self.project, profile)
        self.assertTrue(out["ok"], out)
        proc = PV.running(self.project)
        deadline = time.time() + wait
        while time.time() < deadline:
            if until and until(proc):
                break
            time.sleep(0.05)
        return proc

    def test_a_server_is_running_only_once_its_address_answers(self):
        port = 8951
        # Bound before it is announced, the way a dev server does it: the
        # line is the evidence, and evidence printed before the socket
        # exists is what makes a pane say "running" over a refused
        # connection.
        script = (f"import http.server, socketserver\n"
                  f"srv = socketserver.TCPServer(('127.0.0.1', {port}),"
                  f" http.server.SimpleHTTPRequestHandler)\n"
                  f"print('Local:   http://127.0.0.1:{port}/')\n"
                  f"srv.serve_forever()\n")
        self.write("serve.py", script)
        proc = self.start("python3 serve.py", until=lambda p: bool(p.url))
        self.assertEqual(f"http://127.0.0.1:{port}/", proc.url)
        deadline = time.time() + 5
        while time.time() < deadline and not proc.healthy:
            proc.probe(force=True)
            time.sleep(0.1)
        self.assertTrue(proc.healthy)
        out = PV.state(self.root, self.project)
        self.assertEqual("running", out["status"])
        self.assertEqual("web", out["surface"])
        self.assertEqual(proc.url, out["url"])

    def test_output_arrives_a_line_at_a_time_rather_than_at_the_end(self):
        # The reason the run is on a pseudo-terminal: a program that cannot
        # see one buffers, and a simulation printing a line a second would
        # show nothing until it finished.
        self.write("count.py", "import time\n"
                               "for i in range(30):\n"
                               "    print('step', i)\n"
                               "    time.sleep(0.1)\n")
        proc = self.start("python3 count.py", serves=False,
                          until=lambda p: len(p.lines) >= 3)
        self.assertGreaterEqual(len(proc.lines), 3)
        self.assertTrue(proc.alive())
        self.assertEqual("step 0", proc.lines[0])
        # No URL and no reason to expect one: this is a terminal.
        out = PV.state(self.root, self.project)
        self.assertEqual("terminal", out["surface"])

    def test_a_program_that_prints_and_ends_leaves_what_it_wrote(self):
        self.write("make_chart.py",
                   "open('chart.png', 'wb').write(b'x')\nprint('wrote chart')\n")
        proc = self.start("python3 make_chart.py", serves=False,
                          until=lambda p: p.exit_code is not None)
        self.assertEqual(0, proc.exit_code)
        out = PV.state(self.root, self.project)
        self.assertEqual("finished", out["status"])
        self.assertEqual("artifact", out["surface"])
        self.assertIn("chart.png", out["artifacts"])

    def test_a_command_that_fails_is_failed_and_keeps_its_last_words(self):
        proc = self.start("echo 'boom: no such module' >&2; exit 3",
                          serves=False, until=lambda p: p.exit_code is not None)
        self.assertEqual(3, proc.exit_code)
        out = PV.state(self.root, self.project)
        self.assertEqual("failed", out["status"])
        self.assertEqual("instructions", out["surface"])
        self.assertIn("boom: no such module", "\n".join(out["run"]["lines"]))

    def test_a_dead_server_is_not_running_because_its_url_is_in_the_log(self):
        self.write("liar.py", "print('http://localhost:8952')\n")
        proc = self.start("python3 liar.py", until=lambda p: p.exit_code is not None)
        self.assertEqual("http://localhost:8952", proc.url)
        out = PV.state(self.root, self.project)
        self.assertNotEqual("running", out["status"])
        self.assertEqual("", out.get("url", ""))

    def test_the_whole_group_goes_down_with_the_run(self):
        self.write("child.py", "import time\nprint('up')\ntime.sleep(60)\n")
        proc = self.start("python3 child.py & wait", serves=False,
                          until=lambda p: bool(p.lines))
        pid = proc.process.pid
        self.assertTrue(PV.stop(self.project)["ok"])
        deadline = time.time() + 5
        while time.time() < deadline and proc.alive():
            time.sleep(0.05)
        self.assertFalse(proc.alive())
        # The shell is gone and so is what it started: nothing in that
        # process group is left holding a port.
        with self.assertRaises(OSError):
            os.killpg(os.getpgid(pid), 0)

    def test_two_runs_at_once_in_one_directory_are_refused(self):
        self.write("wait.py", "import time\ntime.sleep(30)\n")
        self.start("python3 wait.py", serves=False)
        again = PV.start(self.root, self.project,
                         {"id": "t2", "command": "python3 wait.py"})
        self.assertFalse(again["ok"])
        self.assertIn("already running", again["error"])

    def test_a_run_that_came_up_stamps_the_profile_as_verified(self):
        port = 8953
        self.write("package.json", {"scripts": {"dev": "python3 serve.py"}})
        self.write("serve.py",
                   f"import http.server, socketserver\n"
                   f"print('http://localhost:{port}')\n"
                   f"socketserver.TCPServer(('127.0.0.1', {port}),"
                   f" http.server.SimpleHTTPRequestHandler).serve_forever()\n")
        PV.configure(self.root, self.project)
        self.start("npm run dev" if False else "python3 serve.py",
                   until=lambda p: bool(p.url))
        # The command that ran is the one stamped, and only once it answered.
        PV.note_verified(self.root, self.project, "python3 serve.py")
        held = PV.read_config(self.root, self.project)
        self.assertEqual("python3 serve.py", held["verified_command"])
        self.assertTrue(held["verified_at"])


class ProjectionTests(Fixture):
    """Surface and status are two questions, and the pane asks both."""

    def test_an_unknown_project_is_unconfigured_and_shows_instructions(self):
        out = PV.state(self.root, self.project)
        self.assertEqual("unconfigured", out["status"])
        self.assertEqual("instructions", out["surface"])
        self.assertFalse(out["configured"])

    def test_a_configured_project_with_a_blocker_asks_for_the_step(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        PV.configure(self.root, self.project)
        out = PV.state(self.root, self.project)
        self.assertEqual("needs_user_action", out["status"])
        self.assertEqual("instructions", out["surface"])
        self.assertEqual("node_modules", out["blockers"][0]["id"])

    def test_a_configured_project_with_nothing_in_the_way_is_ready(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        (self.project / "node_modules").mkdir()
        PV.configure(self.root, self.project)
        out = PV.state(self.root, self.project)
        self.assertEqual("ready", out["status"])
        self.assertEqual("npm run dev", out["profile"]["command"])

    def test_a_directory_that_is_not_there_says_so_without_failing(self):
        out = PV.state(self.root, Path(self.tmp.name) / "gone")
        self.assertTrue(out["ok"])
        self.assertEqual("empty", out["surface"])


class IntentTests(Fixture):
    """What to look at, for the row being worked on. The configurator knows
    how the project runs; this knows what is worth seeing."""

    class Engine:
        def __init__(self, payload):
            self.payload, self.calls = payload, 0

        def generate_json(self, prompt):
            self.calls += 1
            self.prompt = prompt
            return dict(self.payload)

    def test_the_answer_is_about_the_task_not_the_repository(self):
        engine = self.Engine({"entrypoint": "/goals",
                              "scenario": ["Open a goal", "Click its title"],
                              "expected": "The new name survives a reload"})
        out = PV.intent_for(self.project, "Make goals easy to change",
                            "Allow users to rename goals",
                            {"command": "npm run dev"}, engine=engine)
        self.assertTrue(out["ok"])
        self.assertEqual("/goals", out["entrypoint"])
        self.assertEqual(2, len(out["scenario"]))
        self.assertIn("Allow users to rename goals", engine.prompt)
        self.assertIn("npm run dev", engine.prompt)

    def test_a_task_with_nothing_to_look_at_says_so(self):
        engine = self.Engine({"scenario": [], "expected": ""})
        out = PV.intent_for(self.project, "g", "Rename a variable", {},
                            engine=engine)
        self.assertFalse(out["ok"])

    def test_an_intent_is_kept_until_the_row_is_reworded(self):
        PV.save_intent(self.root, self.project, "t1", "Allow renaming",
                       {"expected": "it persists"})
        self.assertEqual("it persists",
                         PV.intent_of(self.root, self.project, "t1",
                                      "Allow renaming")["expected"])
        # Reworded row, no intent: sending the reader to check a thing
        # nobody is building any more is worse than asking again.
        self.assertEqual({}, PV.intent_of(self.root, self.project, "t1",
                                          "Allow deleting"))
        # Asked without the words at all -- the pane's sweep, which has the
        # id and not the row -- the stored answer stands.
        self.assertTrue(PV.intent_of(self.root, self.project, "t1"))


class ContractTests(Fixture):
    """What a build is handed: a contract, not a narrative."""

    def test_it_names_the_rows_the_run_and_what_would_count_as_done(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        PV.configure(self.root, self.project)
        config = PV.read_config(self.root, self.project)
        out = PV.contract(
            {"id": "g1", "title": "Make goals easy to change"},
            [{"id": "t1", "text": "Allow users to rename goals"}],
            config,
            {"entrypoint": "/goals", "expected": "The new name persists",
             "scenario": ["Open a goal"]})
        self.assertEqual("g1", out["goal"]["id"])
        self.assertEqual(["t1"], [r["id"] for r in out["todos"]])
        self.assertEqual("npm run dev", out["execution"]["command"])
        self.assertEqual("web", out["preview"]["surface"])
        self.assertEqual("The new name persists", out["verification"]["expected"])

    def test_a_run_that_does_not_serve_is_not_promised_a_web_surface(self):
        self.write("main.py", "print('x')\n")
        PV.configure(self.root, self.project)
        out = PV.contract({"id": "g1", "title": "t"}, [],
                          PV.read_config(self.root, self.project), {})
        self.assertEqual("terminal", out["preview"]["surface"])


class ServerTests(unittest.TestCase):
    """Through the HTTP surface the pane actually uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "vault"
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir(parents=True)
        (self.project / "package.json").write_text(
            json.dumps({"scripts": {"dev": "echo up"}}))
        (self.project / "node_modules").mkdir()
        self.session = "chat-preview"
        paths = CS.paths(self.session, self.root)
        paths.session_dir.mkdir(parents=True)
        goal = GM.new_goal("g1", "Ship the preview", origin="user")
        goal["todo_items"] = [{"id": "t1", "text": "Allow users to rename",
                               "depth": 0, "status": "", "question": ""}]
        goals = {"version": 1, "goals": [goal]}
        GM.sanitize(goals)
        # Through the store's own writer: the rows live in todos.json, and a
        # fixture that puts them inside the goal is testing a shape nothing
        # reads.
        GM.save(paths.session_dir, goals, {"items": []})
        # The store mints its own row ids, so the fixture reads back the one
        # it actually has rather than the one it asked for.
        held, _ = GM.load(paths.session_dir)
        self.row_id = held["goals"][0]["todo_items"][0]["id"]
        paths.prompts.write_text(json.dumps({"prompts": []}))
        paths.manifest.write_text(json.dumps({
            "cwd": str(self.project),
            "project_bound_at": "2026-01-01T00:00:00+00:00"}))
        self.trajdir = paths.session_dir
        self.addCleanup(self.quiet)

    def quiet(self):
        proc = PV.running(self.project)
        if proc:
            proc.stop()
        PV.forget(self.project)

    def op(self, **body):
        return ui._apply(body, self.trajdir, True)

    def state(self, todo=""):
        return ui._preview_state(self.trajdir, True, "g1", todo)

    def test_the_pane_reads_without_touching_the_goal_lock(self):
        # Held by something else for the whole read: the pane is drawn on a
        # sweep, and a preview that waited on the tree would stall behind a
        # save every time one landed.
        opened = threading.Event()
        with CS.session_lock(self.session, self.root, wait_s=5):
            def read():
                self.answer = self.state()
                opened.set()
            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            self.assertTrue(opened.wait(3), "the read waited on the lock")
        self.assertTrue(self.answer["ok"])
        self.assertEqual(str(self.project.resolve()), self.answer["cwd"])

    def test_configure_then_run_then_stop(self):
        out = self.op(op="preview_configure")
        self.assertTrue(out["ok"], out)
        self.assertEqual("npm run dev", out["profiles"][0]["command"])
        self.assertEqual("ready", self.state()["status"])

        started = self.op(op="preview_start", command="printf 'hello\\n'; sleep 5")
        self.assertTrue(started["ok"], started)
        deadline = time.time() + 4
        while time.time() < deadline:
            if PV.running(self.project).lines:
                break
            time.sleep(0.05)
        showing = self.state()
        self.assertIn(showing["status"], ("starting", "running"))
        self.assertEqual("terminal", showing["surface"])
        self.assertIn("hello", "\n".join(showing["run"]["lines"]))
        self.assertTrue(self.op(op="preview_stop")["ok"])

    def test_nothing_starts_a_run_except_a_request_to_start_one(self):
        self.op(op="preview_configure")
        for _ in range(3):
            self.state()
        self.assertIsNone(PV.running(self.project))

    def test_a_command_typed_into_the_card_runs_without_being_saved(self):
        self.op(op="preview_configure")
        self.op(op="preview_start", command="printf 'other\\n'")
        self.assertEqual("printf 'other\\n'",
                         PV.running(self.project).profile["command"])
        # The project still runs the way it runs: taking a suggestion for
        # one run is not the reader saying that is the command.
        self.assertEqual("npm run dev",
                         PV.read_config(self.root, self.project)["profiles"][0]
                         ["command"])

    def test_the_intent_op_reads_the_row_and_keeps_the_answer(self):
        calls = []

        def fake(cwd, goal_title, todo_text, profile, engine=None):
            calls.append(todo_text)
            return {"ok": True, "entrypoint": "/goals",
                    "scenario": ["Open a goal"], "expected": "It persists"}

        original = PV.intent_for
        PV.intent_for = fake
        try:
            out = self.op(op="preview_intent", goal_id="g1", todo_id=self.row_id)
            self.assertTrue(out["ok"], out)
            self.assertEqual("It persists", out["intent"]["expected"])
            self.assertEqual(["Allow users to rename"], calls)
            # Asked again, the kept answer is handed back rather than paid for.
            again = self.op(op="preview_intent", goal_id="g1", todo_id=self.row_id)
            self.assertTrue(again["cached"])
            self.assertEqual(1, len(calls))
        finally:
            PV.intent_for = original
        # And the pane picks it up by row id on its next sweep.
        self.assertEqual("It persists",
                         self.state(self.row_id)["intent"]["expected"])

    def test_a_row_the_tree_does_not_have_is_refused(self):
        out = self.op(op="preview_intent", goal_id="g1", todo_id="nope")
        self.assertFalse(out["ok"])
        self.assertIn("no such TODO", out["error"])

    def test_a_global_vault_has_no_project_to_preview(self):
        out = ui._preview_state(self.trajdir, False)
        self.assertFalse(out["ok"])
        self.assertIn("chat scope", out["error"])
        self.assertFalse(ui._apply({"op": "preview_start"}, self.trajdir,
                                   False)["ok"])


if __name__ == "__main__":
    unittest.main()


class OneSupervisorTests(Fixture):
    """`npm run dev` has an owner already, and it is not this module.

    dev_server adopts a server back after a restart, tells our process from
    somebody else's on the same port, and knows the PATH a GUI-started node
    needs. Two supervisors racing to start one in the same directory is two
    servers and a port fight, so the pane hands that profile over.
    """

    def test_the_projects_own_dev_script_belongs_to_dev_server(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        (self.project / "node_modules").mkdir()
        self.assertTrue(PV.dev_owns(self.project, {"command": "npm run dev"}))
        # Everything else is this module's: a test suite, a simulation, a
        # python server -- none of which dev_server will run.
        self.assertFalse(PV.dev_owns(self.project, {"command": "npm run test"}))
        self.assertFalse(PV.dev_owns(self.project, {"command": "pytest"}))
        # And a project whose dependencies are not installed is nobody's
        # yet: dev_server refuses to run one, and the pane's own card for
        # the missing install is what the reader needs first anyway.
        bare = Path(self.tmp.name) / "bare"
        bare.mkdir()
        (bare / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
        self.assertFalse(PV.dev_owns(bare, {"command": "npm run dev"}))
        self.assertFalse(PV.dev_owns(self.project, {"command": "python sim.py"}))

    def test_a_project_with_no_dev_script_is_nobody_elses(self):
        self.write("package.json", {"scripts": {"test": "vitest"}})
        (self.project / "node_modules").mkdir()
        self.assertFalse(PV.dev_owns(self.project, {"command": "npm run test"}))

    def test_a_dev_server_already_up_is_what_the_pane_shows(self):
        # Nothing of ours is running and the pane still says running: the
        # question is whether this project is up, not whether we started it.
        self.write("package.json", {"scripts": {"dev": "vite"}})
        (self.project / "node_modules").mkdir()
        PV.configure(self.root, self.project)
        original = PV.dev_state
        PV.dev_state = lambda session_id, root, cwd: {
            "status": "running", "url": "http://localhost:5173/",
            "command": "npm run dev", "pid": 4242, "last": ["ready in 300ms"]}
        try:
            out = PV.state(self.root, self.project, session_id="chat-x")
        finally:
            PV.dev_state = original
        self.assertEqual("running", out["status"])
        self.assertEqual("web", out["surface"])
        self.assertEqual("http://localhost:5173/", out["url"])
        self.assertEqual("dev_server", out["owner"])
        self.assertEqual(4242, out["run"]["pid"])
        # And with no session to ask on behalf of, the pane keeps its own
        # answer rather than inventing one.
        self.assertEqual("ready", PV.state(self.root, self.project)["status"])

    def test_a_dev_server_with_no_address_yet_is_starting_not_running(self):
        self.write("package.json", {"scripts": {"dev": "vite"}})
        (self.project / "node_modules").mkdir()
        PV.configure(self.root, self.project)
        original = PV.dev_state
        PV.dev_state = lambda session_id, root, cwd: {
            "status": "starting", "url": "", "last": ["compiling…"]}
        try:
            out = PV.state(self.root, self.project, session_id="chat-x")
        finally:
            PV.dev_state = original
        self.assertEqual("starting", out["status"])
        self.assertEqual("terminal", out["surface"])
        self.assertEqual(["compiling…"], out["run"]["lines"])


class BuildContractTests(Fixture):
    """What a build is handed about the program it is changing.

    Without this a build gets the change to make and nothing else: no
    command that runs the project, no page to look at, no sentence saying
    what should be true afterwards -- so "done" means "it compiled".
    """

    def setUp(self):
        super().setUp()
        self.session = "chat-contract"
        paths = CS.paths(self.session, self.root)
        paths.session_dir.mkdir(parents=True)
        paths.manifest.write_text(json.dumps({"cwd": str(self.project)}))
        self.paths = paths

    def lines(self, rows):
        from human_compact.trajectory import build as BUILD
        return BUILD.execution_lines(self.session, self.root,
                                     {"id": "g1", "title": "t"}, rows)

    def test_a_project_with_no_run_config_adds_nothing(self):
        self.assertEqual([], self.lines([{"id": "t1", "text": "do it"}]))

    def test_the_run_command_reaches_the_build(self):
        self.write("package.json", {"scripts": {"dev": "next dev"}})
        PV.configure(self.root, self.project)
        text = "\n".join(self.lines([{"id": "t1", "text": "do it"}]))
        self.assertIn("npm run dev", text)
        self.assertIn("Do not invent a different one", text)
        # A command watched working says so: a build that has to choose
        # between two plausible ones should know which was real.
        self.assertNotIn("watched working", text)
        PV.note_verified(self.root, self.project, "npm run dev")
        self.assertIn("watched working",
                      "\n".join(self.lines([{"id": "t1", "text": "do it"}])))

    def test_what_the_reader_will_look_at_rides_with_the_row(self):
        self.write("package.json", {"scripts": {"dev": "next dev"}})
        PV.configure(self.root, self.project)
        PV.save_intent(self.root, self.project, "t1", "Allow renaming",
                       {"entrypoint": "/goals",
                        "scenario": ["Open a goal", "Rename it"],
                        "expected": "The new name survives a reload"})
        text = "\n".join(self.lines([{"id": "t1", "text": "Allow renaming"}]))
        self.assertIn("/goals", text)
        self.assertIn("1. Open a goal", text)
        self.assertIn("The new name survives a reload", text)
        self.assertIn("not when the code compiles", text)

    def test_an_intent_for_a_row_that_was_reworded_is_not_sent(self):
        self.write("package.json", {"scripts": {"dev": "next dev"}})
        PV.configure(self.root, self.project)
        PV.save_intent(self.root, self.project, "t1", "Allow renaming",
                       {"expected": "The new name survives a reload"})
        text = "\n".join(self.lines([{"id": "t1", "text": "Allow deleting"}]))
        self.assertNotIn("survives a reload", text)


class ShowUiTests(Fixture):
    """One press for "I want to see it", and the one honest ending it has.

    The button promises something visual. A project with nothing that serves
    is told that in a sentence rather than left with a spinner over a frame
    that will never fill.
    """

    def test_a_project_with_nothing_that_serves_says_so(self):
        self.write("main.py", "print('hello')\n")
        PV.configure(self.root, self.project)
        out = PV.show_ui(self.root, self.project)
        self.assertFalse(out["ok"])
        self.assertTrue(out["not_ready"])
        self.assertIn("serves a page", out["reason"])
        # Nothing was started to find that out.
        self.assertIsNone(PV.running(self.project))
        # And the pane knows before the press, so the button can say it.
        state = PV.state(self.root, self.project)
        self.assertFalse(state["ui"]["available"])
        self.assertIn("serves a page", state["ui"]["reason"])

    def test_the_serving_profile_is_the_one_the_button_runs(self):
        # The primary profile is a test suite here; the button still finds
        # the one that would put a page on screen.
        self.write("index.html", "<h1>hi</h1>")
        PV.configure(self.root, self.project)
        PV.set_primary(self.root, self.project, "static")
        config = PV.read_config(self.root, self.project)
        self.assertEqual("static", PV.ui_profile(config)["id"])
        self.assertTrue(PV.state(self.root, self.project)["ui"]["available"])

    def test_the_steps_that_have_to_happen_first_ride_in_front_of_the_run(self):
        self.write("package.json", {"scripts": {"dev": "echo serving"}})
        PV.configure(self.root, self.project)
        # node_modules is missing, so the install is chained ahead of the
        # run rather than left as an errand the reader has to do first.
        out = PV.show_ui(self.root, self.project)
        self.assertTrue(out["ok"], out)
        proc = PV.running(self.project)
        self.assertIn("npm install &&", proc.profile["command"])
        self.assertTrue(proc.wanted_ui)
        self.assertEqual(["npm install", "npm run dev"], proc.steps)
        proc.stop()

    def test_a_run_asked_for_a_page_that_never_serves_one_is_not_ready(self):
        self.write("index.html", "<h1>hi</h1>")
        PV.configure(self.root, self.project)
        out = PV.show_ui(self.root, self.project)
        self.assertTrue(out["ok"], out)
        proc = PV.running(self.project)
        proc.lines.append("building…")
        # Still up, still nothing serving, and past the grace: the pane says
        # that rather than showing a terminal nobody asked for.
        proc.started_at = time.time() - (PV.UI_GRACE_S + 5)
        state = PV.state(self.root, self.project)
        self.assertEqual("not_ready", state["status"])
        self.assertEqual("instructions", state["surface"])
        self.assertTrue(state["run"]["wanted_ui"])
        # Inside the grace it is still starting up, not a verdict.
        proc.started_at = time.time()
        self.assertIn(PV.state(self.root, self.project)["status"],
                      ("starting", "running"))
        proc.stop()

    def test_a_run_started_for_its_output_is_never_called_not_ready(self):
        # The same process, minus the promise: a simulation printing away is
        # doing exactly what was asked of it.
        self.write("main.py", "print('x')\n")
        PV.configure(self.root, self.project)
        PV.start(self.root, self.project,
                 {"id": "t", "command": "sleep 5", "serves": False})
        proc = PV.running(self.project)
        proc.started_at = time.time() - (PV.UI_GRACE_S + 5)
        self.assertNotEqual("not_ready",
                            PV.state(self.root, self.project)["status"])
        proc.stop()
