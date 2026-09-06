"""The goal page: what a chat workspace opens on.

The page draws the chat's own goals. These tests cover the half the server
owns -- the root serves it, its files are served by name and nothing else
is, the workspace it replaced still answers at /legacy, what the page reads
and may write, and the change feed that tells it when someone else wrote --
and, where a browser is available, the interactions themselves on a chat's
real files.
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import autosync as AUTOSYNC  # noqa: E402
from human_compact.trajectory import brainstorm as BRAIN  # noqa: E402
from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import reader as READER  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402
from human_compact.trajectory import web_setup as WS  # noqa: E402

NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
GOAL_DIR = ROOT / "hc" / "src" / "human_compact" / "trajectory" / "web" / "goal"

GOAL_TITLE = "Create an interface to import the dataset"
SUBGOAL_TITLES = ("Create a blank interface with an import button",
                  "Save the dataset locally to my project folder",
                  "Allow me to inspect the dataset in a CSV viewer")
FIRST_TODOS = ("Create a blank interface", "Add an import button")


@contextmanager
def server_for(path, chat_scoped=True):
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
    ui._configure_server(server, Path(path), chat_scoped)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def fetch(url):
    """Status, headers and body of one GET; a 404 is an answer, not an error."""
    try:
        with NO_PROXY_OPENER.open(url, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, dict(error.headers), error.read()


def get_json(url):
    return json.loads(fetch(url)[2])


def post_json(url, body, headers=None):
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=request_headers,
        method="POST")
    with NO_PROXY_OPENER.open(request, timeout=15) as response:
        return json.loads(response.read())


def seed_design(chat):
    """The design's example content, written through the page's own
    operations: the goal, three subgoals, two todos on the first. Returns
    the goal's id and the subgoals' ids."""
    goal = ui._apply({"op": "add_goal", "title": GOAL_TITLE}, chat)["id"]
    subgoals = [ui._apply({"op": "add_goal", "title": title,
                           "parent_goal_id": goal}, chat)["id"]
                for title in SUBGOAL_TITLES]
    for text in FIRST_TODOS:
        ui._apply({"op": "add_todo_row", "goal_id": subgoals[0], "text": text},
                  chat)
    return goal, subgoals


# What the site saves at the end of its onboarding, as /bart claims it: three
# directions offered, one chosen and broken into three described pieces, rows
# under the first piece only. Nothing under the two directions not taken.
WEB_SETUP = {
    "name": "Signed uploads",
    "plan": {"description": "Move uploads off the API server.\nSign, then PUT."},
    "goals": [{"label": "Direct-to-storage uploads", "why": "the API is the bottleneck"},
              {"label": "Resumable uploads", "why": "large files fail midway"},
              {"label": "Upload quotas", "why": "storage is unmetered"}],
    "chosen": "Direct-to-storage uploads",
    "todos": [],
    "subgoals": [
        {"label": "Signing route", "description": "Mint short-lived URLs",
         "why": "nothing else can start without it",
         "todos": ["Add POST /uploads/sign", "Scope the token"]},
        {"label": "Client PUTs", "description": "Browser writes to storage",
         "why": "it is the traffic being moved", "todos": []},
        {"label": "Retire the proxy", "description": "Delete the old path",
         "why": "two paths is one too many", "todos": []},
    ],
    "reader": {"name": "Maya", "level": "expert"},
}
PIECE_NOTES = {
    "Signing route": "Mint short-lived URLs\n\nWhy this matters: nothing else can start without it",
    "Client PUTs": "Browser writes to storage\n\nWhy this matters: it is the traffic being moved",
    "Retire the proxy": "Delete the old path\n\nWhy this matters: two paths is one too many",
}


def claim_web_setup(case, payload=WEB_SETUP):
    """What /bart does for a chat the hooks have seen and nobody has asked
    about, on a machine connected to an account with a finished setup
    waiting: the project is made and the chat bound to it. Returns the
    chat's directory and the directory of the workspace holding the
    project's tree -- the two a server can be started on."""
    sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    CS.ingest_hook({"session_id": sid, "hook_event_name": "SessionStart",
                    "cwd": str(case.root / "repo")}, root=case.root)
    (case.home / "auth.json").write_text(json.dumps({
        "schema": 1, "token": "egb_x", "apiBase": "https://example.test"}),
        encoding="utf-8")
    with mock.patch.object(READER, "remember", return_value={"ok": True}):
        said = WS.claim_for_chat(sid, case.root, {"HUMAN_COMPACT_HOME": str(case.home)},
                                 fetch=lambda account: json.loads(json.dumps(payload)))
    case.assertEqual('created "Signed uploads" from your web setup; this chat is in it', said)
    tree = CS.tree_session(sid, case.root)
    case.assertNotEqual(sid, tree)
    return CS.paths(sid, case.root).session_dir, CS.paths(tree, case.root).session_dir


def stored_rows(chat, goal_id):
    """(text, status) of every todo row the chat's todos.json holds for a goal."""
    store = json.loads((Path(chat) / "todos.json").read_text(encoding="utf-8"))
    return [(row["text"], row["status"]) for row in store["todos"].get(goal_id, [])]


def read_event(response, keepalives=False):
    """The next server-sent event on an open stream, as its non-blank lines.
    The keepalive comments between events are skipped unless asked for."""
    lines = []
    while True:
        line = response.readline().decode("utf-8")
        if not line:
            return ["<closed>"] + lines
        if line == "\n":
            if lines and (keepalives or not all(l.startswith(":") for l in lines)):
                return lines
            lines = []
            continue
        lines.append(line.rstrip("\n"))


def revision_of(event):
    data = [line for line in event if line.startswith("data: ")]
    return json.loads(data[0][len("data: "):])["revision"] if data else None


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.05)


# A stand-in for the Engelbart CLI. `logout` removes auth.json and says what
# the real one says; `auth` prints the code and page lines the real one
# prints, then waits for an `approve` or `deny` file where a person would
# approve the code in a browser.
FAKE_CLI = """
import json, os, sys, time
from pathlib import Path

home = Path(os.environ["HUMAN_COMPACT_HOME"])
signals = Path(os.environ["FAKE_ENGELBART_DIR"])
with (signals / "calls.log").open("a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
command = sys.argv[1] if len(sys.argv) > 1 else ""

if command == "logout":
    auth = home / "auth.json"
    if not auth.exists():
        print("This machine is not connected to an Engelbart account.")
        sys.exit(0)
    auth.unlink()
    print("Disconnected. That token is revoked.")
    sys.exit(0)

if command == "auth":
    print("\\nConnect this machine to your Engelbart account.\\n")
    print("  code   WXYZ-2468")
    print("  page   http://127.0.0.1:9/engelbart?code=WXYZ-2468\\n")
    print("Opening that page. Approve the code above to finish.")
    sys.stdout.flush()
    deadline = time.time() + 20
    while time.time() < deadline:
        if (signals / "approve").exists():
            (home / "auth.json").write_text(json.dumps({
                "apiBase": "http://127.0.0.1:9", "token": "fresh-token",
                "email": "someone@example.com"}), encoding="utf-8")
            print("\\nConnected as someone@example.com.")
            sys.exit(0)
        if (signals / "deny").exists():
            print("\\nThat code was rejected in the browser. Nothing was connected.")
            sys.exit(1)
        time.sleep(0.05)
    print("\\nThat code expired before it was approved.")
    sys.exit(1)

print("unknown command", file=sys.stderr)
sys.exit(2)
"""


