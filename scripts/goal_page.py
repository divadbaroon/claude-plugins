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

from human_compact.trajectory import ui as UI  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("--no-open", action="store_true",
                    help="print the URL without opening a browser")
    args = ap.parse_args(argv)
    loaded = Path(UI.__file__).resolve()
    if HC_SRC not in loaded.parents:
        raise SystemExit(
            f"loaded human_compact from {loaded}, not this checkout at {HC_SRC}")
    # The server restarts itself when the package's Python changes, by
    # re-running its own arguments through the cli -- which is not this
    # script. The page's own files are read from disk on every request.
    os.environ.setdefault("HC_AUTO_RELOAD", "0")
    with tempfile.TemporaryDirectory(prefix="engelbart-goal-page-") as tmp:
        UI.run(port=args.port, open_browser=not args.no_open,
               trajdir=Path(tmp) / "chat", label="Goal page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
