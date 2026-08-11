from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple


class TerminalError(RuntimeError):
    pass


@dataclass(frozen=True)
class TerminalTarget:
    path: str
    claude_pid: Optional[int]
    lineage: Tuple[Tuple[int, int, str, str], ...]


def _process_row(pid: int) -> Optional[Tuple[int, str, str]]:
    try:
        output = subprocess.check_output(
            ["ps", "-o", "ppid=,tty=,command=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    parts = output.split(None, 2)
    if len(parts) < 2:
        return None
    return int(parts[0]), parts[1], parts[2] if len(parts) == 3 else ""


def find_terminal() -> TerminalTarget:
    if os.isatty(0) and os.isatty(1):
        try:
            path = os.ttyname(0)
        except OSError as exc:
            raise TerminalError(str(exc)) from exc
        return TerminalTarget(path, None, ())

    lineage: List[Tuple[int, int, str, str]] = []
    terminal_path = ""
    claude_pid: Optional[int] = None
    seen = set()
    pid = os.getppid()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        row = _process_row(pid)
        if row is None:
            break
        parent, tty_name, command = row
        lineage.append((pid, parent, tty_name, command))
        lowered = command.lower()
        executable = lowered.split(None, 1)[0] if lowered else ""
        if claude_pid is None and (
            executable.endswith("/claude")
            or executable == "claude"
            or "/claude/" in executable
            or "@anthropic-ai/claude-code" in lowered
        ):
            claude_pid = pid
        if not terminal_path and tty_name not in {"?", "??", "-"}:
            terminal_path = tty_name if tty_name.startswith("/") else f"/dev/{tty_name}"
        pid = parent
    if not terminal_path:
        raise TerminalError("no terminal was found in the hook process ancestry")
    if claude_pid is None:
        rendered = " -> ".join(f"{pid}:{command[:50]}" for pid, _parent, _tty, command in lineage)
        raise TerminalError(f"terminal found but Claude host process was not identified ({rendered})")
    return TerminalTarget(terminal_path, claude_pid, tuple(lineage))


def _start_resume_watchdog(host_pid: int, timeout: int = 3700) -> Tuple[int, int]:
    read_fd, write_fd = os.pipe()
    watcher = os.fork()
    if watcher == 0:  # pragma: no cover - exercised only by real hook sessions
        os.close(write_fd)
        parent = os.getppid()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                ready, _write, _error = select.select([read_fd], [], [], 0.25)
                if ready:
                    os.read(read_fd, 1)
                    os._exit(0)
                try:
                    os.kill(parent, 0)
                except OSError:
                    break
        finally:
            with contextlib.suppress(OSError):
                os.kill(host_pid, signal.SIGCONT)
            os.close(read_fd)
        os._exit(0)
    os.close(read_fd)
    return watcher, write_fd


@contextlib.contextmanager
def terminal_lease() -> Iterator[TerminalTarget]:
    if os.name != "posix":
        raise TerminalError("inline review currently requires a POSIX terminal")
    target = find_terminal()
    terminal_fd = os.open(target.path, os.O_RDWR)
    saved = [os.dup(fd) for fd in (0, 1, 2)]
    watcher: Optional[int] = None
    watchdog_fd: Optional[int] = None
    stopped = False
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        if target.claude_pid is not None:
            watcher, watchdog_fd = _start_resume_watchdog(target.claude_pid)
            os.kill(target.claude_pid, signal.SIGSTOP)
            stopped = True
        for destination in (0, 1, 2):
            os.dup2(terminal_fd, destination)
        yield target
    finally:
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        for destination, source in zip((0, 1, 2), saved):
            with contextlib.suppress(OSError):
                os.dup2(source, destination)
            os.close(source)
        os.close(terminal_fd)
        if stopped and target.claude_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(target.claude_pid, signal.SIGCONT)
        if watchdog_fd is not None:
            with contextlib.suppress(OSError):
                os.write(watchdog_fd, b"x")
            os.close(watchdog_fd)
        if watcher is not None:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(watcher, 0)
