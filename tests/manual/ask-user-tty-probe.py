#!/usr/bin/env python3
"""Verify that a PreToolUse hook can own /dev/tty for AskUserQuestion."""

import json
import os
import signal
import subprocess
import sys


def terminal_candidates():
    """Yield direct terminal device paths from this hook's ancestors."""
    seen = set()
    pid = os.getppid()
    lineage = []
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            row = subprocess.check_output(
                ["ps", "-o", "ppid=,tty=,comm=", "-p", str(pid)],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            break
        if not row:
            break
        parts = row.split(None, 2)
        if len(parts) < 2:
            break
        parent, tty_name = int(parts[0]), parts[1]
        lineage.append((pid, tty_name, parts[2] if len(parts) == 3 else ""))
        if tty_name not in {"?", "??", "-"}:
            path = tty_name if tty_name.startswith("/") else f"/dev/{tty_name}"
            yield path, lineage
        pid = parent
    yield "", lineage


def main() -> int:
    payload = json.load(sys.stdin)
    questions = payload.get("tool_input", {}).get("questions", [])
    if not questions or questions[0].get("question") != "CF_TTY_PROBE":
        return 0

    tty_fd = None
    host_pid = None
    attempts = []
    for tty_path, lineage in terminal_candidates():
        if not tty_path:
            break
        try:
            tty_fd = os.open(tty_path, os.O_RDWR)
            host_pid = next(
                (pid for pid, _tty, command in lineage if "claude" in command.lower()),
                None,
            )
            break
        except OSError as exc:
            attempts.append(f"{tty_path}: {exc}")
    if tty_fd is None:
        detail = "; ".join(attempts) or f"no ancestor tty; lineage={lineage!r}"
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"CF_TTY_UNAVAILABLE: {detail}",
            }
        }))
        return 0

    if host_pid is None:
        os.close(tty_fd)
        raise RuntimeError(f"terminal found but Claude host absent; lineage={lineage!r}")

    read_fd = os.dup(tty_fd)
    os.kill(host_pid, signal.SIGSTOP)
    try:
        with os.fdopen(read_fd, "r", buffering=1) as tty_in, os.fdopen(
            tty_fd, "w", buffering=1
        ) as tty_out:
            tty_out.write("\nCF_TTY_EXCLUSIVE — press Enter to return the hook answer.\n")
            tty_in.readline()
    finally:
        os.kill(host_pid, signal.SIGCONT)

    question_text = questions[0]["question"]
    updated = dict(payload["tool_input"])
    updated["answers"] = {question_text: "CF_TTY_HOOK_ANSWER"}
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
