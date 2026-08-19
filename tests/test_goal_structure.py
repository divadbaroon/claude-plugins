import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HC_SRC = ROOT / "hc" / "src"
if str(HC_SRC) not in sys.path:
    sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import goals as GM  # noqa: E402


def goal(gid, parent=None):
    return {
        "id": gid,
        "title": gid,
        "status": "active",
        "parent_goal_id": parent,
        "evidence_ids": [],
        "todos": [],
    }


class GoalStructureTests(unittest.TestCase):
    def test_sanitize_breaks_parent_cycles_into_a_reachable_tree(self):
        goals = {"version": 1, "goals": [
            goal("g1", "g2"),
            goal("g2", "g1"),
            goal("g3", "g2"),
        ]}

        GM.sanitize(goals)

        self.assertIsNone(GM.by_id(goals, "g1")["parent_goal_id"])
        self.assertEqual("g1", GM.by_id(goals, "g2")["parent_goal_id"])
        self.assertEqual("g2", GM.by_id(goals, "g3")["parent_goal_id"])
        self.assertTrue(all(GM.depth(goals, g["id"]) <= 3 for g in goals["goals"]))

    def test_incremental_subgoal_keeps_description_and_parent(self):
        goals = {"version": 1, "goals": [goal("g1")]}

        GM.apply_ops(goals, {"items": []}, [{
            "op": "new_goal",
            "parent_goal_id": "g1",
            "title": "Search prior prompts",
            "description": "Offer typo-tolerant, newest-first lookup.",
            "todos": [],
            "evidence_ids": ["event:1"],
        }])

        child = GM.by_id(goals, "g2")
        self.assertEqual("g1", child["parent_goal_id"])
        self.assertEqual(
            "Offer typo-tolerant, newest-first lookup.", child["description"]
        )
        self.assertEqual("inferred", child["origin"])


if __name__ == "__main__":
    unittest.main()


class GlobalInferenceDescriptionTests(unittest.TestCase):
    """Inferred goals should carry the one line that says why they exist."""

    def test_both_global_prompts_request_a_description(self):
        from human_compact.trajectory import goal_synth as GS
        self.assertIn('"description":""', GS.REBUILD_PROMPT)
        self.assertIn('"description":""', GS.CLASSIFY_PROMPT)
        self.assertIn("Subgoals need one", GS.REBUILD_PROMPT)

    def test_rebuild_keeps_the_description_the_model_returned(self):
        from human_compact.trajectory import goal_synth as GS, goals as GM

        class Stub:
            def generate_json(self, prompt):
                return {"goals": [{"id": "g1", "title": "Ship it",
                                   "status": "active", "parent_goal_id": None,
                                   "description": "Why this work exists.",
                                   "evidence_ids": [], "todos": []}]}

        built = GM.sanitize(GS.rebuild(Stub(), []))
        self.assertEqual("Why this work exists.",
                         built["goals"][0]["description"])

    def test_the_classify_tree_shows_existing_descriptions(self):
        from human_compact.trajectory import goal_synth as GS
        digest = GS.tree_digest({"goals": [
            {"id": "g1", "title": "Ship it", "status": "active",
             "parent_goal_id": None, "description": "Why this work exists.",
             "todos": []}]})
        self.assertIn("— Why this work exists.", digest)