def fake_cli(root):
    """Write the stand-in CLI under root; the path to run and the directory
    its `auth` watches for the approval."""
    signals = Path(root) / "engelbart-signals"
    signals.mkdir()
    script = Path(root) / "fake_engelbart.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    if sys.platform == "win32":
        wrapper = Path(root) / "engelbart.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        wrapper = Path(root) / "engelbart"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return str(wrapper), signals


def browser_executable():
    configured = os.environ.get("HC_TEST_BROWSER")
    if configured and Path(configured).is_file():
        return configured
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    mac_chrome = Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    return chrome or (str(mac_chrome) if mac_chrome.is_file() else None)


# A stand-in for the brainstorm's model call behind Bart. Answers with the
# cards given, one per round (the last one again after that), and keeps
# what each round was asked: the transcript, the project as digested, and
# where in it the conversation was said to be.
def fake_bart(*cards):
    asked = []
    left = list(cards)

    def ask(transcript, context="", engine=None, root=None, extra=()):
        asked.append({"transcript": list(transcript or []), "context": context,
                      "extra": list(extra or [])})
        card = left.pop(0) if len(left) > 1 else left[0]
        return dict(card)

    ask.asked = asked
    return ask


TODOS_CARD = {"ok": True, "say": "Two rows, then.", "card": "todos",
              "todos": ["Save the file as parquet", "Name it after the dataset"],
              "subgoals": [{"label": "Reading it back",
                            "todos": ["Open the file the way pandas does"]}]}
PROSE_CARD = {"ok": True, "card": "none", "say":
              "Imagine this subgoal is done — what is the first thing you would see or click?"}


