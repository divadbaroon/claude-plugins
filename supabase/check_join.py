#!/usr/bin/env python3
"""Redeem an invitation as the person it was sent to.

The code is the whole of what they need: it carries the project's address
and public key as well as the token, so it works on a machine that has
never been configured. What it cannot supply is an account -- an invitation
is joined to one, which is the point of it.

    python3 supabase/check_join.py
"""
import getpass, json, sys, urllib.error, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hc" / "src"))
from human_compact.trajectory import supabase_client as SB  # noqa: E402

CODE = "hcjoin1_eyJ1IjoiaHR0cHM6Ly90eW5wcXhlcHV5eXZ4cWR3emhrai5zdXBhYmFzZS5jbyIsImsiOiJleUpoYkdjaU9pSklVekkxTmlJc0luUjVjQ0k2SWtwWFZDSjkuZXlKcGMzTWlPaUp6ZFhCaFltRnpaU0lzSW5KbFppSTZJblI1Ym5CeGVHVndkWGw1ZG5oeFpIZDZhR3RxSWl3aWNtOXNaU0k2SW1GdWIyNGlMQ0pwWVhRaU9qRTNPRGN5T0RNM09EZ3NJbVY0Y0NJNk1qRXdNamcxT1RjNE9IMC5RRnhCcWRTWUFpSXlLdU4wVklDTldXX1gzWF9KcTV5RUllbFprQUpSYk5rIiwidCI6Imhjc184NGU3MWI0NGNkYWYxN2M0YzQ4NWJjNTE4ZjFiZjM2YTliMjNiMzk4MjE3YTg1YmVmZmJlMzY0YTk3MjJlNDE0In0"


def main():
    parts = SB.parse_code(CODE)
    print("the code points at :", parts["url"])
    email = input("their email: ").strip()
    password = getpass.getpass("their password (not stored): ")

    def call(path, token, body):
        r = urllib.request.Request(parts["url"] + path, method="POST",
                                   data=json.dumps(body).encode())
        r.add_header("apikey", parts["anon_key"])
        r.add_header("Authorization", "Bearer " + token)
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=25) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:200]

    st, out = call("/auth/v1/token?grant_type=password", parts["anon_key"],
                   {"email": email, "password": password})
    if st != 200 or not isinstance(out, dict):
        print("could not sign in:", out); return 1
    token = out["access_token"]
    print("signed in as       :", email)

    st, res = call("/rest/v1/rpc/hc_redeem_share", token,
                   {"p_token": parts["token"]})
    print("redeemed           :", st, json.dumps(res) if isinstance(res, dict) else res)

    r = urllib.request.Request(parts["url"]
        + "/rest/v1/hc_goals?select=id,title,user_id&limit=500")
    r.add_header("apikey", parts["anon_key"])
    r.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(r, timeout=25) as x:
        goals = json.loads(x.read().decode())
    mine = sum(1 for g in goals if g["user_id"] == out["user"]["id"])
    print("goals visible      :", len(goals), "(%d his, %d David's)"
          % (mine, len(goals) - mine))
    print("\n(nothing written to disk)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