class DescriptionBackfillTests(unittest.TestCase):
    """Filling a blank description must not disturb anything else."""

    def setUp(self):
        from human_compact.trajectory import goal_synth as GS
        self.GS = GS
        self.goals = {"goals": [
            {"id": "g1", "title": "Kept", "description": "Mine, typed by hand.",
             "evidence_ids": ["a#1"], "todos": [], "notes": "keep me"},
            {"id": "g2", "title": "Blank", "description": "",
             "evidence_ids": ["a#2"], "todos": [{"text": "do it"}]},
            {"id": "g3", "title": "Ungroundable", "description": "",
             "evidence_ids": [], "todos": []},
        ]}
        self.index = {"a#1": {"role": "user", "text": "first message"},
                      "a#2": {"role": "user", "text": "make capture work on mac"}}

    def _provider(self, payload, seen):
        class Stub:
            def generate_json(_, prompt):
                seen.append(prompt)
                return payload
        return Stub()

    def test_only_blank_goals_are_described(self):
        seen = []
        written = self.GS.describe(
            self._provider({"descriptions": {"g2": "Capture on macOS, not just Chrome."}},
                           seen),
            self.goals, self.index)
        self.assertEqual({"g2": "Capture on macOS, not just Chrome."}, written)
        prompt = seen[0]
        self.assertIn("make capture work on mac", prompt)
        self.assertNotIn("Mine, typed by hand.", prompt)   # settled goals stay out

    def test_a_description_for_a_goal_that_had_one_is_refused(self):
        written = self.GS.describe(
            self._provider({"descriptions": {"g1": "Model tried to overwrite."}}, []),
            self.goals, self.index)
        self.assertEqual({}, written)

    def test_ungroundable_goals_are_left_empty(self):
        written = self.GS.describe(
            self._provider({"descriptions": {"g2": "Grounded."}}, []),
            self.goals, self.index)
        self.assertNotIn("g3", written)

    def test_nothing_to_do_makes_no_provider_call(self):
        class Boom:
            def generate_json(self, prompt):
                raise AssertionError("should not call the provider")
        for goal in self.goals["goals"]:
            goal["description"] = "already set"
        self.assertEqual({}, self.GS.describe(Boom(), self.goals, self.index))


class GlobalPromptTaggingTests(unittest.TestCase):
    """The global tree tags goals with the user turns they already cite."""

    def setUp(self):
        import tempfile
        from human_compact.trajectory import goals as GM
        self.GM = GM
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trajdir = Path(self.temp.name) / "vault" / "trajectory"
        self.trajdir.mkdir(parents=True)
        (self.trajdir / "evidence_index.json").write_text(json.dumps({
            "aaaa#001": {"role": "user", "text": "make it a desktop app",
                         "date": "2026-08-01", "session_id": "aaaa"},
            "aaaa#002": {"role": "assistant", "text": "sure", "date": "2026-08-01"},
            "bbbb#000": {"role": "user", "text": "unrelated question",
                         "date": "2026-08-02", "session_id": "bbbb"},
        }))

    def _tree(self, **extra):
        goal = {"id": "g1", "title": "Capture interactions", "status": "active",
                "parent_goal_id": None, "todos": [], "important_item_ids": [],
                "evidence_ids": ["aaaa#001", "aaaa#002"]}
        goal.update(extra)
        return self.GM.sanitize({"version": 1, "goals": [goal]})

    def test_user_turns_become_assignable_prompts(self):
        prompts = self.GM.evidence_prompts(self.trajdir)
        self.assertEqual(["aaaa#001", "bbbb#000"], [p["id"] for p in prompts])
        self.assertTrue(all(p["role"] == "user" for p in prompts))
        self.assertEqual([1, 2], [p["ordinal"] for p in prompts])   # oldest first

    def test_saving_tags_a_goal_with_its_own_user_turns(self):
        self.GM.save(self.trajdir, self._tree(), {"items": []})
        goal = self.GM.load(self.trajdir)[0]["goals"][0]
        self.assertEqual(["aaaa#001"], goal["prompt_ids"])   # not the assistant turn
        self.assertEqual(["aaaa#001"], goal["auto_prompt_ids"])

    def test_a_detached_tag_is_not_reapplied(self):
        self.GM.save(self.trajdir, self._tree(detached_prompt_ids=["aaaa#001"]),
                     {"items": []})
        self.assertEqual([], self.GM.load(self.trajdir)[0]["goals"][0]["prompt_ids"])

    def test_missing_evidence_index_is_not_an_error(self):
        (self.trajdir / "evidence_index.json").unlink()
        self.GM.save(self.trajdir, self._tree(), {"items": []})
        self.assertEqual([], self.GM.load(self.trajdir)[0]["goals"][0]["prompt_ids"])

    def test_todo_rows_persist_in_todos_json_not_in_goals_json(self):
        # The rail's rows have their own file in the global scope too:
        # goals.json (where the notes live) is written without them, and the
        # load lays them back over the same goal.
        rows = [{"id": "t0000000a", "text": "ship it", "depth": 0,
                 "status": "done", "question": ""}]
        self.GM.save(self.trajdir,
                     self._tree(todo_items=[dict(r) for r in rows]),
                     {"items": []})
        stored = json.loads((self.trajdir / "goals.json").read_text())
        self.assertEqual([], stored["goals"][0]["todo_items"])
        held = json.loads((self.trajdir / "todos.json").read_text())
        self.assertEqual({"g1": rows}, held["todos"])
        self.assertEqual(rows, self.GM.load(self.trajdir)[0]["goals"][0]
                         ["todo_items"])

    def test_inline_rows_from_before_the_split_still_load(self):
        (self.trajdir / "goals.json").write_text(json.dumps(
            {"version": 1, "goals": [{"id": "g1", "title": "Old store",
                                      "todo_items": [{"id": "t0000000a",
                                                      "text": "ship it",
                                                      "depth": 0,
                                                      "status": "",
                                                      "question": ""}]}]}))
        goal = self.GM.load(self.trajdir)[0]["goals"][0]
        self.assertEqual("ship it", goal["todo_items"][0]["text"])