class ChatCase(unittest.TestCase):
    """A disposable chat, with the machine's own account and autosync kept
    out: what the tests write stays in the temporary directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.chat = self.root / "chat"
        self.chat.mkdir()
        self.home = self.root / "human-compact"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HUMAN_COMPACT_HOME": str(self.home),
                                           "HC_AUTOSYNC_SECONDS": "0"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def goals(self):
        return CS.load_goals("chat", self.root)


class GoalPageRouteTests(ChatCase):
    def test_the_root_is_the_goal_page(self):
        with server_for(self.chat) as url:
            for path in ("/", "/index.html", "/?quick=1", "/?goal=g1"):
                status, headers, body = fetch(url + path)
                text = body.decode("utf-8")
                self.assertEqual(200, status, path)
                self.assertTrue(headers["Content-Type"].startswith("text/html"), path)
                self.assertEqual("no-store, must-revalidate", headers["Cache-Control"])
                self.assertIn("<title>Engelbart</title>", text)
                self.assertIn('<script type="module" src="/goal/app.js"></script>', text)
                self.assertIn('<link rel="stylesheet" href="/goal/styles.css">', text)
                # Nothing of the workspace it replaced comes along.
                self.assertNotIn("bridge.js", text)
                self.assertNotIn("hc-preboot", text)
                self.assertNotIn("__bundler", text)

    def test_every_file_of_the_page_is_served_by_name(self):
        expected = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
        }
        files = sorted(p for p in GOAL_DIR.rglob("*")
                       if p.is_file() and p.suffix in expected)
        self.assertGreater(len(files), 10)
        with server_for(self.chat) as url:
            for file in files:
                name = file.relative_to(GOAL_DIR).as_posix()
                status, headers, body = fetch(f"{url}/goal/{name}")
                self.assertEqual(200, status, name)
                self.assertEqual(expected[file.suffix], headers["Content-Type"], name)
                self.assertEqual(file.read_bytes(), body, name)
                self.assertEqual("no-store, must-revalidate", headers["Cache-Control"])

    def test_nothing_outside_the_page_is_served(self):
        refused = [
            "/goal/",
            "/goal/missing.js",
            "/goal/components/",
            "/goal/components/missing.js",
            "/goal/../ui.py",
            "/goal/../setup.html",
            "/goal/components/../../setup.js",
            "/goal/%2e%2e/setup.html",
            "/goal/.hidden.js",
            "/goal/app.js.bak",
            "/goal/README.md",
            "/goal//app.js/../../setup.js",
        ]
        with server_for(self.chat) as url:
            for path in refused:
                status, _headers, body = fetch(url + path)
                self.assertEqual(404, status, path)
                self.assertIn(b"not found", body, path)

    def test_the_helper_refuses_every_shape_of_escape(self):
        for relpath in ("", "/", "..", "../ui.py", "goal/../../ui.py",
                        "/etc/passwd", "app.py", "styles.css/", ".env.js",
                        "app.js\x00.html"):
            self.assertIsNone(ui.goal_page_asset(relpath), relpath)
        served = ui.goal_page_asset("components/todos.js")
        self.assertIsNotNone(served)
        self.assertEqual("application/javascript; charset=utf-8", served[1])
        self.assertEqual((GOAL_DIR / "components" / "todos.js").read_bytes(), served[0])

    def test_the_legacy_workspace_still_answers_whole(self):
        with server_for(self.chat) as url:
            for path in ("/legacy", "/legacy/"):
                status, headers, body = fetch(url + path)
                text = body.decode("utf-8")
                self.assertEqual(200, status, path)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertIn('id="hc-preboot"', text)
                self.assertLess(text.index('script type="__bundler/template"'),
                                text.index('<script src="/bridge.js"></script>'))
                self.assertLess(text.index('<script src="/bridge.js"></script>'),
                                text.index("</body>"))
            status, _headers, body = fetch(url + "/bridge.js")
            self.assertEqual(200, status)
            self.assertIn(b"window.__hcServerStale", body)


class GoalDataRouteTests(ChatCase):
    """The goal page's half of the server: what it reads, what it may write,
    and the change feed that tells it when someone else wrote."""

    def test_an_empty_chat_answers_empty(self):
        with server_for(self.chat) as url:
            answer = get_json(url + "/api/goal-page")
        self.assertTrue(answer["ok"])
        self.assertIsNone(answer["goal"])
        self.assertTrue(answer["empty"])
        self.assertEqual([], answer["subgoals"])
        self.assertEqual({}, answer["slices"])
        self.assertEqual([], answer["goals"])
        self.assertIsNone(answer["project"])
        self.assertTrue(answer["revision"])

    def test_the_page_reads_the_goal_its_subgoals_and_their_rows(self):
        goal, subgoals = seed_design(self.chat)
        ui._apply({"op": "set_notes", "goal_id": subgoals[1],
                   "notes": "keep it local"}, self.chat)
        with server_for(self.chat) as url:
            answer = get_json(url + "/api/goal-page")
        self.assertEqual({"id": goal, "title": GOAL_TITLE, "status": "active"},
                         answer["goal"])
        self.assertFalse(answer["empty"])
        self.assertEqual(list(SUBGOAL_TITLES),
                         [s["title"] for s in answer["subgoals"]])
        self.assertEqual(subgoals, [s["id"] for s in answer["subgoals"]])
        self.assertEqual({"active"}, {s["status"] for s in answer["subgoals"]})
        first = answer["slices"][subgoals[0]]
        self.assertEqual("", first["notes"])
        self.assertEqual(list(FIRST_TODOS), [row["text"] for row in first["todos"]])
        for row in first["todos"]:
            self.assertTrue(row["id"])
            self.assertFalse(row["done"])
            self.assertEqual("", row["status"])
        self.assertEqual({"notes": "keep it local", "todos": []},
                         answer["slices"][subgoals[1]])
        self.assertEqual([goal], [g["id"] for g in answer["goals"]])
        # The rows the page reads are the rows the build reads.
        self.assertEqual([(text, "") for text in FIRST_TODOS],
                         stored_rows(self.chat, subgoals[0]))

    def test_without_a_named_goal_the_page_opens_on_the_one_touched_last(self):
        older = ui._apply({"op": "add_goal", "title": "Older"}, self.chat)["id"]
        newer = ui._apply({"op": "add_goal", "title": "Newer"}, self.chat)["id"]
        finished = ui._apply({"op": "add_goal", "title": "Finished"}, self.chat)["id"]
        away = ui._apply({"op": "add_goal", "title": "Put away"}, self.chat)["id"]
        goals, important = self.goals()
        by_id = {g["id"]: g for g in goals["goals"]}
        by_id[older]["updated_at"] = "2026-09-01T10:00:00+00:00"
        by_id[newer]["updated_at"] = "2026-09-05T10:00:00+00:00"
        by_id[finished]["updated_at"] = "2026-09-06T10:00:00+00:00"
        by_id[finished]["status"] = "completed"
        by_id[away]["updated_at"] = "2026-09-07T10:00:00+00:00"
        by_id[away]["status"] = "archived"
        CS.save_goals("chat", goals, important, self.root)
        with server_for(self.chat) as url:
            chosen = get_json(url + "/api/goal-page")
            named = get_json(url + f"/api/goal-page?goal={older}")
            unknown = get_json(url + "/api/goal-page?goal=g99")
            archived = get_json(url + f"/api/goal-page?goal={away}")
            # The newest OPEN goal: a finished one and an archived one were
            # both touched later. Naming one in the address wins; naming
            # one that is not here, or is put away, does not.
            self.assertEqual(newer, chosen["goal"]["id"])
            self.assertEqual(older, named["goal"]["id"])
            self.assertEqual(newer, unknown["goal"]["id"])
            self.assertEqual(newer, archived["goal"]["id"])
            # What the address could name: everything not archived.
            self.assertEqual([older, newer, finished],
                             [g["id"] for g in chosen["goals"]])
            # With nothing open, the page still opens on something.
            goals, important = self.goals()
            for g in goals["goals"]:
                if g["id"] in (older, newer):
                    g["status"] = "completed"
            CS.save_goals("chat", goals, important, self.root)
            self.assertEqual(finished, get_json(url + "/api/goal-page")["goal"]["id"])

    def test_a_goal_with_something_under_it_comes_before_an_empty_one(self):
        # Two open goals, the newer one bare, the older one with a subgoal:
        # the page opens on the one with work under it. An empty goal that
        # is the newest thing is what a direction the reader passed over
        # looks like; and with nothing under any of them, newest wins.
        worked = ui._apply({"op": "add_goal", "title": "Worked"}, self.chat)["id"]
        ui._apply({"op": "add_goal", "title": "Its piece", "parent_goal_id": worked}, self.chat)
        bare = ui._apply({"op": "add_goal", "title": "Bare"}, self.chat)["id"]
        started = ui._apply({"op": "add_goal", "title": "Started"}, self.chat)["id"]
        goals, important = self.goals()
        by_id = {g["id"]: g for g in goals["goals"]}
        by_id[worked]["updated_at"] = "2026-09-01T10:00:00+00:00"
        by_id[bare]["updated_at"] = "2026-09-05T10:00:00+00:00"
        by_id[started]["updated_at"] = "2026-09-03T10:00:00+00:00"
        CS.save_goals("chat", goals, important, self.root)
        with server_for(self.chat) as url:
            self.assertEqual(worked, get_json(url + "/api/goal-page")["goal"]["id"])
            # A goal the reader marked in progress counts the same as one
            # with work under it, and between the two the newer wins.
            goals, important = self.goals()
            for g in goals["goals"]:
                if g["id"] == started:
                    g["status"] = "in_progress"
            CS.save_goals("chat", goals, important, self.root)
            self.assertEqual(started, get_json(url + "/api/goal-page")["goal"]["id"])
            # The address still names any of them.
            self.assertEqual(bare, get_json(url + f"/api/goal-page?goal={bare}")["goal"]["id"])

    def test_a_project_set_up_on_the_web_opens_on_the_direction_chosen(self):
        chat, tree = claim_web_setup(self)
        # The chat's own workspace and the project's read the same tree,
        # and both answer the same page: the direction the reader chose,
        # not the last of the three offered in the same second; its pieces
        # as subgoals, each with notes seeded from what the setup said about
        # it, and rows under the first; and the project with its plan, for
        # the header.
        for where in (chat, tree):
            with server_for(where) as url:
                answer = get_json(url + "/api/goal-page")
            self.assertEqual("Direct-to-storage uploads", answer["goal"]["title"], where)
            self.assertEqual("in_progress", answer["goal"]["status"])
            self.assertEqual(["Signing route", "Client PUTs", "Retire the proxy"],
                             [s["title"] for s in answer["subgoals"]])
            slices = {s["title"]: answer["slices"][s["id"]] for s in answer["subgoals"]}
            self.assertEqual(PIECE_NOTES, {title: s["notes"] for title, s in slices.items()})
            self.assertEqual([["Add POST /uploads/sign", "Scope the token"], [], []],
                             [[t["text"] for t in slices[title]["todos"]]
                              for title in ("Signing route", "Client PUTs", "Retire the proxy")])
            self.assertEqual({"name": "Signed uploads",
                              "objective": "Move uploads off the API server.",
                              "plan": "Move uploads off the API server.\nSign, then PUT."},
                             answer["project"])
            # The directions not taken are kept, out of the way: the
            # address could still name one.
            self.assertEqual(["Direct-to-storage uploads", "Resumable uploads", "Upload quotas"],
                             [g["title"] for g in answer["goals"]])
        # The notes are the reader's from here: a save through the page's
        # door replaces the seed, and the page reads the saved text back.
        with server_for(chat) as url:
            answer = get_json(url + "/api/goal-page")
            first = answer["subgoals"][0]["id"]
            written = post_json(url + "/api/goal-page/op",
                                {"op": "set_notes", "goal_id": first, "notes": "Sign with a KMS key."})
            self.assertTrue(written["ok"])
            self.assertEqual("Sign with a KMS key.",
                             get_json(url + "/api/goal-page")["slices"][first]["notes"])

    def test_the_page_writes_through_its_own_door(self):
        goal, subgoals = seed_design(self.chat)
        with server_for(self.chat) as url:
            def op(body):
                return post_json(url + "/api/goal-page/op", body, {"Origin": url})

            before = get_json(url + "/api/goal-page")["revision"]
            added = op({"op": "add_todo_row", "goal_id": subgoals[1],
                        "text": "Write the file as parquet"})
            self.assertTrue(added["ok"])
            row = added["row"]
            self.assertEqual(("Write the file as parquet", False, ""),
                             (row["text"], row["done"], row["status"]))
            # Every answer carries the goals' revision after the write.
            self.assertNotEqual(before, added["revision"])
            self.assertEqual(added["revision"],
                             get_json(url + "/api/goal-page")["revision"])
            # The same line again is the row it already has, not a second one.
            again = op({"op": "add_todo_row", "goal_id": subgoals[1],
                        "text": "write the file as PARQUET"})
            self.assertEqual(row["id"], again["row"]["id"])
            self.assertTrue(again["existing"])
            self.assertEqual(added["revision"], again["revision"])
            self.assertEqual({"ok": False, "error": "write the todo first"},
                             {k: v for k, v in op({"op": "add_todo_row", "goal_id": subgoals[1],
                                                   "text": "  "}).items() if k != "revision"})
            edited = op({"op": "set_todo_text", "goal_id": subgoals[1],
                         "id": row["id"], "text": "Write it as parquet"})
            self.assertEqual("Write it as parquet", edited["row"]["text"])
            done = op({"op": "set_todo_done", "goal_id": subgoals[1],
                       "id": row["id"], "done": True})
            self.assertEqual((True, "done"), (done["row"]["done"], done["row"]["status"]))
            undone = op({"op": "set_todo_done", "goal_id": subgoals[1],
                         "id": row["id"], "done": False})
            self.assertEqual((False, ""), (undone["row"]["done"], undone["row"]["status"]))
            self.assertTrue(op({"op": "set_notes", "goal_id": subgoals[1],
                                "notes": "parquet, not csv"})["ok"])
            made = op({"op": "add_goal", "title": "Export the dataset to parquet",
                       "parent_goal_id": goal})
            self.assertTrue(made["ok"])
            state = get_json(url + "/api/goal-page")
            self.assertEqual(made["id"], state["subgoals"][-1]["id"])
            self.assertEqual("Export the dataset to parquet", state["subgoals"][-1]["title"])
            self.assertEqual("parquet, not csv", state["slices"][subgoals[1]]["notes"])
            self.assertEqual(["Write it as parquet"],
                             [r["text"] for r in state["slices"][subgoals[1]]["todos"]])
            removed = op({"op": "remove_todo_row", "goal_id": subgoals[1], "id": row["id"]})
            self.assertEqual((True, row["id"]), (removed["ok"], removed["id"]))
            gone = op({"op": "remove_todo_row", "goal_id": subgoals[1], "id": row["id"]})
            self.assertEqual((False, "that todo is no longer on the goal"),
                             (gone["ok"], gone["error"]))
            self.assertEqual([], get_json(url + "/api/goal-page")["slices"][subgoals[1]]["todos"])
        # What the page wrote is what the chat's files hold.
        goals, _important = self.goals()
        self.assertIn(("Export the dataset to parquet", goal),
                      [(g["title"], g.get("parent_goal_id")) for g in goals["goals"]])
        self.assertEqual("parquet, not csv", GM.by_id(goals, subgoals[1])["notes"])
        self.assertEqual([], stored_rows(self.chat, subgoals[1]))

    def test_the_door_is_as_wide_as_the_page(self):
        goal, subgoals = seed_design(self.chat)
        with server_for(self.chat) as url:
            for body in ({"op": "rename_goal", "goal_id": goal, "title": "x"},
                         {"op": "set_status", "goal_id": goal, "status": "archived"},
                         {"op": "import_goals", "goals": []},
                         {"op": "purge_goal", "goal_id": goal},
                         {"op": ""}, {"op": 7}):
                answer = post_json(url + "/api/goal-page/op", body, {"Origin": url})
                self.assertFalse(answer["ok"], body)
                self.assertIn("not an operation of the goal page", answer["error"], body)
            self.assertEqual(GOAL_TITLE, get_json(url + "/api/goal-page")["goal"]["title"])
            # Another site's form is stopped at the media type, as everywhere.
            request = urllib.request.Request(
                url + "/api/goal-page/op", data=b"op=add_goal", method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                NO_PROXY_OPENER.open(request, timeout=5)
            with caught.exception:
                self.assertEqual(415, caught.exception.code)
        # The row operations are goal-scoped like the rest, and every
        # operation the page may send is one the autosync counts as an edit.
        for kind in ("add_todo_row", "set_todo_text", "set_todo_done", "remove_todo_row"):
            self.assertIn(kind, ui.GOAL_OPS)
        self.assertLessEqual(ui.GOAL_PAGE_OPS, AUTOSYNC.WRITE_OPS)

    def test_a_row_the_builder_holds_is_read_not_edited(self):
        goal, subgoals = seed_design(self.chat)
        goals, important = self.goals()
        first = GM.by_id(goals, subgoals[0])
        first["todo_items"][0]["status"] = "building"
        first["todo_items"][1]["status"] = "asking"
        first["todo_items"][1]["question"] = "Which button?"
        CS.save_goals("chat", goals, important, self.root)
        held, asked = first["todo_items"][0]["id"], first["todo_items"][1]["id"]
        with server_for(self.chat) as url:
            state = get_json(url + "/api/goal-page")
            self.assertEqual(["building", "asking"],
                             [r["status"] for r in state["slices"][subgoals[0]]["todos"]])
            for body in ({"op": "set_todo_text", "goal_id": subgoals[0], "id": held, "text": "x"},
                         {"op": "set_todo_done", "goal_id": subgoals[0], "id": held, "done": True},
                         {"op": "remove_todo_row", "goal_id": subgoals[0], "id": asked}):
                answer = post_json(url + "/api/goal-page/op", body, {"Origin": url})
                self.assertEqual((False, "that row is with the builder"),
                                 (answer["ok"], answer["error"]), body)
            self.assertEqual(state["slices"], get_json(url + "/api/goal-page")["slices"])

    def test_build_all_hands_the_open_rows_to_the_builder(self):
        goal, subgoals = seed_design(self.chat)
        handed = []

        def start(session_id, root, goal_id, row_ids, quick=False):
            handed.append((session_id, Path(root).resolve(), goal_id, list(row_ids), quick))
            return {"ok": True, "started": True, "rows": list(row_ids)}

        with mock.patch("human_compact.trajectory.build.start", start), \
                server_for(self.chat) as url:
            ids = [r["id"] for r in get_json(url + "/api/goal-page")["slices"][subgoals[0]]["todos"]]
            answer = post_json(url + "/api/goal-page/op",
                               {"op": "build_todos", "goal_id": subgoals[0], "ids": ids},
                               {"Origin": url})
        self.assertTrue(answer["ok"])
        self.assertEqual(ids, answer["rows"])
        self.assertTrue(answer["revision"])
        self.assertEqual([("chat", self.root.resolve(), subgoals[0], ids, False)], handed)

    def test_the_change_feed_carries_every_writer_s_revision(self):
        goal, subgoals = seed_design(self.chat)
        with mock.patch.object(ui, "SSE_PING_SECONDS", 0.3), server_for(self.chat) as url:
            connection = http.client.HTTPConnection(
                "127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=10)
            try:
                connection.request("GET", "/api/goal-page/events")
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                self.assertEqual("text/event-stream", response.getheader("Content-Type"))
                self.assertEqual("no-store, must-revalidate", response.getheader("Cache-Control"))
                self.assertEqual(["retry: 2000"], read_event(response))
                # The stream opens on the revision the page has just read.
                current = get_json(url + "/api/goal-page")["revision"]
                self.assertEqual(["event: change", 'data: {"revision": "%s"}' % current],
                                 read_event(response))
                # A write through the page's door is announced at once, with
                # the revision the answer carried.
                answer = post_json(url + "/api/goal-page/op",
                                   {"op": "set_notes", "goal_id": subgoals[0], "notes": "hello"},
                                   {"Origin": url})
                self.assertEqual(answer["revision"], revision_of(read_event(response)))
                # A write by anyone else -- here the chat's own store, as the
                # hooks write it -- reaches the page within a tick of the
                # file changing.
                goals, important = self.goals()
                goals["goals"].append(GM.new_goal(
                    GM.next_goal_id(goals), "Export the dataset to parquet", goal,
                    origin="inference"))
                CS.save_goals("chat", goals, important, self.root)
                began = time.monotonic()
                event = read_event(response)
                self.assertLess(time.monotonic() - began, 3)
                state = get_json(url + "/api/goal-page")
                self.assertEqual(state["revision"], revision_of(event))
                self.assertEqual("Export the dataset to parquet", state["subgoals"][-1]["title"])
                # A write that changes nothing the page draws is not announced:
                # the next thing on the stream is the keepalive, not an event.
                CS.save_goals("chat", goals, important, self.root)
                self.assertEqual([": ping"], read_event(response, keepalives=True))
            finally:
                response.close()
                connection.close()


class BartRouteTests(ChatCase):
    """Bart on the page is the brainstorm, asked about one subgoal. The
    model is stood in for; what the route sends it, and what it draws
    from the card that comes back, are the tests."""

    def ask_bart(self, url, subgoal, transcript):
        return post_json(url + "/api/goal-page/bart",
                         {"goal_id": "", "subgoal_id": subgoal,
                          "transcript": transcript}, {"Origin": url})

    def test_a_message_reaches_the_model_with_the_tree_and_the_piece(self):
        goal, subgoals = seed_design(self.chat)
        ask = fake_bart(TODOS_CARD)
        with mock.patch.object(BRAIN, "ask", ask), server_for(self.chat) as url:
            answer = self.ask_bart(url, subgoals[1], [
                {"role": "bart", "text": "Proposed TODO row: Pick a format (added to the list)"},
                {"role": "you", "text": "I want to save the file as parquet"}])
        self.assertTrue(answer["ok"], answer)
        # Prose first, then every row the model put forward, flat or under
        # a piece, each as a proposal of its own.
        self.assertEqual([("text", "Two rows, then."),
                          ("proposal", "Save the file as parquet"),
                          ("proposal", "Name it after the dataset"),
                          ("proposal", "Open the file the way pandas does")],
                         [(r["kind"], r["text"]) for r in answer["replies"]])
        self.assertEqual("todos", answer["card"])
        # The model got the conversation whole, the project as the
        # brainstorm digests it, and where in the project the talk is.
        [asked] = ask.asked
        self.assertEqual([("bart", "Proposed TODO row: Pick a format (added to the list)"),
                          ("you", "I want to save the file as parquet")],
                         [(t["role"], t["text"]) for t in asked["transcript"]])
        self.assertIn(GOAL_TITLE, asked["context"])
        self.assertIn(SUBGOAL_TITLES[1], asked["context"])
        where = "\n".join(asked["extra"])
        self.assertIn('"%s"' % SUBGOAL_TITLES[1], where)
        self.assertIn('"%s"' % GOAL_TITLE, where)
        self.assertIn("`todos`", where)
        self.assertIn("Nothing is written until they add a row", where)

    def test_a_question_and_a_choice_are_said_so_the_reader_can_type_back(self):
        goal, subgoals = seed_design(self.chat)
        ask = fake_bart(
            {"ok": True, "say": "One thing first.", "card": "questions",
             "questions": {"eyebrow": "one question", "items": [
                 {"id": "fmt", "type": "mcq", "title": "Which format?",
                  "subtitle": "pick one", "options": [
                      {"label": "parquet", "why": "columnar"},
                      {"label": "csv", "why": ""}]}]}},
            {"ok": True, "say": "", "card": "focus",
             "focus": {"title": "Which reading of this?", "options": [
                 {"label": "a viewer", "why": "look first"},
                 {"label": "an export", "why": "share first"}]}},
            {"ok": True, "say": "I would write the rows now, shall I?",
             "card": "offer", "offer": "todos"},
            {"ok": True, "say": "", "card": "goals", "goals": [
                {"label": "Keep the data in the browser", "why": "no server",
                 "subgoals": []}]})
        with mock.patch.object(BRAIN, "ask", ask), server_for(self.chat) as url:
            turns = [self.ask_bart(url, subgoals[1], [{"role": "you", "text": t}])
                     for t in ("save it", "ok", "yes", "and goals?")]
        first, second, third, fourth = [[(r["kind"], r["text"]) for r in t["replies"]]
                                        for t in turns]
        self.assertEqual([("text", "One thing first."),
                          ("text", "Which format? (pick one)\n- parquet -- columnar\n- csv")],
                         first)
        self.assertEqual([("text", "Which reading of this?\n- a viewer -- look first"
                                   "\n- an export -- share first")], second)
        self.assertEqual([("text", "I would write the rows now, shall I?")], third)
        # A goal has no place on this page: said, not proposed.
        self.assertEqual([("text", "Keep the data in the browser (no server)")], fourth)

    def test_a_model_that_could_not_be_reached_is_reported_not_drawn(self):
        goal, subgoals = seed_design(self.chat)
        ask = fake_bart({"ok": False, "error": "claude CLI not found on PATH"})
        with mock.patch.object(BRAIN, "ask", ask), server_for(self.chat) as url:
            answer = self.ask_bart(url, subgoals[0], [{"role": "you", "text": "hi"}])
        self.assertFalse(answer["ok"])
        self.assertIn("claude CLI not found", answer["error"])
        self.assertNotIn("replies", answer)

    def test_the_conversation_must_be_a_list_on_a_subgoal_that_exists(self):
        goal, subgoals = seed_design(self.chat)
        ask = fake_bart(TODOS_CARD)
        with mock.patch.object(BRAIN, "ask", ask), server_for(self.chat) as url:
            said = self.ask_bart(url, subgoals[0], "words")
            self.assertFalse(said["ok"])
            self.assertIn("transcript", said["error"])
            gone = self.ask_bart(url, "g99", [{"role": "you", "text": "hi"}])
            self.assertFalse(gone["ok"])
            self.assertIn("no such subgoal", gone["error"])
            # The goal itself is not a piece of the work.
            top = self.ask_bart(url, goal, [{"role": "you", "text": "hi"}])
            self.assertFalse(top["ok"])
            self.assertIn("no such subgoal", top["error"])
            # Another site's form is stopped at the media type, as everywhere.
            request = urllib.request.Request(
                url + "/api/goal-page/bart", data=b"transcript=hi", method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                NO_PROXY_OPENER.open(request, timeout=5)
            with caught.exception:
                self.assertEqual(415, caught.exception.code)
        self.assertEqual([], ask.asked)

    def test_the_model_is_asked_outside_the_state_lock(self):
        # A reply takes as long as the model takes, and meanwhile the page
        # still reads its goal and saves its rows. A stand-in model that
        # reads the page through the server from inside the call would
        # wait on a lock the route was holding, and time out.
        goal, subgoals = seed_design(self.chat)
        seen = {}

        def ask(transcript, context="", engine=None, root=None, extra=()):
            with NO_PROXY_OPENER.open(seen["url"] + "/api/goal-page", timeout=5) as response:
                seen["page"] = json.load(response)["goal"]["title"]
            return dict(TODOS_CARD)

        with mock.patch.object(BRAIN, "ask", ask), server_for(self.chat) as url:
            seen["url"] = url
            answer = self.ask_bart(url, subgoals[0], [{"role": "you", "text": "hi"}])
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(GOAL_TITLE, seen["page"])


class AccountRouteTests(ChatCase):
    """What the page's loadAccount relies on: the machine's own account, as
    the installer wrote it, read by the server and never by the page."""

    def status(self):
        with server_for(self.chat) as url:
            return get_json(url + "/api/supabase")

    def test_a_machine_nobody_connected_says_so(self):
        answer = self.status()
        self.assertTrue(answer["ok"])
        self.assertFalse(answer["connected"])
        self.assertEqual("", answer["email"])

    def test_a_connected_machine_answers_with_its_account(self):
        (self.home / "auth.json").write_text(json.dumps({
            "apiBase": "http://127.0.0.1:9", "token": "machine-token",
            "email": "someone@example.com"}), encoding="utf-8")
        answer = self.status()
        self.assertTrue(answer["connected"])
        self.assertEqual("someone@example.com", answer["email"])


class AccountCommandTests(ChatCase):
    """Sign-out and sign-in from the page run the Engelbart CLI: the
    stand-in here answers like `engelbart logout` and `engelbart auth`,
    down to the lines the real one prints."""

    def setUp(self):
        super().setUp()
        cli, self.signals = fake_cli(self.root)
        self.env = {"HUMAN_COMPACT_HOME": str(self.home), "ENGELBART_CLI": cli,
                    "FAKE_ENGELBART_DIR": str(self.signals)}

    def tearDown(self):
        ui.ACCOUNT_SIGN_IN.cancel()
        super().tearDown()

    def connected(self):
        (self.home / "auth.json").write_text(json.dumps({
            "apiBase": "http://127.0.0.1:9", "token": "machine-token",
            "email": "someone@example.com"}), encoding="utf-8")

    def calls(self):
        log = self.signals / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    @contextmanager
    def page_server(self, env=None):
        with mock.patch.dict(os.environ, env or self.env):
            with server_for(self.chat) as url:
                yield url

    def sign_in_status(self, url, until, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            answer = get_json(url + "/api/account/sign-in")
            if answer["status"] == until or time.monotonic() > deadline:
                return answer
            time.sleep(0.05)

    def test_signing_out_runs_engelbart_logout(self):
        self.connected()
        with self.page_server() as url:
            answer = post_json(url + "/api/account/sign-out", {})
            self.assertEqual(
                {"ok": True, "message": "Disconnected. That token is revoked."}, answer)
            self.assertFalse((self.home / "auth.json").exists())
            self.assertFalse(get_json(url + "/api/supabase")["connected"])
        self.assertEqual(["logout"], self.calls())

    def test_signing_in_shows_the_code_and_waits_for_the_approval(self):
        with self.page_server() as url:
            answer = post_json(url + "/api/account/sign-in", {})
            self.assertEqual("waiting", answer["status"])
            self.assertEqual("WXYZ-2468", answer["code"])
            self.assertEqual("http://127.0.0.1:9/engelbart?code=WXYZ-2468", answer["url"])
            # Asking again while it waits joins the command already running.
            self.assertEqual(answer, post_json(url + "/api/account/sign-in", {}))
            self.assertEqual("waiting", get_json(url + "/api/account/sign-in")["status"])
            (self.signals / "approve").touch()
            self.assertEqual("ready", self.sign_in_status(url, "ready")["status"])
            status = get_json(url + "/api/supabase")
            self.assertTrue(status["connected"])
            self.assertEqual("someone@example.com", status["email"])
        self.assertEqual(["auth --no-open"], self.calls())

    def test_a_code_rejected_in_the_browser_says_so(self):
        with self.page_server() as url:
            post_json(url + "/api/account/sign-in", {})
            (self.signals / "deny").touch()
            answer = self.sign_in_status(url, "failed")
            self.assertEqual("failed", answer["status"])
            self.assertEqual(
                "That code was rejected in the browser. Nothing was connected.",
                answer["error"])
            self.assertFalse((self.home / "auth.json").exists())

    def test_cancelling_stops_the_command(self):
        with self.page_server() as url:
            post_json(url + "/api/account/sign-in", {})
            self.assertTrue(ui.ACCOUNT_SIGN_IN.running())
            answer = post_json(url + "/api/account/sign-in/cancel", {})
            self.assertEqual("cancelled", answer["status"])
            self.assertFalse(ui.ACCOUNT_SIGN_IN.running())
            # A cancelled attempt makes room for the next one.
            self.assertEqual(
                "waiting", post_json(url + "/api/account/sign-in", {})["status"])
        self.assertEqual(["auth --no-open", "auth --no-open"], self.calls())

    def test_without_the_cli_the_page_is_sent_to_a_terminal(self):
        env = dict(self.env, ENGELBART_CLI=str(self.root / "missing"))
        with self.page_server(env) as url:
            started = post_json(url + "/api/account/sign-in", {})
            self.assertEqual("failed", started["status"])
            self.assertIn("engelbart auth", started["error"])
            out = post_json(url + "/api/account/sign-out", {})
            self.assertFalse(out["ok"])
            self.assertIn("engelbart auth", out["error"])
        self.assertEqual([], self.calls())

    def test_another_site_s_form_cannot_sign_the_machine_out(self):
        self.connected()
        with self.page_server() as url:
            request = urllib.request.Request(
                url + "/api/account/sign-out", data=b"x=1", method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                NO_PROXY_OPENER.open(request, timeout=5)
            with caught.exception:
                self.assertEqual(415, caught.exception.code)
        self.assertTrue((self.home / "auth.json").exists())
        self.assertEqual([], self.calls())


class GoalPageModuleTests(unittest.TestCase):
    """The page's modules, read as files: what a browser would refuse."""

    IMPORT = re.compile(r'^import\s.*?\sfrom\s+"([^"]+)";', re.M | re.S)

    def modules(self):
        return sorted(GOAL_DIR.rglob("*.js"))

    def test_every_import_names_a_file_that_exists(self):
        for module in self.modules():
            specifiers = self.IMPORT.findall(module.read_text(encoding="utf-8"))
            for specifier in specifiers:
                self.assertTrue(specifier.startswith(("./", "../")),
                                f"{module.name} imports {specifier}: bare "
                                "specifiers do not resolve in a browser")
                target = (module.parent / specifier).resolve()
                self.assertTrue(target.is_file(), f"{module.name}: {specifier}")
                self.assertTrue(str(target).startswith(str(GOAL_DIR)), specifier)

    def test_the_html_names_only_files_the_server_has(self):
        html = (GOAL_DIR / "index.html").read_text(encoding="utf-8")
        for match in re.finditer(r'(?:src|href)="(/goal/[^"]+)"', html):
            self.assertTrue((GOAL_DIR / match.group(1)[len("/goal/"):]).is_file(),
                            match.group(1))

    def test_every_module_parses_as_an_es_module(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for module in self.modules():
            run = subprocess.run(
                [node, "--input-type=module", "--check", "-"],
                input=module.read_bytes(), capture_output=True, timeout=30,
            )
            self.assertEqual(0, run.returncode,
                             f"{module.relative_to(GOAL_DIR)}: "
                             f"{run.stderr.decode('utf-8', 'replace')}")


class BrowserCase(ChatCase):
    """A chat, a server on it, and a page open in a real browser."""

    def setUp(self):
        super().setUp()
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        self.chrome = browser_executable()
        if not self.chrome:
            self.skipTest("Chrome/Chromium is not installed")
        self.expect, self.sync_playwright = expect, sync_playwright

    def tearDown(self):
        ui.ACCOUNT_SIGN_IN.cancel()
        super().tearDown()

    @contextmanager
    def page_on(self, url):
        with self.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=self.chrome, headless=True,
                args=["--disable-background-networking"])
            try:
                page = browser.new_page(viewport={"width": 1400, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text)
                        if message.type == "error" else None)
                page.goto(url, wait_until="domcontentloaded")
                yield page, errors
            finally:
                browser.close()


