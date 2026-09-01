"""Which account this machine's ``claude`` runs on, and the switch between.

``engelbart auth`` wires an ``apiKeyHelper`` line into Claude Code's settings
pointing at the managed credential helper; while that line stands, every
``claude`` this workspace spawns -- and every new interactive session --
spends the Engelbart credit pool instead of the member's own claude.ai login.

This module reads that wiring so the settings panel can say which account is
live, and edits it both ways.  Taking it out is the credential helper's own
``unwire``; putting it back writes the same three values the engelbart CLI
writes, character for character, so either side recognises and can undo the
other's work.  A helper that is not ours is never touched: silently
redirecting someone's credential is a worse failure than saying no.

The Claude key itself never appears here.  The pool's server answers status
requests with the key in the body, and everything that passes through this
module strips it before the browser sees anything.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import credential_helper as CH


# What the engelbart CLI names its on-disk helper (claude-code.js HELPER_FILE);
# the .ps1 twin is the Windows spelling. The name is the identity: a value in
# `apiKeyHelper` counts as ours only when it runs one of these files.
HELPER_NAMES = ("engelbart-key", "engelbart-key.ps1")


def settings_path(env: Optional[Dict[str, str]] = None) -> Path:
    """The settings file Claude Code actually reads, honoring CLAUDE_CONFIG_DIR."""
    source = os.environ if env is None else env
    config = str(source.get("CLAUDE_CONFIG_DIR") or "").strip()
    if config:
        return Path(config) / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _helper_script(value: Any) -> Optional[Path]:
    """The helper file an ``apiKeyHelper`` value runs, if it is ours.

    On POSIX the value is the script's path.  On Windows it is a command
    string -- ``powershell ... -File "<script>"`` -- so the path is the
    quoted argument.  Anything whose file is not named like ours is someone
    else's helper and answers None.
    """
    text = str(value or "").strip()
    if not text:
        return None
    quoted = re.search(r'-File\s+"([^"]+)"', text)
    path = Path(quoted.group(1)) if quoted else Path(text)
    return path if path.name in HELPER_NAMES else None


def _helper_value(script: Path) -> str:
    """The exact ``apiKeyHelper`` value engelbart writes for this script."""
    if script.suffix == ".ps1":
        return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
    return str(script)


def _script_args(script: Path) -> Dict[str, str]:
    """The --credentials/--base-url arguments baked into the helper script.

    The sh helper single-quotes every word and the ps1 helper single-quotes
    the values, so both read with the same pattern.  A path holding an
    apostrophe would cut short here; the callers treat a missing argument as
    "no engelbart install", which fails toward doing nothing.
    """
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return {}
    found = {}
    for name in ("credentials", "settings", "helper", "base-url"):
        match = re.search(r"'?--" + name + r"'?\s+'([^']*)'", text)
        if match:
            found[name] = match.group(1)
    return found


def _find_helper(env: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """The helper script, wired or not.

    Wired, its path is in the settings.  Unwired, it still sits in the
    managed install's ``bin`` -- disconnecting edits the settings and leaves
    the file.  A member's server runs from that install's venv, so the walk
    up from ``sys.prefix`` passes the install root on the way to ``/``; a
    server run from a source checkout does not, so the engelbart CLI's own
    default root (HUMAN_COMPACT_HOME, else ``~/.human-compact``) is probed
    as well.
    """
    current = CH._settings(settings_path(env))
    if current:
        script = _helper_script(current[1].get("apiKeyHelper"))
        if script and script.is_file():
            return script
    source = os.environ if env is None else env
    home = str(source.get("HUMAN_COMPACT_HOME") or "").strip()
    roots = [Path(home)] if home else [Path.home() / ".human-compact"]
    prefix = Path(sys.prefix).resolve()
    roots.extend((prefix, *prefix.parents))
    for base in roots:
        for name in HELPER_NAMES:
            candidate = base / "bin" / name
            if candidate.is_file():
                return candidate
    return None


def _credentials_record(helper: Optional[Path]) -> Optional[Dict[str, Any]]:
    if helper is None:
        return None
    where = _script_args(helper).get("credentials")
    if not where:
        return None
    record = CH._read_json(Path(where))
    if record is None or not record.get("token") or not record.get("apiBase"):
        return None
    return record


def _dashboard(record: Dict[str, Any]) -> str:
    """The member-facing meter and key page: the site root plus /engelbart."""
    base = str(record.get("apiBase") or "").rstrip("/")
    return base + "/engelbart" if base else ""


def _credit_fields(out: Dict[str, Any], claude: Any) -> None:
    """Budget, spend and standing -- and deliberately never the key."""
    if not isinstance(claude, dict):
        return
    for ours, theirs in (("budget_usd", "budgetUsd"), ("spend_usd", "spendUsd")):
        value = claude.get(theirs)
        if isinstance(value, (int, float)):
            out[ours] = float(value)
    status = str(claude.get("status") or "")
    if status:
        out["credit_status"] = status


def status(env: Optional[Dict[str, str]] = None, fresh: bool = False) -> Dict[str, Any]:
    """Which account ``claude`` will run on, as the settings stand now.

    ``fresh`` asks the pool's server for the live meter rather than showing
    the figures from the last CLI run; a server that cannot answer costs the
    reader nothing but freshness.
    """
    current = CH._settings(settings_path(env))
    value = str((current[1] if current else {}).get("apiKeyHelper") or "")
    script = _helper_script(value)
    foreign = bool(value) and script is None
    helper = script if script else (None if foreign else _find_helper(env))
    record = _credentials_record(helper)
    # The workspace itself may have been opened on the pool key -- `engelbart
    # auth` starts setup with HC_USE_API_KEY=1 -- and that env outlives any
    # settings edit until this server restarts.
    source = os.environ if env is None else env
    pinned = (str(source.get("HC_USE_API_KEY") or "") == "1"
              and bool(source.get("ANTHROPIC_AUTH_TOKEN")))
    out: Dict[str, Any] = {
        "ok": True,
        "using": "engelbart" if (script or pinned) else "own",
        "wired": script is not None,
        "available": record is not None,
        "foreign_helper": foreign,
        "env_pinned": pinned and script is None,
    }
    _credit_fields(out, (record or {}).get("claude"))
    if record is not None:
        out["dashboard"] = _dashboard(record)
    if fresh and record is not None:
        code, body, _ = CH._request(record)
        if 200 <= code < 300 and isinstance(body, dict):
            _credit_fields(out, body)
    return out


# How often the pool is asked whether the credit still stands, and the file
# that remembers the answer between polls -- and across server restarts, so
# a workspace reopened onto an exhaustion it already reported stays quiet.
CREDIT_CHECK_SECONDS = 600
ALERT_FILE = "credit_alert.json"


def credit_alert(state_dir: Any, env: Optional[Dict[str, str]] = None,
                 now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The one-time "the pool's credit just ran out" event, for a poller.

    Rides the state poll rather than a timer of its own: only an open
    workspace asks, at most every ten minutes, and the answer is a real
    round trip to the pool's server -- the stored figures are whatever the
    last CLI run saw.  The event fires once per exhaustion; a pool topped
    back up re-arms it.  Never the key, and never an exception: a poll must
    not fail over a banner.
    """
    import time
    moment = time.time() if now is None else now
    where = Path(state_dir) / ALERT_FILE
    record = CH._read_json(where) or {}
    checked = record.get("checked_at")
    if isinstance(checked, (int, float)) \
            and 0 <= moment - checked < CREDIT_CHECK_SECONDS:
        return None
    kept = {"checked_at": moment,
            "status": str(record.get("status") or ""),
            "alerted": bool(record.get("alerted"))}
    alert = None
    credentials = _credentials_record(_find_helper(env))
    if credentials is not None:
        code, body, _unreachable = CH._request(credentials)
        # Only a served answer moves the state: a refusal or an unreachable
        # server is not news that the credit ran out.
        if 200 <= code < 300 and isinstance(body, dict):
            current = str(body.get("status") or "")
            kept["status"] = current
            if current in CH.SPENT:
                if not kept["alerted"]:
                    kept["alerted"] = True
                    alert = {"kind": "credit_exhausted", "status": current,
                             "dashboard": _dashboard(credentials)}
            else:
                kept["alerted"] = False
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(kept), encoding="utf-8")
    except OSError:
        pass
    return alert


