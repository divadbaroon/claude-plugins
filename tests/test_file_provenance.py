"""What leaves the machine when a project's files go up, and what does not.

The interesting tests here are the negative ones. A payload that carries the
right rows is table stakes; a payload that carries a `.env` because someone
edited it once is a leak, and it would look exactly like success. So most of
what follows is about the files that must NOT be in it, and about the
difference between "git said zero" and "git said nothing".
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import file_provenance as FP  # noqa: E402

OWNER = "11111111-1111-1111-1111-111111111111"


def git(cwd, *args):
    subprocess.run(("git", "-C", str(cwd), *args), check=True,
                   capture_output=True, text=True)


class ProjectFixture(unittest.TestCase):
    """A vault with one session that edited files in a real git repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.vault = base / "vault"
        (self.vault / "chat-sessions").mkdir(parents=True)
        self.project = base / "project"
        self.project.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_VAULT_DIR": str(self.vault)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "config", "user.email", "t@example.com")
        git(self.project, "config", "user.name", "T")
        (self.project / "app.py").write_text(
            "\n".join(f"line {n}" for n in range(1, 31)) + "\n")
        (self.project / ".gitignore").write_text(".env\nsecrets/\n")
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "first")

        # Two files that must never be quoted, whatever the edits say.
        (self.project / ".env").write_text("API_KEY=sk-live-do-not-copy\n")
        (self.project / "secrets").mkdir()
        (self.project / "secrets" / "notes.md").write_text("the password\n")
        (self.project / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n")

    def session(self, session_id="sess-1", touches=(), created="2026-08-01T00:00:00Z"):
        directory = self.vault / "chat-sessions" / session_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "session_id": session_id,
            "cwd": str(self.project.resolve()),
            "created_at": created, "updated_at": created,
            "transcript_path": "", "source": "test", "event_count": 0,
            "last_ordinal": 0, "prompt_count": 0, "analyzer": {}}))
        events = []
        for ordinal, (tool, name) in enumerate(touches, start=1):
            events.append(json.dumps({
                "id": f"tool:{ordinal}", "kind": "tool_use", "tool_name": tool,
                "timestamp": created, "ordinal": ordinal,
                "text": json.dumps({"file_path": str(self.project / name)})}))
        (directory / "events.jsonl").write_text("\n".join(events) + "\n")
        return directory

    def snapshot(self, **kwargs):
        return FP.snapshot(None, str(self.project), OWNER, mint=False, **kwargs)


class WhatIsCarried(ProjectFixture):
    def test_a_session_that_edited_a_file_becomes_a_run_and_a_file_row(self):
        self.session(touches=[("Edit", "app.py"), ("Edit", "app.py")])
        payload = self.snapshot()
        self.assertEqual(FP.counts(payload)["runs"], 1)
        paths = {row["path"] for row in payload["files"]}
        self.assertIn("app.py", paths)
        row = next(r for r in payload["files"] if r["path"] == "app.py")
        self.assertEqual(row["edits"], 2)
        self.assertTrue(row["git_tracked"])
        self.assertEqual(row["git_status"], "clean")
        self.assertEqual(len(row["content_sha256"]), 64)
        self.assertTrue(row["last_commit_sha"])

    def test_reading_a_file_is_not_changing_it(self):
        self.session(touches=[("Read", "app.py")])
        payload = self.snapshot()
        # A run with no writes leaves no run at all, and no file row.
        self.assertEqual(FP.counts(payload)["files"], 0)
        self.assertEqual(FP.counts(payload)["run_files"], 0)

    def test_a_file_the_project_never_touched_is_absent(self):
        (self.project / "untouched.py").write_text("x = 1\n")
        self.session(touches=[("Edit", "app.py")])
        payload = self.snapshot()
        self.assertNotIn("untouched.py",
                         {row["path"] for row in payload["files"]})

    def test_a_deleted_file_is_marked_missing_rather_than_dropped(self):
        self.session(touches=[("Write", "gone.py")])
        payload = self.snapshot()
        row = next(r for r in payload["files"] if r["path"] == "gone.py")
        self.assertTrue(row["missing"])
        self.assertEqual(row["size_bytes"], None)
        # The run still records having written it.
        self.assertEqual(len(payload["run_files"]), 1)

    def test_ids_are_stable_across_two_builds(self):
        self.session(touches=[("Edit", "app.py")])
        first, second = self.snapshot(), self.snapshot()
        self.assertEqual([r["id"] for r in first["files"]],
                         [r["id"] for r in second["files"]])
        self.assertEqual([r["id"] for r in first["excerpts"]],
                         [r["id"] for r in second["excerpts"]])

    def test_every_row_carries_the_owner(self):
        self.session(touches=[("Edit", "app.py")])
        payload = self.snapshot()
        for table in FP.TABLES:
            for row in payload[table]:
                self.assertEqual(row["user_id"], OWNER)


