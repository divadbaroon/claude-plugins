"""_trust_folder: the build carrying the reader's folder-trust answer.

The function edits ~/.claude.json, so every test points HOME at a pytest
tmp_path first -- the real config must never be touched by a test run.
"""
import json

import pytest

from human_compact.trajectory.build import _trust_folder


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def read(home):
    return json.loads((home / ".claude.json").read_text(encoding="utf-8"))


def test_no_config_is_left_alone(home):
    _trust_folder("/tmp/proj")
    assert not (home / ".claude.json").exists()


def test_new_entry_added_and_other_keys_survive(home):
    (home / ".claude.json").write_text(json.dumps(
        {"userID": "u1",
         "projects": {"/x": {"hasTrustDialogAccepted": False,
                             "allowedTools": ["Bash"]}}}))
    _trust_folder("/tmp/proj")
    got = read(home)
    assert got["projects"]["/tmp/proj"] == {"hasTrustDialogAccepted": True}
    assert got["userID"] == "u1"
    assert got["projects"]["/x"]["allowedTools"] == ["Bash"]


def test_existing_entry_flipped_without_losing_its_keys(home):
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/x": {"hasTrustDialogAccepted": False,
                             "allowedTools": ["Bash"]}}}))
    _trust_folder("/x")
    got = read(home)
    assert got["projects"]["/x"]["hasTrustDialogAccepted"] is True
    assert got["projects"]["/x"]["allowedTools"] == ["Bash"]


def test_already_trusted_writes_nothing(home):
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {"/x": {"hasTrustDialogAccepted": True}}}))
    before = (home / ".claude.json").read_text(encoding="utf-8")
    _trust_folder("/x")
    assert (home / ".claude.json").read_text(encoding="utf-8") == before


def test_unreadable_config_is_left_alone(home):
    (home / ".claude.json").write_text("{not json")
    _trust_folder("/x")
    assert (home / ".claude.json").read_text(encoding="utf-8") == "{not json"