def _wire_once(settings_file: Path, helper_value: str, base_url: str) -> bool:
    """One attempt at writing what ``engelbart auth`` writes, and only that.

    The same last-moment check as the helper's ``_unwire_once``: Claude Code
    saves this file while running, and the one unforgivable failure is
    replacing bytes other than the exact version read.  A missing file is a
    machine that never ran Claude Code; it gets a fresh one.
    """
    before = CH._settings(settings_file)
    raw = before[0] if before else None
    parsed = dict(before[1]) if before else {}
    if settings_file.exists() and before is None:
        return False
    existing = parsed.get("apiKeyHelper")
    if existing and existing != helper_value:
        return False
    held = parsed.get("env")
    env = dict(held) if isinstance(held, dict) else {}
    env["ANTHROPIC_BASE_URL"] = base_url
    env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] = CH.HELPER_TTL_MS
    parsed["apiKeyHelper"] = helper_value
    parsed["env"] = env

    temporary = Path(str(settings_file) + f".engelbart-{os.getpid()}")
    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(parsed, indent=2) + "\n"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if raw is None:
            if settings_file.exists():
                temporary.unlink(missing_ok=True)
                return False
        elif settings_file.read_text(encoding="utf-8") != raw:
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, settings_file)
        return True
    except OSError:
        temporary.unlink(missing_ok=True)
        return False


