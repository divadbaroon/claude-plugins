#!/usr/bin/env python3
"""One goal-page run, as the plugin would POST it to the site.

Drives a disposable goal page through what a reader does -- opens it, adds
a goal, asks Bart, has an operation refused, saves the conversation --
with the model stood in for, and writes the resulting envelope where the
site keeps its examples (docs/observability/example-goal-page-run.json in
the berkeley-research checkout beside this one, or the path given).

The site's ingest test reads that file, so the example is the contract
made concrete: regenerate it when the tracer's shapes change, and the
test says whether the site still takes them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hc" / "src"))

from human_compact import telemetry as T  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

DEFAULT_OUT = REPO.parent / "berkeley-research" / "docs" / "observability" / "example-goal-page-run.json"
SESSION_ID = "3f9c2b7e-1d4a-4e0b-9c6d-2a8e5f7b1c3d"
PROJECT = "Dataset importer"
CARD = {"ok": True, "say": "Two rows, then.", "card": "todos",
        "todos": ["Save the file as parquet", "Name it after the dataset"],
        "subgoals": [{"label": "Reading it back", "todos": ["Open the file the way pandas does"]}]}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(url, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json", "Origin": url})
    with OPENER.open(request, timeout=30) as response:
        return json.loads(response.read())


def fake_claude(command, **kwargs):
    """The claude subprocess, stood in for: one card, whatever is asked."""
    return subprocess.CompletedProcess(command, 0, stdout="Here is the card:\n" + json.dumps(CARD), stderr="")


def drive(chat):
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
    ui._configure_server(server, chat, True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        with mock.patch("human_compact.trajectory.providers.subprocess.run", fake_claude):
            goal = call(url, "/api/goal-page/op", {"op": "add_goal", "title": "Create an interface to import the dataset"})
            piece = call(url, "/api/goal-page/op", {"op": "add_goal", "parent_goal_id": goal["id"],
                                                     "title": "Save the dataset locally to my project folder"})
            call(url, "/api/goal-page")
            call(url, "/api/goal-page/bart", {"goal_id": "", "subgoal_id": piece["id"],
                                              "transcript": [{"role": "you", "text": "I want to save the file as parquet"}]})
            call(url, "/api/goal-page/chat", {"subgoal_id": piece["id"], "messages": [
                {"id": "m-1", "who": "you", "kind": "text", "text": "I want to save the file as parquet"},
                {"id": "m-2", "who": "bart", "kind": "text", "text": "Two rows, then."},
                {"id": "m-3", "who": "bart", "kind": "proposal", "text": "Save the file as parquet"}]})
            call(url, "/api/goal-page/op", {"op": "no_such_op"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return server.telemetry_run


def envelope_of(chat, run):
    """The POST body the forward sink would build from these records."""
    records = T.FileSink.read(chat / "telemetry")
    sink = T.ForwardSink(chat / "telemetry", run, root=chat.parent, post=lambda *a: (200, "{}"),
                         session=lambda: None)
    try:
        return sink.envelope(records["operations"], records["snapshots"], records["events"])
    finally:
        sink.close(0)


def main(argv):
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        chat = root / SESSION_ID
        chat.mkdir()
        project = root / "dataset-importer"
        project.mkdir()
        (project / "main.py").write_text("print('hello')\n")
        home = root / "human-compact"
        home.mkdir()
        (chat / "manifest.json").write_text(json.dumps({
            "cwd": str(project), "project_bound_at": "2026-09-06T12:00:00+00:00"}))
        env = {"HUMAN_COMPACT_HOME": str(home), "HC_AUTOSYNC_SECONDS": "0", "HC_CHAT_PROVIDER": "claude",
               "ENGELBART_TELEMETRY_FORWARD": "false", "ENGELBART_TELEMETRY_FILE": "true",
               "ENGELBART_TELEMETRY": "on", "ENGELBART_TRACE_CONTENT": "true"}
        with mock.patch.dict(os.environ, env):
            run = drive(chat)
            run = dict(run, label=PROJECT, user_hash=None, code_version=run.get("code_version") or "0.20.0")
            envelope = envelope_of(chat, run)
        text = json.dumps(envelope, indent=2, ensure_ascii=False)
        # No path of this machine in the example: the temporary directory
        # is spelled the way a chat's would be on anyone's.
        for real, shown in ((str(root.resolve()), "/tmp/example"), (str(root), "/tmp/example"),
                            (str(Path.home()), "/home/reader")):
            text = text.replace(real, shown)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    counts = {k: len(envelope[k]) for k in ("operations", "snapshots", "events")}
    print("wrote %s: %s, %d bytes" % (out, counts, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
