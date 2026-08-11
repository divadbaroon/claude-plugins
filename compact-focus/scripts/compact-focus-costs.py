#!/usr/bin/env python3
"""compact-focus-costs — per-prompt context-window cost from a session JSONL.

A unit = one real user prompt plus everything until the next one (assistant
turns, tool calls, tool results). Cost = chars/4 token estimate of all that
content — what removing the unit would actually free. Window inferred from
model ids in the transcript ("1m" marker → 1M), COMPACT_FOCUS_WINDOW
overrides, 200k default.

Usage: compact-focus-costs.py <transcript.jsonl>
Emits JSON: {"window": N, "units": [{"i","prompt","tokens","pct"}]}"""

import json
import os
import sys


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


def est_chars(content):
    """Char count with base64 payloads (screenshots, PDF pages) charged a
    flat image-token cost instead of their raw length — vision inputs are
    tokenized as images, not text, and chars/4 on base64 overcounts 20x."""
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


def prompt_text(entry):
    content = entry["message"]["content"]
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return " ".join(content.split())


def main(path):
    window = int(os.environ.get("COMPACT_FOCUS_WINDOW", 0)) or None
    units, chars, model_1m = [], 0, False
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            model = str((e.get("message") or {}).get("model", ""))
            if "1m" in model.lower():
                model_1m = True
            msg = e.get("message")
            if not isinstance(msg, dict) or "content" not in msg:
                continue
            if is_prompt(e):
                if units:
                    units[-1]["chars"] = chars
                chars = 0
                units.append({"i": len(units) + 1, "prompt": prompt_text(e)[:100]})
            chars += est_chars(msg.get("content"))
    if units:
        units[-1]["chars"] = chars
    if window is None:
        window = 1_000_000 if model_1m else 200_000
    for u in units:
        u["tokens"] = u.pop("chars") // 4
        u["pct"] = round(u["tokens"] * 100.0 / window, 1)
    print(json.dumps({"window": window, "units": units}))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: compact-focus-costs.py <transcript.jsonl>"}))
        sys.exit(1)
    try:
        main(sys.argv[1])
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
