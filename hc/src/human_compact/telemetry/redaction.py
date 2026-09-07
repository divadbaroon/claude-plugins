"""Central redaction for everything the telemetry layer records.

Callers hand over the real value -- a prompt, a subprocess's output, an
error, a request body -- and get back a bounded copy with credentials,
signed tokens and raw bytes gone. No caller redacts on its own; this is
the one place the rules live (the same rules as the site's
``api/_lib/telemetry/redaction.js``), so a new secret is caught everywhere
by adding it here once.
"""
from __future__ import annotations

import hashlib
import os
import re
import traceback
from datetime import date, datetime
from pathlib import PurePath
from typing import Any, Dict, Iterable, Optional, Set
from urllib.parse import urlsplit

REDACTED = "[redacted]"
MAX_STRING = 64 * 1024
MAX_DEPTH = 24

# Object keys whose values are never recorded, whatever they hold.
SECRET_KEY = re.compile("^(?:" + "|".join([
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "apikey", "api[-_]?key", "x-api-key", "anon[-_]?key",
    "service[-_]?role[-_]?key", "master[-_]?key",
    "token", "access[-_]?token", "refresh[-_]?token", "id[-_]?token",
    "paper[-_]?token", "machine[-_]?token",
    "secret", "client[-_]?secret", "password", "passwd", "private[-_]?key",
    "signature", "upload[-_]?url", "signed[-_]?url", "signedurl",
]) + ")$", re.I)

# Substrings that read as credentials wherever they appear inside a string.
# Only the signed-URL pattern keeps a prefix (its capture group).
SECRET_PATTERNS = (
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), None),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), None),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), None),
    (re.compile(r"\begb_[A-Za-z0-9_-]{8,}"), None),
    (re.compile(r"([?&](?:token|apikey|api_key|access_token|signature|sig|key|code)=)[^&\s\"'<>]+", re.I), 1),
)

# Environment variables whose VALUES are stripped from every recorded
# string, plus any variable whose name says it holds a credential.
ENV_SECRET_NAMES = ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY",
                    "LITELLM_MASTER_KEY", "ENGELBART_CREDENTIAL_KEY",
                    "ENGELBART_ADMIN_SESSION_SECRET", "OTEL_EXPORTER_OTLP_HEADERS",
                    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                    "HC_SUPABASE_ANON_KEY")
