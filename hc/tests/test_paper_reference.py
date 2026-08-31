"""The canonical paper reference from web setup must reach a goal's Paper tab.

These cover the hc half of the path: the setup payload's ``paper`` -> the chosen
goal's ``paper`` (with its canonical id) -> a saved-goal round trip that keeps
the id. The Paper tab then has the id it needs to ask the backend for a signed
URL. A goal never keeps a signed URL or a storage path -- only the id.
"""

import re

import pytest

from human_compact.trajectory import goals as GM
from human_compact.trajectory import setup_chat as SETUP
from human_compact.trajectory import supabase_client as SB

PID = "11111111-2222-3333-4444-555555555555"
_UUID = re.compile(r"^[0-9a-f-]{36}$")


def test_normalize_paper_keeps_a_valid_canonical_id():
    p = GM.normalize_paper({"paper_id": PID, "title": "T", "url": "https://doi/x"})
    assert p["paper_id"] == PID
    assert p["title"] == "T"
    assert p["url"] == "https://doi/x"
    assert p["pdf"] == ""


def test_normalize_paper_rejects_a_bad_id_and_bad_url():
    p = GM.normalize_paper({"paper_id": "nope", "url": "javascript:x"})
    assert p["paper_id"] == ""
    assert p["url"] == ""


def test_normalize_paper_default_shape_has_the_id_field():
    assert GM.normalize_paper(None) == {
        "title": "", "url": "", "pdf": "", "paper_id": ""}


def test_new_goal_seeds_an_empty_paper_id():
    g = GM.new_goal("g1", "Title")
    assert g["paper"]["paper_id"] == ""


def test_sanitize_preserves_the_paper_id_on_a_goal():
    # sanitize() is what commit() runs before the tree is saved; it must keep
    # the canonical id (this is the goal-store round trip).
    doc = {"goals": [GM.new_goal("g1", "Reading")]}
    doc["goals"][0]["paper"] = {
        "paper_id": PID, "title": "T", "url": "https://doi/x",
        "pdf": "", "signedUrl": "https://leak/should-be-dropped"}
    GM.sanitize(doc)
    kept = doc["goals"][0]["paper"]
    assert kept["paper_id"] == PID
    assert "signedUrl" not in kept       # only the id/title/url/pdf survive


def test_to_goals_attaches_the_paper_to_the_chosen_goal():
    goals = SETUP.to_goals(
        offered=[{"label": "Study drift", "why": "w"},
                 {"label": "Other", "why": "w2"}],
        chosen="Study drift",
        todos=[],
        subgoals=[],
        paper={"paper_id": PID, "title": "A Paper", "url": "https://doi/x"})
    picked = [g for g in goals if g["title"] == "Study drift"][0]
    other = [g for g in goals if g["title"] == "Other"][0]
    assert picked["paper"]["paper_id"] == PID
    # the paper rides ONLY the chosen goal, never the ones left unstarted
    assert other["paper"]["paper_id"] == ""


def test_to_goals_ignores_a_paper_with_no_valid_id():
    goals = SETUP.to_goals(
        offered=[{"label": "Study drift", "why": "w"}],
        chosen="Study drift", todos=[], subgoals=[],
        paper={"paper_id": "bad", "title": "x"})
    picked = goals[0]
    assert picked["paper"]["paper_id"] == ""


def test_to_goals_without_a_paper_leaves_the_default_empty():
    goals = SETUP.to_goals(
        offered=[{"label": "Study drift", "why": "w"}],
        chosen="Study drift", todos=[], subgoals=[])
    assert goals[0]["paper"]["paper_id"] == ""


# --- the local -> backend proxy that mints the signed URL --------------------

def test_engelbart_paper_pdf_sends_only_the_id_and_token(monkeypatch):
    # Proves the machine sends the paper's id + its own session token, and gets
    # the momentary signed URL back -- it never originates or stores a path.
    seen = {}
    monkeypatch.setattr(SB, "engelbart_credentials",
                        lambda: {"apiBase": "https://app.example/", "token": "eng"})
    monkeypatch.setattr(SB, "current_session",
                        lambda root=None: {"access_token": "jwt-abc"})

    def fake_post(url, headers, body, where="rpc"):
        seen.update(url=url, headers=headers, body=body)
        return {"available": True,
                "signedUrl": "https://store.example/signed?token=zzz", "title": "T"}

    monkeypatch.setattr(SB, "_post", fake_post)
    out = SB.engelbart_paper_pdf(PID)
    assert seen["url"] == "https://app.example/api/engelbart-paper"
    assert seen["headers"]["Authorization"] == "Bearer jwt-abc"
    assert seen["body"] == {"action": "pdf_url", "paperId": PID}
    assert out["available"] is True and out["signedUrl"].startswith("https://store.example/")


def test_engelbart_paper_pdf_needs_a_connected_machine(monkeypatch):
    monkeypatch.setattr(SB, "engelbart_credentials", lambda: {})
    with pytest.raises(SB.SupabaseError):
        SB.engelbart_paper_pdf(PID)


def test_engelbart_paper_pdf_rejects_an_empty_id():
    with pytest.raises(SB.SupabaseError):
        SB.engelbart_paper_pdf("")
