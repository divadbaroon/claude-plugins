"""The hand-off: the workspace as one markdown file for a teammate's agent.

The document is the state rendered, not summarised: every goal in tree
order with its status, notes, saved prompt and TODO rows carrying their
build states and open questions; the repository's git and GitHub metadata;
and, at the top, a prompt that has the receiving agent write an HTML
briefing and open it. The server serves it on /api/handoff and keeps a copy
beside the goals; the header button puts it on the clipboard.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import handoff as HO  # noqa: E402
from test_chat_ui_server import get_json, server_for, write_scope  # noqa: E402
from test_goal_ui_bridge import BridgeTestCase, NODE  # noqa: E402


NOW = datetime(2026, 8, 20, 21, 5, tzinfo=timezone.utc)


def goal(gid, title, parent=None, status="active", **more):
    out = {"id": gid, "title": title, "parent_goal_id": parent,
           "status": status, "notes": "", "prompt_md": "", "todo_items": [],
           "prompt_ids": [], "sources": [], "priority": "normal"}
    out.update(more)
    return out


def row(rid, text, status="", depth=0, question="", attachments=None):
    out = {"id": rid, "text": text, "depth": depth, "status": status,
           "question": question}
    if attachments:
        out["attachments"] = attachments
    return out


def git_repo(path, remote="git@github.com:acme/widgets.git"):
    """A repository with one commit, a branch, and an origin URL."""
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        subprocess.run(["git"] + list(args), cwd=path, check=True, env=env,
                       capture_output=True)

    git("init", "-q", "-b", "feature/x")
    (Path(path) / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first light")
    git("remote", "add", "origin", remote)
    (Path(path) / "b.txt").write_text("dirty\n")
    return git


class RenderTests(unittest.TestCase):
    GOALS = [
        goal("g1", "Ship the rail", status="in_progress", priority="high",
             notes="# Plan\nkeep it small\n", prompt_md="do the rail",
             prompt_ids=["p1", "p9"],
             sources=[{"id": "s1", "type": "github", "label": "acme/widgets"}],
             todo_items=[
                 row("t1", "wire it [attachment #1]", "asking",
                     question="which  port?",
                     attachments=[{"n": 1, "path": "/shots/a.png", "name": "a"}]),
                 row("t2", "child of wire", "", depth=1),
                 row("t3", "test it", "done"),
                 row("t4", "", "done")]),
        goal("g2", "Polish", parent="g1", status="completed",
             todo_items=[row("t5", "sand it", "done")]),
        goal("g3", "Later", status="active",
             todo_items=[row("t6", "not yet", "")]),
        goal("g4", "Dropped", status="abandoned",
             todo_items=[row("t7", "never", "failed")]),
    ]
    PROMPTS = [{"id": "p1", "role": "user", "text": "make  the rail\nnice"}]
    GIT = {"available": True, "cwd": "/w", "root": "/w/repo",
           "branch": "feature/x", "head": "abc123def", "head_short": "abc123d",
           "head_subject": "first light", "remote": "git@github.com:acme/widgets.git",
           "github": HO.github_of("git@github.com:acme/widgets.git"),
           "upstream": "origin/main", "ahead": 2, "behind": 1,
           "commits": [{"sha": "abc123d", "author": "t", "when": "now",
                        "subject": "first light"}],
           "dirty": ["?? b.txt"], "dirty_count": 1,
           "pr": {"number": 17, "title": "Rail", "url": "https://github.com/acme/widgets/pull/17",
                  "state": "OPEN", "base": "main", "draft": False}}

    def doc(self, **kw):
        args = dict(prompts=self.PROMPTS, git=self.GIT, session_id="s-1", now=NOW)
        args.update(kw)
        return HO.render(self.GOALS, **args)

    def test_the_prompt_for_the_agent_leads_and_names_the_html_file(self):
        doc = self.doc()
        top = doc.index("## Prompt for your coding agent")
        self.assertLess(top, doc.index("## Where I left off"))
        self.assertLess(doc.index("## Where I left off"), doc.index("## Goals"))
        self.assertLess(doc.index("## Goals"), doc.index("## Repository"))
        self.assertIn("`hc-handoff-20260820-2105.html`", doc)
        for word in ("open <file>", "xdg-open <file>", "start <file>",
                     "Do not edit, commit or push", "do not summarise"):
            self.assertIn(word, doc)

    def test_goals_come_in_tree_order_with_status_and_depth(self):
        doc = self.doc()
        g1 = doc.index("### Ship the rail  `[in progress]`")
        g2 = doc.index("#### Polish  `[completed]`")
        g3 = doc.index("### Later  `[active]`")
        g4 = doc.index("### Dropped  `[abandoned]`")
        self.assertTrue(g1 < g2 < g3 < g4)
        self.assertIn("_id `g2` · under Ship the rail_", doc)
        self.assertIn("priority high", doc)

    def test_todo_rows_carry_their_build_state_question_and_attachments(self):
        doc = self.doc()
        self.assertIn("- [asking] wire it [attachment #1]\n"
                      "    > Claude asked: which port?\n"
                      "    - [not yet sent] child of wire\n"
                      "- [done] test it", doc)
        self.assertIn("- [attachment #1]: /shots/a.png", doc)
        # a blank row is not a row
        self.assertNotIn("- [done] \n", doc)

    def test_notes_and_saved_prompt_are_verbatim_in_fences(self):
        doc = self.doc()
        self.assertIn("```md\n# Plan\nkeep it small\n```", doc)
        self.assertIn("```md\ndo the rail\n```", doc)
        # linked prompts, in the author's words; unknown ids are skipped
        self.assertIn('- "make the rail nice"', doc)
        self.assertIn("- acme/widgets (github)", doc)

    def test_a_fence_inside_the_notes_does_not_break_the_fence(self):
        tricky = [goal("g", "T", notes="```js\nx\n```\n")]
        doc = HO.render(tricky, now=NOW)
        self.assertIn("````md\n```js\nx\n```\n````", doc)

    def test_the_glance_counts_goals_rows_and_names_what_to_pick_up(self):
        doc = self.doc()
        self.assertIn("- Goals: 4 total — 1 active, 1 in progress, "
                      "1 completed, 1 abandoned", doc)
        self.assertIn("- TODO rows: 6 — 2 not yet sent, 1 asking, 2 done, "
                      "1 failed", doc)
        pick = doc[doc.index("### Pick up here"):doc.index("## Goals")]
        self.assertIn("- [asking] wire it [attachment #1] — asked: which port?"
                      "  _(under: Ship the rail)_", pick)
        self.assertIn("- [not yet sent] child of wire  _(under: Ship the rail)_", pick)
        # an unsent row under a merely active goal is not mid-flight, and
        # a failed row under an abandoned goal is nobody's to pick up
        self.assertNotIn("_(under: Later)_", pick)
        self.assertNotIn("never", pick)

    def test_git_and_github_metadata_are_laid_out_with_links(self):
        doc = self.doc()
        self.assertIn("# Hand-off: acme/widgets", doc)
        self.assertIn("- Repository: [acme/widgets](https://github.com/acme/widgets)"
                      " · branch `feature/x` · HEAD `abc123d` · 2 ahead / 1 behind"
                      " `origin/main` · 1 uncommitted path", doc)
        self.assertIn("- Open PR: #17 \"Rail\" (open) — "
                      "https://github.com/acme/widgets/pull/17", doc)
        repo = doc[doc.index("## Repository"):]
        self.assertIn("https://github.com/acme/widgets/tree/feature/x", repo)
        self.assertIn("https://github.com/acme/widgets/commit/abc123def", repo)
        self.assertIn("https://github.com/acme/widgets/compare/main...feature/x", repo)
        self.assertIn("```\n?? b.txt\n```", repo)
        self.assertIn("- `abc123d` first light — t, now", repo)

    def test_no_repository_is_said_plainly(self):
        doc = self.doc(git={"available": False, "cwd": "/nowhere"})
        self.assertIn("- Repository: none found where this workspace runs", doc)
        self.assertIn("No git repository was found at `/nowhere`", doc)

    def test_nothing_mid_flight_says_so(self):
        quiet = [goal("g", "Calm", status="active",
                      todo_items=[row("t", "later", "")])]
        doc = HO.render(quiet, now=NOW)
        self.assertIn("- Nothing is mid-flight", doc)


class GitMetadataTests(unittest.TestCase):
    def test_github_remotes_in_either_syntax(self):
        for remote in ("git@github.com:acme/widgets.git",
                       "https://github.com/acme/widgets",
                       "https://github.com/acme/widgets.git",
                       "ssh://git@github.com/acme/widgets.git"):
            self.assertEqual("acme/widgets", HO.github_of(remote)["slug"], remote)
            self.assertEqual("https://github.com/acme/widgets",
                             HO.github_of(remote)["url"])
        self.assertIsNone(HO.github_of("https://gitlab.com/acme/widgets.git"))
        self.assertIsNone(HO.github_of(""))

    def test_a_repository_reports_branch_head_remote_and_dirt(self):
        with tempfile.TemporaryDirectory() as tmp:
            git_repo(tmp)
            got = HO.git_metadata(tmp, with_gh=False)
        self.assertTrue(got["available"])
        self.assertEqual("feature/x", got["branch"])
        self.assertEqual("first light", got["head_subject"])
        self.assertEqual(len(got["head_short"]), len(got["head"][:len(got["head_short"])]))
        self.assertEqual("acme/widgets", got["github"]["slug"])
        self.assertEqual(["?? b.txt"], got["dirty"])
        self.assertEqual(1, got["dirty_count"])
        self.assertEqual("first light", got["commits"][0]["subject"])
        self.assertEqual("", got["upstream"])
        self.assertIsNone(got["ahead"])
        self.assertIsNone(got["pr"])

    def test_a_directory_without_git_is_not_a_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = HO.git_metadata(tmp, with_gh=False)
        self.assertFalse(got["available"])
        self.assertEqual(tmp, got["cwd"])
        self.assertFalse(HO.git_metadata(tmp + "/missing")["available"])

    def test_the_filename_names_the_repo_and_the_moment(self):
        self.assertEqual(
            "hc-handoff-widgets-20260820-2105.md",
            HO.filename({"github": {"repo": "widgets"}}, NOW))
        self.assertEqual("hc-handoff-workspace-20260820-2105.md",
                         HO.filename({}, NOW))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git_repo(str(self.repo))
        self.chat = self.root / "chat-a"
        write_scope(self.chat, [
            goal("a1", "Chat goal", status="in_progress",
                 todo_items=[row("t1", "build me", "queued")])],
            [{"id": "p1", "role": "user", "text": "hello", "ordinal": 1}])
        (self.chat / "manifest.json").write_text(json.dumps(
            {"session_id": "chat-a", "cwd": str(self.repo)}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_chat_workspace_hands_off_its_tree_and_its_repository(self):
        with server_for(self.chat) as url:
            # git is shelled out to several times on the way; a cold CI
            # runner has taken longer than the helper's two seconds once.
            body = get_json(url + "/api/handoff", timeout=20)
        self.assertTrue(body["ok"], body)
        doc = body["markdown"]
        self.assertIn("### Chat goal  `[in progress]`", doc)
        self.assertIn("- [queued] build me", doc)
        self.assertIn("of session `chat-a`", doc)
        self.assertIn("branch `feature/x`", doc)
        self.assertEqual("feature/x", body["git"]["branch"])
        self.assertEqual("acme/widgets", body["git"]["github"]["slug"])
        self.assertEqual(str(self.repo), body["git"]["cwd"])
        self.assertTrue(body["filename"].startswith("hc-handoff-widgets-"))
        # kept beside the goals, byte for byte
        self.assertEqual(str(self.chat / "handoff.md"), body["path"])
        self.assertEqual(doc, (self.chat / "handoff.md").read_text())
        self.assertEqual(len(doc.encode()), body["bytes"])

    def test_a_global_vault_hands_off_too(self):
        vault = self.root / "vault"
        write_scope(vault, [goal("v1", "Vault goal")], [])
        with server_for(vault, chat_scoped=False) as url:
            body = get_json(url + "/api/handoff", timeout=20)
        self.assertTrue(body["ok"], body)
        self.assertIn("### Vault goal  `[active]`", body["markdown"])
        self.assertIn("from the global vault.", body["markdown"])
        self.assertTrue((vault / "handoff.md").is_file())


@unittest.skipUnless(NODE, "node is required for bridge.js tests")
class HandoffButtonTests(BridgeTestCase):
    """The header button: drawn by the sweep, copies on click, says so."""

    PRELUDE = (
        "var H = window.__hcPromptUI.handoff;"
        "var slot = document.createElement('span'); slot.className = 'hc-handoff';"
        "header.appendChild(slot);"
        "window.__hcPromptUI.acceptState({ goals: [], prompts: [], scope: %s,"
        "  session_id: '7f3a1b2c-4d5e-4f60-8a9b-0c1d2e3f4a5b' });"
        "var copied = [];"
        "navigator.clipboard = { writeText: function (t) { copied.push(t);"
        "  return Promise.resolve(); } };"
        "fetch = function (url, opts) {"
        "  calls.push([url, null]);"
        "  var body = String(url).indexOf('/api/handoff') >= 0"
        "    ? { ok: true, markdown: '# Hand-off: acme/widgets\\n', filename: 'hc-handoff-widgets.md' }"
        "    : { ok: true };"
        "  return Promise.resolve({ ok: true, json: function () {"
        "    return Promise.resolve(body); } }); };"
        "var btn = function () { H.render(); return slot.querySelector('.hc-handoff-btn'); };"
        "var said = function () { var b = btn(); return b ? [b.getAttribute('data-hc-handoff'),"
        "  b.querySelector('.hc-handoff-said').textContent] : null; };"
    )

    def handoff(self, tail, scope="chat"):
        return self.run_js((self.PRELUDE % json.dumps(scope)) + tail,
                           extra_env={"HC_DEFER_TIMEOUT": "1"})

    def test_a_global_vault_draws_no_button(self):
        got = self.handoff("[H.render(), slot.children.length];", scope="global")
        self.assertEqual([False, 0], got)

if __name__ == "__main__":
    unittest.main()
