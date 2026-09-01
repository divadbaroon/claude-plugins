"""The build feed behind the trajectory map's build nodes.

Builds never enter the vault (a build is not the reader's chat), so the map
cannot learn of them through extraction. Instead every finished build leaves
a line in the vault's builds.json (build._note_build) and the map's server
folds those lines into /api/graph as merged nodes (build_activity_nodes).
"""
import json

import pytest

from human_compact.trajectory import build as B
from human_compact.trajectory import discover as D
from human_compact.trajectory.graph_build import build_activity_nodes


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "VAULT", tmp_path)
    return tmp_path


GOALS = {"goals": [{"id": "g1", "title": "  Snappy   preview  ", "todo_items": [
    {"id": "r1", "text": "make the spinner blue", "status": "done"},
    {"id": "r2", "text": "untouched row", "status": "pending"},
    {"id": "r3", "text": "row another build did", "status": "done"},
]}]}


@pytest.fixture()
def goals(monkeypatch):
    monkeypatch.setattr(B.CS, "load_goals", lambda sid, root: (GOALS, {}))


def feed(vault):
    return json.loads((vault / "trajectory" / "builds.json").read_text())


def test_no_trajectory_dir_writes_nothing(vault, goals):
    B._note_build("s", None, "g1", ["r1"], True)
    assert not (vault / "trajectory").exists()


def test_finished_build_leaves_one_line(vault, goals):
    (vault / "trajectory").mkdir()
    B._note_build("s", None, "g1", ["r1", "r2"], True)
    recs = feed(vault)
    assert len(recs) == 1
    assert recs[0]["goal"] == "Snappy preview"
    assert recs[0]["quick"] is True
    # r2 was picked but never finished; r3 finished but was not this build's
    assert recs[0]["rows"] == ["make the spinner blue"]
    assert len(recs[0]["date"]) == 10


def test_build_that_finished_no_rows_leaves_nothing(vault, goals):
    (vault / "trajectory").mkdir()
    B._note_build("s", None, "g1", ["r2"], False)
    assert not (vault / "trajectory" / "builds.json").exists()
    B._note_build("s", None, "missing-goal", ["r1"], False)
    assert not (vault / "trajectory" / "builds.json").exists()


def test_lines_accumulate_and_nodes_merge_per_goal(vault, goals, tmp_path):
    (vault / "trajectory").mkdir()
    B._note_build("s", None, "g1", ["r1"], True)
    B._note_build("s", None, "g1", ["r3"], False)
    assert len(feed(vault)) == 2
    nodes = build_activity_nodes(vault / "trajectory")
    assert len(nodes) == 1
    n = nodes[0]
    assert n["label"] == "built: Snappy preview"
    assert n["type"] == "action" and n["weight"] == 2
    assert n["id"] == "b:0" and n["evidence_ids"] == []


def test_nodes_survive_a_garbage_feed(tmp_path):
    assert build_activity_nodes(tmp_path) == []
    (tmp_path / "builds.json").write_text("{not json")
    assert build_activity_nodes(tmp_path) == []
    (tmp_path / "builds.json").write_text(json.dumps(
        ["not-a-dict", {"goal": "", "date": "2026-09-01"},
         {"goal": "x", "date": "bad-date"}]))
    assert build_activity_nodes(tmp_path) == []


def test_goals_merge_case_insensitively_with_date_span(tmp_path):
    (tmp_path / "builds.json").write_text(json.dumps([
        {"date": "2026-08-30", "goal": "Snappy preview", "quick": True},
        {"date": "2026-09-01", "goal": "snappy preview", "quick": True},
        {"date": "2026-09-01", "goal": "Windows port", "quick": False},
    ]))
    nodes = build_activity_nodes(tmp_path)
    by = {n["label"]: n for n in nodes}
    assert set(by) == {"built: Snappy preview", "built: Windows port"}
    sp = by["built: Snappy preview"]
    assert sp["date_first"] == "2026-08-30" and sp["date"] == "2026-09-01"
    assert sp["weight"] == 2