class GoalPageBrowserTests(BrowserCase):
    """The design's interactions, on a chat's real files."""

    def test_the_design_s_interactions_hold_on_the_chat_s_goals(self):
        expect = self.expect
        active = re.compile(r"\bis-active\b")
        goal, subgoals = seed_design(self.chat)
        # The builder, stood in for: first it will not start, then it takes
        # the rows the way the real one does, marking them as it goes.
        build = {"ok": False, "error": "no project to build in"}

        def start(session_id, root, goal_id, row_ids, quick=False):
            if not build["ok"]:
                return dict(build)
            goals, important = CS.load_goals(session_id, root)
            for row in GM.by_id(goals, goal_id)["todo_items"]:
                if row["id"] in row_ids:
                    row["status"] = "building"
            CS.save_goals(session_id, goals, important, root)
            return {"ok": True, "started": True, "rows": list(row_ids)}

        # Bart, stood in for: a row for the first message, a question after.
        ask = fake_bart({"ok": True, "say": "", "card": "todos",
                         "todos": ["save the file as parquet"]}, PROSE_CARD)

        with mock.patch("human_compact.trajectory.build.start", start), \
                mock.patch.object(BRAIN, "ask", ask), \
                server_for(self.chat) as url, self.page_on(url) as (page, errors):
            # The goal, its breakdown, and the first subgoal selected with
            # its own two todos -- all of it from the chat's goals.json.
            expect(page.get_by_role("heading", name=GOAL_TITLE)).to_be_visible()
            subs = page.locator(".rail .sub")
            expect(subs).to_have_count(3)
            expect(subs.nth(0)).to_have_class(active)
            rows = page.locator(".todo-list .todo:not(.todo-new)")
            expect(rows).to_have_count(2)
            expect(rows.nth(0).locator(".todo-text")).to_have_value(FIRST_TODOS[0])
            expect(page.get_by_role("button", name="Build all")).to_be_enabled()

            # Notes belong to the subgoal they were written on, and reach
            # the chat's files once the reader pauses.
            page.locator(".notes-input").fill("first notes")
            subs.nth(1).click()
            expect(subs.nth(1)).to_have_class(active)
            expect(page.locator(".notes-input")).to_have_value("")
            self.assertTrue(wait_for(
                lambda: GM.by_id(self.goals()[0], subgoals[0])["notes"] == "first notes"))
            # No todos yet, so the pane is folded away behind its button.
            expect(page.get_by_role("button", name="Show todos")).to_be_visible()
            expect(page.locator(".todo-list")).to_have_count(0)

            # Bart's row for the first message comes back as a proposal,
            # and Add puts it on the list -- and in todos.json.
            composer = page.get_by_label("Message Bart")
            composer.fill("I want to save the file as parquet")
            composer.press("Enter")
            expect(page.locator(".msg.from-you .bubble")).to_have_text("I want to save the file as parquet")
            expect(composer).to_have_value("")
            expect(page.locator(".proposal-text")).to_have_text("save the file as parquet", timeout=5_000)
            page.get_by_role("button", name="Add", exact=True).click()
            expect(page.locator(".proposal-note")).to_have_text("added to todos")
            expect(rows).to_have_count(1)
            expect(rows.nth(0).locator(".todo-text")).to_have_value("save the file as parquet")
            self.assertTrue(wait_for(lambda: stored_rows(self.chat, subgoals[1]) ==
                                     [("save the file as parquet", "")]))

            # A todo typed, toggled, edited in place, and removed; each
            # lands in the file.
            new_todo = page.get_by_label("New todo")
            new_todo.fill("write the tests")
            new_todo.press("Enter")
            expect(rows).to_have_count(2)
            expect(new_todo).to_have_value("")
            rows.nth(1).get_by_role("button", name="Mark as done").click()
            expect(rows.nth(1)).to_have_class(re.compile(r"\bis-done\b"))
            rows.nth(0).locator(".todo-text").fill("save as parquet")
            expect(rows.nth(0).locator(".todo-text")).to_have_value("save as parquet")
            expect(rows.nth(0).locator(".todo-text")).to_be_focused()
            self.assertTrue(wait_for(lambda: stored_rows(self.chat, subgoals[1]) == [
                ("save as parquet", ""), ("write the tests", "done")]),
                stored_rows(self.chat, subgoals[1]))
            rows.nth(1).get_by_role("button", name="Remove todo").click()
            expect(rows).to_have_count(1)
            self.assertTrue(wait_for(
                lambda: stored_rows(self.chat, subgoals[1]) == [("save as parquet", "")]))

            # A prose answer is a bubble; and the second round carried the
            # whole conversation, the proposal as Bart's own turn, taken.
            composer.fill("and then?")
            page.get_by_role("button", name="Send").click()
            expect(page.locator(".msg.from-bart .bubble")).to_have_text(
                re.compile("Imagine this subgoal is done"), timeout=5_000)
            expect(composer).to_be_focused()
            self.assertEqual(
                [("you", "I want to save the file as parquet"),
                 ("bart", "Proposed TODO row: save the file as parquet (added to the list)"),
                 ("you", "and then?")],
                [(t["role"], t["text"]) for t in ask.asked[1]["transcript"]])

            # Back on the first subgoal: its notes, its todos, none of the
            # second's conversation.
            subs.nth(0).click()
            expect(page.locator(".notes-input")).to_have_value("first notes")
            expect(rows).to_have_count(2)
            expect(page.locator(".msg")).to_have_count(0)

            # The other two panes, and the host beside them.
            page.get_by_role("tab", name="Live preview").click()
            expect(page.get_by_role("button", name="Import dataset")).to_be_visible()
            expect(page.get_by_text("CSV only · up to 100mb")).to_be_visible()
            expect(page.locator(".host")).to_have_text("localhost:5173")
            page.get_by_role("tab", name="Terminal").click()
            expect(page.locator(".terminal")).to_contain_text("$ npm run dev")
            expect(page.locator(".terminal")).to_contain_text("ready · http://localhost:5173")
            expect(page.locator(".host")).to_have_text("localhost:5173")
            page.get_by_role("tab", name="Plan").click()
            expect(page.locator(".host")).to_have_count(0)
            expect(page.locator(".notes-input")).to_have_value("first notes")

            # A subgoal added from the rail is selected as it lands, and is
            # a goal under this one in the chat's tree.
            page.get_by_role("button", name="+ Add subgoal").click()
            added = page.get_by_label("New subgoal")
            expect(added).to_be_focused()
            added.fill("Export the dataset to parquet")
            added.press("Enter")
            expect(subs).to_have_count(4)
            expect(subs.nth(3)).to_have_class(active)
            expect(subs.nth(3)).to_have_text("Export the dataset to parquet")
            expect(page.locator(".todo-list")).to_have_count(0)
            self.assertIn(("Export the dataset to parquet", goal),
                          [(g["title"], g.get("parent_goal_id")) for g in self.goals()[0]["goals"]])
            # Escape drops an empty one.
            page.get_by_role("button", name="+ Add subgoal").click()
            page.get_by_label("New subgoal").press("Escape")
            expect(subs).to_have_count(4)

            # A subgoal the chat writes while the page is open -- as the
            # hooks write one while the reader talks -- reaches the rail
            # without a reload, and takes nothing from the reader.
            goals, important = self.goals()
            goals["goals"].append(GM.new_goal(
                GM.next_goal_id(goals), "Validate the dataset's columns", goal,
                origin="inference"))
            CS.save_goals("chat", goals, important, self.root)
            expect(subs).to_have_count(5, timeout=5_000)
            expect(subs.nth(4)).to_have_text("Validate the dataset's columns")
            expect(subs.nth(3)).to_have_class(active)
            subs.nth(0).click()
            expect(page.locator(".notes-input")).to_have_value("first notes")
            expect(rows).to_have_count(2)

            # Build all hands the open rows to the builder. Refused, it says
            # why under the button and the rows are untouched; taken, the
            # rows say they are building and are the builder's to edit.
            page.get_by_role("button", name="Build all").click()
            expect(page.locator(".build-note")).to_have_text("no project to build in")
            expect(rows).to_have_count(2)
            expect(page.locator(".todo-status")).to_have_count(0)
            expect(page.get_by_role("button", name="Build all")).to_be_enabled()
            build.update({"ok": True, "error": ""})
            page.get_by_role("button", name="Build all").click()
            expect(page.locator(".todo-status")).to_have_count(2, timeout=5_000)
            expect(page.locator(".todo-status").nth(0)).to_have_text("building…")
            expect(page.locator(".build-btn")).to_have_text(re.compile("Building…"))
            expect(page.locator(".build-btn")).to_be_disabled()
            expect(page.locator(".build-note")).to_have_count(0)
            expect(rows.nth(0).locator(".todo-text")).to_have_attribute("readonly", "")
            expect(rows.nth(0).get_by_role("button", name="Remove todo")).to_have_count(0)
            self.assertEqual([(text, "building") for text in FIRST_TODOS],
                             stored_rows(self.chat, subgoals[0]))

            self.assertEqual([], errors)

    def test_an_empty_chat_asks_for_the_goal(self):
        expect = self.expect
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            asked = page.get_by_label("What is the goal?")
            expect(asked).to_be_visible()
            expect(asked).to_be_focused()
            expect(page.locator(".rail")).to_have_count(0)
            expect(page.get_by_role("heading")).to_have_count(0)
            expect(page.get_by_role("tab")).to_have_count(0)
            asked.fill(GOAL_TITLE)
            asked.press("Enter")
            # The line is the goal at the top of the chat's tree, and the
            # page is about it from then on. With nothing under it yet, the
            # rail is ready for the first subgoal and the pane says so.
            expect(page.get_by_role("heading", name=GOAL_TITLE)).to_be_visible()
            expect(page.get_by_label("What is the goal?")).to_have_count(0)
            expect(page.get_by_role("tab", name="Plan")).to_be_visible()
            expect(page.locator(".pane.is-blank")).to_contain_text("Break it into subgoals")
            expect(page.locator(".notes-input")).to_have_count(0)
            first = page.get_by_label("New subgoal")
            expect(first).to_be_focused()
            goals, _important = self.goals()
            self.assertEqual([(GOAL_TITLE, "active")],
                             [(g["title"], g["status"]) for g in goals["goals"]])
            self.assertFalse(goals["goals"][0].get("parent_goal_id"))
            # Escape leaves the hint with its own way in; the first subgoal
            # named is selected, with its notes and conversation.
            first.press("Escape")
            page.get_by_role("button", name="+ Add the first subgoal").click()
            first = page.get_by_label("New subgoal")
            expect(first).to_be_focused()
            first.fill(SUBGOAL_TITLES[0])
            first.press("Enter")
            expect(page.locator(".rail .sub")).to_have_count(1)
            expect(page.locator(".rail .sub").nth(0)).to_have_class(re.compile(r"\bis-active\b"))
            expect(page.locator(".notes-input")).to_be_visible()
            expect(page.locator(".pane.is-blank")).to_have_count(0)
            # This load and the next.
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name=GOAL_TITLE)).to_be_visible()
            expect(page.locator(".rail .sub")).to_have_text([SUBGOAL_TITLES[0]])
            self.assertEqual([], errors)

    def test_a_project_set_up_on_the_web_fills_the_page(self):
        expect = self.expect
        chat, _tree = claim_web_setup(self)
        with server_for(chat) as url, self.page_on(url) as (page, errors):
            # The header is the path to where the reader is -- the project
            # from the web setup, then the direction they chose -- with the
            # plan they approved under it.
            expect(page.locator(".crumbs")).to_contain_text("Engelbart")
            expect(page.locator(".project-name")).to_have_text("Signed uploads")
            expect(page.get_by_role("heading", name="Direct-to-storage uploads")).to_be_visible()
            expect(page.locator(".goal-plan")).to_have_text(
                "Move uploads off the API server.\nSign, then PUT.")
            expect(page.get_by_label("What is the goal?")).to_have_count(0)
            # The pieces are the rail, the first one open, its notes what
            # the setup said about it and its rows ready to build.
            expect(page.locator(".rail .sub")).to_have_text(
                ["Signing route", "Client PUTs", "Retire the proxy"])
            expect(page.locator(".notes-input")).to_have_value(PIECE_NOTES["Signing route"])
            rows = page.locator(".todo-list .todo:not(.todo-new)")
            expect(rows).to_have_count(2)
            expect(rows.nth(0).locator(".todo-text")).to_have_value("Add POST /uploads/sign")
            expect(rows.nth(1).locator(".todo-text")).to_have_value("Scope the token")
            page.locator(".rail .sub").nth(2).click()
            expect(page.locator(".notes-input")).to_have_value(PIECE_NOTES["Retire the proxy"])
            # Editable as before: typed over, saved to the tree, kept.
            page.locator(".notes-input").fill("Delete the old path once the client PUTs land.")
            self.assertTrue(wait_for(lambda: any(
                g.get("notes") == "Delete the old path once the client PUTs land."
                for g in CS.load_goals(chat.name, self.root)[0]["goals"])))
            page.reload(wait_until="domcontentloaded")
            expect(page.locator(".project-name")).to_have_text("Signed uploads")
            page.locator(".rail .sub").nth(2).click()
            expect(page.locator(".notes-input")).to_have_value(
                "Delete the old path once the client PUTs land.")
            self.assertEqual([], errors)

    def test_the_account_icon_says_who_the_machine_is_connected_as(self):
        expect = self.expect
        seed_design(self.chat)
        cli, signals = fake_cli(self.root)
        env = {"ENGELBART_CLI": cli, "FAKE_ENGELBART_DIR": str(signals)}
        with mock.patch.dict(os.environ, env), \
                server_for(self.chat) as url, self.page_on(url) as (page, errors):
            account = page.get_by_role("button", name=re.compile("connected|account", re.I))

            # Nobody has connected this machine: the avatar says so and
            # the menu offers the way in.
            expect(account).to_have_attribute("aria-label", "Not connected")
            menu = page.get_by_role("menu", name="Account")
            expect(menu).to_have_count(0)
            account.click()
            expect(menu).to_be_visible()
            expect(menu).to_contain_text("Not connected")
            expect(menu.get_by_role("menuitem", name="Sign in")).to_be_visible()
            expect(menu.get_by_role("menuitem", name="Sign out")).to_have_count(0)
            page.keyboard.press("Escape")
            expect(menu).to_have_count(0)
            account.click()
            expect(menu).to_be_visible()
            page.locator(".goal-title").click()
            expect(menu).to_have_count(0)

            # Connected: the account the installer wrote, by email, and
            # a way out.
            (self.home / "auth.json").write_text(json.dumps({
                "apiBase": "http://127.0.0.1:9", "token": "machine-token",
                "email": "someone@example.com"}), encoding="utf-8")
            page.reload(wait_until="domcontentloaded")
            expect(account).to_have_attribute(
                "aria-label", "Connected as someone@example.com")
            account.click()
            expect(menu).to_contain_text("someone@example.com")
            # Sign out runs `engelbart logout` (the stand-in here): the
            # account is gone from disk and the menu says what happened.
            menu.get_by_role("menuitem", name="Sign out").click()
            expect(account).to_have_attribute("aria-label", "Not connected")
            expect(menu).to_be_visible()
            expect(menu).to_contain_text("Disconnected. That token is revoked.")
            self.assertFalse((self.home / "auth.json").exists())

            # Sign in runs `engelbart auth`: the code it printed and the
            # page that approves it stay up until the approval lands.
            menu.get_by_role("menuitem", name="Sign in").click()
            expect(menu).to_contain_text("WXYZ-2468")
            expect(menu.get_by_role("link", name="Open the approval page")).to_have_attribute(
                "href", "http://127.0.0.1:9/engelbart?code=WXYZ-2468")
            expect(menu.get_by_role("button", name="Cancel")).to_be_visible()
            (signals / "approve").touch()
            expect(account).to_have_attribute(
                "aria-label", "Connected as someone@example.com", timeout=10000)
            expect(menu).to_contain_text("someone@example.com")
            expect(menu.get_by_role("menuitem", name="Sign in")).to_have_count(0)
            self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
