"""Sending a project to Supabase: where the keys live, and how a row gets up.

Two files, both in the vault and both private to the user -- never in the
repository, where a key is one ``git add .`` away from being public:

* ``supabase.json``  -- the project URL and the anon key. Written by hand
  (or by ``hc supabase-setup``), read on every send.
* ``supabase-session.json`` -- what a sign-in returned. An access token and
  the refresh token that renews it; the PASSWORD IS NEVER WRITTEN, and never
  passes through anything but the one request that exchanges it.

The anon key is the right key here, not the service key. ``hc_sync_project``
and every policy behind it are written against ``auth.uid()``: they need a
signed-in user, and the service key -- which has no user and bypasses row
security entirely -- would both fail the function and, if it leaked, hand
over every row of every account. An anon key is public by design; the user's
own token is what unlocks the user's own rows.

Env vars win over the file, for a machine that keeps its secrets elsewhere:
``HC_SUPABASE_URL``, ``HC_SUPABASE_ANON_KEY``.
"""
from __future__ import annotations

import json
import base64
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import chat_state as CS
from . import project_sync as SY
from .secure_io import atomic_write_json

CONFIG_NAME = "supabase.json"
SESSION_NAME = "supabase-session.json"
TIMEOUT_S = 20.0
# Renew a little before the token actually lapses: a send that starts valid
# and finishes expired is a confusing failure to read.
REFRESH_MARGIN_S = 120

CONFIG_TEMPLATE = {
    "url": "https://YOUR-PROJECT-REF.supabase.co",
    "anon_key": "PASTE-YOUR-ANON-PUBLIC-KEY-HERE",
    "email": "you@example.com",
}


class SupabaseError(RuntimeError):
    """Something the reader can act on, phrased for them rather than raised
    as whatever urllib said."""


def _vault(root: Optional[Path] = None) -> Path:
    """The vault root, not the sessions directory under it: these keys
    belong to the account, not to any one chat.

    Callers arrive holding different halves of the same layout. A server
    derives its root from the session directory it was given, so it hands
    over ``<vault>/chat-sessions``; a CLI with no session passes nothing and
    means the vault itself. Both have to land on one file, or the button
    reads a config the setup command never wrote.
    """
    base = CS._state_location(root)[1] if root is None else Path(root)
    return base.parent if base.name == "chat-sessions" else base


def config_path(root: Optional[Path] = None) -> Path:
    return _vault(root) / CONFIG_NAME


def session_path(root: Optional[Path] = None) -> Path:
    return _vault(root) / SESSION_NAME


def write_template(root: Optional[Path] = None) -> Tuple[Path, bool]:
    """Put the file where it goes, with the shape but none of the secrets.

    Returns the path and whether it was created now. An existing file is
    left exactly as it is: it holds real keys, and a template that
    overwrites them is a template that loses them.
    """
    path = config_path(root)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, CONFIG_TEMPLATE, root=path.parent)
    return path, True


def load_config(root: Optional[Path] = None) -> Dict[str, str]:
    """URL and anon key, from the environment or the file."""
    stored: Dict[str, Any] = {}
    try:
        value = json.loads(config_path(root).read_text(encoding="utf-8"))
        if isinstance(value, dict):
            stored = value
    except (OSError, ValueError):
        stored = {}
    url = normalize_url(os.environ.get("HC_SUPABASE_URL")
                        or str(stored.get("url") or ""))
    key = (os.environ.get("HC_SUPABASE_ANON_KEY")
           or str(stored.get("anon_key") or "")).strip()
    email = str(stored.get("email") or "").strip()
    if email == CONFIG_TEMPLATE["email"]:
        email = ""      # the template's stand-in is not an address
    if url and url.startswith("http://") and "127.0.0.1" not in url \
            and "localhost" not in url:
        # A key sent in the clear is a key given away.
        raise SupabaseError("the Supabase URL must be https")
    if any(marker in url or marker in key
           for marker in ("YOUR-PROJECT-REF", "PASTE-YOUR")):
        return {"url": "", "anon_key": "", "email": email}
    return {"url": url, "anon_key": key, "email": email}


# The dashboard shows several URLs and they are easy to confuse: the REST
# endpoint, the auth endpoint, the project page. Only the origin is wanted --
# each client appends its own path -- so a pasted endpoint is trimmed back to
# it rather than refused. Left on, "<origin>/rest/v1" would ask for
# "<origin>/rest/v1/auth/v1/token" and get a 404 that reads like a missing
# migration.
API_SUFFIXES = ("/rest/v1", "/auth/v1", "/storage/v1", "/realtime/v1",
                "/functions/v1", "/graphql/v1")


