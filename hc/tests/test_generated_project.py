"""A generated project must import into the exact shape the workspace expects.

Covers the hc half of Generate Project: the structured pending-setup payload ->
phase-tagged local goals, each carrying its own description, purpose (Why this
matters), and goal-level resource -- a persisted Brainstorm document pointed at
explicitly, and Understand goals bound to canonical papers -- plus structured
provenance on the project record. Legacy payloads still import unchanged.
"""

from human_compact.trajectory import chat_state as CS
from human_compact.trajectory import goals as GM
from human_compact.trajectory import project_store as PS
from human_compact.trajectory import setup_chat as SETUP

PI = "aaaaaaaa-0000-0000-0000-000000000001"
S1 = "aaaaaaaa-0000-0000-0000-000000000002"
PA1 = "bbbbbbbb-0000-0000-0000-000000000001"
PA2 = "bbbbbbbb-0000-0000-0000-000000000002"

OFFERED = [{"label": "My Project", "why": "the objective"}]
SUBGOALS = [
    {"label": "Shape the project", "phase": "brainstorm", "why": "scope it first",
     "description": "decide what to build",
     "document": {"title": "Shaping: My Project", "body_md": "# Shaping\n- what should it do?"},
     "todos": []},
    {"label": "Read “Paper One”", "phase": "understand", "why": "grounds the method",
     "description": "covers the core method",
     "paper": {"paper_id": PA1, "title": "Paper One", "url": "https://doi.org/1"},
     "todos": ["read the intro"]},
    {"label": "Read “Paper Two”", "phase": "understand", "why": "the baseline",
     "description": "covers the comparison",
     "paper": {"paper_id": PA2, "title": "Paper Two", "url": "https://x/2"},
     "todos": ["read the results"]},
    {"label": "Build the prototype", "phase": "implement", "why": "prove it runs",
     "description": "a first cut", "todos": ["scaffold the repo"]},
    {"label": "Package the result", "phase": "apply", "why": "close the loop",
     "description": "share it back", "todos": ["write it up"]},
]
PROV = {
    "interest": "machine learning",
    "lab": {"pi_id": PI, "lab_name": "X Lab"},
    "pi": {"id": PI, "name": "Prof X"},
    "students": [{"id": S1, "name": "Stu One"}],
    "papers": [{"paper_id": PA1, "title": "Paper One"}, {"paper_id": PA2, "title": "Paper Two"}],
    "idea": {"title": "Idea", "inspired": "Paper One"},
}


def _kids(goals, parent_id):
    return [g for g in goals if g.get("parent_goal_id") == parent_id]


def test_to_goals_builds_phase_goals_with_resources():
    goals = SETUP.to_goals(OFFERED, "My Project", [], SUBGOALS)
    top = [g for g in goals if g["title"] == "My Project"][0]
    kids = _kids(goals, top["id"])
    by_phase = lambda ph: [g for g in kids if g.get("phase") == ph]  # noqa: E731

    brain = by_phase("brainstorm")[0]
    assert brain["documents"] and brain["primary_document_id"]
    assert brain["primary_document_id"] == brain["documents"][0]["id"]
    assert "# Shaping" in brain["documents"][0]["body_md"]
    assert brain["relevance_why"] == "scope it first"        # purpose -> Why this matters
    assert brain["description"] == "decide what to build"

    und = by_phase("understand")
    assert len(und) == 2
    assert {g["paper"]["paper_id"] for g in und} == {PA1, PA2}
    assert all(g["description"] for g in und)
    assert all(g["relevance_why"] for g in und)

    assert by_phase("implement")[0]["todo_items"]            # implement keeps its TODOs
    assert by_phase("apply")


def test_sanitize_preserves_document_pointer_and_papers():
    doc = {"goals": SETUP.to_goals(OFFERED, "My Project", [], SUBGOALS)}
    GM.sanitize(doc)
    brain = [g for g in doc["goals"] if g.get("phase") == "brainstorm"][0]
    assert brain["primary_document_id"] == brain["documents"][0]["id"]
    und = [g for g in doc["goals"] if g.get("phase") == "understand"]
    assert {g["paper"]["paper_id"] for g in und} == {PA1, PA2}


def test_commit_round_trip_persists_structure_and_provenance(tmp_path):
    res = SETUP.commit(tmp_path, "My Project", {"description": "the objective\nmore lines"},
                       OFFERED, "My Project", [], SUBGOALS, provenance=PROV)
    assert res.get("ok"), res
    goals, _ = CS.load_goals(res["tree_session"], tmp_path)
    glist = goals["goals"]

    phases = {g.get("phase") for g in glist}
    assert {"brainstorm", "understand", "implement", "apply"} <= phases

    brain = [g for g in glist if g.get("phase") == "brainstorm"][0]
    assert brain["primary_document_id"] == brain["documents"][0]["id"]
    assert "# Shaping" in brain["documents"][0]["body_md"]

    und = [g for g in glist if g.get("phase") == "understand"]
    assert {g["paper"]["paper_id"] for g in und} == {PA1, PA2}

    # provenance persisted on the project record (structured, not prose)
    rec = PS.read_file(tmp_path, res["cwd"])
    prov = rec["project"]["provenance"]
    assert prov["lab"]["pi_id"] == PI
    assert prov["pi"]["name"] == "Prof X"
    assert len(prov["papers"]) == 2
    assert prov["idea"]["inspired"] == "Paper One"


def test_legacy_payload_still_imports(tmp_path):
    res = SETUP.commit(tmp_path, "Legacy", {"description": "obj"},
                       [{"label": "Legacy", "why": "w"}], "Legacy",
                       ["do a thing"], [])
    assert res.get("ok"), res
    goals, _ = CS.load_goals(res["tree_session"], tmp_path)
    g = [x for x in goals["goals"] if x["title"] == "Legacy"][0]
    assert g["phase"] == ""
    assert g["paper"]["paper_id"] == ""
    assert g["primary_document_id"] == ""
    # no provenance on a legacy project record
    rec = PS.read_file(tmp_path, res["cwd"])
    assert "provenance" not in rec["project"]


def test_normalize_provenance_bounds_and_drops_bad_ids():
    prov = PS.normalize_provenance({
        "lab": {"pi_id": PI, "lab_name": "X Lab"},
        "papers": [{"paper_id": PA1, "title": "P"}, {"paper_id": "nope"}],
        "students": [{"id": S1, "name": "Stu"}, {"id": "bad", "name": "No Id"}],
    })
    assert prov["lab"]["pi_id"] == PI
    assert len(prov["papers"]) == 1              # a paper needs a valid id
    assert prov["students"][1]["id"] == ""       # bad id blanked, name kept


def test_normalize_provenance_empty_is_empty():
    assert PS.normalize_provenance({}) == {}
    assert PS.normalize_provenance("x") == {}
    assert PS.normalize_provenance(None) == {}
