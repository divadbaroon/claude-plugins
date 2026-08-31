"""Cross-platform runtime primitives. These run on the CI host (POSIX and,
via windows-port CI, Windows) and assert the OS-appropriate behavior."""
import os
import subprocess
import sys

import pytest

from human_compact import platform_compat as pc


IS_WINDOWS = os.name == "nt"


def test_maybe_fchmod_is_a_noop_without_os_fchmod(monkeypatch, tmp_path):
    # Simulate Windows (no os.fchmod) and confirm the write path does not crash.
    monkeypatch.delattr(os, "fchmod", raising=False)
    path = tmp_path / "f"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        pc.maybe_fchmod(fd, 0o600)  # must not raise
    finally:
        os.close(fd)
    assert path.exists()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX branch")
def test_maybe_fchmod_sets_mode_on_posix(tmp_path):
    path = tmp_path / "f"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        pc.maybe_fchmod(fd, 0o600)
    finally:
        os.close(fd)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_detached_popen_kwargs_matches_platform():
    kwargs = pc.detached_popen_kwargs()
    if IS_WINDOWS:
        assert "creationflags" in kwargs and kwargs["creationflags"]
        assert "start_new_session" not in kwargs
    else:
        assert kwargs == {"start_new_session": True}


def test_pid_alive_true_for_self_false_for_dead():
    assert pc.pid_alive(os.getpid()) is True
    # Spawn a child that exits immediately, reap it, then probe.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    # A reaped pid should read as not-alive (Windows handle gone; POSIX no such pid).
    assert pc.pid_alive(child.pid) is False


def test_a_detached_child_actually_runs_and_can_be_probed(tmp_path):
    marker = tmp_path / "ran.txt"
    code = (
        "import pathlib,sys,time\n"
        f"pathlib.Path(r'{marker}').write_text('ok')\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], **pc.detached_popen_kwargs())
    child.wait(timeout=10)
    assert marker.read_text() == "ok"


def test_replace_process_runs_and_exits_with_child_status():
    # replace_process exits the process, so run it in a subprocess and read the code.
    prog = (
        "import sys;"
        "from human_compact import platform_compat as pc;"
        "pc.replace_process(sys.executable, ['-c','import sys;sys.exit(7)'], dict(__import__('os').environ))"
    )
    result = subprocess.run([sys.executable, "-c", prog])
    assert result.returncode == 7
