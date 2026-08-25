#!/usr/bin/env python3
"""A second contributor pushes their own goals into a shared project.

Before the scoped-prune migration this would have deleted every one of the
owner's goals: hc_sync_project pruned whatever its payload lacked, and a
collaborator's payload lacks all of it. Run it and see that it does not.

    python3 supabase/check_editor_push.py
"""
import getpass
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hc" / "src"))
from human_compact.trajectory import supabase_client as SB  # noqa: E402

PROJECT = "84aabd29-560e-49fc-a05b-a8fd45f9ecf0"
SESSION = "hudson-demo-chat"


def call(cfg, path, token, method="POST", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(cfg["url"] + path, method=method, data=data)
    r.add_header("apikey", cfg["anon_key"])
    r.add_header("Authorization", "Bearer " + token)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=25) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:220]


def main():
    cfg = SB.load_config()
    email = input("teammate email: ").strip()
    password = getpass.getpass("teammate password (not stored): ")
    st, out = call(cfg, "/auth/v1/token?grant_type=password", cfg["anon_key"],
                   body={"email": email, "password": password})
    if st != 200 or not isinstance(out, dict):
        print("could not sign in:", out)
        return 1
    token, uid = out["access_token"], out["user"]["id"]
    print(f"\nsigned in as {email}\n  user_id {uid}")

    ns = uuid.UUID(PROJECT)
    def u5(*p): return str(uuid.uuid5(ns, "\x1f".join(p)))

    goals = []
    for n, title in ((1, "Hudson: read the shared tree"),
                     (2, "Hudson: try the reader view")):
        goals.append({
            "id": u5("goal", SESSION, f"h{n}"), "session_id": SESSION,
            "local_id": f"h{n}", "parent_id": None, "title": title,
            "status": "active", "priority": "normal", "origin": "user",
            "description": "", "notes": "added by the second contributor",
            "prompt": "", "evidence_ids": [], "important": [],
            "updated_at": "2026-08-21T22:00:00+00:00"})
    payload = {
        "schema_version": 1, "generated_at": "2026-08-21T22:00:00+00:00",
        "project_id": PROJECT, "user_id": uid,
        # Deliberately names the project: an editor's push must NOT be able
        # to rewrite the owner's objective.
        "projects": [{"id": PROJECT, "cwd": "/hudson/elsewhere",
                      "name": "HUDSON-RENAMED-THIS", "objective": "hijacked",
                      "description": "", "generated_at": None}],
        "project_sources": [], "chats": [{
            "id": u5("chat", SESSION), "session_id": SESSION,
            "created_at": None, "updated_at": None,
            "prompt_count": 0, "goal_count": len(goals)}],
        "goals": goals,
        "todos": [{"id": u5("todo", SESSION, "h1", "t1"),
                   "goal_id": goals[0]["id"], "local_id": "trow0001",
                   "position": 0, "depth": 0,
                   "text": "look at what David shared", "status": "",
                   "question": ""}],
        "goal_sources": [], "related_prompts": []}

    st, res = call(cfg, "/rest/v1/rpc/hc_sync_project", token,
                   body={"payload": payload})
    print("\npush result        :", st, json.dumps(res) if isinstance(res, dict) else res)

    _, projects = call(cfg, "/rest/v1/hc_projects?select=id,name,objective",
                       token, "GET")
    print("project row now    :", projects)
    _, goals_seen = call(cfg, "/rest/v1/hc_goals?select=id&limit=500", token, "GET")
    print("goals he can see   :", len(goals_seen) if isinstance(goals_seen, list) else goals_seen)
    print("\n(no session written; this script stores nothing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
