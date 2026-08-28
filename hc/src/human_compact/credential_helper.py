"""Node-free ``apiKeyHelper`` for an Engelbart-managed Claude Code install.

The standalone installer promises an empty machine: it downloads its own
Python runtime as part of installing human-compact.  Claude Code can start
from a GUI session whose PATH contains neither Node nor a login shell, so the
credential helper runs on that managed Python rather than adding a second,
undeclared runtime dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


HELPER_TTL_MS = "900000"
REFUSED = {401, 402, 403, 404, 409}
SPENT = {"exhausted", "revoked", "blocked"}


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _settings(path: Path) -> Optional[Tuple[str, Dict[str, Any]]]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, ValueError):
        return None
    return (raw, value) if isinstance(value, dict) else None


def _unwire_once(settings_file: Path, helper: str, base_url: str) -> bool:
    before = _settings(settings_file)
    if before is None or before[1].get("apiKeyHelper") != helper:
        return False
    raw, current = before
    next_value = dict(current)
    next_value.pop("apiKeyHelper", None)
    held_env = next_value.get("env")
    if isinstance(held_env, dict):
        env = dict(held_env)
        if base_url and env.get("ANTHROPIC_BASE_URL") == base_url:
            env.pop("ANTHROPIC_BASE_URL", None)
        if env.get("CLAUDE_CODE_API_KEY_HELPER_TTL_MS") == HELPER_TTL_MS:
            env.pop("CLAUDE_CODE_API_KEY_HELPER_TTL_MS", None)
        if env:
            next_value["env"] = env
        else:
            next_value.pop("env", None)

    temporary = Path(str(settings_file) + f".engelbart-{os.getpid()}")
    try:
        data = json.dumps(next_value, indent=2) + "\n"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Claude Code may save settings while its helper is running.  Never
        # replace bytes other than the exact version parsed above.
        if settings_file.read_text(encoding="utf-8") != raw:
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, settings_file)
        return True
    except OSError:
        temporary.unlink(missing_ok=True)
        return False


def unwire(settings_file: Path, helper: str, base_url: str,
           attempts: int = 1) -> bool:
    return any(_unwire_once(settings_file, helper, base_url)
               for _ in range(max(1, attempts)))


def disconnected(settings_file: Path, helper: str) -> bool:
    current = _settings(settings_file)
    return current is None or current[1].get("apiKeyHelper") != helper


def disconnect(settings_file: Path, helper: str, base_url: str) -> int:
    if disconnected(settings_file, helper):
        print("Claude Code is already on your own account.")
        return 0
    if unwire(settings_file, helper, base_url, attempts=3):
        print("Claude Code is back on your own account.")
        print("Reconnect with `engelbart auth`.")
        return 0
    print(f"Could not edit {settings_file}.", file=sys.stderr)
    print('Remove "apiKeyHelper" from it by hand to undo this.',
          file=sys.stderr)
    return 1


def _refuse(settings_file: Path, helper: str, base_url: str,
            reason: str) -> int:
    removed = unwire(settings_file, helper, base_url)
    print("Engelbart: " + reason, file=sys.stderr)
    if removed:
        print("Restart Claude Code and it will use your own account again.",
              file=sys.stderr)
        print("This session already loaded the gateway, so it will keep "
              "failing until you do.", file=sys.stderr)
        print("Once your credit is topped up, reconnect with `engelbart auth`.",
              file=sys.stderr)
    else:
        print("To put Claude Code back on your own account, run:\n\n"
              f"    {helper} --disconnect\n\nThen restart Claude Code.",
              file=sys.stderr)
    return 1


def _request(record: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]], str]:
    api_base = str(record.get("apiBase") or "").rstrip("/")
    token = str(record.get("token") or "")
    if not api_base or not token:
        return 0, None, "credentials are incomplete"
    request = urllib.request.Request(
        api_base + "/api/engelbart-credentials",
        headers={"Accept": "application/json",
                 "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = int(response.status)
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        status = int(error.code)
        raw = error.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as error:
        return 0, None, str(getattr(error, "reason", error))
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    return status, body if isinstance(body, dict) else None, ""


def fetch_key(credentials_file: Path, settings_file: Path, helper: str,
              configured_base_url: str) -> int:
    record = _read_json(credentials_file)
    if record is None or not record.get("token") or not record.get("apiBase"):
        return 1
    claude = record.get("claude")
    base_url = (str(claude.get("baseUrl") or "")
                if isinstance(claude, dict) else "") or configured_base_url
    status, body, unreachable = _request(record)
    if status == 0:
        print(f"Engelbart: could not reach {record['apiBase']} for a session "
              f"key ({unreachable}).", file=sys.stderr)
        print("Nothing is wrong with your account. Try again in a moment.",
              file=sys.stderr)
        return 1
    if status in REFUSED:
        detail = str((body or {}).get("error") or
                     "this account has no spendable Claude credit right now.")
        return _refuse(settings_file, helper, base_url, detail)
    if 200 <= status < 300:
        if str((body or {}).get("status") or "") in SPENT:
            return _refuse(settings_file, helper, base_url,
                           "your Engelbart Claude credit is used up.")
        key = str((body or {}).get("apiKey") or "")
        if key:
            sys.stdout.write(key)
            return 0
    print(f"Engelbart: {record['apiBase']} returned no session key ({status}).",
          file=sys.stderr)
    print("Nothing is wrong with your account. Try again in a moment.",
          file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--helper", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--disconnect", action="store_true")
    args = parser.parse_args(argv)
    settings_file = Path(args.settings)
    if args.disconnect:
        return disconnect(settings_file, args.helper, args.base_url)
    return fetch_key(Path(args.credentials), settings_file, args.helper,
                     args.base_url)


if __name__ == "__main__":
    raise SystemExit(main())