class PromotionTests(unittest.TestCase):
    """Todos are goals: one node type at every depth."""

    def tree(self, todos):
        return {"version": 1, "goals": [
            GM.new_goal("g1", "Parent", None, todos=todos)]}

    def test_a_todo_becomes_a_child_goal(self):
        goals = GM.sanitize(self.tree([
            {"text": "first action", "done": False, "evidence_ids": ["a#1"]},
            {"text": "second action", "done": True, "evidence_ids": []}]))
        kids = [g for g in goals["goals"] if g["parent_goal_id"] == "g1"]
        self.assertEqual(["first action", "second action"],
                         [g["title"] for g in kids])
        self.assertEqual(["active", "completed"], [g["status"] for g in kids])
        self.assertEqual(["a#1"], kids[0]["evidence_ids"])
        # A goal in full: it can hold everything its parent can.
        for key in ("description", "notes", "priority", "prompt_ids",
                    "auto_prompt_ids", "detached_prompt_ids"):
            self.assertIn(key, kids[0])
        self.assertEqual([], goals["goals"][0]["todos"])

    def test_promotion_is_idempotent(self):
        goals = GM.sanitize(self.tree([{"text": "only once", "done": False}]))
        before = [g["id"] for g in goals["goals"]]
        GM.sanitize(goals)
        GM.sanitize(goals)
        self.assertEqual(before, [g["id"] for g in goals["goals"]])

    def test_reemitted_inference_folds_into_the_promoted_node(self):
        goals = GM.sanitize(self.tree([
            {"text": "keep me", "done": False, "evidence_ids": ["a#1"]}]))
        child = [g for g in goals["goals"] if g["parent_goal_id"] == "g1"][0]
        child["description"] = "user wrote this"
        # The analyzer emits the same todo again, now finished.
        goals["goals"][0]["todos"] = [
            {"text": "keep me", "done": True, "evidence_ids": ["a#2"]}]
        GM.sanitize(goals)
        kids = [g for g in goals["goals"] if g["parent_goal_id"] == "g1"]
        self.assertEqual(1, len(kids), "must not duplicate the node")
        self.assertEqual("user wrote this", kids[0]["description"])
        self.assertEqual(["a#1", "a#2"], kids[0]["evidence_ids"])
        self.assertEqual("completed", kids[0]["status"])

    def test_a_promoted_leaf_sits_within_the_depth_limit(self):
        goals = {"version": 1, "goals": [
            GM.new_goal("g1", "Top"),
            GM.new_goal("g1a", "Sub", "g1"),
            GM.new_goal("g1a1", "Sub sub", "g1a", todos=[{"text": "leaf action"}]),
        ]}
        GM.sanitize(goals)
        leaf = next(g for g in goals["goals"] if g["title"] == "leaf action")
        self.assertEqual(4, GM.depth(goals, leaf["id"]))
        self.assertEqual("g1a1", leaf["parent_goal_id"])   # not reparented away

    def test_add_todo_op_creates_a_goal(self):
        goals = GM.sanitize(self.tree([]))
        changes = GM.apply_ops(goals, {"items": []},
                               [{"op": "add_todo", "goal_id": "g1",
                                 "text": "new action", "evidence_ids": ["a#3"]}])
        child = [g for g in goals["goals"] if g["parent_goal_id"] == "g1"][0]
        self.assertEqual("new action", child["title"])
        self.assertEqual(["a#3"], child["evidence_ids"])
        self.assertTrue(changes)

    def test_complete_todo_op_completes_the_child_goal(self):
        goals = GM.sanitize(self.tree([{"text": "finish the audit", "done": False}]))
        GM.apply_ops(goals, {"items": []},
                     [{"op": "complete_todo", "goal_id": "g1",
                       "text_match": "finish audit"}])
        child = [g for g in goals["goals"] if g["parent_goal_id"] == "g1"][0]
        self.assertEqual("completed", child["status"])