class ChangeHistory(ProjectFixture):
    def test_an_uncommitted_edit_is_counted_by_git_not_guessed(self):
        (self.project / "app.py").write_text("changed\nlines\nhere\n")
        self.session(touches=[("Edit", "app.py")])
        row = self.snapshot()["run_files"][0]
        self.assertIsNotNone(row["lines_added"])
        self.assertGreater(row["lines_added"], 0)

    def test_an_unchanged_file_reports_zero_rather_than_unknown(self):
        self.session(touches=[("Edit", "app.py")])
        row = self.snapshot()["run_files"][0]
        self.assertEqual((row["lines_added"], row["lines_removed"]), (0, 0))

    def test_outside_a_repository_the_counts_are_unknown_not_zero(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        (plain / "a.py").write_text("x = 1\n")
        directory = self.vault / "chat-sessions" / "sess-plain"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({
            "session_id": "sess-plain", "cwd": str(plain.resolve()),
            "created_at": "2026-08-01T00:00:00Z"}))
        (directory / "events.jsonl").write_text(json.dumps({
            "kind": "tool_use", "tool_name": "Edit",
            "timestamp": "2026-08-01T00:00:00Z",
            "text": json.dumps({"file_path": str(plain / "a.py")})}) + "\n")
        payload = FP.snapshot(None, str(plain), OWNER, mint=False)
        row = payload["run_files"][0]
        self.assertIsNone(row["lines_added"])
        self.assertEqual(payload["files"][0]["git_status"], "")

    def test_the_commit_a_change_landed_in_is_recorded(self):
        self.session(touches=[("Edit", "app.py")])
        row = self.snapshot()["run_files"][0]
        self.assertEqual(len(row["commit_sha"]), 40)


