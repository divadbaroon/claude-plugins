#!/usr/bin/env python3
"""compact-focus-costs — ACTIVE-context trace + per-prompt cost.

Measures the context that is actually live: everything AFTER the latest
compaction boundary (isCompactSummary entry), with the compact summary
itself as source u0. The full historical JSONL is never summed — a long
session's history is not the window's contents.

Emits (stdout and, via the caller, trace.json/costs.json):
{
  "schema_version": 2, "session_id", "project_id",
  "window": {"size": N, "source": "statusline|env|model-marker|model-family|default"},
  "boundary": {"found": bool, "line": n},
  "units": [{"u": 0, "kind": "compact_summary"|"prompt", "prompt": head,
             "tokens": n, "pct": x, "bytes": [start, end], "uuids": [...]}],
  "classes": {file_changes|subagents|todos|other: {tokens, pct}}
}
Percentages are ESTIMATES (chars/4, flat image charge) until calibrated
against /context.

Usage: compact-focus-costs.py <transcript.jsonl> [--statusline <schema.json>]"""

import hashlib
import json
import os
import sys

FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
AGENT_TOOLS = {"Task", "Agent"}
TODO_TOOLS = {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput"}


def classify(name):
    if name in FILE_TOOLS:
        return "file_changes"
    if name in AGENT_TOOLS:
        return "subagents"
    if name in TODO_TOOLS:
        return "todos"
    return "other"


def est_chars(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(est_chars(b) for b in content)
    if isinstance(content, dict):
        total = 0
        for k, v in content.items():
            if k == "data" and isinstance(v, str) and len(v) > 1000:
                total += 6000  # ~1500 tokens per embedded image
            else:
                total += est_chars(v)
        return total
    return 0


def is_prompt(entry):
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return False
    text = text.strip()
    return bool(text) and not text.startswith("<") and not text.startswith("/")


def prompt_text(entry):
    content = entry["message"]["content"]
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return " ".join(content.split())


def infer_window(statusline_path, entries):
    if os.environ.get("COMPACT_FOCUS_WINDOW"):
        return int(os.environ["COMPACT_FOCUS_WINDOW"]), "env"
    if statusline_path and os.path.isfile(statusline_path):
        try:
            p = json.load(open(statusline_path))
            for path_keys in (("context_window", "context_window_size"),
                              ("context_window_size",), ("context_window", "total_tokens"),
                              ("context_window", "max_tokens"), ("context", "window_size"),
                              ("model", "context_window")):
                v = p
                for k in path_keys:
                    v = v.get(k) if isinstance(v, dict) else None
                    if v is None:
                        break
                if isinstance(v, (int, float)) and v > 0:
                    return int(v), "statusline"
        except Exception:
            pass
    models = {str((e.get("message") or {}).get("model", "")) for e in entries}
    joined = " ".join(models).lower()
    if "1m" in joined:
        return 1_000_000, "model-marker"
    if any(m for m in ("fable", "-5-", "opus-5", "sonnet-5") if m in joined):
        return 1_000_000, "model-family"
    return 200_000, "default"


def main(path, statusline_path=None):
    raw_entries = []           # (entry, byte_start, byte_end, line_no)
    pos = 0
    with open(path, "rb") as f:
        for ln, line in enumerate(f):
            end = pos + len(line)
            try:
                e = json.loads(line)
                if isinstance(e, dict):
                    raw_entries.append((e, pos, end, ln))
            except Exception:
                pass
            pos = end

    # latest compaction boundary
    boundary_idx = None
    for i, (e, *_rest) in enumerate(raw_entries):
        if e.get("isCompactSummary"):
            boundary_idx = i
    active = raw_entries[boundary_idx:] if boundary_idx is not None else raw_entries
    window, wsource = infer_window(statusline_path, [e for e, *_ in raw_entries])

    session_id = next((e.get("sessionId") for e, *_ in raw_entries if e.get("sessionId")), "")
    cwd = next((e.get("cwd") for e, *_ in raw_entries if e.get("cwd")), "")
    project_id = hashlib.sha1(cwd.encode()).hexdigest()[:12] if cwd else ""

    units, classes, toolmap = [], {"file_changes": 0, "subagents": 0, "todos": 0, "other": 0}, {}

    def new_unit(kind, head, bstart):
        units.append({"u": len(units), "kind": kind, "prompt": head[:100],
                      "chars": 0, "bytes": [bstart, bstart], "uuids": []})

    for i, (e, bstart, bend, ln) in enumerate(active):
        msg = e.get("message")
        if boundary_idx is not None and i == 0:
            new_unit("compact_summary", "(compaction summary — current baseline)", bstart)
        elif is_prompt(e):
            new_unit("prompt", prompt_text(e), bstart)
        if not isinstance(msg, dict) or "content" not in msg:
            continue
        if not units:
            new_unit("preamble", "(pre-prompt context)", bstart)
        u = units[-1]
        u["chars"] += est_chars(msg.get("content"))
        u["bytes"][1] = bend
        if e.get("uuid"):
            u["uuids"].append(e["uuid"])
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    cls = classify(str(b.get("name", "")))
                    toolmap[b.get("id")] = cls
                    classes[cls] += est_chars(b)
                elif b.get("type") == "tool_result":
                    classes[toolmap.get(b.get("tool_use_id"), "other")] += est_chars(b)
                else:
                    classes["other"] += est_chars(b)
        else:
            classes["other"] += est_chars(content)

    for u in units:
        u["tokens"] = u.pop("chars") // 4
        u["pct"] = round(u["tokens"] * 100.0 / window, 1)
    out = {
        "schema_version": 2,
        "session_id": session_id,
        "project_id": project_id,
        "window": {"size": window, "source": wsource},
        "boundary": {"found": boundary_idx is not None,
                     "line": raw_entries[boundary_idx][3] if boundary_idx is not None else None},
        "units": units,
        "classes": {k: {"tokens": v // 4, "pct": round(v / 4 * 100.0 / window, 1)}
                    for k, v in classes.items()},
    }
    print(json.dumps(out))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: compact-focus-costs.py <transcript.jsonl> [--statusline <f>]"}))
        sys.exit(1)
    sl = None
    if "--statusline" in sys.argv:
        sl = sys.argv[sys.argv.index("--statusline") + 1]
    try:
        main(sys.argv[1], sl)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