class AutomaticDescriptionTests(unittest.TestCase):
    """A tree built from scratch arrives described, with no manual step."""

    def setUp(self):
        import json
        from human_compact.trajectory import goal_synth as GS
        self.GS = GS
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name)
        (self.trajdir / "evidence_index.json").write_text(json.dumps(
            {"a#1": {"role": "user", "text": "make capture work on mac"}}))
        self.goals = {"goals": [
            {"id": "g1", "title": "Blank", "description": "",
             "evidence_ids": ["a#1"], "todos": []}]}

    def _provider(self, payload):
        class Stub:
            def generate_json(_, prompt):
                return payload
        return Stub()

    def test_it_writes_the_descriptions_onto_the_tree(self):
        written = self.GS.backfill_descriptions(
            self._provider({"descriptions": {"g1": "Capture on macOS."}}),
            self.trajdir, self.goals)
        self.assertEqual({"g1": "Capture on macOS."}, written)
        self.assertEqual("Capture on macOS.",
                         self.goals["goals"][0]["description"])

    def test_a_fully_described_tree_never_calls_the_provider(self):
        class Boom:
            def generate_json(self, prompt):
                raise AssertionError("should not call the provider")
        self.goals["goals"][0]["description"] = "already set"
        self.assertEqual({}, self.GS.backfill_descriptions(
            Boom(), self.trajdir, self.goals))

    def test_a_missing_evidence_index_is_not_fatal(self):
        (self.trajdir / "evidence_index.json").unlink()
        self.assertEqual({}, self.GS.backfill_descriptions(
            self._provider({"descriptions": {"g1": "x"}}),
            self.trajdir, self.goals))


class InferredProjectDirectoryTests(unittest.TestCase):
    """CODE CONTEXT starts from the directory the goal's own turns came from."""

    def setUp(self):
        import json
        from human_compact.trajectory import worker as W
        self.W = W
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trajdir = Path(self.tmp.name) / "traj"
        self.trajdir.mkdir()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.trajdir / "evidence_index.json").write_text(json.dumps(
            {"a#1": {"role": "user", "text": "hi", "cwd": str(self.project)}}))
        self.goals = {"goals": [
            {"id": "g1", "title": "Work", "evidence_ids": ["a#1"]}]}

    def test_the_goals_own_directory_is_attached(self):
        self.assertEqual(1, self.W.attach_project_dirs(self.trajdir, self.goals))
        self.assertEqual([{"id": "s1", "type": "local",
                           "label": str(self.project)}],
                         self.goals["goals"][0]["sources"])

    def test_a_source_the_user_chose_is_never_replaced(self):
        mine = [{"id": "s1", "type": "github", "label": "octo/repo"}]
        self.goals["goals"][0]["sources"] = mine
        self.assertEqual(0, self.W.attach_project_dirs(self.trajdir, self.goals))
        self.assertEqual(mine, self.goals["goals"][0]["sources"])

    def test_a_directory_that_no_longer_exists_is_not_attached(self):
        import shutil
        shutil.rmtree(self.project)
        self.assertEqual(0, self.W.attach_project_dirs(self.trajdir, self.goals))
        self.assertNotIn("sources", self.goals["goals"][0])


