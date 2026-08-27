#!/usr/bin/env python3
"""Run Engelbart's real onboarding UI against a disposable local vault."""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    "worked-chat": Scenario(
        "a new project whose one chat already contains substantive work",
        saved_projects=True,
        starts_in_saved_project=True,
        already_bound=True,
    ),
    "cold-start": Scenario(
        "a new project whose blank chat contains only the /bart launch",
        saved_projects=True,
        starts_in_saved_project=True,
        already_bound=True,
    ),
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

CONSOLE_SCENARIOS = ("worked-chat", "cold-start")

CONSOLE_DETAILS = {
    "worked-chat": (
        "One substantive Claude Code conversation is present before /bart. "
        "Four inferred outcome-goals appear, with three levels at the deepest "
        "branch. Every goal is core because the new project has no objective."
    ),
    "cold-start": (
        "The chat contains only /bart, which is deliberately excluded from "
        "goal evidence. The real goals workspace therefore opens with zero "
        "goals. This checkout has a weak '+ Add goal' affordance, but no "
        "guided first-goal or review flow."
    ),
}

SHOWCASE_PROJECT = "Engelbart First-Run Study"


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


def _showcase_project(state_root: Path) -> Path:
    home = PS.workspace_home(state_root, SHOWCASE_PROJECT)
    if home is None:
        raise RuntimeError("could not create the showcase project")
    (home / "src").mkdir(parents=True, exist_ok=True)
    (home / "README.md").write_text(
        "# Engelbart onboarding\n\nCompare worked-chat and cold-start states.\n",
        encoding="utf-8",
    )
    (home / "src" / "onboarding.js").write_text(
        "export const firstRun = { review: false, emptyPrompt: false };\n",
        encoding="utf-8",
    )
    # Named, but deliberately without an objective: inference has no baseline
    # against which to call any goal supporting or unrelated.
    PS.save_project(state_root, str(home), {"name": SHOWCASE_PROJECT})
    return home


def _user_record(text: str, uid: str, cwd: Path, minute: int) -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "promptId": uid,
        "timestamp": f"2026-08-26T17:{minute:02d}:00Z",
        "cwd": str(cwd),
        "isSidechain": False,
        "origin": {"kind": "human"},
        "promptSource": {"kind": "typed"},
        "message": {"role": "user", "content": text},
    }


def _assistant_record(blocks: list, uid: str, cwd: Path, minute: int) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "timestamp": f"2026-08-26T17:{minute:02d}:00Z",
        "cwd": str(cwd),
        "isSidechain": False,
        "message": {"role": "assistant", "content": blocks},
    }


def _tool_result_record(
    tool_id: str,
    text: str,
    uid: str,
    assistant_uid: str,
    cwd: Path,
    minute: int,
) -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "timestamp": f"2026-08-26T17:{minute:02d}:00Z",
        "cwd": str(cwd),
        "isSidechain": False,
        "sourceToolAssistantUUID": assistant_uid,
        "toolUseResult": {"stdout": text, "stderr": ""},
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": text,
            }],
        },
    }


def _write_showcase_transcript(
    state_root: Path,
    project: Path,
    worked: bool,
) -> Path:
    transcript = state_root / "claude-transcript.jsonl"
    records = []
    if worked:
        records.extend([
            _user_record(
                "Build a reproducible way to experience Engelbart onboarding "
                "without replacing my installed data.",
                "prompt-1", project, 1,
            ),
            _assistant_record([
                {"type": "text", "text":
                 "I am tracing the onboarding state boundary and will keep "
                 "the simulator isolated from the installed vault."},
                {"type": "tool_use", "id": "plan-1", "name": "update_plan",
                 "input": {"plan": [
                     {"step": "Model a worked chat", "status": "in_progress"},
                     {"step": "Model a cold start", "status": "pending"},
                     {"step": "Verify source provenance", "status": "pending"},
                 ]}},
                {"type": "tool_use", "id": "read-1", "name": "Read",
                 "input": {"file_path": str(project / "README.md")}},
            ], "assistant-1", project, 2),
            _tool_result_record(
                "plan-1", "Plan recorded.", "result-1", "assistant-1", project, 3,
            ),
            _tool_result_record(
                "read-1", "Compared worked-chat and cold-start states.",
                "result-2", "assistant-1", project, 4,
            ),
            _user_record(
                "The cold-start path is blank. Compare it with a chat that "
                "already contains substantive work.",
                "prompt-2", project, 5,
            ),
            _assistant_record([
                {"type": "text", "text":
                 "The launcher is excluded from evidence, so a blank chat "
                 "correctly infers no goals but leaves the user without a wedge."},
            ], "assistant-2", project, 6),
            _user_record(
                "Expose both states from a terminal-style web launcher and "
                "make sure it imports the checked-out source.",
                "prompt-3", project, 7,
            ),
            _assistant_record([
                {"type": "text", "text":
                 "The scenario console now launches the production workspace "
                 "against one disposable chat store at a time."},
            ], "assistant-3", project, 8),
        ])
    records.append(_user_record("/bart", "bart-launcher", project, 9))
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return transcript


