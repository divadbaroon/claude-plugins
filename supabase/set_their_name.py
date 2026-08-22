#!/usr/bin/env python3
"""Set another account's display name, from that account.

A profile is written by whoever owns it -- nobody can name anyone else --
so this signs in as them and asks them to choose. The password is typed
here and stored nowhere.

    python3 supabase/set_their_name.py
"""
import getpass
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hc" / "src"))
from human_compact.trajectory import supabase_client as SB  # noqa: E402


def main():
    cfg = SB.load_config()
    email = input("their email: ").strip()
    password = getpass.getpass("their password (not stored): ")
    name = input("what should their goals be signed with? ").strip()

    def call(path, token, body):
        r = urllib.request.Request(cfg["url"] + path, method="POST",
                                   data=json.dumps(body).encode())
        r.add_header("apikey", cfg["anon_key"])
        r.add_header("Authorization", "Bearer " + token)
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:180]

    st, out = call("/auth/v1/token?grant_type=password", cfg["anon_key"],
                   {"email": email, "password": password})
    if st != 200 or not isinstance(out, dict):
        print("could not sign in:", out)
        return 1
    st, res = call("/rest/v1/rpc/hc_set_display_name", out["access_token"],
                   {"p_name": name})
    print("\n", st, res)
    print("(nothing was written to disk)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
