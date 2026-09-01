"""Cross-platform runtime primitives.

The backend was written for POSIX: it tightens files with mode bits, detaches
children into their own session, kills them by process group, and replaces its
own image with ``execvpe``. Windows has none of those exactly -- NTFS enforces
access through ACLs rather than 0600/0700 bits, there is no process group to
signal, and a process cannot replace its own image. These helpers keep the
POSIX behavior identical where it exists and provide the nearest Windows
equivalent everywhere else, so call sites carry no per-OS branching of their own.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Mapping, NoReturn, Sequence


IS_WINDOWS = os.name == "nt"


def maybe_fchmod(fd: int, mode: int) -> None:
    """Tighten an open descriptor's mode where the OS honors it.

    POSIX enforces the 0600/0700 secrecy this backend relies on through these
    bits; NTFS does not have them (and ``os.fchmod`` does not exist on Windows),
    so there is nothing to set and this is a no-op there. Windows privacy comes
    from the file living under the user's own profile.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)


def detached_popen_kwargs() -> dict:
    """``subprocess``/``Popen`` kwargs that fully detach a background child so it
    outlives the process that spawned it.

    POSIX puts it in its own session (``start_new_session``); Windows detaches it
    from this console and gives it a new process group so no window appears and a
    later ``kill_process_tree`` can find it.
    """
    if IS_WINDOWS:
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return {"creationflags": flags}
    return {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    """Report whether a process is still running, without disturbing it.

    POSIX uses the classic ``kill(pid, 0)`` probe. On Windows that same call maps
    to ``TerminateProcess`` and would *kill* the process, so liveness is queried
    through the Win32 handle API instead (WAIT_TIMEOUT on a zero-length wait
    means the process has not exited).
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if IS_WINDOWS:
        import ctypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists but belongs to another user; that still counts as alive.
        return True
    except OSError:
        return False
    return True


def kill_process_tree(pid: int, *, force: bool = False) -> None:
    """Terminate a detached child and every descendant it started.

    POSIX signals the process group the detached child leads (that is why the
    spawners set ``start_new_session``); Windows walks the tree with
    ``taskkill /T``. Missing or already-dead processes are not an error.
    """
    if IS_WINDOWS:
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        subprocess.run(args, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_pid(pid: int, *, force: bool = False) -> None:
    """Signal a single process portably. ``SIGKILL`` does not exist on Windows,
    where a forced kill is ``taskkill /F`` (TerminateProcess)."""
    if IS_WINDOWS:
        args = ["taskkill", "/PID", str(pid)]
        if force:
            args.append("/F")
        subprocess.run(args, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def replace_process(path: str, args: Sequence[str],
                    env: Mapping[str, str]) -> NoReturn:
    """Hand this process off to another program, resolving ``path`` on PATH.

    POSIX replaces the image with ``execvpe`` so the PID, signals and terminal
    carry straight over. Windows cannot replace an image, so the child is run to
    completion and this process exits with its status -- the closest observable
    equivalent (the caller becomes a thin parent for the child's lifetime).
    ``args`` are the arguments after the program name.
    """
    if IS_WINDOWS:
        completed = subprocess.run([path, *args], env=dict(env))
        sys.exit(completed.returncode)
    os.execvpe(path, [path, *args], dict(env))