class GoalDocumentTests(unittest.TestCase):
    """A goal's notes field is one markdown document with H1 sections."""

    def test_default_doc_is_empty_and_the_sections_are_still_named(self):
        # No spine of empty headings: a goal opens as a blank document, and a
        # heading appears with the first thing written under it. The section
        # names remain the ones inference addresses.
        self.assertEqual("", GM.default_doc())
        self.assertEqual(list(GM.DOC_SECTIONS), list(GM.SECTION_KEYS.values()))

    def test_sanitize_lifts_the_prompt_but_never_touches_todos_in_the_notes(self):
        # The prompt still moves to its own field once. A "# TODOs" heading
        # in the notes is the reader's own writing now: it is never read as
        # the rail's list and never deleted for it -- the list is its own
        # store, decoupled from the document entirely.
        notes = ("# Objective\n\n# TODOs\n- one\n    - two\n\n# In my words\n\n"
                 "# Decisions\n- keep sqlite\n\n# Prompt\nmy words\n")
        goals = {"version": 1, "goals": [dict(goal("g1"), notes=notes)]}
        GM.sanitize(goals)
        g = goals["goals"][0]
        self.assertEqual("", g["todos_md"])
        self.assertEqual([], g["todo_items"])
        self.assertEqual("my words\n", g["prompt_md"])
        self.assertEqual("# TODOs\n- one\n    - two\n\n"
                         "# Decisions\n- keep sqlite\n", g["notes"])
        # And a second pass changes nothing.
        GM.sanitize(goals)
        self.assertEqual("", goals["goals"][0]["todos_md"])
        self.assertEqual("# TODOs\n- one\n    - two\n\n"
                         "# Decisions\n- keep sqlite\n",
                         goals["goals"][0]["notes"])

    def test_deleting_in_the_notes_never_deletes_a_todo(self):
        goals = {"version": 1, "goals": [dict(
            goal("g1"), notes="# Decisions\n- keep sqlite\n",
            todos_md="- ship it\n")]}
        GM.sanitize(goals)
        goals["goals"][0]["notes"] = ""
        GM.sanitize(goals)
        self.assertEqual("- ship it\n", goals["goals"][0]["todos_md"])

    def test_todos_is_not_a_section_inference_can_write_to(self):
        # The rail's list is its own store; if "todos" ever re-enters the
        # section map, inference regains a path into the notes document.
        self.assertNotIn("todos", GM.SECTION_KEYS)
        self.assertNotIn("TODOs", GM.DOC_SECTIONS)

    def test_split_and_join_round_trip_preamble_and_unknown_sections(self):
        doc = ("scratch line\n\n# Objective\nShip it\n\n"
               "# Decisions\n- keep sqlite\n\n# Scratch\nkeep me\n")

        parts = GM.split_doc(doc)

        self.assertEqual(["", "Objective", "Decisions", "Scratch"],
                         [title for title, _ in parts])
        self.assertEqual("scratch line", parts[0][1])
        self.assertEqual("- keep sqlite", GM.section_body(doc, "Decisions"))
        self.assertEqual(doc, GM.join_doc(parts))

    def test_an_h2_stays_inside_the_body_of_its_h1(self):
        parts = GM.split_doc("# Built\n## Done\n- a\n")

        self.assertEqual(["Built"], [title for title, _ in parts])
        self.assertEqual("## Done\n- a", parts[0][1])

    def test_ensure_doc_sections_adds_nothing(self):
        once = GM.ensure_doc_sections("# Decisions\n- keep sqlite\n")

        self.assertEqual("# Decisions\n- keep sqlite\n", once)
        self.assertEqual(once, GM.ensure_doc_sections(once))
        self.assertEqual("", GM.ensure_doc_sections(""))

    def test_append_to_section_lands_at_the_end_and_spares_its_neighbours(self):
        start = GM.ensure_doc_sections(
            "# Decisions\n- keep sqlite\n\n# Blockers\n- no ci yet\n")

        out = GM.append_to_section(start, "Decisions", "- use WAL")

        self.assertEqual("- keep sqlite\n\n- use WAL",
                         GM.section_body(out, "Decisions"))
        self.assertEqual(GM.section_body(start, "Blockers"),
                         GM.section_body(out, "Blockers"))
        self.assertLess(out.index("- keep sqlite"), out.index("- use WAL"))

    def test_append_to_section_refuses_to_repeat_a_line_it_already_holds(self):
        once = GM.append_to_section(GM.default_doc(), "Built", "- the doc model")
        twice = GM.append_to_section(once, "Built", "- the doc model")

        self.assertEqual(once, twice)
        self.assertEqual(1, once.count("- the doc model"))

    def test_append_to_section_creates_the_section_it_needs(self):
        out = GM.append_to_section("", "Open questions", "- who owns the cap?")

        self.assertEqual("# Open questions\n- who owns the cap?\n", out)
        self.assertNotIn("# Objective", out)

    def test_sanitize_leaves_a_long_document_uncut(self):
        document = "# Decisions\n" + "\n".join(f"- decision {n}" for n in range(900))
        goals = {"version": 1, "goals": [dict(goal("g1"), notes=document)]}

        GM.sanitize(goals)

        self.assertEqual(document, goals["goals"][0]["notes"])
        self.assertGreater(len(document), 4000)

    def test_an_empty_or_unnamed_append_section_op_writes_nothing(self):
        goals = {"version": 1, "goals": [dict(goal("g1"), notes="mine")]}

        changes = GM.apply_ops(goals, {"items": []}, [
            {"op": "append_section", "goal_id": "g1", "section": "decisions",
             "text": "   "},
            {"op": "append_section", "goal_id": "g1", "section": "nonsense",
             "text": "- smuggled"},
        ])

        self.assertEqual([], changes)
        self.assertEqual("mine", goals["goals"][0]["notes"])

    def test_append_section_op_adds_once_and_reports_only_real_writes(self):
        goals = {"version": 1, "goals": [
            dict(goal("g1"), notes="# Decisions\n- keep sqlite\n")]}
        op = {"op": "append_section", "goal_id": "g1", "section": "decisions",
              "text": "- use WAL"}

        first = GM.apply_ops(goals, {"items": []}, [op])
        notes = goals["goals"][0]["notes"]
        second = GM.apply_ops(goals, {"items": []}, [op])

        self.assertEqual(1, len(first))
        self.assertEqual([], second)
        self.assertIn("# Decisions\n- keep sqlite\n\n- use WAL", notes)
        self.assertEqual(notes, goals["goals"][0]["notes"])

    def test_a_heading_inside_a_fenced_block_is_body_not_a_section(self):
        doc = ("# Decisions\n- run this:\n\n```bash\n# install deps\n"
               "npm i\n```\n")

        parts = GM.split_doc(doc)

        self.assertEqual(["Decisions"], [title for title, _ in parts])
        self.assertEqual("- run this:\n\n```bash\n# install deps\nnpm i\n```",
                         parts[0][1])

        out = GM.append_to_section(doc, "Built", "- the thing")

        self.assertEqual(GM.section_body(doc, "Decisions"),
                         GM.section_body(out, "Decisions"))
        self.assertIn("```bash\n# install deps\nnpm i\n```", out)
        self.assertEqual(2, out.count("```"))
        self.assertIn("# Built\n- the thing", out)

    def test_a_tilde_fence_hides_a_heading_the_same_way(self):
        doc = "# Built\n~~~\n# not a header\n~~~\n\n# Blockers\n- none\n"

        parts = GM.split_doc(doc)

        self.assertEqual(["Built", "Blockers"], [title for title, _ in parts])
        self.assertIn("# not a header", GM.section_body(doc, "Built"))
        self.assertEqual(doc, GM.join_doc(parts))

    def test_strip_empty_spine_keeps_written_and_foreign_headings(self):
        doc = ("# Blockers\n\n# Objective\nShip it\n\n# Scratch\n\n"
               "# Built\n")

        out = GM.strip_empty_spine(doc)

        # Blockers and Built were empty spine headings; Scratch is not the
        # spine's and stays however empty; Objective has text.
        self.assertEqual(["Objective", "Scratch"],
                         [title for title, _ in GM.split_doc(out)])
        self.assertEqual(out, GM.strip_empty_spine(out))
        self.assertEqual("", GM.strip_empty_spine("# Objective\n\n# Built\n"))

    def test_a_repeated_heading_survives_and_the_first_takes_the_append(self):
        doc = ("# Decisions\n- keep sqlite\n\n# Built\n- the parser\n\n"
               "# Decisions\n- a later thought\n")

        self.assertEqual(["Decisions", "Built", "Decisions"],
                         [title for title, _ in GM.split_doc(doc)])
        self.assertEqual(doc, GM.join_doc(GM.split_doc(doc)))

        out = GM.append_to_section(doc, "Decisions", "- use WAL")

        self.assertIn("# Decisions\n- keep sqlite\n\n- use WAL", out)
        self.assertIn("# Decisions\n- a later thought", out)
        self.assertEqual(2, out.count("# Decisions"))

    def test_a_fence_the_user_has_not_closed_freezes_the_document(self):
        doc = "# Decisions\n- keep sqlite\n\n```bash\nnpm i\n"

        self.assertEqual(doc, GM.ensure_doc_sections(doc))
        self.assertEqual(doc, GM.append_to_section(doc, "Decisions", "- use WAL"))
        self.assertEqual(doc, GM.append_to_section(doc, "Built", "- the thing"))
        self.assertEqual(doc, GM.join_doc(GM.split_doc(doc)))

    def test_repeated_appends_into_an_open_fence_never_grow_the_document(self):
        doc = "# Decisions\n- keep sqlite\n\n```bash\nnpm i\n"
        goals = {"version": 1, "goals": [dict(goal("g1"), notes=doc)]}
        op = {"op": "append_section", "goal_id": "g1", "section": "decisions",
              "text": "- use WAL"}

        for _ in range(4):
            self.assertEqual([], GM.apply_ops(goals, {"items": []}, [op]))
            self.assertEqual(doc, goals["goals"][0]["notes"])

    def test_closing_the_fence_lets_the_next_append_land_exactly_once(self):
        goals = {"version": 1, "goals": [dict(
            goal("g1"), notes="# Decisions\n- keep sqlite\n\n```bash\nnpm i\n")]}
        op = {"op": "append_section", "goal_id": "g1", "section": "decisions",
              "text": "- use WAL"}
        GM.apply_ops(goals, {"items": []}, [op])

        goals["goals"][0]["notes"] += "```\n"       # the user closes it
        changes = GM.apply_ops(goals, {"items": []}, [op])

        notes = goals["goals"][0]["notes"]
        self.assertEqual(1, len(changes))
        self.assertEqual(1, notes.count("- use WAL"))
        self.assertIn("```bash\nnpm i\n```\n\n- use WAL", notes)
        self.assertEqual("- keep sqlite\n\n```bash\nnpm i\n```\n\n- use WAL",
                         GM.section_body(notes, "Decisions"))
        self.assertEqual([], GM.apply_ops(goals, {"items": []}, [op]))
