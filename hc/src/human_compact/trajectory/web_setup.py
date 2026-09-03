"""The project a reader finished setting up on the web, claimed from /bart.

The web onboarding ends with "open a new terminal, run claude, type /bart".
By then the site holds a finished setup -- a name, a plan, the direction the
reader chose and the pieces under it -- waiting on the account this machine
was connected to with ``--code``. A chat's first /bart used to open on the
question "what is this chat for"; a reader who has just answered that on the
web must not be asked again. So before that question, the account is read,
the site is asked once, and what comes back is made into the project this
chat is bound to.

The claim is single-use on the server. A payload that cannot be made into a
project (a name already taken, a broken store) is written to disk with the
command that retries it, the same file and the same command the installer's
own import falls back to.

Everything here is quiet on failure. There may be no account, no network,
nothing waiting, or a server that answers something else: each of those
means the ordinary onboarding, not an error in the chat.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

SETUP_ENDPOINT = "/api/engelbart-setup"
PENDING_FILE = "pending-setup.json"
# The claim runs inside /bart, which Claude Code gives a bounded turn; a
# site that does not answer in this long is treated as one with nothing.
TIMEOUT_S = 5


def managed_root(env: Optional[Dict[str, str]] = None) -> Path:
    """Where the installer keeps this machine's account: ``auth.json``."""
    source = os.environ if env is None else env
    configured = str(source.get("HUMAN_COMPACT_HOME") or "").strip()
    return Path(configured) if configured else Path.home() / ".human-compact"


def stored_account(env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """The machine token and the deployment it was issued for, or None.

    The engelbart CLI writes both together: a token minted against one
    deployment must not be replayed at another, so the base travels with
    it and a record missing either is treated as no account at all.
    """
    try:
        raw = (managed_root(env) / "auth.json").read_text(encoding="utf-8")
        record = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    token = str(record.get("token") or "").strip()
    base = str(record.get("apiBase") or "").strip().rstrip("/")
    if not token or not base:
        return None
    return {"token": token, "apiBase": base}


def fetch_pending(account: Dict[str, str], opener: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
    """Ask the site for the setup waiting on this account; None when nothing is.

    One POST, ``{"action": "pending"}``, bearer-authenticated. The reply is
    ``{"payload": <object or null>}``. Anything that is not a 2xx with an
    object payload -- a refused token, an outage, a malformed body -- is
    the same answer as nothing waiting, because that is what it means for
    the chat that asked.
    """
    body = json.dumps({"action": "pending"}).encode("utf-8")
    request = urllib.request.Request(
        account["apiBase"] + SETUP_ENDPOINT,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "Authorization": "Bearer " + account["token"]})
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=TIMEOUT_S) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, ValueError):
        return None
    if not 200 <= status < 300:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    payload = value.get("payload") if isinstance(value, dict) else None
    return payload if isinstance(payload, dict) and payload else None


def materialize(payload: Dict[str, Any], root: Optional[Path] = None,
                bind: str = "") -> Dict[str, Any]:
    """Make the project the web page saved, exactly as ``hc setup-import`` does.

    The payload is the page's saved answers in setup_chat's own commit
    vocabulary; commit() re-normalizes every field, so a payload from a
    different (or hostile) origin can make at most a project with odd text
    in it, never anything else. Who the project is for came with it, and is
    remembered once the project exists so its first prompt is already in
    the reader's register. *bind* names the chat that joins the project,
    the way the local setup page binds the chat that asked.
    """
    from . import setup_chat as SETUP
    result = SETUP.commit(root, payload.get("name"), payload.get("plan"),
                          payload.get("goals"), payload.get("chosen"),
                          payload.get("todos"), payload.get("subgoals") or [],
                          bind=bind, paper=payload.get("paper"),
                          provenance=payload.get("provenance"))
    if not result.get("ok"):
        return result
    reader = payload.get("reader")
    if isinstance(reader, dict) and reader:
        from . import reader as READER
        try:
            READER.remember(reader, root)
        except Exception:  # noqa: BLE001 - the project exists; the profile is a bonus
            pass
    return result


def save_pending(payload: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> str:
    """Keep a claimed payload that could not be made into a project.

    The same file the installer's import falls back to, so one retry
    command covers both: ``hc setup-import --file <path>``. Returns the
    path written, or "" when even that failed.
    """
    target = managed_root(env) / PENDING_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
    except OSError:
        return ""
    return str(target)


def _plain_chat_awaiting_onboarding(session_id: str, root: Optional[Path]) -> bool:
    """Whether this is a chat that would be asked what it is for.

    Only a chat the hooks have seen, with no binding and no tree of its
    own, is about to be asked -- the same rule the workspace applies before
    it shows the question. A session this vault minted for a directory
    (the setup page's, a project's) has no conversation behind it and is
    never a candidate: binding one of those would attach the web project
    to nobody.
    """
    from . import chat_state as CS
    if not CS.needs_project_onboarding(session_id, root):
        return False
    try:
        manifest = CS.load_manifest(session_id, root)
    except (OSError, ValueError, TypeError):
        return False
    if str(manifest.get("origin") or "") == "workspace":
        return False
    try:
        goals, _ = CS.load_goals(session_id, root)
    except Exception:  # noqa: BLE001 - an unreadable tree is not a tree
        return False
    return not (goals or {}).get("goals")


def claim_for_chat(session_id: str, root: Optional[Path] = None,
                   env: Optional[Dict[str, str]] = None,
                   fetch: Optional[Callable] = None) -> str:
    """Bind this chat to the project its reader set up on the web, if one waits.

    Returns one line for the reader, or "" when there was nothing to say:
    no onboarding due, no account on this machine, nothing pending, or a
    site that did not answer. The line is what the /bart hook shows beside
    the workspace URL, so it names the project and nothing more.
    """
    try:
        if not _plain_chat_awaiting_onboarding(session_id, root):
            return ""
        account = stored_account(env)
        if account is None:
            return ""
        payload = (fetch or fetch_pending)(account)
        if not isinstance(payload, dict) or not payload:
            return ""
        result = materialize(payload, root, bind=session_id)
    except Exception:  # noqa: BLE001 - /bart opens with or without the web
        return ""
    if not result.get("ok"):
        # Claimed, so gone from the server: the file is the only copy now.
        where = save_pending(payload, env)
        why = str(result.get("error") or "it could not be created")
        if not where:
            return f"your web setup could not be created ({why}) or saved"
        return (f"your web setup could not be created ({why}); it is saved at "
                f"{where} -- fix that, then run: hc setup-import --file {where}")
    name = str(result.get("name") or payload.get("name") or "your project")
    if result.get("bound"):
        return f'created "{name}" from your web setup; this chat is in it'
    return (f'created "{name}" from your web setup; pick it from the '
            "project list to put this chat in it")
