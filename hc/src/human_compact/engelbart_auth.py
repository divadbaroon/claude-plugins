"""Connect Claude Code to a metered Engelbart LiteLLM account.

Supabase proves the human identity. Berkeley's authenticated control plane
returns that member's LiteLLM virtual key and policy. The key stays in the
owner-only Claude vault and Claude Code reads it through ``bart token``; it is
never copied into ``settings.json`` or a shell profile.
"""
from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from .trajectory import supabase_client as SB
from .trajectory.secure_io import atomic_write_json

CONFIG_URL = "https://berkeley.mathetic.com/api/engelbart-config"
CREDENTIALS_URL = "https://berkeley.mathetic.com/api/engelbart-credentials"
CREDENTIALS_NAME = "engelbart-credentials.json"
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_S = 20.0

_TOP_SETTINGS = (
    "apiKeyHelper",
    "availableModels",
    "enforceAvailableModels",
    "model",
)
_ENV_SETTINGS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
)


class EngelbartAuthError(RuntimeError):
    """An authentication/configuration error the operator can act on."""


def credentials_path(root: Optional[Path] = None) -> Path:
    return SB._vault(root) / CREDENTIALS_NAME


def claude_settings_path(home: Optional[Path] = None) -> Path:
    configured = str(os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    base = (Path(configured).expanduser().absolute()
            if configured else (home or Path.home()) / ".claude")
    return base / "settings.json"


def _read_json(path: Path, *, absent=None):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return absent
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EngelbartAuthError(f"refusing non-regular private file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EngelbartAuthError(f"cannot read {path}: {exc}") from None
    return value


def _request_json(url: str, *, method: str = "GET",
                  access_token: str = "", open_url=None) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    request = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        headers=headers,
        method=method,
    )
    opener = open_url or urllib.request.urlopen
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            value = json.loads(raw.decode("utf-8", errors="replace"))
            detail = str(value.get("error") or "") if isinstance(value, dict) else ""
        except (OSError, ValueError):
            detail = ""
        if exc.code in (401, 403):
            message = "the Engelbart session is no longer authorized; run `bart auth` again"
        elif exc.code == 409:
            message = detail or "the Engelbart credit account is not ready"
        elif exc.code >= 500:
            message = "the Engelbart credit service is not configured or is temporarily unavailable"
        else:
            message = detail or f"the Engelbart service returned HTTP {exc.code}"
        raise EngelbartAuthError(message) from None
    except (urllib.error.URLError, OSError) as exc:
        raise EngelbartAuthError(f"could not reach Engelbart: {exc}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise EngelbartAuthError("the Engelbart service returned an unexpectedly large response")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EngelbartAuthError("the Engelbart service returned invalid JSON") from None
    if not isinstance(value, dict):
        raise EngelbartAuthError("the Engelbart service returned an invalid response")
    return value


def _https_origin(value: Any, name: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise EngelbartAuthError(f"Engelbart returned an invalid {name}")
    return f"https://{parsed.netloc}"


def fetch_public_config(open_url=None) -> Dict[str, Any]:
    value = _request_json(CONFIG_URL, open_url=open_url)
    url = _https_origin(value.get("supabaseUrl"), "Supabase URL")
    anon_key = str(value.get("supabaseAnonKey") or "").strip()
    if len(anon_key) < 20 or any(character.isspace() for character in anon_key):
        raise EngelbartAuthError("Engelbart returned an invalid Supabase public key")
    return {
        "url": url,
        "anon_key": anon_key,
        "credits_enabled": bool(value.get("creditsEnabled")),
    }


def ensure_mathetic_config(public: Dict[str, Any],
                           root: Optional[Path] = None) -> Path:
    expected_url = SB.normalize_url(str(public["url"]))
    expected_key = str(public["anon_key"])
    path = SB.config_path(root)
    try:
        effective = SB.load_config(root)
    except SB.SupabaseError as exc:
        raise EngelbartAuthError(str(exc)) from None
    effective_url = SB.normalize_url(effective.get("url") or "")
    effective_key = str(effective.get("anon_key") or "")
    if ((effective_url or effective_key)
            and (effective_url != expected_url or effective_key != expected_key)):
        raise EngelbartAuthError(
            "HC_SUPABASE_URL/HC_SUPABASE_ANON_KEY or the stored config points "
            "to another Supabase project; Engelbart will not override it."
        )

    stored = _read_json(path, absent=None)
    if stored is not None:
        if not isinstance(stored, dict):
            raise EngelbartAuthError(f"{path} must contain a JSON object")
        stored_url = SB.normalize_url(str(stored.get("url") or ""))
        stored_key = str(stored.get("anon_key") or "").strip()
        placeholders = ("YOUR-PROJECT-REF", "PASTE-YOUR")
        if any(marker in stored_url or marker in stored_key for marker in placeholders):
            stored_url = stored_key = ""
        if stored_url or stored_key:
            if stored_url != expected_url or stored_key != expected_key:
                raise EngelbartAuthError(
                    f"{path} is connected to another Supabase project; "
                    "Engelbart will not overwrite it. Use a separate "
                    "CLAUDE_VAULT_DIR or move that config aside."
                )
            return path
        if stored and not ("url" in stored and "anon_key" in stored):
            raise EngelbartAuthError(
                f"{path} is not an Engelbart Supabase config; move it aside "
                "or use a separate CLAUDE_VAULT_DIR."
            )
    try:
        return SB.save_config(expected_url, expected_key, root=root)
    except SB.SupabaseError as exc:
        raise EngelbartAuthError(str(exc)) from None


def _current_or_browser_session(root: Optional[Path] = None,
                                announce=None) -> Dict[str, Any]:
    try:
        return SB.current_session(root)
    except SB.SupabaseError:
        SB.sign_out(root)
    if announce:
        announce("Opening the Engelbart sign-in page in your browser…")
    try:
        return SB.sign_in_with_browser(
            root=root,
            announce=(lambda url: announce("If it did not open: " + url))
            if announce else None,
        )
    except SB.SupabaseError as exc:
        raise EngelbartAuthError(str(exc)) from None


def fetch_credentials(access_token: str, open_url=None) -> Dict[str, Any]:
    provisioned = _request_json(
        CREDENTIALS_URL,
        method="POST",
        access_token=access_token,
        open_url=open_url,
    )
    if provisioned.get("ready") is not True:
        raise EngelbartAuthError("the Engelbart credit account was not provisioned")
    value = _request_json(
        CREDENTIALS_URL,
        access_token=access_token,
        open_url=open_url,
    )
    api_key = str(value.get("apiKey") or "")
    base_url = _https_origin(value.get("baseUrl"), "LiteLLM base URL")
    models = value.get("models")
    if not api_key.startswith("sk-") or any(character.isspace() for character in api_key):
        raise EngelbartAuthError("Engelbart returned an invalid virtual key")
    if (not isinstance(models, list) or not models
            or any(not isinstance(model, str) or not model.strip() for model in models)):
        raise EngelbartAuthError("Engelbart returned an invalid model policy")
    try:
        budget = float(value.get("budgetUsd") or 0)
        spend = float(value.get("spendUsd") or 0)
    except (TypeError, ValueError):
        raise EngelbartAuthError("Engelbart returned an invalid budget policy") from None
    if not math.isfinite(budget) or budget <= 0 or not math.isfinite(spend) or spend < 0:
        raise EngelbartAuthError("Engelbart returned an invalid budget policy")
    return {
        "apiKey": api_key,
        "baseUrl": base_url,
        "budgetUsd": budget,
        "spendUsd": spend,
        "models": list(dict.fromkeys(model.strip() for model in models)),
        "rpmLimit": value.get("rpmLimit"),
        "tpmLimit": value.get("tpmLimit"),
    }


def _bart_executable() -> str:
    explicit = str(os.environ.get("BART_EXECUTABLE") or "").strip()
    if explicit:
        return str(Path(explicit).expanduser().absolute())
    invoked = str(sys.argv[0] or "bart")
    if os.sep in invoked:
        return str(Path(invoked).expanduser().absolute())
    found = shutil.which(invoked) or shutil.which("bart")
    if not found:
        raise EngelbartAuthError("cannot locate the stable `bart` executable")
    return str(Path(found).absolute())


def _setting_backup(settings: Dict[str, Any]) -> Dict[str, Any]:
    environment = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    return {
        "top": {
            name: {"present": name in settings, "value": settings.get(name)}
            for name in _TOP_SETTINGS
        },
        "env": {
            name: {"present": name in environment, "value": environment.get(name)}
            for name in _ENV_SETTINGS
        },
    }


def _managed_settings(credentials: Dict[str, Any], executable: str) -> Dict[str, Any]:
    models = credentials["models"]
    default = models[0]

    def family(name: str) -> str:
        return next((model for model in models if name in model), default)

    return {
        "top": {
            "apiKeyHelper": shlex.quote(executable) + " token",
            "availableModels": models,
            "enforceAvailableModels": True,
            "model": default,
        },
        "env": {
            "ANTHROPIC_BASE_URL": credentials["baseUrl"],
            "ANTHROPIC_DEFAULT_OPUS_MODEL": family("opus"),
            "ANTHROPIC_DEFAULT_SONNET_MODEL": family("sonnet"),
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": family("haiku"),
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        },
    }


def configure_claude(credentials: Dict[str, Any], *,
                     settings_file: Optional[Path] = None,
                     previous_record: Optional[Dict[str, Any]] = None,
                     executable: Optional[str] = None) -> Dict[str, Any]:
    path = settings_file or claude_settings_path()
    settings = _read_json(path, absent={})
    if not isinstance(settings, dict):
        raise EngelbartAuthError(f"{path} must contain a JSON object")
    old_backup = ((previous_record or {}).get("settingsBackup")
                  if isinstance(previous_record, dict) else None)
    backup = old_backup if isinstance(old_backup, dict) else _setting_backup(settings)
    managed = _managed_settings(credentials, executable or _bart_executable())
    environment = settings.get("env")
    if environment is None:
        environment = {}
    if not isinstance(environment, dict):
        raise EngelbartAuthError(f"the env field in {path} must be a JSON object")
    environment = dict(environment)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        environment.pop(name, None)
    environment.update(managed["env"])
    settings.update(managed["top"])
    settings["env"] = environment
    atomic_write_json(path, settings, root=path.parent, indent=2)
    return {"settingsBackup": backup, "settingsManaged": managed}


def _restore_slot(container: Dict[str, Any], name: str,
                  managed: Dict[str, Any], backup: Dict[str, Any]) -> None:
    if name not in managed or container.get(name) != managed[name]:
        return
    original = backup.get(name) if isinstance(backup, dict) else None
    if isinstance(original, dict) and original.get("present"):
        container[name] = original.get("value")
    else:
        container.pop(name, None)


def restore_claude(record: Dict[str, Any], *,
                   settings_file: Optional[Path] = None) -> None:
    path = settings_file or claude_settings_path()
    settings = _read_json(path, absent=None)
    if settings is None:
        return
    if not isinstance(settings, dict):
        raise EngelbartAuthError(f"{path} must contain a JSON object")
    backup = record.get("settingsBackup") or {}
    managed = record.get("settingsManaged") or {}
    for name in _TOP_SETTINGS:
        _restore_slot(settings, name, managed.get("top") or {}, backup.get("top") or {})
    environment = settings.get("env")
    if isinstance(environment, dict):
        # Authentication deliberately removes static credentials so the
        # helper wins. Restore an old one only while the slot is still empty;
        # a credential the user added after `bart auth` is their newer choice.
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            original = (backup.get("env") or {}).get(name)
            if (name not in environment and isinstance(original, dict)
                    and original.get("present")):
                environment[name] = original.get("value")
        for name in _ENV_SETTINGS:
            _restore_slot(
                environment,
                name,
                managed.get("env") or {},
                backup.get("env") or {},
            )
        if not environment:
            settings.pop("env", None)
    atomic_write_json(path, settings, root=path.parent, indent=2)


def authenticate(*, root: Optional[Path] = None,
                 settings_file: Optional[Path] = None,
                 open_url=None, announce=None,
                 executable: Optional[str] = None) -> Dict[str, Any]:
    public = fetch_public_config(open_url)
    ensure_mathetic_config(public, root)
    session = _current_or_browser_session(root, announce)
    credentials = fetch_credentials(session["access_token"], open_url)
    existing = _read_json(credentials_path(root), absent={})
    if existing is not None and not isinstance(existing, dict):
        raise EngelbartAuthError(f"{credentials_path(root)} must contain a JSON object")
    settings_state = configure_claude(
        credentials,
        settings_file=settings_file,
        previous_record=existing,
        executable=executable,
    )
    record = {
        "schema": 1,
        **credentials,
        "email": session.get("email") or "",
        "userId": session.get("user_id") or "",
        "updatedAt": int(time.time()),
        **settings_state,
    }
    atomic_write_json(
        credentials_path(root),
        record,
        root=credentials_path(root).parent,
        indent=2,
    )
    return record


def token(root: Optional[Path] = None) -> str:
    record = _read_json(credentials_path(root), absent={})
    key = str(record.get("apiKey") or "") if isinstance(record, dict) else ""
    if not key.startswith("sk-") or any(character.isspace() for character in key):
        raise EngelbartAuthError("Engelbart is not connected; run `bart auth`")
    return key


def status(root: Optional[Path] = None) -> Dict[str, Any]:
    account = SB.status(root)
    record = _read_json(credentials_path(root), absent={})
    connected = isinstance(record, dict) and str(record.get("apiKey") or "").startswith("sk-")
    return {
        "signedIn": bool(account.get("signed_in")),
        "connected": connected,
        "email": (record.get("email") if connected else account.get("email")) or "",
        "budgetUsd": float(record.get("budgetUsd") or 0) if connected else 0,
        "spendUsd": float(record.get("spendUsd") or 0) if connected else 0,
        "models": record.get("models") or [] if connected else [],
    }


def logout(*, root: Optional[Path] = None,
           settings_file: Optional[Path] = None) -> None:
    record = _read_json(credentials_path(root), absent={})
    if isinstance(record, dict) and record:
        restore_claude(record, settings_file=settings_file)
    credentials_path(root).unlink(missing_ok=True)
    SB.sign_out(root)


def shell_credential_conflicts() -> list:
    return [name for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
            if str(os.environ.get(name) or "").strip()]
