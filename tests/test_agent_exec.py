"""Agent execution state: Claude's live plan observed onto a Vault goal.

These tests hold the boundary the feature exists to protect: the agent's task
list is recorded against a goal, and the persistent human goal never moves as
a side effect of it.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import agent_exec as AE
from human_compact.trajectory import ui as UI  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_SID = "11111111-2222-4333-8444-555555555555"


def goal(gid, title, parent=None, todos=(), **extra):
    row = {"id": gid, "title": title, "status": "active", "parent_goal_id": parent,
           "evidence_ids": [], "important_item_ids": [], "prompt_ids": [],
           "todos": [{"text": t, "done": False, "evidence_ids": []} for t in todos],
           "priority": "normal", "notes": "", "description": "", "origin": "user",
           "updated_at": "2026-08-01T00:00:00+00:00"}
    row.update(extra)
    return row


class AgentExecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        self.trajdir = self.vault / "trajectory"
        self.trajdir.mkdir(parents=True)
        self.goals = {"version": 1, "goals": [
            goal("g1", "Build the goal platform"),
            goal("g2", "Connect Claude to Vault goals", parent="g1",
                 todos=("Decide the persistence boundary",),
                 description="Bind a session to a goal.", notes="keep it small"),
            goal("g3", "Unrelated goal"),
        ]}
        self.important = {"items": []}
        GM.sanitize(self.goals)          # todos become child goals
        GM.save(self.trajdir, self.goals, self.important)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(AE.GOAL_ENV, None)

    # --- helpers ---------------------------------------------------------

    def hook(self, event, **extra):
        payload = {"session_id": SID, "hook_event_name": event,
                   "cwd": str(self.vault)}
        payload.update(extra)
        return AE.observe_hook(payload, self.trajdir, self.goals)

    def batch(self, *calls):
        return self.hook("PostToolBatch", tool_calls=list(calls))

    def goals_digest(self):
        return (self.trajdir / "goals.json").read_bytes()

    def statuses(self, run):
        return [(t["task_id"], t["status"]) for t in run["tasks"]]

    # --- binding ---------------------------------------------------------

    def test_unbound_sessions_leave_no_execution_state(self):
        self.assertIsNone(self.hook("SessionStart"))
        self.assertFalse(AE.runs_dir(self.trajdir).exists())

    def test_ui_claim_binds_exactly_one_session(self):
        AE.arm(self.trajdir, "g2", "Connect Claude to Vault goals")
        run = self.hook("SessionStart")
        self.assertEqual("g2", run["vault_goal_id"])
        self.assertEqual(SID, run["claude_session_id"])
        self.assertEqual("running", run["status"])
        self.assertIsNone(AE.pending_claim(self.trajdir))

        second = AE.observe_hook(
            {"session_id": OTHER_SID, "hook_event_name": "SessionStart"},
            self.trajdir, self.goals)
        self.assertIsNone(second)

    def test_environment_binding_survives_a_stale_claim(self):
        AE.arm(self.trajdir, "g3", "Unrelated goal")
        os.environ[AE.GOAL_ENV] = "g2"
        self.assertEqual("g2", self.hook("SessionStart")["vault_goal_id"])

    def test_binding_refuses_a_goal_that_does_not_exist(self):
        os.environ[AE.GOAL_ENV] = "g99"
        self.assertIsNone(self.hook("SessionStart"))
        os.environ[AE.GOAL_ENV] = "../escape"
        self.assertIsNone(self.hook("SessionStart"))

    def test_expired_claims_are_ignored(self):
        AE.arm(self.trajdir, "g2")
        path = AE.runs_dir(self.trajdir) / AE.PENDING_NAME
        claim = json.loads(path.read_text())
        claim["expires_at"] = 0
        path.write_text(json.dumps(claim))
        self.assertIsNone(AE.pending_claim(self.trajdir))
        self.assertIsNone(self.hook("SessionStart"))

    # --- task capture ----------------------------------------------------

    def test_task_lifecycle_is_captured_from_tool_calls(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        self.hook("UserPromptSubmit", prompt="wire up the session binding")

        run = self.batch({
            "tool_name": "TaskCreate",
            "tool_use_id": "t1",
            "tool_input": {"subject": "Inspect current persistence",
                           "description": "Read goals.py",
                           "activeForm": "Inspecting persistence"},
            "tool_response": "Task #1 created successfully: Inspect current persistence",
        }, {
            "tool_name": "TaskCreate",
            "tool_use_id": "t2",
            "tool_input": {"subject": "Add session binding",
                           "description": "goal id to session id"},
            "tool_response": [{"type": "text",
                               "text": "Task #2 created successfully: Add session binding"}],
        })
        self.assertEqual([("1", "pending"), ("2", "pending")], self.statuses(run))
        self.assertEqual("wire up the session binding", run["user_prompt"])

        run = self.batch({
            "tool_name": "TaskUpdate",
            "tool_input": {"taskId": "1", "status": "in_progress"},
            "tool_response": "Updated task #1 status",
        })
        self.assertEqual([("1", "in_progress"), ("2", "pending")], self.statuses(run))

        run = self.batch({
            "tool_name": "TaskUpdate",
            "tool_input": {"taskId": "2", "owner": "explorer",
                           "addBlockedBy": ["1"]},
            "tool_response": "Updated task #2",
        })
        task = run["tasks"][1]
        self.assertEqual("explorer", task["owner"])
        self.assertEqual(["1"], task["blockedBy"])

        # Every captured task carries its authorship and its associations.
        for task in run["tasks"]:
            self.assertEqual("agent", task["source"])
            self.assertEqual("g2", task["vault_goal_id"])
            self.assertEqual(SID, task["claude_session_id"])
            self.assertTrue(task["created_at"] and task["updated_at"])

    def test_task_list_reconciles_state_the_hook_missed(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        run = self.batch({
            "tool_name": "TaskList",
            "tool_input": {},
            "tool_response": "#1 [completed] Read existing goal schema\n"
                             "#2 [in_progress] Add session binding\n"
                             "#3 [pending] Test task synchronization\n",
        })
        self.assertEqual(
            [("1", "completed"), ("2", "in_progress"), ("3", "pending")],
            self.statuses(run))
        self.assertEqual("Read existing goal schema", run["tasks"][0]["subject"])

    def test_deletion_is_recorded_rather_than_dropped(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        self.batch({"tool_name": "TaskCreate",
                    "tool_input": {"subject": "Try an approach", "description": "x"},
                    "tool_response": "Task #1 created successfully: Try an approach"})
        run = self.batch({"tool_name": "TaskUpdate",
                          "tool_input": {"taskId": "1", "status": "deleted"},
                          "tool_response": "Deleted task #1"})
        self.assertEqual([("1", "deleted")], self.statuses(run))

    def test_task_hook_events_are_captured_without_tool_payloads(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        run = self.hook("TaskCreated", task_id="7",
                        task_subject="Ship the feature",
                        task_description="end to end")
        self.assertEqual([("7", "pending")], self.statuses(run))
        run = self.hook("TaskCompleted", task_id="7",
                        task_subject="Ship the feature")
        self.assertEqual([("7", "completed")], self.statuses(run))

    def test_unrelated_tool_calls_are_ignored(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        run = self.batch({"tool_name": "Bash",
                          "tool_input": {"command": "ls"},
                          "tool_response": "a\nb"})
        self.assertEqual([], run["tasks"])

    # --- the boundary the feature exists to protect ----------------------

    def test_agent_progress_never_moves_the_human_goal(self):
        before = self.goals_digest()
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        self.batch({"tool_name": "TaskCreate",
                    "tool_input": {"subject": "Do the work", "description": "x"},
                    "tool_response": "Task #1 created successfully: Do the work"})
        self.batch({"tool_name": "TaskUpdate",
                    "tool_input": {"taskId": "1", "status": "completed"},
                    "tool_response": "Updated task #1 status"})
        self.hook("SessionEnd", reason="clear")

        self.assertEqual(before, self.goals_digest())
        goals, _ = GM.load(self.trajdir)
        target = GM.by_id(goals, "g2")
        self.assertEqual("active", target["status"])
        human = [g for g in goals["goals"] if g["parent_goal_id"] == "g2"]
        self.assertEqual(["active"], [g["status"] for g in human])

    # --- persistence -----------------------------------------------------

    def test_session_end_saves_an_execution_record(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        self.hook("UserPromptSubmit", prompt="connect the goal")
        self.batch({"tool_name": "TaskCreate",
                    "tool_input": {"subject": "Do the work", "description": "x"},
                    "tool_response": "Task #1 created successfully: Do the work"})
        self.hook("Stop", last_assistant_message="Bound the session and captured tasks.")
        run = self.hook("SessionEnd", reason="exit")

        self.assertEqual("finished", run["status"])
        self.assertTrue(run["finished_at"])
        self.assertEqual("exit", run["end_reason"])
        self.assertEqual("connect the goal", run["user_prompt"])
        self.assertIn("Bound the session", run["summary"])
        self.assertEqual("g2", run["vault_goal_id"])

        stored = json.loads(
            (AE.runs_dir(self.trajdir) / f"{SID}.json").read_text())
        self.assertEqual(run, stored)
        self.assertEqual(0o600, (AE.runs_dir(self.trajdir) / f"{SID}.json")
                         .stat().st_mode & 0o777)

    def test_git_branch_is_recorded_when_available(self):
        repo = self.vault / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/goal-exec\n")
        AE.arm(self.trajdir, "g2")
        run = AE.observe_hook(
            {"session_id": SID, "hook_event_name": "SessionStart",
             "cwd": str(repo)}, self.trajdir, self.goals)
        self.assertEqual("feat/goal-exec", run["git_branch"])

    # --- reading ---------------------------------------------------------

    def test_ui_payload_exposes_the_plan_for_the_goal(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        self.batch({"tool_name": "TaskCreate",
                    "tool_input": {"subject": "Inspect persistence",
                                   "description": "x",
                                   "activeForm": "Inspecting persistence"},
                    "tool_response": "Task #1 created successfully: Inspect persistence"})
        self.batch({"tool_name": "TaskUpdate",
                    "tool_input": {"taskId": "1", "status": "in_progress"},
                    "tool_response": "ok"})

        payload = ui._payload(self.trajdir, chat_scoped=False)
        plan = payload["agent_runs"]["g2"][0]
        self.assertEqual(SID, plan["session_id"])
        self.assertEqual("running", plan["status"])
        self.assertEqual([{"task_id": "1", "subject": "Inspect persistence",
                           "status": "in_progress",
                           "activeForm": "Inspecting persistence",
                           "owner": "", "blockedBy": [], "source": "agent"}],
                         plan["tasks"])
        self.assertEqual(1, plan["counts"]["in_progress"])
        self.assertNotIn("g3", payload["agent_runs"])

    def test_start_agent_run_arms_a_claim_without_touching_goals(self):
        before = self.goals_digest()
        result = ui._apply({"op": "start_agent_run", "goal_id": "g2"},
                           self.trajdir, chat_scoped=False)
        self.assertTrue(result["ok"])
        self.assertEqual("hc work g2", result["command"])
        self.assertEqual("g2", AE.pending_claim(self.trajdir)["vault_goal_id"])
        self.assertEqual(before, self.goals_digest())

        self.assertTrue(ui._apply({"op": "cancel_agent_run", "goal_id": "g2"},
                                  self.trajdir, chat_scoped=False)["ok"])
        self.assertIsNone(AE.pending_claim(self.trajdir))

    def test_start_agent_run_rejects_an_unknown_goal(self):
        result = ui._apply({"op": "start_agent_run", "goal_id": "nope"},
                           self.trajdir, chat_scoped=False)
        self.assertFalse(result["ok"])

    def test_goal_context_is_scoped_to_the_selected_goal(self):
        AE.arm(self.trajdir, "g2")
        self.hook("SessionStart")
        self.batch({"tool_name": "TaskCreate",
                    "tool_input": {"subject": "Earlier work", "description": "x"},
                    "tool_response": "Task #1 created successfully: Earlier work"})
        self.hook("Stop", last_assistant_message="Did the first pass.")
        self.hook("SessionEnd", reason="exit")

        context = AE.goal_context(self.trajdir, self.goals, "g2")
        self.assertIn("FOCUS", context)
        self.assertIn("g2", context)
        self.assertIn("GRAND GOAL", context)
        self.assertIn("Build the goal platform", context)
        self.assertIn("It breaks down into", context)
        self.assertIn("Decide the persistence boundary", context)
        self.assertIn("keep it small", context)
        self.assertIn("EARLIER CLAUDE SESSIONS ON THIS GOAL", context)
        self.assertIn("Did the first pass.", context)
        # The whole tree never goes into the prompt.
        self.assertNotIn("Unrelated goal", context)
        self.assertLess(len(context), 4100)


class WorkCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        self.trajdir = self.vault / "trajectory"
        self.trajdir.mkdir(parents=True)
        GM.save(self.trajdir, {"version": 1, "goals": [
            goal("g1", "Build the goal platform"),
            goal("g2", "Connect Claude to Vault goals", parent="g1"),
        ]}, {"items": []})
        from human_compact.trajectory import discover
        patch = mock.patch.object(discover, "VAULT", self.vault)
        patch.start()
        self.addCleanup(patch.stop)
        import human_compact.cli as cli
        self.cli = cli

    def run_work(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.cli.work_main(argv)
        return code, out.getvalue()

    def test_listing_shows_workable_goals(self):
        code, out = self.run_work(["--list"])
        self.assertEqual(0, code)
        self.assertIn("g2", out)
        self.assertIn("Connect Claude to Vault goals", out)

    def test_dry_run_reports_the_bound_launch(self):
        code, out = self.run_work(["g2", "--dry-run"])
        self.assertEqual(0, code)
        self.assertIn("HC_VAULT_GOAL_ID=g2", out)

    def test_a_title_fragment_resolves_to_one_goal(self):
        code, out = self.run_work(["connect claude", "--dry-run"])
        self.assertEqual(0, code)
        self.assertIn("HC_VAULT_GOAL_ID=g2", out)

    def test_an_unknown_goal_is_refused(self):
        code, out = self.run_work(["not-a-goal", "--dry-run"])
        self.assertEqual(2, code)
        self.assertIn("no goal matches", out)

    def test_print_context_shows_the_briefing_only(self):
        code, out = self.run_work(["g2", "--print-context"])
        self.assertEqual(0, code)
        self.assertIn("Your assignment for this session", out)
        self.assertIn("FOCUS", out)
        self.assertNotIn("HC_VAULT_GOAL_ID", out)


if __name__ == "__main__":
    unittest.main()


class LaunchTests(unittest.TestCase):
    """One click has to know where the work lives, and quote nothing."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        self.trajdir = self.vault / "trajectory"
        self.trajdir.mkdir(parents=True)
        self.project = Path(self.temp.name) / "Papert Lab"     # a space on purpose
        self.project.mkdir()
        (self.trajdir / "evidence_index.json").write_text(json.dumps({
            "a#1": {"role": "user", "text": "x", "cwd": str(self.project)},
            "a#2": {"role": "user", "text": "y", "cwd": str(self.project)},
            "a#3": {"role": "user", "text": "z", "cwd": "/nowhere/at/all"},
        }))
        self.goals = {"version": 1, "goals": [
            goal("g1", "Parent", evidence_ids=["a#1"]),
            goal("g1a", "Child", parent="g1",
                 evidence_ids=["a#2", "a#2", "a#3"],
                 description="Make capture work."),
            goal("g2", "No evidence anywhere"),
        ]}

    def test_the_project_directory_comes_from_the_goals_own_evidence(self):
        self.assertEqual(str(self.project),
                         AE.goal_cwd(self.trajdir, self.goals, "g1a"))

    def test_a_goal_without_evidence_falls_back_to_its_parent(self):
        self.goals["goals"][1]["evidence_ids"] = []
        self.assertEqual(str(self.project),
                         AE.goal_cwd(self.trajdir, self.goals, "g1a"))

    def test_an_unrecorded_directory_is_not_guessed(self):
        self.assertIsNone(AE.goal_cwd(self.trajdir, self.goals, "g2"))

    def test_a_recorded_directory_that_no_longer_exists_is_refused(self):
        (self.trajdir / "evidence_index.json").write_text(json.dumps({
            "a#1": {"role": "user", "text": "x", "cwd": "/gone/missing"}}))
        self.assertIsNone(AE.goal_cwd(self.trajdir, self.goals, "g1"))

    def test_the_opening_message_names_the_goal_without_repeating_context(self):
        opening = AE.launch_prompt(self.goals, "g1a")
        self.assertIn("g1a", opening)
        self.assertIn("Child", opening)
        # The description lives in the briefing; repeating it here is waste.
        self.assertNotIn("Make capture work.", opening)

    def test_the_launch_script_quotes_a_path_with_spaces(self):
        script = AE.write_launch_script(
            self.trajdir, "g1a", str(self.project), ["hc", "work", "g1a", "--start"])
        body = script.read_text()
        self.assertIn(f"cd '{self.project}'", body)
        self.assertEqual(0o700, script.stat().st_mode & 0o777)

    @unittest.skipUnless(Path(AE.EXPECT_BIN).exists(),
                         "expect drives the injection; absent on some systems")
    def test_the_session_starts_with_the_goal_typed_but_unsent(self):
        AE.write_launch_script(self.trajdir, "g1a", str(self.project),
                               ["hc", "work", "g1a"], "Work on goal g1a.")
        driver = (AE.runs_dir(self.trajdir) / "launch" / "g1a.exp").read_text()
        self.assertIn("spawn -noecho hc work g1a", driver)
        self.assertIn("send -- $body", driver)
        self.assertIn("interact", driver)
        # A carriage return anywhere here would submit it for the user.
        self.assertNotIn("send -- \"$body\\r\"", driver)
        self.assertNotIn("\\r", driver)
        prompt = (AE.runs_dir(self.trajdir) / "launch" / "g1a.prompt")
        self.assertEqual("Work on goal g1a.", prompt.read_text())

    def test_a_multiline_prompt_is_flattened_so_it_cannot_submit_early(self):
        # Holds on every platform: a newline in the injected text would press
        # Enter for the user.
        self.assertEqual("first line second line third",
                         AE.single_line("first line\nsecond line\n\nthird"))
        self.assertNotIn("\n", AE.single_line("a\rb\nc"))

    def test_without_expect_the_command_is_pre_typed_instead(self):
        # No injection is possible, so the session is not started for the user;
        # the command waits at the shell prompt.
        original = AE.EXPECT_BIN
        AE.EXPECT_BIN = "/nonexistent/expect"
        try:
            script = AE.write_launch_script(
                self.trajdir, "g1a", str(self.project),
                ["hc", "work", "g1a", "--start"], "Work on goal g1a.")
        finally:
            AE.EXPECT_BIN = original
        body = script.read_text()
        self.assertIn("HC_LAUNCH_CMD='hc work g1a --start'", body)
        self.assertNotIn("exec hc work", body)

    def test_without_a_prompt_it_falls_back_to_a_pre_typed_command(self):
        script = AE.write_launch_script(
            self.trajdir, "g1a", str(self.project), ["hc", "work", "g1a", "--start"])
        body = script.read_text()
        self.assertIn("HC_LAUNCH_CMD='hc work g1a --start'", body)
        self.assertNotIn("exec hc work", body)
        zshrc = (AE.runs_dir(self.trajdir) / "launch" / "zdotdir" / ".zshrc")
        self.assertIn("print -z", zshrc.read_text())

    def test_a_container_directory_is_never_offered(self):
        home = Path.home()
        (self.trajdir / "evidence_index.json").write_text(json.dumps({
            "a#1": {"role": "user", "text": "x", "cwd": str(home)},
            "a#2": {"role": "user", "text": "y", "cwd": str(home / "Desktop")},
        }))
        self.goals["goals"][0]["evidence_ids"] = ["a#1", "a#2"]
        self.goals["goals"][1]["evidence_ids"] = []
        self.assertIsNone(AE.goal_cwd(self.trajdir, self.goals, "g1"))

    def test_a_parent_borrows_the_project_from_its_subgoals(self):
        home = Path.home()
        (self.trajdir / "evidence_index.json").write_text(json.dumps({
            "a#1": {"role": "user", "text": "x", "cwd": str(home)},
            "a#2": {"role": "user", "text": "y", "cwd": str(self.project)},
        }))
        self.goals["goals"][0]["evidence_ids"] = ["a#1"]     # parent: home only
        self.goals["goals"][1]["evidence_ids"] = ["a#2"]     # child: real project
        self.assertEqual(str(self.project),
                         AE.goal_cwd(self.trajdir, self.goals, "g1"))

    def test_a_bogus_goal_id_never_reaches_the_filesystem(self):
        for bad in ("../escape", "a/b", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                AE.write_launch_script(self.trajdir, bad, str(self.project),
                                       ["hc", "work"])


class ClaimTimingTests(unittest.TestCase):
    """A claim binds a session that starts, never one already in progress."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trajdir = Path(self.temp.name) / "vault" / "trajectory"
        self.trajdir.mkdir(parents=True)
        self.goals = {"version": 1, "goals": [goal("g1", "Target")]}
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(AE.GOAL_ENV, None)

    def hook(self, event):
        return AE.observe_hook({"session_id": SID, "hook_event_name": event},
                               self.trajdir, self.goals)

    def test_a_message_in_a_live_session_does_not_consume_the_claim(self):
        AE.arm(self.trajdir, "g1")
        self.assertIsNone(self.hook("UserPromptSubmit"))
        self.assertIsNone(self.hook("PostToolBatch"))
        self.assertIsNotNone(AE.pending_claim(self.trajdir), "claim must survive")

    def test_session_start_still_consumes_it(self):
        AE.arm(self.trajdir, "g1")
        self.assertEqual("g1", self.hook("SessionStart")["vault_goal_id"])
        self.assertIsNone(AE.pending_claim(self.trajdir))

    def test_the_environment_still_binds_at_any_point(self):
        os.environ[AE.GOAL_ENV] = "g1"
        self.assertEqual("g1", self.hook("UserPromptSubmit")["vault_goal_id"])


class BriefingTests(unittest.TestCase):
    """Self-contained context: the user's words plus distilled outcomes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trajdir = Path(self.temp.name) / "vault" / "trajectory"
        (self.trajdir / "conversations").mkdir(parents=True)
        (self.trajdir / "evidence_index.json").write_text(json.dumps({
            "aaaa#001": {"role": "user", "date": "2026-08-01",
                         "text": "i thought we were doing a mac os level app?"},
            "aaaa#002": {"role": "assistant", "date": "2026-08-01",
                         "text": "I have now rewritten overlay.js to add the handler."},
            "bbbb#001": {"role": "user", "date": "2026-08-02",
                         "text": "unrelated work in another project"},
        }))
        (self.trajdir / "conversations" / "aaaa1111-2222.json").write_text(json.dumps({
            "extracted": {"decisions": ["Build as a menu bar app"],
                          "artifacts_or_outputs": ["Overlay.app - 172KB"],
                          "blockers": ["Accessibility invalidated after rebuilds"],
                          "unresolved_questions": ["Does VS Code expose a11y?"]}}))
        (self.trajdir / "conversations" / "bbbb3333-4444.json").write_text(json.dumps({
            "extracted": {"decisions": ["Something from another goal entirely"]}}))
        self.goals = {"version": 1, "goals": [goal(
            "g1", "Capture interactions", evidence_ids=["aaaa#001", "aaaa#002"],
            prompt_ids=["aaaa#001"], description="Capture across apps.")]}

    def brief(self):
        return AE.goal_context(self.trajdir, self.goals, "g1")

    def test_the_users_own_words_appear_verbatim(self):
        self.assertIn('"i thought we were doing a mac os level app?"', self.brief())

    def test_assistant_prose_is_never_quoted(self):
        # Its outcome is carried as facts instead; its narration ages badly.
        self.assertNotIn("I have now rewritten overlay.js", self.brief())

    def test_distilled_outcomes_come_from_the_cited_conversation(self):
        brief = self.brief()
        self.assertIn("ALREADY DECIDED", brief)
        self.assertIn("Build as a menu bar app", brief)
        self.assertIn("Overlay.app - 172KB", brief)
        self.assertIn("Accessibility invalidated after rebuilds", brief)
        self.assertIn("Does VS Code expose a11y?", brief)
        self.assertNotIn("another goal entirely", brief)   # not this goal's evidence

    def test_stale_context_is_labelled_as_such(self):
        self.assertIn("gone stale", self.brief())

    def test_the_launchers_own_prompt_never_returns_as_user_intent(self):
        (self.trajdir / "evidence_index.json").write_text(json.dumps({
            "aaaa#003": {"role": "user", "date": "2026-08-03",
                         "text": AE.LAUNCH_PREFIX + "g1: Capture interactions."}}))
        self.goals["goals"][0]["prompt_ids"] = ["aaaa#003"]
        self.assertNotIn("IN THEIR WORDS", self.brief())

    def test_a_briefing_stays_small_enough_to_be_worth_reading(self):
        self.assertLessEqual(len(self.brief()), AE.MAX_BRIEFING_CHARS + 1)

    def test_a_goal_with_no_history_still_produces_a_briefing(self):
        self.goals["goals"][0]["evidence_ids"] = []
        self.goals["goals"][0]["prompt_ids"] = []
        brief = self.brief()
        self.assertIn("Capture interactions", brief)
        self.assertIn("## 1. WHERE THIS SITS", brief)
        self.assertNotIn("ALREADY DECIDED", brief)


class SourcesTests(unittest.TestCase):
    """User-attached context: readable dirs vs references to cite."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trajdir = Path(self.temp.name) / "vault" / "trajectory"
        (self.trajdir / "conversations").mkdir(parents=True)
        (self.trajdir / "evidence_index.json").write_text("{}")
        self.extra = Path(self.temp.name) / "shared-lib"
        self.extra.mkdir()
        # Typed rows are what the UI now sends; plain strings still parse.
        self.goals = {"version": 1, "goals": [goal("g1", "Ship it", sources=[
            {"id": "c1", "type": "local", "label": str(self.extra)},
            {"id": "c2", "type": "github",
             "label": "https://github.com/divadbaroon/papertlab/issues/12"},
            {"id": "c3", "type": "local", "label": "/definitely/not/here"},
            {"id": "d1", "type": "doc", "label": "design-notes.md"},
        ])]}

    def test_only_existing_directories_become_readable(self):
        dirs, refs = AE.goal_sources(self.goals, "g1")
        self.assertEqual([str(self.extra)], dirs)
        self.assertIn("https://github.com/divadbaroon/papertlab/issues/12", refs)
        # A path that does not exist is cited, never granted.
        self.assertIn("/definitely/not/here", refs)
        self.assertIn("design-notes.md", refs)

    def test_a_repo_is_never_granted_as_a_readable_directory(self):
        dirs, _refs = AE.goal_sources(self.goals, "g1")
        self.assertNotIn("https://github.com/divadbaroon/papertlab/issues/12", dirs)

    def test_plain_strings_still_work(self):
        self.goals["goals"][0]["sources"] = [str(self.extra), "notes.md"]
        dirs, refs = AE.goal_sources(self.goals, "g1")
        self.assertEqual([str(self.extra)], dirs)
        self.assertEqual(["notes.md"], refs)

    def test_sources_appear_in_the_briefing(self):
        brief = AE.goal_context(self.trajdir, self.goals, "g1")
        self.assertIn("CONTEXT THE USER ATTACHED", brief)
        self.assertIn(f"{self.extra} (readable this session)", brief)
        self.assertIn("issues/12", brief)

    def test_a_goal_without_sources_has_no_reference_section(self):
        self.goals["goals"][0]["sources"] = []
        self.assertNotIn("CONTEXT THE USER ATTACHED",
                         AE.goal_context(self.trajdir, self.goals, "g1"))

    def test_sources_are_never_inferred(self):
        # Nothing derives sources from evidence; an empty list stays empty.
        from human_compact.trajectory import goals as GM
        tree = GM.sanitize({"version": 1, "goals": [goal("g2", "No sources")]})
        self.assertEqual([], tree["goals"][0]["sources"])


class OpeningLineTests(unittest.TestCase):
    """The line typed into the composer is short, and the user's to change."""

    def setUp(self):
        self.goals = {"version": 1, "goals": [goal(
            "g1b2", "Show finished projects as cards; reveal process on click",
            description="A long description that should not be repeated here "
                        "because the briefing already carries it in full.")]}

    def test_it_names_the_goal_and_stops(self):
        line = AE.launch_prompt(self.goals, "g1b2")
        self.assertIn("g1b2", line)
        self.assertIn("Show finished projects as cards", line)
        self.assertNotIn("should not be repeated", line)
        self.assertNotIn("session context", line)
        self.assertLess(len(line), 200)

    def test_a_goal_can_override_its_own_opening(self):
        self.goals["goals"][0]["opening"] = "pick up the card layout work"
        self.assertEqual("pick up the card layout work",
                         AE.launch_prompt(self.goals, "g1b2"))


class PromptShapeTests(unittest.TestCase):
    """The prompt should be readable top-down, and numbered without gaps."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trajdir = Path(self.temp.name) / "vault" / "trajectory"
        (self.trajdir / "conversations").mkdir(parents=True)
        (self.trajdir / "evidence_index.json").write_text("{}")
        self.goals = {"version": 1, "goals": [
            goal("g1", "Build the platform"),
            goal("g1b", "Build the browsing UI", parent="g1"),
            goal("g1b2", "Show finished projects as cards", parent="g1b"),
            goal("g1b21", "Make the cards bigger", parent="g1b2"),
        ]}

    def test_it_opens_by_saying_what_it_is(self):
        brief = AE.goal_context(self.trajdir, self.goals, "g1b2")
        first = brief.splitlines()[0]
        self.assertEqual("# Your assignment for this session", first)
        self.assertIn("work only on the FOCUS goal", brief)

    def test_the_hierarchy_reads_from_grand_goal_down_to_focus(self):
        lines = AE.goal_context(self.trajdir, self.goals, "g1b2").splitlines()
        chain = [l for l in lines if " · " in l and any(
            m in l for m in ("GRAND GOAL", "PARENT", "FOCUS"))]
        self.assertTrue(chain[0].startswith("GRAND GOAL"))
        self.assertIn("Build the platform", chain[0])
        self.assertIn("PARENT", chain[1])
        self.assertIn("FOCUS", chain[2])
        self.assertIn("Show finished projects as cards", chain[2])
        # Indented, so depth is visible at a glance.
        self.assertLess(len(chain[0]) - len(chain[0].lstrip()),
                        len(chain[2]) - len(chain[2].lstrip()))

    def test_the_focus_goals_own_subgoals_are_listed(self):
        brief = AE.goal_context(self.trajdir, self.goals, "g1b2")
        self.assertIn("It breaks down into", brief)
        self.assertIn("Make the cards bigger", brief)

    def test_section_numbers_never_skip(self):
        brief = AE.goal_context(self.trajdir, self.goals, "g1b2")
        numbers = [int(l.split(".")[0][3:]) for l in brief.splitlines()
                   if l.startswith("## ")]
        self.assertEqual(list(range(1, len(numbers) + 1)), numbers)

    def test_a_top_level_goal_has_no_orientation_chain(self):
        brief = AE.goal_context(self.trajdir, self.goals, "g1")
        self.assertNotIn("GRAND GOAL", brief)
        self.assertIn("FOCUS", brief)


class TaskPayloadShapeTests(unittest.TestCase):
    """Task events arrive in more than one shape; read all of them."""

    def run_with(self, call):
        run = {"vault_goal_id": "g1", "claude_session_id": SID, "tasks": []}
        AE.observe_tool_call(run, call)
        return run["tasks"]

    def test_the_id_is_taken_from_the_structured_result(self):
        tasks = self.run_with({
            "tool_name": "TaskCreate",
            "tool_input": {"subject": "Do the thing", "description": "x"},
            "tool_response": {"task": {"id": "42", "subject": "Do the thing"}},
        })
        self.assertEqual("42", tasks[0]["task_id"])
        self.assertEqual("Do the thing", tasks[0]["subject"])

    def test_the_id_falls_back_to_the_visible_text(self):
        tasks = self.run_with({
            "tool_name": "TaskCreate",
            "tool_input": {"subject": "Do the thing", "description": "x"},
            "tool_response": "Task #7 created successfully: Do the thing",
        })
        self.assertEqual("7", tasks[0]["task_id"])

    def test_update_accepts_the_unrepaired_key_names(self):
        # The model may emit id/task_id and active_form; the repair to taskId
        # and activeForm is not guaranteed to be what an observer sees.
        for key in ("taskId", "id", "task_id"):
            with self.subTest(key=key):
                run = {"vault_goal_id": "g1", "claude_session_id": SID,
                       "tasks": [{"task_id": "3", "subject": "s", "status": "pending",
                                  "description": "", "activeForm": "", "owner": "",
                                  "blocks": [], "blockedBy": [], "source": "agent",
                                  "vault_goal_id": "g1", "claude_session_id": SID,
                                  "created_at": "t", "updated_at": "t"}]}
                AE.observe_tool_call(run, {
                    "tool_name": "TaskUpdate",
                    "tool_input": {key: "3", "status": "in_progress",
                                   "active_form": "Doing it"},
                    "tool_response": "ok"})
                self.assertEqual("in_progress", run["tasks"][0]["status"])
                self.assertEqual("Doing it", run["tasks"][0]["activeForm"])


class ReviewTests(unittest.TestCase):
    """What a run left behind, and how to go look at it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        self.trajdir = self.vault / "trajectory"
        (self.trajdir / "conversations").mkdir(parents=True)
        (self.trajdir / "evidence_index.json").write_text("{}")
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.goals = {"version": 1, "goals": [goal("g1", "Ship it")]}
        os.environ[AE.GOAL_ENV] = "g1"
        self.addCleanup(os.environ.pop, AE.GOAL_ENV, None)
        AE.observe_hook({"session_id": SID, "hook_event_name": "SessionStart",
                         "cwd": str(self.project)}, self.trajdir, self.goals)

    def batch(self, *calls):
        return AE.observe_hook({"session_id": SID, "hook_event_name": "PostToolBatch",
                                "tool_calls": list(calls)}, self.trajdir, self.goals)

    def test_written_files_are_recorded_with_counts(self):
        target = str(self.project / "app.py")
        self.batch({"tool_name": "Edit", "tool_input": {"file_path": target}},
                   {"tool_name": "Edit", "tool_input": {"file_path": target}},
                   {"tool_name": "Write",
                    "tool_input": {"file_path": str(self.project / "README.md")}})
        run = AE.load_run(self.trajdir, SID)
        by_path = {f["path"]: f["edits"] for f in run["files"]}
        self.assertEqual(2, by_path[target])
        self.assertEqual(1, by_path[str(self.project / "README.md")])

    def test_reading_a_file_is_not_a_change(self):
        self.batch({"tool_name": "Read",
                    "tool_input": {"file_path": str(self.project / "app.py")}},
                   {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual([], AE.load_run(self.trajdir, SID)["files"])

    def test_no_file_contents_are_ever_copied(self):
        target = self.project / "secret.py"
        target.write_text("token = 'do not copy me'")
        self.batch({"tool_name": "Edit", "tool_input": {"file_path": str(target)}})
        stored = (AE.runs_dir(self.trajdir) / f"{SID}.json").read_text()
        self.assertIn("secret.py", stored)
        self.assertNotIn("do not copy me", stored)

    def test_review_lists_files_relative_to_the_project(self):
        self.batch({"tool_name": "Edit",
                    "tool_input": {"file_path": str(self.project / "src/app.py")}})
        data = AE.review(self.trajdir, self.goals, "g1")
        row = data["runs"][0]["files"][0]
        self.assertEqual("src/app.py", row["path"])
        self.assertEqual(str(self.project / "src/app.py"), row["full"])

    def test_a_project_without_git_is_told_how_to_open_it(self):
        self.batch({"tool_name": "Edit",
                    "tool_input": {"file_path": str(self.project / "app.py")}})
        how = AE.review(self.trajdir, self.goals, "g1")["runs"][0]["how"]
        commands = " ".join(h["command"] for h in how)
        self.assertIn(f"open {self.project}", commands)
        self.assertNotIn("git diff", commands)

    def test_a_git_project_is_told_the_diff_command(self):
        (self.project / ".git").mkdir()
        (self.project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.project / ".git" / "refs" / "heads").mkdir(parents=True)
        (self.project / ".git" / "refs" / "heads" / "main").write_text("a" * 40)
        self.batch({"tool_name": "Edit",
                    "tool_input": {"file_path": str(self.project / "app.py")}})
        how = AE.review(self.trajdir, self.goals, "g1")["runs"][0]["how"]
        self.assertIn("git -C", how[0]["command"])
        self.assertIn("diff", how[0]["command"])

    def test_commits_made_during_a_run_are_noticed(self):
        (self.project / ".git").mkdir()
        (self.project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        heads = self.project / ".git" / "refs" / "heads"
        heads.mkdir(parents=True)
        (heads / "main").write_text("a" * 40)
        run = AE.load_run(self.trajdir, SID)
        run["git_head_before"] = "b" * 40
        AE.save_run(self.trajdir, run)
        AE.observe_hook({"session_id": SID, "hook_event_name": "SessionEnd",
                         "reason": "exit"}, self.trajdir, self.goals)
        row = AE.review(self.trajdir, self.goals, "g1")["runs"][0]
        self.assertTrue(row["committed"])
        self.assertIn("log --oneline", row["how"][0]["command"])


class OnboardingTests(unittest.TestCase):
    """The UI asks; these are the effects it can actually cause."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        self.trajdir = self.vault / "trajectory"
        self.trajdir.mkdir(parents=True)
        from human_compact.trajectory import discover, state as ST
        for module, attr, value in ((discover, "VAULT", self.vault),):
            patch = mock.patch.object(module, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        home = mock.patch.dict(os.environ, {"HC_HOME": str(self.temp.name),
                                            "CLAUDE_VAULT_DIR": str(self.vault)})
        home.start()
        self.addCleanup(home.stop)
        # is_enabled() honours a legacy CLAUDE_VAULT=1 export when no explicit
        # choice is recorded, and the developer running these may have one.
        os.environ.pop("CLAUDE_VAULT", None)
        GM.save(self.trajdir, {"version": 1, "goals": []}, {"items": []})

    def test_the_legacy_environment_optin_still_counts_as_capture(self):
        os.environ["CLAUDE_VAULT"] = "1"
        self.assertTrue(ui.setup_state(self.trajdir)["storage"])

    def test_a_fresh_vault_reports_nothing_answered(self):
        state = ui.setup_state(self.trajdir)
        self.assertFalse(state["storage"])
        self.assertIsNone(state["analysis"])
        self.assertFalse(state["done"])

    def test_analysis_is_refused_until_capture_is_enabled(self):
        # Order matters: analysing history you never agreed to keep is not a
        # thing this can be talked into doing.
        result = ui._apply({"op": "start_analysis", "provider": "claude"},
                           self.trajdir, chat_scoped=False)
        self.assertFalse(result["ok"])
        self.assertIn("enable capture first", result["error"])

    def test_an_unknown_provider_is_refused(self):
        result = ui._apply({"op": "start_analysis", "provider": "sneaky"},
                           self.trajdir, chat_scoped=False)
        self.assertFalse(result["ok"])

    def test_declining_analysis_starts_nothing(self):
        with mock.patch.object(ui, "_spawn_analysis") as spawned:
            result = ui._apply({"op": "start_analysis", "provider": "none"},
                               self.trajdir, chat_scoped=False)
        self.assertTrue(result["ok"])
        spawned.assert_not_called()

    def test_the_chat_workspace_cannot_change_global_capture(self):
        result = ui._apply({"op": "enable_capture", "enabled": True},
                           self.trajdir, chat_scoped=True)
        self.assertFalse(result["ok"])

    def test_the_chosen_provider_is_written_before_analysis_runs(self):
        from human_compact import global_vault
        with mock.patch.object(global_vault, "is_enabled", return_value=True), \
             mock.patch("subprocess.Popen") as popen:
            ui._apply({"op": "start_analysis", "provider": "local"},
                      self.trajdir, chat_scoped=False)
        config = json.loads((self.trajdir / "config.json").read_text())
        self.assertEqual("ollama", config["extract_provider"])
        self.assertEqual("ollama", config["synth_provider"])
        popen.assert_called_once()
        self.assertIn("analyze", popen.call_args[0][0])
        self.assertNotIn("refresh", popen.call_args[0][0])


class ActivityLogTests(unittest.TestCase):
    """What the session is doing, as it does it."""

    def setUp(self):
        self.run = {"tasks": [], "files": []}

    def test_a_tool_batch_becomes_one_readable_line(self):
        for call, expected in (
            ({"tool_name": "Read", "tool_input": {"file_path": "/a/b/main.py"}},
             "read main.py"),
            ({"tool_name": "Edit", "tool_input": {"file_path": "/a/bridge.js"}},
             "edited bridge.js"),
            ({"tool_name": "Bash", "tool_input": {"command": "npm test -- -w"}},
             "ran npm test -- -w"),
            ({"tool_name": "Grep", "tool_input": {"pattern": "x"}},
             "searched the project"),
        ):
            self.assertEqual(expected, AE.describe_call(call))

    def test_a_tool_worth_nothing_to_the_reader_is_skipped(self):
        self.assertEqual("", AE.describe_call({"tool_name": "TaskUpdate"}))

    def test_file_contents_never_reach_the_log(self):
        got = AE.describe_call({"tool_name": "Write", "tool_input": {
            "file_path": "/a/secrets.env", "content": "TOKEN=hunter2"}})
        self.assertEqual("edited secrets.env", got)
        self.assertNotIn("hunter2", got)

    def test_entries_are_appended_with_a_time_and_a_kind(self):
        self.assertTrue(AE.note_activity(self.run, "did", "read main.py"))
        entry = self.run["activity"][0]
        self.assertEqual("did", entry["kind"])
        self.assertEqual("read main.py", entry["text"])
        self.assertTrue(entry["at"])

    def test_the_same_line_twice_running_is_not_repeated(self):
        AE.note_activity(self.run, "did", "read main.py")
        self.assertFalse(AE.note_activity(self.run, "did", "read main.py"))
        self.assertEqual(1, len(self.run["activity"]))

    def test_the_log_is_bounded(self):
        for i in range(AE.MAX_ACTIVITY + 25):
            AE.note_activity(self.run, "did", f"read file{i}.py")
        self.assertEqual(AE.MAX_ACTIVITY, len(self.run["activity"]))
        self.assertIn("file84", self.run["activity"][-1]["text"])

    def test_an_empty_line_is_not_recorded(self):
        self.assertFalse(AE.note_activity(self.run, "did", "   "))
        self.assertEqual([], self.run.get("activity", []))


class ConfirmedLaunchTests(unittest.TestCase):
    """Confirmation moved into the UI, so the launcher may send the prompt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name) / "vault" / "trajectory"
        self.trajdir.mkdir(parents=True)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()

    def _script(self, send):
        AE.write_launch_script(self.trajdir, "g1", str(self.project),
                               ["hc", "work", "g1"], "Work on my Vault goal g1",
                               send=send)
        return (AE.runs_dir(self.trajdir) / "launch" / "g1.exp").read_text()

    def test_a_confirmed_launch_runs_the_command_itself(self):
        # No pty typing and no Return to race: --start hands Claude the
        # opening message as an argument and it submits that itself.
        AE.write_launch_script(self.trajdir, "g1", str(self.project),
                               ["hc", "work", "g1", "--start"], "x", send=True)
        body = (AE.runs_dir(self.trajdir) / "launch" / "g1.sh").read_text()
        self.assertIn("exec hc work g1 --start", body)
        self.assertFalse((AE.runs_dir(self.trajdir) / "launch"
                          / "g1.exp").exists())

    def test_an_unconfirmed_launch_still_waits_for_a_keypress(self):
        AE.write_launch_script(self.trajdir, "g1", str(self.project),
                               ["hc", "work", "g1"], "Work on it", send=False)
        body = (AE.runs_dir(self.trajdir) / "launch" / "g1.sh").read_text()
        self.assertNotIn("exec hc work g1 --start", body)

    @unittest.skipUnless(Path(AE.EXPECT_BIN).exists(), "expect is required")
    def test_an_unconfirmed_launch_types_and_waits(self):
        body = self._script(False)
        self.assertIn("send -- $body", body)
        self.assertNotIn("send -- \\r", body)


class RunReportTests(unittest.TestCase):
    """Review answers: what did it do, and does it need me?"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name) / "vault" / "trajectory"
        (self.trajdir / "agent-runs").mkdir(parents=True)
        self.goals = {"version": 1, "goals": [
            goal("g1", "Ship it"),
            goal("g1a", "First", parent="g1", status="completed"),
            goal("g1b", "Second", parent="g1"),
        ]}

    def _write(self, **over):
        run = {"claude_session_id": "abc-123", "vault_goal_id": "g1",
               "status": "running", "started_at": "2026-08-14T10:00:00+00:00",
               "tasks": [], "files": [], "activity": []}
        run.update(over)
        (self.trajdir / "agent-runs" / "abc-123.json").write_text(json.dumps(run))
        return AE.review(self.trajdir, self.goals, "g1")["runs"][0]

    def test_a_live_run_reads_as_running(self):
        self.assertEqual("running", self._write()["state"])

    def test_a_run_that_handed_the_turn_back_is_waiting_on_you(self):
        row = self._write(awaiting_user=True, summary="Should I migrate them?")
        self.assertEqual("waiting", row["state"])
        self.assertEqual("Should I migrate them?", row["attention"])

    def test_a_finished_run_shows_no_question(self):
        row = self._write(status="finished", end_reason="clear",
                          summary="All done.")
        self.assertEqual("finished", row["state"])
        self.assertEqual("", row["attention"])

    def test_a_crashed_run_is_not_reported_as_finished(self):
        self.assertEqual("failed", self._write(status="finished",
                                               end_reason="error")["state"])

    def test_it_reports_what_was_checked_and_nothing_else(self):
        row = self._write(activity=[
            {"at": "2026-08-14T10:01:00+00:00", "kind": "did",
             "text": "ran npm test -- --watch=false"},
            {"at": "2026-08-14T10:02:00+00:00", "kind": "did",
             "text": "ran git status"},
            {"at": "2026-08-14T10:03:00+00:00", "kind": "did",
             "text": "edited main.py"},
        ])
        self.assertEqual(["npm test -- --watch=false"], row["checked"])

    def test_the_chronology_is_what_it_did(self):
        row = self._write(activity=[
            {"at": "2026-08-14T10:01:02+00:00", "kind": "did", "text": "read a.py"},
            {"at": "2026-08-14T10:02:03+00:00", "kind": "task",
             "text": "finished: wire it up"},
        ])
        self.assertEqual(["read a.py", "finished: wire it up"],
                         [d["text"] for d in row["did"]])
        self.assertEqual("10:01:02", row["did"][0]["at"])

    def test_progress_counts_its_tasks_and_the_goals_subgoals(self):
        row = self._write(tasks=[{"task_id": "1", "status": "completed"},
                                 {"task_id": "2", "status": "in_progress"}])
        self.assertEqual({"done": 1, "total": 2}, row["tasks"])
        self.assertEqual({"done": 1, "total": 2}, row["subgoals"])

    def test_it_says_how_to_reopen_the_session(self):
        self.assertEqual("claude -r abc-123", self._write()["resume"])

    def test_elapsed_is_reported_for_a_finished_run(self):
        row = self._write(status="finished",
                          finished_at="2026-08-14T10:08:00+00:00")
        self.assertEqual("8 min", row["elapsed"])


class LaunchCommandTests(unittest.TestCase):
    """A confirmed run asks for --start; an unconfirmed one does not."""

    def _command(self, confirmed):
        seen = {}

        def capture(trajdir, goal_id, cwd, command, prompt="", send=False):
            seen["command"] = command
            seen["send"] = send
            return Path("/tmp/never-run.sh")

        goals = {"version": 1, "goals": [goal("g1", "Ship it")]}
        op = {"op": "launch_agent_run", "goal_id": "g1"}
        if confirmed:
            op["confirmed"] = True
        with mock.patch.object(UI.AE, "write_launch_script", capture), \
             mock.patch.object(UI.AE, "goal_cwd", return_value="/repo"), \
             mock.patch.object(UI.AE, "open_terminal", return_value="Terminal"), \
             mock.patch.object(UI.AE, "clear_claim"), \
             mock.patch.object(UI.GM, "load", return_value=(goals, {"items": []})), \
             mock.patch.object(UI.GM, "save"):
            UI._apply(op, trajdir=Path("/nowhere"), chat_scoped=False)
        return seen

    def test_a_confirmed_run_starts_the_session(self):
        seen = self._command(True)
        self.assertEqual(["hc", "work", "g1", "--start"], seen["command"])
        self.assertTrue(seen["send"])

    def test_an_unconfirmed_run_leaves_it_to_the_user(self):
        seen = self._command(False)
        self.assertEqual(["hc", "work", "g1"], seen["command"])
        self.assertFalse(seen["send"])


class StartArgumentOrderTests(unittest.TestCase):
    """`--add-dir` is variadic, so the prompt cannot follow it."""

    def _argv(self, sources):
        import human_compact.cli as CLI
        goals = {"version": 1, "goals": [
            goal("g1", "Ship it"),
        ]}
        goals["goals"][0]["sources"] = sources
        printed = []
        with mock.patch.object(CLI, "print", create=True,
                               side_effect=lambda *a, **k: printed.append(a)), \
             mock.patch("human_compact.trajectory.goals.load",
                        return_value=(goals, {"items": []})), \
             mock.patch("human_compact.trajectory.state.trajdir",
                        return_value=Path("/nowhere")):
            CLI.work_main(["g1", "--start", "--dry-run"])
        return printed[-1][0] if printed else ""

    def test_the_prompt_comes_before_add_dir(self):
        # goal_sources only offers a directory that really exists.
        with tempfile.TemporaryDirectory() as real:
            line = self._argv([{"id": "s1", "type": "local", "label": real}])
            self.assertIn("--add-dir", line)
            self.assertLess(line.index("Work on my Vault goal g1"),
                            line.index("--add-dir"),
                            "a prompt after --add-dir is read as a directory")

    def test_the_prompt_is_still_passed_without_sources(self):
        self.assertIn("Work on my Vault goal g1", self._argv([]))


class TaskEventShapeTests(unittest.TestCase):
    """A task event may arrive flat or nested; both are the same task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name) / "vault" / "trajectory"
        (self.trajdir / "agent-runs").mkdir(parents=True)
        (self.trajdir / "agent-runs" / "s1.json").write_text(json.dumps({
            "claude_session_id": "s1", "vault_goal_id": "g1",
            "status": "running", "started_at": "2026-08-15T03:00:00+00:00",
            "tasks": [], "files": [], "activity": [], "schema_version": 1}))

    def _tasks(self, payload):
        payload = dict(payload, session_id="s1")
        AE.observe_hook(payload, self.trajdir, None)
        saved = json.loads(
            (self.trajdir / "agent-runs" / "s1.json").read_text())
        return saved["tasks"]

    def test_a_flat_event_is_recorded(self):
        tasks = self._tasks({"hook_event_name": "TaskCreated",
                             "task_subject": "Scan the repo"})
        self.assertEqual("Scan the repo", tasks[0]["subject"])

    def test_a_nested_event_is_recorded_with_its_real_id(self):
        tasks = self._tasks({"hook_event_name": "TaskCreated",
                             "task": {"id": "t9", "subject": "Wire it up"}})
        self.assertEqual("t9", tasks[0]["task_id"])
        self.assertEqual("Wire it up", tasks[0]["subject"])

    def test_completion_finds_the_task_it_names(self):
        self._tasks({"hook_event_name": "TaskCreated",
                     "task": {"id": "t9", "subject": "Wire it up"}})
        tasks = self._tasks({"hook_event_name": "TaskCompleted",
                             "task": {"id": "t9"}})
        self.assertEqual(1, len(tasks))
        self.assertEqual("completed", tasks[0]["status"])


class WaitingOnYouTests(unittest.TestCase):
    """A session that hands the turn back is waiting, message or not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name) / "vault" / "trajectory"
        (self.trajdir / "agent-runs").mkdir(parents=True)
        (self.trajdir / "agent-runs" / "s1.json").write_text(json.dumps({
            "claude_session_id": "s1", "vault_goal_id": "g1",
            "status": "running", "started_at": "2026-08-15T03:00:00+00:00",
            "tasks": [], "files": [], "activity": [], "schema_version": 1}))

    def _run(self, payload):
        AE.observe_hook(dict(payload, session_id="s1"), self.trajdir, None)
        return json.loads(
            (self.trajdir / "agent-runs" / "s1.json").read_text())

    def test_a_stop_without_a_message_still_marks_it_waiting(self):
        # Gating on last_assistant_message made a waiting run read as running.
        run = self._run({"hook_event_name": "Stop"})
        self.assertTrue(run["awaiting_user"])
        self.assertIn("waiting on you", run["activity"][-1]["text"])

    def test_a_stop_with_a_question_keeps_the_question(self):
        run = self._run({"hook_event_name": "Stop",
                         "last_assistant_message": "Migrate the records?"})
        self.assertTrue(run["awaiting_user"])
        self.assertEqual("Migrate the records?", run["summary"])

    def test_answering_it_clears_the_flag(self):
        self._run({"hook_event_name": "Stop",
                   "last_assistant_message": "Migrate?"})
        run = self._run({"hook_event_name": "UserPromptSubmit",
                         "prompt": "yes please"})
        self.assertFalse(run["awaiting_user"])

    def test_the_review_state_follows(self):
        goals = {"version": 1, "goals": [goal("g1", "Ship it")]}
        self._run({"hook_event_name": "Stop", "last_assistant_message": "Ask?"})
        row = AE.review(self.trajdir, goals, "g1")["runs"][0]
        self.assertEqual("waiting", row["state"])
        self.assertEqual("Ask?", row["attention"])


class TerminalWindowTests(unittest.TestCase):
    """The session is already open somewhere; remember where."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name) / "vault" / "trajectory"
        self.trajdir.mkdir(parents=True)

    def test_a_parked_window_is_read_once(self):
        AE.remember_window(self.trajdir, "g1", "4242")
        self.assertEqual("4242", AE.take_window(self.trajdir, "g1"))
        self.assertEqual("", AE.take_window(self.trajdir, "g1"),
                         "a consumed window must not bind a second session")

    def test_no_window_is_not_an_error(self):
        self.assertEqual("", AE.take_window(self.trajdir, "g1"))

    def test_a_goal_id_cannot_escape_the_launch_directory(self):
        AE.remember_window(self.trajdir, "../../etc/passwd", "1")
        self.assertFalse((self.trajdir / "agent-runs" / "launch").exists())

    def test_a_bound_run_inherits_the_window_its_launcher_opened(self):
        (self.trajdir / "agent-runs").mkdir(parents=True, exist_ok=True)
        AE.remember_window(self.trajdir, "g1", "77")
        goals = {"version": 1, "goals": [goal("g1", "Ship it")]}
        with mock.patch.dict(os.environ, {AE.GOAL_ENV: "g1"}):
            AE.observe_hook({"hook_event_name": "SessionStart",
                             "session_id": "22222222-3333-4444-5555-666666666666",
                             "cwd": str(self.trajdir)}, self.trajdir, goals)
        run = AE.load_runs(self.trajdir)[0]
        self.assertEqual("77", run["terminal_window"])


class RaiseWindowTests(unittest.TestCase):
    """Surfacing the session's own window, not opening a second one."""

    def test_the_script_does_not_need_accessibility(self):
        # System Events needs an Accessibility grant, and one osascript that
        # fails anywhere fails everywhere — bundling them made a raise that
        # would have worked report failure and open a duplicate session.
        self.assertNotIn("System Events", AE._RAISE_WINDOW)
        self.assertIn('tell application "Terminal"', AE._RAISE_WINDOW)

    def test_a_window_that_is_gone_reports_failure(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="x")
            self.assertFalse(AE.raise_window("4242"))

    def test_a_live_window_reports_success_and_focuses_terminal(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertTrue(AE.raise_window("4242"))
        # first the window ordering, then bringing Terminal forward
        self.assertEqual(2, run.call_count)

    def test_no_window_id_is_not_an_attempt(self):
        with mock.patch("subprocess.run") as run:
            self.assertFalse(AE.raise_window(""))
        run.assert_not_called()


class IdleInferenceTests(unittest.TestCase):
    """Silence is evidence, not proof — report it as such or not at all."""

    def _run(self, seconds_ago, status="running"):
        from datetime import datetime, timedelta, timezone
        when = (datetime.now(timezone.utc)
                - timedelta(seconds=seconds_ago)).isoformat()
        return {"claude_session_id": "s1", "vault_goal_id": "g1",
                "status": status, "started_at": when, "updated_at": when,
                "tasks": [], "files": [],
                "activity": [{"at": when, "kind": "did", "text": "read a.py"}]}

    def test_a_busy_run_is_not_called_quiet(self):
        self.assertEqual("", AE.review.__globals__["_ago"](0) and "" or "")
        run = self._run(5)
        self.assertLess(AE.idle_seconds(run), AE.IDLE_HINT_SECONDS)

    def test_a_long_silence_is_measured(self):
        self.assertGreaterEqual(AE.idle_seconds(self._run(300)), 300)

    def test_a_finished_run_is_never_reported_as_quiet(self):
        goals = {"version": 1, "goals": [goal("g1", "Ship it")]}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trajdir = Path(tmp.name) / "vault" / "trajectory"
        (trajdir / "agent-runs").mkdir(parents=True)
        run = self._run(600, status="finished")
        run["finished_at"] = run["updated_at"]
        (trajdir / "agent-runs" / "s1.json").write_text(json.dumps(run))
        row = AE.review(trajdir, goals, "g1")["runs"][0]
        self.assertEqual("", row["quiet_for"])
        self.assertEqual(0, row["idle_seconds"])

    def test_a_stalled_run_reports_how_long(self):
        goals = {"version": 1, "goals": [goal("g1", "Ship it")]}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trajdir = Path(tmp.name) / "vault" / "trajectory"
        (trajdir / "agent-runs").mkdir(parents=True)
        (trajdir / "agent-runs" / "s1.json").write_text(
            json.dumps(self._run(600)))
        row = AE.review(trajdir, goals, "g1")["runs"][0]
        self.assertEqual("10 min", row["quiet_for"])