def switch(which: str, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Move ``claude`` onto the pool key or back onto the member's own login.

    Subprocesses this workspace spawns read the settings when they start, so
    the switch is live for the next one; an interactive Claude Code that is
    already open keeps whatever it loaded until it restarts, and the panel
    says so rather than letting the edit look like it reached it.
    """
    settings_file = settings_path(env)
    if which == "own":
        current = CH._settings(settings_file)
        value = str((current[1] if current else {}).get("apiKeyHelper") or "")
        script = _helper_script(value)
        if not value or script is None:
            # Nothing of ours is wired: either already on the member's own
            # account, or wired to a helper that is not ours to remove.
            answer = status(env)
            if value:
                answer.update(ok=False, error="Claude Code is wired to a "
                              "different credential helper; nothing changed.")
            return answer
        base_url = (_script_args(script).get("base-url", "")
                    if script.is_file() else "")
        if not CH.unwire(settings_file, value, base_url, attempts=3):
            return {"ok": False, "error": f"could not edit {settings_file}"}
        return status(env)

    if which != "engelbart":
        return {"ok": False, "error": "unknown account choice"}

    current = CH._settings(settings_file)
    value = str((current[1] if current else {}).get("apiKeyHelper") or "")
    if value and _helper_script(value) is None:
        return {"ok": False, "error": "Claude Code is wired to a different "
                "credential helper; nothing changed."}
    helper = _find_helper(env)
    record = _credentials_record(helper)
    if helper is None or record is None:
        return {"ok": False, "error": "No Engelbart credential on this "
                "machine. Run `engelbart auth` first."}
    # Never wire a key the pool will refuse: asked of the server now, the
    # same way the CLI asks before it exports anything.
    code, body, unreachable = CH._request(record)
    if code == 0:
        return {"ok": False, "error": "Could not reach the Engelbart server "
                f"({unreachable}). Nothing changed; try again in a moment."}
    if code in CH.REFUSED:
        detail = str((body or {}).get("error") or
                     "this account has no spendable Claude credit right now")
        return {"ok": False, "error": detail}
    if not (200 <= code < 300) or not isinstance(body, dict):
        return {"ok": False, "error": "The Engelbart server returned no "
                f"credential ({code}). Nothing changed."}
    if str(body.get("status") or "") in CH.SPENT:
        return {"ok": False,
                "error": "Your Engelbart Claude credit is used up."}
    claude = record.get("claude")
    base_url = (str(body.get("baseUrl") or "")
                or (str(claude.get("baseUrl") or "")
                    if isinstance(claude, dict) else "")
                or _script_args(helper).get("base-url", ""))
    if not base_url:
        return {"ok": False, "error": "No gateway URL for this credential. "
                "Run `engelbart auth` again."}
    if not any(_wire_once(settings_file, _helper_value(helper), base_url)
               for _ in range(3)):
        return {"ok": False, "error": f"could not edit {settings_file}"}
    answer = status(env)
    _credit_fields(answer, body)
    return answer
