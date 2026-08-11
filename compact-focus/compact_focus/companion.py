from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .state import StatePaths, atomic_write_text, load_json


class CompanionError(RuntimeError):
    pass


def _plugin_command() -> Path:
    command = Path(__file__).resolve().parents[1] / "bin" / "compact-focus"
    if not command.exists():
        raise CompanionError(f"plugin launcher is unavailable: {command}")
    return command


def _shell_command(values: Sequence[object]) -> str:
    return " ".join(shlex.quote(str(value)) for value in values)


def _review_script(
    paths: StatePaths,
    cycle_id: str,
    result_path: Path,
) -> str:
    command = _shell_command(
        (
            _plugin_command(),
            "--state-root",
            paths.base,
            "review",
            "--session",
            paths.session_id,
            "--cycle",
            cycle_id,
            "--result-file",
            result_path,
        )
    )
    fallback = '{"approved":false,"error":"review window closed before a decision"}'
    return f"""#!/bin/sh
set -u
umask 077
result={shlex.quote(str(result_path))}
terminal_state=""
if [ -t 0 ]; then
  terminal_state=$(stty -g 2>/dev/null || true)
  stty -ixon 2>/dev/null || true
fi
finish() {{
  code=$?
  trap - EXIT HUP INT TERM
  if [ -n "$terminal_state" ]; then
    stty "$terminal_state" 2>/dev/null || true
  fi
  if [ ! -f "$result" ]; then
    umask 077
    printf '%s\\n' {shlex.quote(fallback)} > "$result"
  fi
  exit "$code"
}}
trap finish EXIT HUP INT TERM
if ! cd -- {shlex.quote(paths.cwd)}; then
  printf '%s\\n' '{{"approved":false,"error":"project directory is unavailable"}}' > "$result"
  exit 1
fi
{command}
"""


def _custom_launcher(script: Path) -> Optional[Tuple[Sequence[str], str]]:
    configured = os.environ.get("COMPACT_FOCUS_TERMINAL_LAUNCHER", "").strip()
    if not configured:
        return None
    try:
        values = shlex.split(configured)
    except ValueError as exc:
        raise CompanionError(
            f"COMPACT_FOCUS_TERMINAL_LAUNCHER is invalid: {exc}"
        ) from exc
    if not values:
        raise CompanionError("COMPACT_FOCUS_TERMINAL_LAUNCHER is empty")
    if any("{script}" in value for value in values):
        values = [value.replace("{script}", str(script)) for value in values]
    else:
        values.append(str(script))
    return values, "custom"


def terminal_launch_command(script: Path) -> Tuple[Sequence[str], str]:
    custom = _custom_launcher(script)
    if custom:
        return custom
    if os.environ.get("TMUX") and shutil.which("tmux"):
        return ("tmux", "split-window", "-h", str(script)), "tmux"
    if sys.platform == "darwin":
        executable = shutil.which("open")
        if executable:
            application = os.environ.get("COMPACT_FOCUS_MAC_TERMINAL", "Terminal")
            return (executable, "-a", application, str(script)), application
    candidates = (
        ("x-terminal-emulator", "-e"),
        ("gnome-terminal", "--"),
        ("konsole", "-e"),
        ("xterm", "-e"),
    )
    for executable, separator in candidates:
        resolved = shutil.which(executable)
        if resolved:
            return (resolved, separator, str(script)), executable
    raise CompanionError(
        "no companion terminal is available; set COMPACT_FOCUS_TERMINAL_LAUNCHER "
        "to a terminal command ending in {script}"
    )


def open_review_terminal(
    script: Path,
) -> Tuple[str, Optional[subprocess.Popen]]:
    command, launcher = terminal_launch_command(script)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise CompanionError(f"could not open the {launcher} review terminal: {exc}") from exc
    try:
        returncode = process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        # xterm and some custom launchers remain alive until their child exits.
        # Keep the handle so the caller can reap it after the decision artifact lands.
        return launcher, process
    if returncode != 0:
        raise CompanionError(
            f"could not open the {launcher} review terminal (exit {returncode})"
        )
    return launcher, None


def _read_result(path: Path, cycle_id: str) -> Optional[Dict[str, Any]]:
    value = load_json(path, None)
    if not isinstance(value, dict):
        return None
    recorded_cycle = str(value.get("cycle_id") or cycle_id)
    if recorded_cycle != cycle_id:
        raise CompanionError(
            f"review result belongs to {recorded_cycle}, expected {cycle_id}"
        )
    return value


def wait_for_review(
    result_path: Path,
    cycle_id: str,
    *,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    if timeout is None:
        configured = os.environ.get("COMPACT_FOCUS_REVIEW_TIMEOUT_SECONDS", "3300")
        try:
            timeout = float(configured)
        except ValueError as exc:
            raise CompanionError(
                f"COMPACT_FOCUS_REVIEW_TIMEOUT_SECONDS is invalid: {configured}"
            ) from exc
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        value = _read_result(result_path, cycle_id)
        if value is not None:
            return value
        time.sleep(0.1)
    raise CompanionError(
        f"review timed out after {timeout:g}s; compaction was not performed"
    )


def run_companion_review(paths: StatePaths, cycle_id: str) -> Tuple[bool, str]:
    cycle = paths.cycle(cycle_id)
    result_path = cycle / "review-result.json"
    script_path = cycle / "open-review.command"
    with contextlib.suppress(FileNotFoundError):
        result_path.unlink()
    atomic_write_text(
        script_path,
        _review_script(paths, cycle_id, result_path),
        mode=0o700,
    )
    launch_process: Optional[subprocess.Popen] = None
    try:
        launcher, launch_process = open_review_terminal(script_path)
        result = wait_for_review(result_path, cycle_id)
    finally:
        with contextlib.suppress(FileNotFoundError):
            script_path.unlink()
        if launch_process is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                launch_process.wait(timeout=2)
    error = str(result.get("error") or "").strip()
    if error:
        raise CompanionError(error)
    return bool(result.get("approved")), launcher
