"""Discover Vault sessions in the analysis window and normalize their turns.

Turn IDs are stable evidence anchors: "<sid8>#<index>" where index counts
user/assistant turns in transcript order after noise filtering. Every
downstream inference must trace to these IDs.
"""
import json
import os
from datetime import date, timedelta
from pathlib import Path

from .secure_io import atomic_write_json, secure_dir

VAULT = Path(os.environ.get("CLAUDE_VAULT_DIR", Path.home() / ".claude-vault"))
MAX_TURNS = 80          # last N turns per session fed to extraction
MAX_TURN_CHARS = 300
LOW_EVIDENCE_USER_TURNS = 3


def _text_of(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _clean(t):
    t = " ".join(t.split())
    return t[:MAX_TURN_CHARS] + "…" if len(t) > MAX_TURN_CHARS else t


def load_session(conv_path: Path, day: str):
    sid = conv_path.parent.name
    turns, cwd = [], ""
    try:
        with conv_path.open() as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("isMeta") or e.get("isCompactSummary") or e.get("isSidechain"):
                    continue
                if not cwd and e.get("cwd"):
                    cwd = e["cwd"]
                role = e.get("type")
                if role not in ("user", "assistant"):
                    continue
                text = _clean(_text_of(e.get("message", {}) or {}))
                if not text or text.startswith("<"):
                    continue
                if role == "user" and text.startswith("/"):
                    continue
                turns.append({"role": role, "text": text})
    except OSError:
        return None
    turns = turns[-MAX_TURNS:]
    for i, t in enumerate(turns):
        t["id"] = f"{sid[:8]}#{i:03d}"
    n_user = sum(1 for t in turns if t["role"] == "user")
    if n_user == 0:
        return None          # nothing observable, not merely "short"
    return {
        "session_id": sid, "date": day, "cwd": cwd, "turns": turns,
        "user_turn_count": n_user,
        "low_evidence": n_user < LOW_EVIDENCE_USER_TURNS,
    }


def discover(days: int):
    """Yield normalized sessions from the last `days` days, oldest first."""
    root = VAULT / "sessions"
    if not root.is_dir():
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for datedir in sorted(root.iterdir()):
        if not datedir.is_dir() or datedir.name < cutoff:
            continue
        for sess in sorted(datedir.iterdir()):
            conv = sess / "conversation.jsonl"
            if conv.is_file():
                s = load_session(conv, datedir.name)
                if s:
                    out.append(s)
    return out


def write_evidence_index(sessions, outdir: Path):
    """Turn-id -> resolvable evidence record, for the UI drawer."""
    secure_dir(outdir, VAULT)
    idx = {}
    for s in sessions:
        for t in s["turns"]:
            idx[t["id"]] = {"session_id": s["session_id"], "date": s["date"],
                            "role": t["role"], "text": t["text"], "cwd": s["cwd"]}
    atomic_write_json(outdir / "evidence_index.json", idx, root=VAULT)
    return idx