def _worked_goal_tree(state_root: Path, project: Path) -> dict:
    events = [
        event for event in CS.load_events(SESSION_ID, state_root)
        if event.get("usable_for_goals") and not event.get("redacted")
    ]
    prompts = CS.load_prompts(SESSION_ID, state_root)
    evidence = [str(event["id"]) for event in events]
    prompt_ids = [str(prompt["id"]) for prompt in prompts]
    source = [{"id": "s1", "type": "local", "label": str(project)}]

    def goal(gid, title, parent, description, prompt_slice):
        return GM.new_goal(
            gid,
            title,
            parent,
            description=description,
            evidence_ids=list(evidence),
            prompt_ids=prompt_ids[prompt_slice],
            sources=source,
            relevance="core",
            relevance_why="",
            relevance_for="",
            origin="inferred",
        )

    return {"version": 1, "goals": [
        goal(
            "g1", "Make Engelbart onboarding reproducible", None,
            "A developer can repeatedly enter first-run states without "
            "changing the installed vault.", slice(0, 3),
        ),
        goal(
            "g2", "Model first-run states faithfully", "g1",
            "The preview distinguishes a substantive prior chat from a "
            "launcher-only cold start.", slice(1, 3),
        ),
        goal(
            "g3", "Keep simulation isolated from installed Engelbart data", "g2",
            "Every run uses a disposable store and the checked-out source.",
            slice(0, 1),
        ),
        goal(
            "g4", "Give cold-start users an actionable goals landing", None,
            "The empty state makes the missing first-goal prompt observable.",
            slice(1, 3),
        ),
    ]}


def seed(state_root: Path, scenario_name: str = "returning") -> Path:
    """Create one scenario below state_root and return its real chat store."""
    state_root = state_root.resolve()
    try:
        scenario = SCENARIOS[scenario_name]
    except KeyError as exc:
        raise ValueError(f"unknown onboarding scenario: {scenario_name}") from exc

    showcase = scenario_name in CONSOLE_SCENARIOS
    if showcase:
        starting_dir = _showcase_project(state_root)
        projects = {SHOWCASE_PROJECT: starting_dir}
    else:
        projects = _example_projects(state_root) if scenario.saved_projects else {}
        starting_dir = (
            projects[EXAMPLE_PROJECTS[0][0]]
            if scenario.starts_in_saved_project
            else state_root / "project-open-in-claude"
        )
    starting_dir.mkdir(parents=True, exist_ok=True)

    transcript = (
        _write_showcase_transcript(
            state_root, starting_dir, worked=scenario_name == "worked-chat"
        )
        if showcase else None
    )

    CS.ingest_hook(
        {
            "session_id": SESSION_ID,
            "hook_event_name": "SessionStart",
            "cwd": str(starting_dir),
            **({"transcript_path": str(transcript)} if transcript else {}),
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
    if scenario_name == "worked-chat":
        CS.save_goals(
            SESSION_ID,
            _worked_goal_tree(state_root, starting_dir),
            {"items": []},
            root=state_root,
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


def _child_environment(state_root: Path) -> dict:
    env = os.environ.copy()
    env["HC_CHAT_STATE_DIR"] = str(state_root)
    env["HC_CHAT_UI_IDLE_SECONDS"] = "0"
    prior_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(HC_SRC)] + ([prior_pythonpath] if prior_pythonpath else [])
    )
    return env


class ScenarioRunner:
    """Own exactly one disposable production workspace at a time."""

    def __init__(self, port: int = 0):
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self.temporary: Optional[tempfile.TemporaryDirectory] = None
        self.state_root: Optional[Path] = None
        self.scenario_name = ""
        self.url = ""

    def start(self, scenario_name: str) -> str:
        self.stop()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="engelbart-onboarding-"
        )
        self.state_root = Path(self.temporary.name)
        self.scenario_name = scenario_name
        try:
            session_dir = seed(self.state_root, scenario_name)
            command = [
                sys.executable,
                "-u",
                "-m",
                "human_compact.cli",
                "chat-serve",
                "--session",
                SESSION_ID,
                "--port",
                str(self.port),
            ]
            self.process = subprocess.Popen(
                command,
                cwd=REPO,
                env=_child_environment(self.state_root),
            )
            self.url = _server_url(session_dir / "server.json", self.process)
            return self.url
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        _stop(self.process)
        self.process = None
        self.url = ""
        self.scenario_name = ""
        self.state_root = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None