def normalize_url(url: str) -> str:
    """A project's origin, from whatever part of it was pasted."""
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    lowered = text.lower()
    # The dashboard page for a project is not its API at all.
    if "supabase.com/dashboard/project/" in lowered:
        ref = text.split("/project/", 1)[1].split("/", 1)[0].strip()
        if ref:
            return f"https://{ref}.supabase.co"
        return text
    changed = True
    while changed:
        changed = False
        for suffix in API_SUFFIXES:
            if text.lower().endswith(suffix):
                text = text[: -len(suffix)].rstrip("/")
                changed = True
    return text


def save_config(url: str, anon_key: str, email: str = "",
                root: Optional[Path] = None) -> Path:
    """Write the project URL and the anon key, from the settings panel.

    The anon key belongs in a form: it is public by design and ships in the
    bundle of every Supabase web app. The service key does not, and is
    refused here -- it bypasses row security entirely, and a workspace that
    accepted one would be one leaked file away from every account's rows.
    """
    url = normalize_url(url)
    anon_key = str(anon_key or "").strip()
    email = str(email or "").strip()
    if not url or not anon_key:
        raise SupabaseError("both the project URL and the anon key are needed")
    local = "127.0.0.1" in url or "localhost" in url
    if not url.startswith("https://") and not (local and url.startswith("http://")):
        raise SupabaseError("the Supabase URL must be https")
    if _looks_like_service_key(anon_key):
        raise SupabaseError(
            "that looks like the service_role key -- use the anon (public) "
            "key: the workspace signs in as you and row security does the rest")
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    stored: Dict[str, Any] = {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            stored = value
    except (OSError, ValueError):
        stored = {}
    stored.update({"url": url, "anon_key": anon_key})
    if email:
        stored["email"] = email
    atomic_write_json(path, stored, root=path.parent)
    return path


def _looks_like_service_key(key: str) -> bool:
    """A Supabase key is a JWT whose payload names its role."""
    parts = key.split(".")
    if len(parts) != 3:
        return False
    import base64
    body = parts[1]
    body += "=" * (-len(body) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(claims, dict) and claims.get("role") == "service_role"


def configured(root: Optional[Path] = None) -> bool:
    try:
        config = load_config(root)
    except SupabaseError:
        return False
    return bool(config["url"] and config["anon_key"])


def _post(url: str, headers: Dict[str, str], body: Any,
          where: str = "rpc") -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    for name, value in headers.items():
        request.add_header(name, value)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:      # noqa: BLE001 - the status is the useful part
            pass
        raise SupabaseError(_explain(exc.code, detail, where)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise SupabaseError(f"could not reach Supabase: {exc}") from None
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise SupabaseError("Supabase replied with something that is not "
                            "JSON") from None


def _server_said(detail: str) -> str:
    """The message out of a PostgREST error body, when there is one."""
    try:
        value = json.loads(detail)
    except (ValueError, TypeError):
        return ""
    if not isinstance(value, dict):
        return ""
    said = str(value.get("message") or "")
    hint = str(value.get("hint") or "")
    return (said + (f" ({hint})" if hint and hint != "null" else ""))[:300]


def _explain(code: int, detail: str, where: str = "rpc") -> str:
    """The status, in words the reader can do something about.

    *where* is which call met it. A 404 signing in and a 404 sending rows
    have nothing to do with each other -- the first is almost always a URL
    that is not the project's origin, the second a migration not yet
    applied -- and one message for both sends the reader to the wrong end.
    """
    if code == 400 and where != "auth":
        # A 400 from a call is Postgres saying no, and what it said is the
        # only useful part. Guessing at "your credentials are wrong" here
        # sends the reader to the wrong end of the problem.
        return f"Supabase refused the call: {_server_said(detail) or detail[:200]}"
    if code in (400, 401):
        if where == "auth" or "invalid" in detail.lower() \
                or "credential" in detail.lower():
            return ("Supabase refused the sign-in: check the email and "
                    "password, and that the anon key belongs to this project")
        return ("Supabase refused the request (401): the token may have "
                "lapsed -- run `hc supabase-login` again")
    if code == 403:
        return ("Supabase allowed the call but row security refused the "
                "rows (403): every row must carry your own user_id")
    if code == 404:
        if where == "auth":
            return ("Supabase has no auth endpoint at that URL: check the "
                    "project URL is the origin (https://<ref>.supabase.co) "
                    "with no /rest/v1 or /auth/v1 on the end")
        # PostgREST names the function it could not find, and which
        # argument names it looked for. That is worth more than a guess at
        # which migration is missing.
        said = _server_said(detail)
        if said:
            return f"Supabase has no such function: {said}"
        # No body to quote. Two things look identical from here and the
        # reader should be told both, because one of them is far more
        # likely and takes a second to rule out.
        return ("Supabase has no such function or table. Either the "
                "migration has not been applied, or this workspace is "
                "running code older than the one that added it -- restart "
                "the server and try again.")
    return f"Supabase returned {code}: {detail[:200]}"


def _store_session(payload: Dict[str, Any],
                   root: Optional[Path] = None) -> Dict[str, Any]:
    user = payload.get("user") or {}
    session = {
        "access_token": str(payload.get("access_token") or ""),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "expires_at": int(time.time()) + int(payload.get("expires_in") or 3600),
        "user_id": str(user.get("id") or ""),
        "email": str(user.get("email") or ""),
    }
    if not session["access_token"] or not session["user_id"]:
        raise SupabaseError("Supabase signed in but returned no user")
    path = session_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, session, root=path.parent)
    return session


def load_session(root: Optional[Path] = None) -> Dict[str, Any]:
    try:
        value = json.loads(session_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def sign_in(email: str, password: str,
            root: Optional[Path] = None) -> Dict[str, Any]:
    """Exchange a password for tokens, once. The password is not kept.

    Called from the terminal, where the reader types it themselves -- it
    does not travel through the workspace, and nothing writes it down.
    """
    config = load_config(root)
    if not config["url"] or not config["anon_key"]:
        raise SupabaseError(f"fill in {config_path(root)} first")
    payload = _post(
        f"{config['url']}/auth/v1/token?grant_type=password",
        {"apikey": config["anon_key"]},
        {"email": email, "password": password}, "auth")
    return _store_session(payload, root)


# --- signing in through the browser -------------------------------------
#
# A password typed at a terminal is a password the terminal has seen. The
# ordinary way in is the browser the reader is already signed into: they
# press a provider button, and the CLI is handed the result.
#
# PKCE rather than the implicit flow, for one practical reason: the
# implicit flow returns tokens in the URL *fragment*, which a browser never
# sends to a server, so a local listener cannot see them without a page
# that reads location.hash and posts it back. PKCE returns a code in the
# query string, which the listener reads directly -- no page script, and
# the code is worthless to anyone who does not hold the verifier.

OAUTH_PROVIDERS = (
    "google",
    "github",
)
OAUTH_WAIT_S = 300

# --- the connect page -------------------------------------------------
#
# A provider button needs a provider somebody configured, and Supabase only
# sends a browser back to an address on the project's allow-list -- which a
# loopback port picked at run time can never be on. The connect page is the
# one fixed address Supabase knows. It takes an email, sends the sign-in
# link with THIS process's challenge attached, and when the link brings the
# browser back with a code, hands the code on to the listener here. The page
# never holds a token: a code without the verifier is nothing, and the
# verifier never left this process.
CONNECT_URL = "https://engelbart.mathetic.com/connect"
CONNECT_WAIT_S = 600        # an email has to arrive and be opened first


def pkce_pair() -> Dict[str, str]:
    """A fresh verifier and the challenge derived from it.

    The verifier never leaves this process until it is exchanged, and the
    challenge is what travels through the browser -- so a code lifted from
    the redirect cannot be spent by whoever lifted it.
    """
    import hashlib
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii")
    verifier = verifier.rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return {"verifier": verifier, "challenge": challenge}


def authorize_url(provider: str, redirect: str, challenge: str,
                  root: Optional[Path] = None) -> str:
    """Where to send the browser to sign in with a provider."""
    from urllib.parse import urlencode
    name = str(provider or "").strip().lower()
    if name not in OAUTH_PROVIDERS:
        raise SupabaseError("unknown sign-in provider: " + (name or "(none)"))
    config = load_config(root)
    if not config["url"] or not config["anon_key"]:
        raise SupabaseError(f"fill in {config_path(root)} first")
    query = urlencode({
        "provider": name,
        "redirect_to": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
    })
    return f"{config['url']}/auth/v1/authorize?{query}"


def exchange_code(code: str, verifier: str,
                  root: Optional[Path] = None) -> Dict[str, Any]:
    """Turn the code the browser handed back into a stored session.

    The same landing place as a password sign-in: one session file, at
    0600, with the refresh token that keeps it alive. Nothing about the
    rest of the client knows which way the reader came in.
    """
    config = load_config(root)
    if not config["url"] or not config["anon_key"]:
        raise SupabaseError(f"fill in {config_path(root)} first")
    if not str(code or "").strip():
        raise SupabaseError("the browser did not hand back a code")
    payload = _post(
        f"{config['url']}/auth/v1/token?grant_type=pkce",
        {"apikey": config["anon_key"]},
        {"auth_code": str(code).strip(), "code_verifier": str(verifier)},
        "auth")
    return _store_session(payload, root)


def connect_page_url(redirect: str, challenge: str,
                     root: Optional[Path] = None) -> str:
    """Where to send the browser when no provider was named.

    Everything the page needs rides in the URL *fragment*, not the query:
    a fragment never leaves the browser, so the project's address and its
    public key reach the page's script and nothing else -- not the host,
    not its logs. ``HC_CONNECT_URL`` points a machine at another page.
    """
    from urllib.parse import urlencode
    config = load_config(root)
    if not config["url"] or not config["anon_key"]:
        raise SupabaseError(f"fill in {config_path(root)} first")
    base = (os.environ.get("HC_CONNECT_URL") or CONNECT_URL).strip()
    fragment = urlencode({
        "url": config["url"],
        "apikey": config["anon_key"],
        "challenge": challenge,
        "redirect": redirect,
        "email": config.get("email") or "",
    })
    return f"{base}#{fragment}"


DONE_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Signed in</title>"
    "<style>body{font:15px/1.6 ui-monospace,Menlo,monospace;color:#111;"
    "background:#fff;display:flex;align-items:center;justify-content:center;"
    "height:100vh;margin:0}p{max-width:26em;text-align:center}"
    "@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}}"
    "</style><p><strong>%s</strong><br>%s</p>")


def _one_shot_listener():
    """A server that exists to catch one redirect and stop.

    Bound to the loopback address on a port the operating system picks:
    nothing outside this machine can reach it, and nothing has to be
    reserved in advance. It answers exactly one request -- whatever the
    browser sends first -- and holds what it saw for the caller.
    """
    import http.server
    from urllib.parse import parse_qs, urlsplit

    caught = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802 - the API's name
            query = parse_qs(urlsplit(self.path).query)
            caught["code"] = (query.get("code") or [""])[0]
            # A reader who presses cancel in the browser is redirected here
            # too, with why. Saying it beats a CLI that waits five minutes
            # for something that is never coming.
            caught["error"] = (query.get("error_description")
                               or query.get("error") or [""])[0]
            good = bool(caught["code"]) and not caught["error"]
            body = (DONE_PAGE % (
                "Signed in." if good else "Sign-in did not finish.",
                "You can close this tab and go back to the terminal."
                if good else str(caught["error"] or "No code came back.")
            )).encode("utf-8")
            self.send_response(200 if good else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            """Quiet: this is a sign-in, not a web server."""

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    return server, caught


def sign_in_with_browser(provider: Optional[str] = None,
                         root: Optional[Path] = None,
                         open_browser=None, wait_s: Optional[int] = None,
                         announce=None) -> Dict[str, Any]:
    """Sign in through the browser the reader is already signed into.

    The whole round trip: a listener on loopback, a page in the browser,
    and the code it redirects back with, exchanged for a session that
    lands where a password sign-in would have put it.

    With no provider named, the page is the connect page: it takes an
    email and sends the sign-in link. With one named, the browser goes
    straight to that provider's button at Supabase -- which needs the
    listener's address on the project's redirect allow-list.

    Nothing here is interactive on this side. A reader who never finishes
    is not a reader to wait on for ever, so the listener has a deadline --
    a longer one when an email has to arrive first.
    """
    import threading
    import webbrowser
    pair = pkce_pair()
    server, caught = _one_shot_listener()
    redirect = "http://127.0.0.1:%d/callback" % server.server_address[1]
    try:
        if provider:
            where = authorize_url(provider, redirect, pair["challenge"], root)
            deadline = OAUTH_WAIT_S if wait_s is None else wait_s
        else:
            where = connect_page_url(redirect, pair["challenge"], root)
            deadline = CONNECT_WAIT_S if wait_s is None else wait_s
    except SupabaseError:
        server.server_close()
        raise
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    if announce:
        announce(where)
    (open_browser or webbrowser.open)(where)
    thread.join(timeout=max(1, int(deadline)))
    server.server_close()
    if thread.is_alive():
        raise SupabaseError(
            "timed out waiting for the browser -- nothing was signed in")
    if caught.get("error"):
        raise SupabaseError("sign-in was refused: " + str(caught["error"]))
    return exchange_code(caught.get("code") or "", pair["verifier"], root)


def sign_out(root: Optional[Path] = None) -> None:
    session_path(root).unlink(missing_ok=True)


def _refresh(session: Dict[str, Any],
             root: Optional[Path] = None) -> Dict[str, Any]:
    config = load_config(root)
    token = str(session.get("refresh_token") or "")
    if not token:
        raise SupabaseError("not signed in -- run `hc supabase-login`")
    payload = _post(f"{config['url']}/auth/v1/token?grant_type=refresh_token",
                    {"apikey": config["anon_key"]},
                    {"refresh_token": token}, "auth")
    return _store_session(payload, root)


def current_session(root: Optional[Path] = None) -> Dict[str, Any]:
    """A session whose access token is good for the next call."""
    session = load_session(root)
    if not session.get("access_token"):
        raise SupabaseError("not signed in -- run `hc supabase-login`")
    if int(session.get("expires_at") or 0) - REFRESH_MARGIN_S <= time.time():
        session = _refresh(session, root)
    return session


def status(root: Optional[Path] = None) -> Dict[str, Any]:
    """What the workspace shows on the button before anything is sent."""
    try:
        config = load_config(root)
    except SupabaseError as exc:
        return {"configured": False, "signed_in": False, "error": str(exc),
                "config_path": str(config_path(root))}
    session = load_session(root)
    return {
        "configured": bool(config["url"] and config["anon_key"]),
        "signed_in": bool(session.get("access_token")),
        "email": session.get("email") or config.get("email") or "",
        "user_id": session.get("user_id") or "",
        "expires_at": session.get("expires_at") or 0,
        "config_path": str(config_path(root)),
    }


# --------------------------------------------------------------- sharing

# What a collaborator is handed. The URL and the anon key travel with the
# token because the reader's workspace has to know which project to ask --
# and the anon key is public by design, so the code is exactly as secret as
# the token inside it, and no more.
CODE_PREFIX = "hcjoin1_"


def join_code(url: str, anon_key: str, token: str) -> str:
    """One pasteable string: where, which key, which token."""
    import base64
    raw = json.dumps({"u": url, "k": anon_key, "t": token},
                     separators=(",", ":")).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_code(code: str) -> Dict[str, str]:
    """The three parts back out, or a refusal that says which part is bad."""
    import base64
    text = str(code or "").strip()
    if not text.startswith(CODE_PREFIX):
        raise SupabaseError("that is not an invite code (it should start "
                            f"with {CODE_PREFIX})")
    body = text[len(CODE_PREFIX):]
    body += "=" * (-len(body) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SupabaseError("that invite code is damaged") from None
    if not isinstance(value, dict) or not all(
            isinstance(value.get(k), str) and value.get(k)
            for k in ("u", "k", "t")):
        raise SupabaseError("that invite code is missing part of itself")
    return {"url": normalize_url(value["u"]), "anon_key": value["k"],
            "token": value["t"]}


def _rpc(name: str, body: Any, config: Dict[str, str],
         bearer: str) -> Dict[str, Any]:
    return _post(f"{config['url']}/rest/v1/rpc/{name}",
                 {"apikey": config["anon_key"],
                  "Authorization": f"Bearer {bearer}"}, body)


def create_share(root: Optional[Path], cwd, label: str = "",
                 expires_in_days: Optional[int] = None,
                 kind: str = "invite", role: str = "reader",
                 max_uses: Optional[int] = None) -> Dict[str, Any]:
    """Mint a token for this project and wrap it in an invite code.

    The token comes back from Postgres once and is never stored here or
    there -- only its hash is kept. Losing it means minting another, which
    is the same trade a password makes and for the same reason.
    """
    config = load_config(root)
    session = current_session(root)
    project_id = SY.project_uuid(root, cwd)
    out = _rpc("hc_create_share",
               {"p_project_id": project_id, "p_label": label or "",
                "p_expires_in_days": expires_in_days,
                "p_kind": kind, "p_role": role, "p_max_uses": max_uses},
               config, session["access_token"])
    token = (out or {}).get("token")
    if not token:
        raise SupabaseError("Supabase minted no token")
    return {"ok": True, "id": out.get("id"), "token": token,
            "kind": out.get("kind", kind), "role": out.get("role", role),
            "code": join_code(config["url"], config["anon_key"], token)}


def list_shares(root: Optional[Path], cwd) -> Dict[str, Any]:
    """The shares open on this project. Never the tokens: they are gone."""
    config = load_config(root)
    session = current_session(root)
    project_id = SY.project_uuid(root, cwd, mint=False)
    query = ("/rest/v1/hc_project_shares?select=id,label,kind,role,can_write,"
             "created_at,expires_at,revoked_at,last_used_at,uses"
             f"&project_id=eq.{project_id}&order=created_at.desc")
    request = urllib.request.Request(config["url"] + query, method="GET")
    request.add_header("apikey", config["anon_key"])
    request.add_header("Authorization", f"Bearer {session['access_token']}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            rows = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SupabaseError(_explain(exc.code, "")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SupabaseError(f"could not read the shares: {exc}") from None
    return {"ok": True, "shares": rows if isinstance(rows, list) else []}


def revoke_share(root: Optional[Path], share_id: str) -> Dict[str, Any]:
    config = load_config(root)
    session = current_session(root)
    out = _rpc("hc_revoke_share", {"share_id": share_id}, config,
               session["access_token"])
    return {"ok": bool((out or {}).get("ok"))}


# ------------------------------------------------------------- membership

def add_member(root: Optional[Path], cwd, email: str,
               role: str = "reader") -> Dict[str, Any]:
    """Put someone on this project by the email they signed up with.

    They must already have an account in this Supabase project -- the owner
    creates it, or lets them sign up. Postgres answers plainly when they do
    not, because "nothing happened" is the least useful thing to say.
    """
    config = load_config(root)
    session = current_session(root)
    project_id = SY.project_uuid(root, cwd)
    out = _rpc("hc_add_member",
               {"p_project_id": project_id,
                "p_email": str(email or "").strip(),
                "p_role": role}, config, session["access_token"])
    if not isinstance(out, dict) or not out.get("ok"):
        raise SupabaseError((out or {}).get("error") or "could not add them")
    return out


def list_members(root: Optional[Path], cwd) -> Dict[str, Any]:
    config = load_config(root)
    session = current_session(root)
    project_id = SY.project_uuid(root, cwd, mint=False)
    out = _rpc("hc_list_members", {"project_id": project_id}, config,
               session["access_token"])
    return {"ok": True, "members": out if isinstance(out, list) else []}


def remove_member(root: Optional[Path], cwd, user_id: str) -> Dict[str, Any]:
    config = load_config(root)
    session = current_session(root)
    project_id = SY.project_uuid(root, cwd, mint=False)
    out = _rpc("hc_remove_member",
               {"project_id": project_id, "member": user_id}, config,
               session["access_token"])
    return {"ok": bool((out or {}).get("ok"))}


def shared_projects(root: Optional[Path] = None) -> Dict[str, Any]:
    """Projects someone else has put this account on.

    A plain select: the widened read policy is what makes them visible, so
    if this returns nothing the policy is the thing to look at, not this.
    """
    config = load_config(root)
    session = current_session(root)
    query = ("/rest/v1/hc_projects?select=id,cwd,name,objective,description,"
             "generated_at&order=generated_at.desc")
    request = urllib.request.Request(config["url"] + query, method="GET")
    request.add_header("apikey", config["anon_key"])
    request.add_header("Authorization", f"Bearer {session['access_token']}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            rows = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SupabaseError(_explain(exc.code, "")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SupabaseError(f"could not list projects: {exc}") from None
    return {"ok": True, "projects": rows if isinstance(rows, list) else []}


def redeem(code: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """Turn an invitation into a place on a project's roll.

    Unlike a view code, this buys nothing on its own: it must be redeemed
    by someone signed in, and what they get afterwards belongs to their
    account rather than to the code. So a leaked invite leaves a name the
    owner can strike off, which a leaked view link never does.
    """
    text = str(code or "").strip()
    if not text:
        raise SupabaseError("paste the invite code first")
    token = text
    if not text.startswith(CODE_PREFIX) and not text.startswith("hcs_"):
        # Said here rather than left to Postgres, which can only report
        # that some string it was handed is not a live token.
        raise SupabaseError("that is not an invite code -- it should start "
                            f"with {CODE_PREFIX}")
    if text.startswith(CODE_PREFIX):
        # A full code carries where to go as well as the token, which is
        # what lets someone join a project they have never configured.
        parts = parse_code(text)
        token = parts["token"]
        if not configured(root):
            save_config(parts["url"], parts["anon_key"], "", root)
    if not configured(root):
        raise SupabaseError("connect to Supabase first, in settings")
    config = load_config(root)
    try:
        session = current_session(root)
    except SupabaseError:
        raise SupabaseError("sign in first: an invitation is joined to an "
                            "account, not held on its own") from None
    out = _rpc("hc_redeem_share", {"p_token": token}, config,
               session["access_token"])
    if not isinstance(out, dict) or not out.get("ok"):
        raise SupabaseError((out or {}).get("error")
                            or "that invitation is not open")
    return out


def _select(config, token, path):
    request = urllib.request.Request(config["url"] + path, method="GET")
    request.add_header("apikey", config["anon_key"])
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:      # noqa: BLE001
            pass
        raise SupabaseError(_explain(exc.code, detail)) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SupabaseError(f"could not read the project: {exc}") from None


def fetch_project(project_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """Every row of one shared project this account is allowed to see.

    Plain selects rather than the anonymous read function: a signed-in
    member has row security working for them, and going through it means
    the owner sees contributions and a member sees the owner's work,
    without a second code path deciding who sees what.
    """
    config = load_config(root)
    session = current_session(root)
    token = session["access_token"]
    pid = str(project_id)
    want = {
        "projects": ("hc_projects", "id,cwd,name,objective,description,"
                     "generated_at", "id"),
        "chats": ("hc_chats", "id,user_id,session_id,created_at,updated_at,"
                  "prompt_count,goal_count", "project_id"),
        "goals": ("hc_goals", "id,user_id,session_id,local_id,parent_id,title,"
                  "status,priority,origin,description,notes,prompt,"
                  "evidence_ids,relevance,relevance_why,relevance_for,"
                  "updated_at", "project_id"),
        "todos": ("hc_todos", "id,user_id,goal_id,local_id,position,depth,"
                  "text,status,question", "project_id"),
        "goal_sources": ("hc_goal_sources",
                         "id,user_id,goal_id,local_id,type,label,position",
                         "project_id"),
        "related_prompts": ("hc_related_prompts",
                            "id,user_id,goal_id,prompt_id,text,session_id,"
                            "auto,created_at,position", "project_id"),
    }
    out: Dict[str, Any] = {}
    for key, (table, columns, column) in want.items():
        out[key] = _select(config, token,
                           f"/rest/v1/{table}?select={columns}"
                           f"&{column}=eq.{pid}&limit=2000")
    out["me"] = session.get("user_id")
    return out


def member_names(project_id: str, root: Optional[Path] = None) -> Dict[str, str]:
    """User ids to what each person is called on this project.

    The name they chose, when they have chosen one. Falling back to the
    local part of an email gives "dbarron410" where a reader expects
    "David", so it is only a fallback, and only the owner sees emails at
    all -- a member is told names, not addresses.
    """
    config = load_config(root)
    session = current_session(root)
    try:
        out = _post(f"{config['url']}/rest/v1/rpc/hc_project_people",
                    {"apikey": config["anon_key"],
                     "Authorization": f"Bearer {session['access_token']}"},
                    {"p_project_id": str(project_id)})
    except SupabaseError:
        return {}
    if not isinstance(out, list):
        return {}
    names = {}
    for person in out:
        if not isinstance(person, dict) or not person.get("user_id"):
            continue
        chosen = str(person.get("display_name") or "").strip()
        email = str(person.get("email") or "")
        names[person["user_id"]] = chosen or email
    return names


def display_name(root: Optional[Path] = None) -> str:
    """What this account has chosen to be called, if anything."""
    config = load_config(root)
    session = current_session(root)
    rows = _select(config, session["access_token"],
                   "/rest/v1/hc_profiles?select=display_name&limit=1")
    if isinstance(rows, list) and rows:
        return str(rows[0].get("display_name") or "")
    return ""


def set_display_name(name: str, root: Optional[Path] = None) -> Dict[str, Any]:
    config = load_config(root)
    session = current_session(root)
    out = _rpc("hc_set_display_name", {"p_name": str(name or "").strip()[:60]},
               config, session["access_token"])
    return {"ok": True, "display_name": (out or {}).get("display_name", "")}


def can_write(project_id: str, root: Optional[Path] = None) -> bool:
    """Whether this account may contribute to a project: its owner, or an
    editor on its roll. Asked of Postgres rather than guessed, because the
    same function decides it in the policies."""
    config = load_config(root)
    session = current_session(root)
    try:
        out = _rpc("hc_can_write", {"pid": str(project_id)}, config,
                   session["access_token"])
    except SupabaseError:
        return False
    return out is True


def project_revision(project_id: str,
                     root: Optional[Path] = None) -> Dict[str, Any]:
    """Counts and latest timestamps: the cheap question, one round trip."""
    config = load_config(root)
    session = current_session(root)
    out = _rpc("hc_project_revision", {"p_project_id": str(project_id)},
               config, session["access_token"])
    return out if isinstance(out, dict) else {}


# One project's last answer, kept so a poll that finds nothing changed costs
# a single query instead of seven. Keyed by project; a workspace serves one.
_SHARED_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_GUARD = __import__("threading").Lock()


def shared_payload(project_id: str, root: Optional[Path] = None,
                   force: bool = False) -> Dict[str, Any]:
    """The ``/api/state`` a shared workspace serves.

    Rebuilt only when the project has actually moved. An open tab polls
    every few seconds and the answer is nearly always "the same as before";
    fetching every row to discover that cost seven round trips and better
    than a second, which made the view stale by the time it arrived.
    """
    from . import shared_state
    pid = str(project_id)
    revision = None
    if not force:
        try:
            revision = project_revision(pid, root)
        except SupabaseError:
            revision = None      # ask the long way rather than fail
        if revision is not None:
            with _CACHE_GUARD:
                held = _SHARED_CACHE.get(pid)
            if held and held.get("revision") == revision:
                return held["payload"]

    rows = fetch_project(pid, root)
    payload = shared_state.build(rows, rows.get("me"),
                                 member_names(pid, root),
                                 can_write=can_write(pid, root))
    if revision is None:
        try:
            revision = project_revision(pid, root)
        except SupabaseError:
            revision = None
    if revision is not None:
        with _CACHE_GUARD:
            _SHARED_CACHE[pid] = {"revision": revision, "payload": payload}
    return payload


def forget_shared(project_id: str) -> None:
    """Drop a cached project -- after a write, so the next read is fresh."""
    with _CACHE_GUARD:
        _SHARED_CACHE.pop(str(project_id), None)


def read_shared(code: str) -> Dict[str, Any]:
    """Open a project someone shared. No account, no session, no vault.

    The anon key in the code is the only key used: the reader is not signed
    in as anyone, and the function on the other side is the only thing they
    are allowed to call.
    """
    parts = parse_code(code)
    config = {"url": parts["url"], "anon_key": parts["anon_key"]}
    out = _post(f"{parts['url']}/rest/v1/rpc/hc_read_shared",
                {"apikey": parts["anon_key"],
                 "Authorization": f"Bearer {parts['anon_key']}"},
                {"token": parts["token"]})
    if not isinstance(out, dict) or not out.get("ok"):
        raise SupabaseError((out or {}).get("error")
                            or "that share is not open")
    return out


def sync_project(root: Optional[Path], cwd) -> Dict[str, Any]:
    """One project, up to Supabase: build the rows, then hand them over.

    The whole project goes in one call. ``hc_sync_project`` upserts what is
    here and deletes the rows of this project that are not, which is only
    correct if it sees the complete set -- so this is deliberately not
    batched.
    """
    if not cwd:
        raise SupabaseError("this chat has no project directory")
    config = load_config(root)
    if not config["url"] or not config["anon_key"]:
        raise SupabaseError(f"fill in {config_path(root)} first")
    session = current_session(root)
    payload = SY.snapshot(root, cwd, session["user_id"])
    result = _post(
        f"{config['url']}/rest/v1/rpc/hc_sync_project",
        {"apikey": config["anon_key"],
         "Authorization": f"Bearer {session['access_token']}"},
        {"payload": payload})
    return {"ok": True, "project_id": payload["project_id"],
            "sent": SY.counts(payload), "result": result}