class ContentIsTheException(ProjectFixture):
    def test_content_off_carries_history_and_no_source(self):
        self.session(touches=[("Edit", "app.py")])
        payload = self.snapshot(content=False)
        self.assertEqual(payload["excerpts"], [])
        self.assertTrue(payload["files"])
        self.assertTrue(payload["run_files"])

    def test_an_ignored_file_is_never_quoted(self):
        self.session(touches=[("Edit", ".env"),
                              ("Edit", "secrets/notes.md")])
        payload = self.snapshot()
        quoted = "\n".join(e["content"] for e in payload["excerpts"])
        self.assertNotIn("sk-live-do-not-copy", quoted)
        self.assertNotIn("the password", quoted)
        # The metadata is still there: that a `.env` was edited is worth
        # knowing, and its name is not its contents.
        self.assertIn(".env", {row["path"] for row in payload["files"]})

    def test_a_credential_shaped_name_is_never_quoted_even_when_tracked(self):
        git(self.project, "add", "-f", "id_rsa")
        git(self.project, "commit", "-qm", "oops")
        self.session(touches=[("Edit", "id_rsa")])
        payload = self.snapshot()
        self.assertEqual(
            [e for e in payload["excerpts"]
             if e["file_id"] == next(r["id"] for r in payload["files"]
                                     if r["path"] == "id_rsa")], [])

    def test_a_binary_file_is_never_quoted(self):
        (self.project / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
        self.session(touches=[("Write", "logo.png")])
        payload = self.snapshot()
        row = next(r for r in payload["files"] if r["path"] == "logo.png")
        self.assertTrue(row["binary_file"])
        self.assertEqual([e for e in payload["excerpts"]
                          if e["file_id"] == row["id"]], [])

    def test_an_excerpt_quotes_the_changed_lines_and_says_so(self):
        lines = [f"line {n}" for n in range(1, 31)]
        lines[19] = "THE CHANGED LINE"
        (self.project / "app.py").write_text("\n".join(lines) + "\n")
        self.session(touches=[("Edit", "app.py")])
        excerpts = self.snapshot()["excerpts"]
        self.assertTrue(excerpts)
        first = excerpts[0]
        self.assertIn("THE CHANGED LINE", first["content"])
        self.assertIn("not yet committed", first["reason"])
        self.assertLessEqual(first["start_line"], 20)
        self.assertGreaterEqual(first["end_line"], 20)
        # And it is a passage, not the file.
        self.assertNotIn("line 1\n", first["content"].split("\n")[0] + "\n"
                         if first["start_line"] > 1 else "line 1\n")

    def test_an_excerpt_is_stamped_with_the_hash_it_was_taken_from(self):
        self.session(touches=[("Edit", "app.py")])
        payload = self.snapshot()
        row = next(r for r in payload["files"] if r["path"] == "app.py")
        for excerpt in payload["excerpts"]:
            if excerpt["file_id"] == row["id"]:
                self.assertEqual(excerpt["file_sha256"], row["content_sha256"])

    def test_no_excerpt_exceeds_the_cap_the_table_enforces(self):
        (self.project / "big.py").write_text("x = 1\n" * 20000)
        self.session(touches=[("Write", "big.py")])
        for excerpt in self.snapshot()["excerpts"]:
            self.assertLessEqual(len(excerpt["content"]), FP.MAX_EXCERPT_CHARS)
            self.assertLessEqual(
                excerpt["end_line"] - excerpt["start_line"] + 1,
                FP.MAX_EXCERPT_LINES)

    def test_the_number_of_files_quoted_is_bounded(self):
        touches = []
        for n in range(FP.MAX_EXCERPT_FILES + 10):
            name = f"f{n}.py"
            (self.project / name).write_text(f"# file {n}\nx = {n}\n")
            touches.append(("Write", name))
        self.session(touches=touches)
        payload = self.snapshot()
        quoted = {e["file_id"] for e in payload["excerpts"]}
        self.assertLessEqual(len(quoted), FP.MAX_EXCERPT_FILES)


class Naming(unittest.TestCase):
    def test_the_credential_pattern_catches_the_usual_shapes(self):
        for name in (".env", ".env.local", "id_rsa", "server.pem",
                     "app.key", "my-secrets.json", "AWS_CREDENTIALS",
                     ".netrc", "token", "config/secret.yml"):
            self.assertTrue(FP._secretish(name), name)

    def test_it_does_not_catch_ordinary_source(self):
        for name in ("tokenizer.py", "environment.md", "keyboard.ts",
                     "src/keys/index.js", "renv.lock", "token_bucket.go"):
            self.assertFalse(FP._secretish(name), name)

    def test_it_errs_towards_excluding_and_that_is_the_intent(self):
        # `password_reset_flow.md` is a document about a feature, not a
        # credential, and it is excluded anyway. That asymmetry is chosen:
        # a wrongly excluded file costs the reader one excerpt they can
        # still open themselves, and a wrongly included one cannot be
        # taken back once it is in someone else's database.
        for name in ("password_reset_flow.md", "secret_santa.py"):
            self.assertTrue(FP._secretish(name), name)


if __name__ == "__main__":
    unittest.main()