def run(
    scenario_name: str = "returning",
    port: int = 0,
    open_browser: bool = True,
) -> int:
    _assert_checkout_import()
    runner = ScenarioRunner(port)
    print(f"\n  scenario · {scenario_name}", flush=True)
    print(f"  state    · {SCENARIOS[scenario_name].description}", flush=True)
    print(f"  source   · {HC_SRC} ({_checkout_label()})", flush=True)
    print("  package  · none; the npm wheel and installed runtime are bypassed",
          flush=True)
    print("  Ctrl-C deletes the sandbox and resets the scenario\n", flush=True)
    try:
        url = runner.start(scenario_name)
        print("  sandbox  · " + str(runner.state_root), flush=True)
        if open_browser:
            webbrowser.open(url)
        return runner.process.wait() if runner.process is not None else 1
    except KeyboardInterrupt:
        return 130
    finally:
        runner.stop()


def console_html(token: str) -> bytes:
    cards = []
    labels = {
        "worked-chat": "01 · worked chat before /bart",
        "cold-start": "02 · blank chat, then /bart",
    }
    expected = {
        "worked-chat": "expect: 4 core outcome-goals · max depth 3",
        "cold-start": "expect: 0 goals · no error · weak manual-add affordance",
    }
    for name in CONSOLE_SCENARIOS:
        cards.append(
            '<section class="scenario">'
            f'<div class="scenario-name">{html.escape(labels[name])}</div>'
            f'<p>{html.escape(CONSOLE_DETAILS[name])}</p>'
            f'<div class="expect">{html.escape(expected[name])}</div>'
            f'<button data-run="{html.escape(name)}">run {html.escape(name)}</button>'
            '</section>'
        )
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Engelbart · first-run console</title>
<style>
:root{color-scheme:dark;--bg:#080b0d;--panel:#0e1316;--line:#263139;
--ink:#d8e1e5;--mut:#7f919a;--green:#8ccf7e;--amber:#e6b566;--red:#d96c75}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
main{width:min(980px,calc(100% - 32px));margin:7vh auto}
.terminal{border:1px solid var(--line);background:var(--panel);box-shadow:0 24px 80px #0008}
.bar{padding:9px 13px;border-bottom:1px solid var(--line);color:var(--mut)}
.bar b{color:var(--green);font-weight:500}.body{padding:24px}
h1{font-size:15px;font-weight:500;margin:0 0 8px;color:var(--green)}
.lede{color:var(--mut);margin:0 0 24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.scenario{border:1px solid var(--line);padding:17px;min-height:260px;display:flex;flex-direction:column}
.scenario-name{color:var(--amber);font-weight:600}.scenario p{color:#a9b7bd}
.expect{margin-top:auto;color:var(--mut);border-top:1px dashed var(--line);padding-top:12px}
button{margin-top:15px;text-align:left;font:inherit;color:var(--green);background:#0a100c;
border:1px solid #31513a;padding:9px 11px;cursor:pointer}button:hover{border-color:var(--green)}
.controls{display:flex;gap:9px;margin-top:16px}.controls button{margin:0;color:var(--mut);border-color:var(--line);background:none}
pre{white-space:pre-wrap;min-height:72px;color:var(--mut);margin:20px 0 0}
.prompt{color:var(--green)}.error{color:var(--red)}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
</style></head><body><main><div class="terminal">
<div class="bar"><b>●</b> engelbart-sim · checked-out source · disposable state</div>
<div class="body"><h1>$ engelbart-sim first-run</h1>
<p class="lede">Choose the evidence state that exists immediately before /bart. The selected state opens in the unchanged production workspace.</p>
<div class="grid">__CARDS__</div>
<div class="controls"><button id="stop">stop active scenario</button></div>
<pre id="log"><span class="prompt">ready.</span> each run replaces the previous sandbox.</pre>
</div></div></main>
<script>
const token=__TOKEN__, log=document.getElementById('log');
function say(text,bad=false){log.className=bad?'error':'';log.textContent=text}
async function post(action,scenario,target){
  say('$ '+action+(scenario?' '+scenario:'')+'\nstarting…');
  try{
    const response=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,scenario})});
    const data=await response.json(); if(!response.ok||!data.ok)throw new Error(data.error||'request failed');
    if(data.url){say('$ run '+scenario+'\nsource: '+data.source+'\nworkspace: '+data.url+'\nopened in the scenario tab');target.location=data.url}
    else say('$ stop\nno scenario is running');
  }catch(error){if(target)target.close();say('$ '+action+'\n'+error.message,true)}
}
document.querySelectorAll('[data-run]').forEach(button=>button.onclick=()=>{
  const target=window.open('about:blank','engelbart-scenario');
  post('run',button.dataset.run,target);
});
document.getElementById('stop').onclick=()=>post('stop');
</script></body></html>"""
    return (page.replace("__CARDS__", "".join(cards))
            .replace("__TOKEN__", json.dumps(token))).encode("utf-8")


class ScenarioConsole:
    def __init__(self):
        self.runner = ScenarioRunner(0)
        self.lock = threading.RLock()

    def start(self, scenario_name: str) -> str:
        if scenario_name not in CONSOLE_SCENARIOS:
            raise ValueError("the web console exposes only the two first-run scenarios")
        with self.lock:
            return self.runner.start(scenario_name)

    def stop(self) -> None:
        with self.lock:
            self.runner.stop()


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "EngelbartScenarioConsole/1"

    def log_message(self, _format, *_args):
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json")

    def _trusted_host(self) -> bool:
        return self.headers.get("Host") == self.server.expected_host

    def do_GET(self):
        if not self._trusted_host():
            self._json(403, {"ok": False, "error": "wrong host"})
        elif self.path == "/":
            self._send(200, console_html(self.server.token), "text/html; charset=utf-8")
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._trusted_host():
            self._json(403, {"ok": False, "error": "wrong host"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._json(415, {"ok": False, "error": "application/json required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 <= length <= 4096:
            self._json(400, {"ok": False, "error": "invalid request length"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, TypeError):
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        supplied = str(body.get("token") or "") if isinstance(body, dict) else ""
        if not secrets.compare_digest(supplied, self.server.token):
            self._json(403, {"ok": False, "error": "invalid console token"})
            return
        try:
            if self.path == "/api/run":
                name = str(body.get("scenario") or "")
                url = self.server.console.start(name)
                self._json(200, {"ok": True, "scenario": name, "url": url,
                                 "source": str(HC_SRC)})
            elif self.path == "/api/stop":
                self.server.console.stop()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except (OSError, RuntimeError, ValueError) as exc:
            self._json(500, {"ok": False, "error": str(exc)[:300]})


def run_console(port: int = 0, open_browser: bool = True) -> int:
    _assert_checkout_import()
    server = HTTPServer(("127.0.0.1", port), ConsoleHandler)
    server.expected_host = f"127.0.0.1:{server.server_address[1]}"
    server.token = secrets.token_urlsafe(24)
    server.console = ScenarioConsole()
    url = f"http://{server.expected_host}/"
    print("\n  Engelbart first-run scenario console · " + url, flush=True)
    print(f"  source · {HC_SRC} ({_checkout_label()})", flush=True)
    print("  Ctrl-C stops the console and deletes the active sandbox\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.console.stop()
        server.server_close()
    return 0


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
        default="web",
        choices=tuple(SCENARIOS) + ("list", "web"),
        help="scenario to run directly, 'web' for the console, or 'list'",
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
        if args.scenario == "web":
            return run_console(port=args.port, open_browser=not args.no_open)
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
