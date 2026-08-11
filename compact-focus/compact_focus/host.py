from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


HOST_CLAUDE = "claude"
HOST_CODEX = "codex"
HOSTS = {HOST_CLAUDE, HOST_CODEX}

_CODEX_RECORD_TYPES = {
    "compacted",
    "event_msg",
    "response_item",
    "session_meta",
    "turn_context",
    "world_state",
}


def transcript_host(path: Path) -> Optional[str]:
    """Identify a supported rollout without depending on its filename."""
    try:
        with path.open(encoding="utf-8") as handle:
            for _index, line in zip(range(64), handle):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                row_type = str(row.get("type") or "")
                if row_type in _CODEX_RECORD_TYPES:
                    return HOST_CODEX
                if "message" in row or "sessionId" in row or "isCompactSummary" in row:
                    return HOST_CLAUDE
    except OSError:
        return None
    return None


def detect_host(
    payload: Optional[Dict[str, Any]] = None,
    transcript: Optional[Path] = None,
) -> str:
    override = os.environ.get("COMPACT_FOCUS_HOST", "").strip().lower()
    if override in HOSTS:
        return override

    candidate = transcript
    if candidate is None and isinstance(payload, dict):
        raw = str(payload.get("transcript_path") or "")
        if raw:
            candidate = Path(raw).expanduser()
    if candidate is not None:
        identified = transcript_host(candidate)
        if identified:
            return identified

    # Codex defines both the native names and Claude-compatible aliases. Claude
    # Code currently defines only the latter.
    if os.environ.get("PLUGIN_ROOT") or os.environ.get("PLUGIN_DATA"):
        return HOST_CODEX
    return HOST_CLAUDE
