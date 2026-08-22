"""Reading one attached source, each kind the way that kind can be read.

A document is a file of the project, under the containment rule the file
pane already uses. A conversation is a chat of this vault, read as its
turns. A repository is a name and a link: this route reads the disk, and a
project overview has no business fetching over the network.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from test_project_json import ProjectFixture, server_for  # noqa: E402
from test_chat_ui_server import get_json, post_json  # noqa: E402


class SourceRouteTests(ProjectFixture):

    def attach(self, rows):
        with server_for(self.trajdir) as url:
            post_json(url + "/api/op",
                      {"op": "set_project_meta", "sources": rows})
            return get_json(url + "/api/state")["project"]["sources"]

    def read(self, rows, want):
        with server_for(self.trajdir) as url:
            post_json(url + "/api/op",
                      {"op": "set_project_meta", "sources": rows})
            return get_json(url + "/api/source?id=" + want)

    def test_the_sources_reach_the_state_the_page_reads(self):
        got = self.attach([{"type": "doc", "label": "notes.md"},
                           {"type": "chat", "label": self.twin}])
        self.assertEqual([{"id": "s1", "type": "doc", "label": "notes.md"},
                          {"id": "s2", "type": "chat", "label": self.twin}],
                         got)

    def test_a_document_of_the_project_is_read(self):
        (self.project / "notes.md").write_text("# Notes\n\nthe shape.\n")
        got = self.read([{"type": "doc", "label": "notes.md"}], "s1")
        self.assertEqual({"ok": True, "kind": "doc", "path": "notes.md",
                          "text": "# Notes\n\nthe shape.\n",
                          "truncated": False}, got)

    def test_an_absolute_path_inside_the_project_is_read_too(self):
        (self.project / "notes.md").write_text("inside\n")
        got = self.read([{"type": "doc",
                          "label": str(self.project / "notes.md")}], "s1")
        self.assertEqual(True, got["ok"])
        self.assertEqual("inside\n", got["text"])

    def test_a_file_outside_the_project_reads_nothing(self):
        # The same containment rule the file pane uses: a source is not a
        # way around it.
        (self.other / "secret.md").write_text("not yours\n")
        got = self.read([{"type": "doc",
                          "label": str(self.other / "secret.md")}], "s1")
        self.assertEqual(False, got["ok"])
        self.assertIn("outside the project", got["error"])

    def test_a_traversal_out_of_the_project_reads_nothing(self):
        (self.other / "secret.md").write_text("not yours\n")
        got = self.read([{"type": "doc",
                          "label": "../elsewhere/secret.md"}], "s1")
        self.assertEqual(False, got["ok"])
        self.assertEqual("no such file in the project", got["error"])

    def test_a_conversation_reads_as_its_turns(self):
        got = self.read([{"type": "chat", "label": self.twin}], "s1")
        self.assertEqual(True, got["ok"])
        self.assertEqual("chat", got["kind"])
        self.assertEqual(["chosen prompt", "linked prompt", "unmarked prompt"],
                         [t["text"] for t in got["turns"]])
        self.assertEqual(3, got["total"])

    def test_a_session_this_vault_does_not_hold_is_refused(self):
        got = self.read([{"type": "chat", "label": "../../etc"}], "s1")
        self.assertEqual(False, got["ok"])
        self.assertEqual("that is not a chat of this vault", got["error"])

    def test_a_repository_is_a_label_and_nothing_is_fetched(self):
        got = self.read([{"type": "github", "label": "acme/other"}], "s1")
        self.assertEqual({"ok": True, "kind": "github", "label": "acme/other"},
                         got)

    def test_a_remote_document_is_a_link_not_a_read(self):
        got = self.read([{"type": "doc",
                          "label": "https://example.com/spec"}], "s1")
        self.assertEqual({"ok": True, "kind": "doc",
                          "label": "https://example.com/spec"}, got)

    def test_a_source_that_is_not_attached_is_not_readable(self):
        got = self.read([{"type": "doc", "label": "notes.md"}], "s9")
        self.assertEqual({"ok": False, "error": "no such source"}, got)


if __name__ == "__main__":
    unittest.main()
