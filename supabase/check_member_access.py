#!/usr/bin/env python3
"""Prove what a second account can and cannot see.

Run it yourself: the teammate's password is typed here and goes nowhere but
the one request that exchanges it for a token. Nothing is written to disk.

    python3 supabase/check_member_access.py
"""
import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hc" / "src"))
from human_compact.trajectory import supabase_client as SB  # noqa: E402

OWNER_PROJECT = None      # discovered below


def call(cfg, path, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(cfg["url"] + path, method=method, data=data)
    r.add_header("apikey", cfg["anon_key"])
    r.add_header("Authorization", "Bearer " + token)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:160]


def main():
    cfg = SB.load_config()
    email = input("teammate email: ").strip()
    password = getpass.getpass("teammate password (not stored): ")

    status, out = call(cfg, "/auth/v1/token?grant_type=password",
                       cfg["anon_key"], "POST",
                       {"email": email, "password": password})
    if status != 200 or not isinstance(out, dict) or not out.get("access_token"):
        print("could not sign in as the teammate:", out)
        return 1
    token = out["access_token"]
    who = (out.get("user") or {}).get("id")
    print(f"\nsigned in as {email}\n  user_id {who}\n")

    _, projects = call(cfg, "/rest/v1/hc_projects?select=id,name", token)
    print("projects visible   :", projects)
    _, goals = call(cfg, "/rest/v1/hc_goals?select=id,title&limit=100", token)
    print("goals visible      :", len(goals) if isinstance(goals, list) else goals)
    _, todos = call(cfg, "/rest/v1/hc_todos?select=id&limit=200", token)
    print("todo rows visible  :", len(todos) if isinstance(todos, list) else todos)

    if isinstance(goals, list) and goals:
        gid = goals[0]["id"]
        # The check that matters: reads widened, writes did not.
        st, res = call(cfg, f"/rest/v1/hc_goals?id=eq.{gid}", token, "DELETE")
        _, after = call(cfg, f"/rest/v1/hc_goals?select=id&id=eq.{gid}", token)
        survived = isinstance(after, list) and len(after) == 1
        print(f"delete a goal      : HTTP {st} -> row "
              f"{'SURVIVED (correct)' if survived else 'GONE — BAD'}")
        st, _ = call(cfg, f"/rest/v1/hc_goals?id=eq.{gid}", token, "PATCH",
                     {"title": "tampered"})
        _, back = call(cfg, f"/rest/v1/hc_goals?select=title&id=eq.{gid}", token)
        title = back[0]["title"] if isinstance(back, list) and back else "?"
        print(f"rename a goal      : HTTP {st} -> title is now {title!r}")

    st, res = call(cfg, "/rest/v1/rpc/hc_list_members", token, "POST",
                   {"project_id": (projects[0]["id"] if isinstance(projects, list)
                                   and projects else OWNER_PROJECT)})
    print("read the roll      :", st, str(res)[:90])
    print("\n(no session was written; this script stores nothing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
