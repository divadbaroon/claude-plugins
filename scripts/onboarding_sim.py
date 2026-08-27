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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[1]
HC_SRC = REPO / "hc" / "src"
sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
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


@dataclass(frozen=True)
class Scenario:
    description: str
    saved_projects: bool = False
    starts_in_saved_project: bool = False
    legacy_goal: bool = False
    already_bound: bool = False


SCENARIOS = {
    "first-use": Scenario(
        "an unbound first chat with no projects Engelbart has saved",
    ),
    "returning": Scenario(
        "an unbound chat with two projects available to resume",
        saved_projects=True,
    ),
    "in-project": Scenario(
        "an unbound chat opened inside a project Engelbart already knows",
        saved_projects=True,
        starts_in_saved_project=True,
    ),
    "legacy-goals": Scenario(
        "an upgraded chat with existing goals but no explicit project binding",
        saved_projects=True,
        starts_in_saved_project=True,
        legacy_goal=True,
    ),
    "already-onboarded": Scenario(
        "a chat already bound to a project, showing the post-onboarding landing",
        saved_projects=True,
        starts_in_saved_project=True,
        already_bound=True,
    ),
}


def _example_projects(state_root: Path) -> dict[str, Path]:
    homes = {}
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
        homes[name] = home
    return homes


def seed(state_root: Path, scenario_name: str = "returning") -> Path:
    """Create one scenario below state_root and return its real chat store."""
    state_root = state_root.resolve()
    try:
        scenario = SCENARIOS[scenario_name]
    except KeyError as exc:
        raise ValueError(f"unknown onboarding scenario: {scenario_name}") from exc

    projects = _example_projects(state_root) if scenario.saved_projects else {}
    starting_dir = (
        projects[EXAMPLE_PROJECTS[0][0]]
        if scenario.starts_in_saved_project
        else state_root / "project-open-in-claude"
    )
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

    if scenario.legacy_goal:
        CS.save_goals(
            SESSION_ID,
            {
                "version": 1,
                "goals": [GM.new_goal(
                    "g1",
                    "Preserve the work already in this chat",
                    description="This goal predates explicit project binding.",
                )],
            },
            {"items": []},
            root=state_root,
        )
    if scenario.already_bound:
        CS.bind_project(SESSION_ID, str(starting_dir), root=state_root)

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


def _checkout_label() -> str:
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=REPO,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        return f"{branch or 'detached'} @ {commit}{' + working edits' if dirty else ''}"
    except (OSError, subprocess.CalledProcessError):
        return "working tree"


def _assert_checkout_import() -> None:
    loaded = Path(CS.__file__).resolve()
    if HC_SRC.resolve() not in loaded.parents:
        raise RuntimeError(
            f"loaded human_compact from {loaded}, not this checkout at {HC_SRC}"
        )


def run(
    scenario_name: str = "returning",
    port: int = 0,
    open_browser: bool = True,
) -> int:
    _assert_checkout_import()
    process: Optional[subprocess.Popen] = None
    with tempfile.TemporaryDirectory(prefix="engelbart-onboarding-") as held:
        state_root = Path(held)
        session_dir = seed(state_root, scenario_name)
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
        print(f"\n  scenario · {scenario_name}", flush=True)
        print(f"  state    · {SCENARIOS[scenario_name].description}", flush=True)
        print(f"  source   · {HC_SRC} ({_checkout_label()})", flush=True)
        print("  package  · none; the npm wheel and installed runtime are bypassed",
              flush=True)
        print("  sandbox  · " + str(state_root), flush=True)
        print("  Ctrl-C deletes the sandbox and resets the scenario\n", flush=True)
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
        "scenario",
        nargs="?",
        default="returning",
        choices=tuple(SCENARIOS) + ("list",),
        help="scenario to run, or 'list' to describe them",
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
    if args.scenario == "list":
        width = max(map(len, SCENARIOS))
        for name, scenario in SCENARIOS.items():
            print(f"{name:<{width}}  {scenario.description}")
        return 0
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        return run(
            scenario_name=args.scenario,
            port=args.port,
            open_browser=not args.no_open,
        )
    except RuntimeError as exc:
        print(f"onboarding simulator: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
