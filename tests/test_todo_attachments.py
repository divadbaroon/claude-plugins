"""Screenshots pasted into a TODO row.

A paste lands as "[attachment #N]" in the row's text and a file on disk under
the workspace; the row remembers which file each marker names, and every body
that leaves the rail (Copy all, Copy prompt, a Build) ends with the markers
resolved to their paths, so the session reading it can open them.
"""

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import build as BUILD  # noqa: E402
from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402

from test_chat_ui_server import NO_PROXY_OPENER, server_for  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)


def rows_with_shot():
    return [
        {"id": "taaaa0001", "text": "Fix the header [attachment #1]",
         "depth": 0, "status": "",
         "attachments": [{"n": 1, "path": "/tmp/shots/one.png",
                          "name": "one.png"}]},
        {"id": "taaaa0002", "text": "and the footer", "depth": 0,
         "status": ""},
        # The marker was deleted from the text: the file is no longer cited.
        {"id": "taaaa0003", "text": "orphaned", "depth": 0, "status": "",
         "attachments": [{"n": 2, "path": "/tmp/shots/two.png",
                          "name": "two.png"}]},
    ]


class AttachmentModelTests(unittest.TestCase):
    def test_normalize_keeps_well_formed_attachments_and_drops_the_rest(self):
        out = GM.normalize_todo_items([
            {"id": "taaaa0001", "text": "a [attachment #1]", "depth": 0,
             "attachments": [
                 {"n": 1, "path": "/x/a.png", "name": "a.png"},
                 {"n": "bad", "path": "/x/b.png"},
                 "junk",
                 {"n": 3, "path": "", "name": "c.png"}]},
            {"id": "taaaa0002", "text": "b", "depth": 0, "attachments": []},
            {"id": "taaaa0003", "text": "c", "depth": 0},
        ])
        self.assertEqual([{"n": 1, "path": "/x/a.png", "name": "a.png"}],
                         out[0]["attachments"])
        # A row without any has no key at all, so the browser's rows and the
        # server's compare equal field for field.
        self.assertNotIn("attachments", out[1])
        self.assertNotIn("attachments", out[2])

    def test_cited_attachments_are_the_markers_still_present_in_some_row(self):
        self.assertEqual(
            [{"n": 1, "path": "/tmp/shots/one.png", "name": "one.png"}],
            GM.todo_attachments(rows_with_shot()))
        self.assertEqual("[attachment #1]: /tmp/shots/one.png\n",
                         GM.render_attachments(rows_with_shot()))
        self.assertEqual("", GM.render_attachments([]))

    def test_a_marker_moved_to_another_row_still_counts(self):
        rows = rows_with_shot()
        rows[0]["text"] = "Fix the header"
        rows[1]["text"] = "and the footer [attachment #1]"
        self.assertEqual(1, len(GM.todo_attachments(rows)))


class AttachmentBodiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = "chat-shots"
        p = chat_state.paths(self.session, self.root)
        p.session_dir.mkdir(parents=True)
        g = GM.new_goal("g1", "Polish the page", None, origin="user")
        g["todo_items"] = rows_with_shot()
        goals = {"version": 1, "goals": [g]}
        GM.sanitize(goals)
        p.goals.write_text(json.dumps(goals))
        p.important.write_text(json.dumps({"items": []}))
        p.prompts.write_text(json.dumps({"prompts": []}))

    def test_the_build_prompt_resolves_the_markers_under_the_work(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        rows = BUILD.picked_with_children(g["todo_items"], ["taaaa0001"])
        prompt = BUILD.compose_prompt(self.session, goals, important, [], g, rows)
        work = prompt.split("# The work")[1]
        self.assertIn("- Fix the header [attachment #1] [taaaa0001]", work)
        self.assertIn("# Attachments", work)
        self.assertIn("[attachment #1]: /tmp/shots/one.png", work)
        self.assertNotIn("two.png", prompt)
        # the footer sits before the protocol, not after it
        self.assertLess(work.index("# Attachments"), work.index("# How to work"))

    def test_a_build_with_no_cited_attachments_has_no_footer(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        g = GM.by_id(goals, "g1")
        rows = BUILD.picked_with_children(g["todo_items"], ["taaaa0002"])
        prompt = BUILD.compose_prompt(self.session, goals, important, [], g, rows)
        self.assertNotIn("# Attachments", prompt)

    def test_the_injected_goal_tree_names_the_files_under_the_todos(self):
        goals, important = chat_state.load_goals(self.session, self.root)
        text = chat_state._goal_context_text(self.session, goals, important, [])
        self.assertIn("- TODOS:", text)
        self.assertIn("- ATTACHMENTS:", text)
        self.assertIn("[attachment #1]: /tmp/shots/one.png", text)
        self.assertNotIn("two.png", text)


class AttachmentRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope = Path(self.tmp.name) / "chat-a"
        self.scope.mkdir()
        (self.scope / "goals.json").write_text(json.dumps({"version": 1, "goals": []}))
        (self.scope / "important.json").write_text(json.dumps({"items": []}))
        (self.scope / "prompts.json").write_text(json.dumps({"prompts": []}))

    def upload(self, base, data, ctype="image/png", name=None, headers=None):
        h = {"Content-Type": ctype}
        if name is not None:
            h["X-HC-Name"] = name
        h.update(headers or {})
        request = urllib.request.Request(base + "/api/attachment", data=data,
                                         headers=h, method="POST")
        try:
            with NO_PROXY_OPENER.open(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_an_image_is_written_under_the_workspace_and_its_path_returned(self):
        with server_for(self.scope) as base:
            code, body = self.upload(base, PNG, name="Screenshot 2026-08-20.png")
        self.assertEqual(200, code, body)
        self.assertTrue(body["ok"], body)
        path = Path(body["path"])
        self.assertTrue(path.is_absolute())
        self.assertEqual((self.scope / "attachments").resolve(), path.parent)
        self.assertEqual(".png", path.suffix)
        self.assertEqual(PNG, path.read_bytes())
        self.assertEqual("Screenshot 2026-08-20.png", body["name"])

    def test_two_uploads_never_share_a_file(self):
        with server_for(self.scope) as base:
            _, a = self.upload(base, PNG)
            _, b = self.upload(base, PNG)
        self.assertNotEqual(a["path"], b["path"])

    def test_only_images_are_taken(self):
        with server_for(self.scope) as base:
            code, body = self.upload(base, b"hello", ctype="text/plain")
        self.assertEqual(415, code)
        self.assertFalse(body["ok"])
        self.assertEqual([], list((self.scope / "attachments").glob("*"))
                         if (self.scope / "attachments").exists() else [])

    def test_an_oversized_image_is_refused(self):
        with server_for(self.scope) as base:
            request = urllib.request.Request(
                base + "/api/attachment", data=PNG,
                headers={"Content-Type": "image/png",
                         "Content-Length": str(ui_max() + 1)}, method="POST")
            try:
                with NO_PROXY_OPENER.open(request, timeout=2) as response:
                    code = response.status
            except urllib.error.HTTPError as err:
                code = err.code
        self.assertEqual(413, code)


def ui_max():
    from human_compact.trajectory import ui
    return ui.MAX_ATTACHMENT_BYTES


if __name__ == "__main__":
    unittest.main()