ENV_SECRET_NAME = re.compile(r"(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.I)


def secret_values(env: Optional[Dict[str, str]] = None,
                  extra: Iterable[Any] = ()) -> Set[str]:
    out: Set[str] = set()
    source = os.environ if env is None else env
    for name, value in source.items():
        if name not in ENV_SECRET_NAMES and not ENV_SECRET_NAME.search(name):
            continue
        text = str(value or "")
        if len(text) >= 8:
            out.add(text)
    for value in extra:
        text = str(value or "")
        if len(text) >= 8:
            out.add(text)
    return out


def redact_string(value: str, secrets: Iterable[str], max_string: int = MAX_STRING) -> str:
    text = value
    # Patterns first, so "Bearer <known secret>" goes as one token; the known
    # values then catch anything the patterns did not recognise.
    for pattern, keep in SECRET_PATTERNS:
        if keep is None:
            text = pattern.sub(REDACTED, text)
        else:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    if len(text) > max_string:
        text = "%s [… %d more chars truncated]" % (text[:max_string], len(text) - max_string)
    return text


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_base64_source(value: Any) -> bool:
    return (isinstance(value, dict) and value.get("type") == "base64"
            and isinstance(value.get("data"), str))


def bytes_ref(data: bytes, media_type: Optional[str] = None) -> Dict[str, Any]:
    """Bytes are referenced, never copied: their length and hash."""
    out: Dict[str, Any] = {"[bytes]": len(data), "sha256": sha256(data)}
    if media_type:
        out["media_type"] = media_type
    return out


class _State:
    __slots__ = ("secrets", "max_string", "max_depth", "seen")

    def __init__(self, secrets, max_string, max_depth):
        self.secrets = secrets
        self.max_string = max_string
        self.max_depth = max_depth
        self.seen: Set[int] = set()


def _redact_value(value: Any, state: _State, depth: int) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return redact_string(value, state.secrets, state.max_string)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes_ref(bytes(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, PurePath):
        return redact_string(str(value), state.secrets, state.max_string)
    if isinstance(value, BaseException):
        return sanitize_error(value, secrets=state.secrets)
    if callable(value) and not isinstance(value, (dict, list, tuple, set, frozenset)):
        return None
    if depth >= state.max_depth:
        return "[depth]"
    marker = id(value)
    if marker in state.seen:
        return "[cycle]"
    state.seen.add(marker)
    try:
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_redact_value(item, state, depth + 1) for item in value]
        if _is_base64_source(value):
            import base64
            try:
                data = base64.b64decode(value["data"], validate=False)
            except Exception:                            # noqa: BLE001
                data = b""
            rest = {k: v for k, v in value.items() if k != "data"}
            out = _redact_value(rest, state, depth + 1)
            out["data"] = REDACTED
            out["source_ref"] = bytes_ref(data, value.get("media_type"))
            return out
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                name = str(key)
                out[name] = REDACTED if SECRET_KEY.match(name) else _redact_value(item, state, depth + 1)
            return out
        if hasattr(value, "_asdict"):
            return _redact_value(value._asdict(), state, depth + 1)
        return redact_string(str(value), state.secrets, state.max_string)
    finally:
        state.seen.discard(marker)


def redact(value: Any, secrets: Optional[Iterable[str]] = None,
           max_string: int = MAX_STRING, max_depth: int = MAX_DEPTH,
           env: Optional[Dict[str, str]] = None) -> Any:
    """A safe deep copy of any value: credentials out, bytes referenced,
    strings bounded, cycles cut. Never raises -- a value it cannot walk is
    described."""
    state = _State(set(secrets) if secrets is not None else secret_values(env),
                   int(max_string) or MAX_STRING, int(max_depth) or MAX_DEPTH)
    try:
        return _redact_value(value, state, 0)
    except Exception as exc:                                 # noqa: BLE001
        return {"[unredactable]": str(exc)[:200]}


def _error_attribute(error, name):
    # Some exceptions (notably HTTPError on Python 3.9) delegate missing
    # attributes to a response stream that may not exist. Telemetry must
    # still preserve the original error instead of raising another one.
    try:
        return getattr(error, name, None)
    except Exception:
        return None


def sanitize_error(error: Any, secrets: Optional[Iterable[str]] = None,
                   stack: bool = False, env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """The parts of an error the record may hold. The message is redacted
    like any string; the stack only when asked, since it can quote inputs."""
    if error is None:
        return None
    known = set(secrets) if secrets is not None else secret_values(env)
    if isinstance(error, BaseException):
        name = type(error).__name__
        message = str(error) or name
    elif isinstance(error, dict):
        name = str(error.get("name") or "Error")
        message = str(error.get("message") or error)
    else:
        name = "Error"
        message = str(error)
    out: Dict[str, Any] = {"name": name[:80],
                           "message": redact_string(message, known, 500)}
    status = _error_attribute(error, "status_code")
    if status is None:
        code = _error_attribute(error, "code")
        if isinstance(code, int) and not isinstance(code, bool):
            status = code
    if isinstance(error, dict):
        status = error.get("status_code", status)
    if status is not None:
        try:
            out["status_code"] = int(status)
        except (TypeError, ValueError):
            out["status_code"] = 0
    code = _error_attribute(error, "code")
    if isinstance(error, dict):
        code = error.get("code")
    if isinstance(code, str) and code:
        out["code"] = code[:80]
    detail = _error_attribute(error, "detail")
    if isinstance(error, dict):
        detail = error.get("detail")
    if detail is not None:
        out["detail"] = redact_string(str(detail), known, 300)
    if stack and isinstance(error, BaseException):
        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        out["stack"] = redact_string(text, known, 4000)
    return out


def safe_url(value: Any) -> str:
    """A URL with the query and fragment gone: a signed URL's token lives there."""
    try:
        parts = urlsplit(str(value))
        if not parts.scheme or not parts.netloc:
            return ""
        return "%s://%s%s" % (parts.scheme, parts.netloc, parts.path)
    except (ValueError, TypeError):
        return ""


def host_of(value: Any) -> str:
    try:
        return urlsplit(str(value)).hostname or ""
    except (ValueError, TypeError):
        return ""
