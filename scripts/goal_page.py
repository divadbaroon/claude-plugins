#!/usr/bin/env python3
"""Open the goal page from this checkout, on a disposable session.

The installed runtime under ~/.human-compact serves whatever wheel it was
built from. This serves web/goal as it is in the working tree instead, so
an edit to the page shows on the next reload. Nothing is written outside a
temporary directory, and Ctrl-C removes it.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HC_SRC = REPO / "hc" / "src"
sys.path.insert(0, str(HC_SRC))

from human_compact.trajectory import chat_state as CS  # noqa: E402
from human_compact.trajectory import project_store as PS  # noqa: E402
from human_compact.trajectory import ui as UI  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("--no-open", action="store_true",
                    help="print the URL without opening a browser")
    ap.add_argument("--seed", action="store_true",
                    help="start on the design's example goal, in a project with a "
                         "plan, not an empty chat (Build all still runs a real "
                         "build, in this directory)")
    args = ap.parse_args(argv)
    loaded = Path(UI.__file__).resolve()
    if HC_SRC not in loaded.parents:
        raise SystemExit(
            f"loaded human_compact from {loaded}, not this checkout at {HC_SRC}")
    # The server restarts itself when the package's Python changes, by
    # re-running its own arguments through the cli -- which is not this
    # script. The page's own files are read from disk on every request.
    os.environ.setdefault("HC_AUTO_RELOAD", "0")
    # A disposable session is not this machine's work: the edits made here
    # stay in the temporary directory and are never sent to the account.
    os.environ.setdefault("HC_AUTOSYNC_SECONDS", "0")
    with tempfile.TemporaryDirectory(prefix="engelbart-goal-page-") as tmp:
        chat = Path(tmp) / "chat"
        if args.seed:
            chat.mkdir()
            seed(chat)
        UI.run(port=args.port, open_browser=not args.no_open,
               trajdir=chat, label="Goal page")
    return 0


def seed(chat):
    """The design's example content, written through the page's own
    operations: the goal, three subgoals, two todos on the first, notes on
    the first as the setup would seed them -- in a project with a name and
    a plan, as a chat that came through the web setup is."""
    root = chat.parent
    project = root / "dataset-importer"
    project.mkdir()
    PS.save_project(root, str(project), {
        "name": "Dataset importer",
        "objective": "Get the lab's survey data into the browser.",
        "description": "Get the lab's survey data into the browser.\n"
                       "Import a CSV, keep it with the project, look at it."})
    CS.bind_project(chat.name, str(project), root)
    goal = UI._apply({"op": "add_goal",
                      "title": "Create an interface to import the dataset"},
                     chat)["id"]
    subgoals = [
        UI._apply({"op": "add_goal", "title": title, "parent_goal_id": goal},
                  chat)["id"]
        for title in ("Create a blank interface with an import button",
                      "Save the dataset locally to my project folder",
                      "Allow me to inspect the dataset in a CSV viewer")]
    UI._apply({"op": "set_notes", "goal_id": subgoals[0],
               "notes": "One window, one button, nothing else on it yet.\n\n"
                        "Why this matters: nothing can be inspected before "
                        "it can be brought in."}, chat)
    for text in ("Create a blank interface", "Add an import button"):
        UI._apply({"op": "add_todo_row", "goal_id": subgoals[0], "text": text},
                  chat)


if __name__ == "__main__":
    raise SystemExit(main())
