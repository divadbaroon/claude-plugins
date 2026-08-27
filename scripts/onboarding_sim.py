#!/usr/bin/env python3
"""Run Engelbart's real onboarding UI against a disposable local vault."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[1]
HC_SRC = REPO / "hc" / "src"
sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import ui as UI  # noqa: E402


SESSION_ID = "engelbart-onboarding-sim"
EXAMPLE_PROJECTS = (
    (
        "CourseText Commons",
        "Make difficult course material easier to navigate without flattening it.",
    ),
    (
        "Requirement Testing Study",
        "Test whether black-box specifications improve transfer in CS1.",
    ),
)


def seed(state_root: Path) -> Path:
    """Create one unbound chat and realistic choices, all below state_root."""
    state_root = state_root.resolve()
    starting_dir = state_root / "starting-directory"
    starting_dir.mkdir(parents=True, exist_ok=True)

    CS.ingest_hook(
        {
            "session_id": SESSION_ID,
            "hook_event_name": "SessionStart",
            "cwd": str(starting_dir),
        },
        root=state_root,
    )
    CS.mark_goals_ui_invoked(SESSION_ID, root=state_root)

    for name, objective in EXAMPLE_PROJECTS:
        home = PS.workspace_home(state_root, name)
        if home is None:
            raise RuntimeError(f"could not create example project: {name}")
        home.mkdir(parents=True, exist_ok=True)
        PS.save_project(
            state_root,
            str(home),
            {"name": name, "objective": objective},
        )

    return CS.paths(SESSION_ID, state_root).session_dir


def _server_url(registry: Path, process: subprocess.Popen, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"onboarding server exited during startup ({process.returncode})"
            )
        try:
            record = json.loads(registry.read_text(encoding="utf-8"))
            url = record.get("url") if isinstance(record, dict) else None
            if isinstance(url, str) and url.startswith("http://127.0.0.1:"):
                return url
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise RuntimeError("onboarding server did not publish a URL within 10 seconds")


def _stop(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def run(port: int = 0, open_browser: bool = True) -> int:
    process: Optional[subprocess.Popen] = None
    with tempfile.TemporaryDirectory(prefix="engelbart-onboarding-") as held:
        state_root = Path(held)
        session_dir = seed(state_root)
        env = os.environ.copy()
        env["HC_CHAT_STATE_DIR"] = str(state_root)
        env["HC_CHAT_UI_IDLE_SECONDS"] = "0"
        prior_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(HC_SRC)] + ([prior_pythonpath] if prior_pythonpath else [])
        )
        command = [
            sys.executable,
            "-u",
            "-m",
            "human_compact.cli",
            "chat-serve",
            "--session",
            SESSION_ID,
            "--port",
            str(port),
        ]
        print("\n  disposable onboarding state · " + str(state_root), flush=True)
        print("  both onboarding forks are available; Ctrl-C resets everything\n",
              flush=True)
        try:
            process = subprocess.Popen(command, cwd=REPO, env=env)
            url = _server_url(session_dir / "server.json", process)
            if open_browser:
                webbrowser.open(url)
            return process.wait()
        except KeyboardInterrupt:
            return 130
        finally:
            _stop(process)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Open the production Engelbart onboarding workflow with isolated, "
            "disposable state."
        )
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="first loopback port to try (default: any free port)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the URL without opening a browser",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        return run(port=args.port, open_browser=not args.no_open)
    except RuntimeError as exc:
        print(f"onboarding simulator: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
