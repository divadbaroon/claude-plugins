"""hc ui — localhost goal browser. Reads and writes the SAME goals.json
through the goals model (goal_context.md stays in sync for SessionStart
injection). Stdlib only; localhost only; Ctrl-C to stop."""
import difflib
import hashlib
import json
import os
import re
import socketserver
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import agent_exec as AE, chat_state as CS, goals as GM, state
from . import secure_io as SIO


DEFAULT_CHAT_IDLE_SECONDS = 8 * 60 * 60
MAX_JSON_BYTES = 2 * 1024 * 1024
SERVER_REGISTRY = "server.json"

# This release ships one surface: the per-chat goal workspace. The rest of the
# goal system is built and tested but not exposed, so its entry points are
# disconnected here rather than deleted — the implementations stay reachable to
# anyone who opts in, and to the tests that hold their contracts.
EXPERIMENTAL_OPS = frozenset({
    "set_opening", "start_agent_run", "cancel_agent_run", "launch_agent_run",
    "resume_agent_run", "enable_capture", "start_analysis",
})
EXPERIMENTAL_ROUTES = ("/api/briefing", "/api/briefings", "/api/plan",
                       "/api/review", "/api/setup", "/api/conversation")
EXPERIMENTAL_ERROR = "experimental in this release; set HC_EXPERIMENTAL=1"


def _experimental_enabled():
    """One flag, one spelling, shared with the CLI's command gate.

    Imported inside the call rather than at module load: ``cli`` reaches these
    trajectory modules from inside its own functions, so a module-level import
    back into ``cli`` would close that loop and break the day someone hoists
    one of those imports.
    """
    from ..cli import experimental_enabled
    return experimental_enabled()


def _experimental_route(path):
    """True for any GET the router would hand to a disconnected handler.

    The router reaches several of these by prefix (``/api/plan`` matches
    ``/api/plan?goal=x`` and anything after it), so the gate matches by prefix
    too: a route that is off is off for every path that reaches it.
    """
    base = path.split("?", 1)[0]
    return any(base.startswith(route) for route in EXPERIMENTAL_ROUTES)


def _version():
    try:
        from importlib.metadata import version
        return version("human-compact")
    except Exception:                     # noqa: BLE001 - a label, never logic
        return "unknown"


def _registry_path(trajdir):
    return Path(trajdir) / SERVER_REGISTRY


def _read_registry(trajdir):
    try:
        value = json.loads(_registry_path(trajdir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_registry(trajdir, url):
    from .secure_io import atomic_write_json
    atomic_write_json(_registry_path(trajdir), {
        "schema_version": 1, "scope": "global", "pid": os.getpid(),
        "url": url, "version": _version(), "started_at": time.time(),
    }, root=Path(trajdir).parent)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _server_identity(record):
    """Confirm the recorded pid really is our server before signalling it.

    A pid outlives its process and gets recycled, so aliveness alone is not
    identity. Only a loopback server that answers as this scope is ours to
    stop; anything else is left strictly alone.
    """
    import http.client
    from urllib.parse import urlparse
    if not isinstance(record, dict) or not _pid_alive(record.get("pid")):
        return None
    url = record.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        return None
    connection = None
    try:
        # Speak HTTP directly to loopback: urllib would inherit system proxy
        # settings, and this probe must never leave the machine.
        connection = http.client.HTTPConnection("127.0.0.1", parsed.port,
                                                timeout=0.5)
        connection.request("GET", "/api/health", headers={"Host": parsed.netloc})
        response = connection.getresponse()
        body = json.loads(response.read())
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return None
    finally:
        if connection is not None:
            connection.close()
    if not (isinstance(body, dict) and response.status == 200
            and body.get("ok") is True and body.get("scope") == "global"):
        return None
    return {"pid": int(record["pid"]), "url": url,
            "version": body.get("version") or record.get("version")}


def stop_existing(trajdir, timeout=6.0):
    """Stop the global server this trajdir already owns; return what it was.

    Without this, every launch scanned upward for a free port and left the
    previous one serving whatever code it started with — a browser tab could
    sit on a months-old build and look like a missing feature.
    """
    import signal
    current = _server_identity(_read_registry(trajdir))
    if current is None:
        _registry_path(trajdir).unlink(missing_ok=True)
        return None
    try:
        os.kill(current["pid"], signal.SIGTERM)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(current["pid"]):
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(current["pid"], signal.SIGKILL)
        except OSError:
            pass
    _registry_path(trajdir).unlink(missing_ok=True)
    return current


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """Loopback server that never performs reverse DNS during bind."""

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(host), which can block
        # for tens of seconds on macOS even though this server accepts only a
        # numeric loopback address. Preserve TCPServer's bind semantics and
        # keep the already-known numeric identity.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def _scope(trajdir=None):
    """Resolve legacy global UI scope or an explicitly bound chat scope."""
    return Path(trajdir) if trajdir is not None else state.trajdir()


def _chat_identity(trajdir):
    """Map an explicit session directory back to chat_state's safe identity."""
    session_dir = Path(trajdir).expanduser().resolve()
    session_id, root = session_dir.name, session_dir.parent
    if CS.paths(session_id, root).session_dir != session_dir:
        raise ValueError("invalid chat session directory")
    return session_id, root


@contextmanager
def _state_access(trajdir, chat_scoped):
    """Share chat_state's cross-process lock with ingestion and analysis."""
    if not chat_scoped:
        yield
        return
    session_id, root = _chat_identity(trajdir)
    with CS.session_lock(session_id, root, wait_s=5):
        yield


def _load_goals(trajdir, chat_scoped):
    if chat_scoped:
        session_id, root = _chat_identity(trajdir)
        return CS.load_goals(session_id, root)
    return GM.load(trajdir)


def _save_goals(trajdir, goals, important, chat_scoped):
    if chat_scoped:
        session_id, root = _chat_identity(trajdir)
        if not CS.save_goals(session_id, goals, important, root):
            raise RuntimeError("chat goal state changed during save")
        return
    GM.save(trajdir, goals, important)


def _load_prompts(trajdir, chat_scoped=False):
    """Read assignable human prompts for this scope.

    Chat scope has a per-session prompt store; the global tree derives its
    prompts from the evidence index, whose user turns are the same records
    goals already cite. Malformed/incomplete rows never reach the UI.
    """
    if chat_scoped:
        session_id, root = _chat_identity(trajdir)
        rows = CS.load_prompts(session_id, root)
    else:
        rows = GM.evidence_prompts(trajdir)
    out, seen = [], set()
    for prompt in rows:
        if not (isinstance(prompt, dict) and prompt.get("role") == "user"
                and isinstance(prompt.get("id"), str)
                and isinstance(prompt.get("text"), str)
                and prompt["id"] not in seen):
            continue
        seen.add(prompt["id"])
        out.append(prompt)
    return out


def _spawn_analysis(provider, trajdir):
    """Run the analysis the user just asked for, without holding the request.

    It can take minutes over a long history, so it runs detached and reports
    progress through /api/setup rather than blocking the browser.
    """
    import subprocess
    import sys
    from .secure_io import atomic_write_json
    config = {}
    try:
        config = json.loads((Path(trajdir) / "config.json").read_text())
    except (OSError, ValueError):
        pass
    config.update({"extract_provider": provider, "synth_provider": provider})
    atomic_write_json(Path(trajdir) / "config.json", config,
                      root=Path(trajdir).parent)
    log = Path(trajdir) / "analysis.log"
    with open(log, "ab", buffering=0) as stream:
        subprocess.Popen(
            [sys.executable, "-m", "human_compact.cli", "analyze"],
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            close_fds=True, start_new_session=True)


def _first(value, default=""):
    """First entry of a list field that may be present but empty."""
    if isinstance(value, list) and value:
        return str(value[0])
    return default


def thread_rows(turns, limit, chars):
    """A conversation as the artifact renders it.

    It splits the two sides by comparing the first element to "YOU", so the
    label is load-bearing, not decoration. Assistant turns belong here too:
    a thread of only one voice is not the conversation the user had.
    """
    rows = []
    for turn in (turns or [])[:limit]:
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        who = "YOU" if turn.get("role") == "user" else "CLAUDE"
        rows.append([who, text[:chars]])
    return rows


def conversation_thread(trajdir, session_id, limit=400, chars=6000):
    """The full thread for one conversation, fetched only when it is opened.

    The list payload is polled while analysis runs, so it carries a short
    preview; the whole transcript would make every poll pay for a screen the
    user is usually not looking at.
    """
    from . import discover as D
    try:
        sessions = D.discover(30)
    except Exception:                        # noqa: BLE001 - advisory listing
        return None
    for session in sessions:
        if session.get("session_id") == session_id:
            return thread_rows(session.get("turns"), limit, chars)
    return None


def plan_preview(trajdir, goal_id, generate=True):
    """The steps an agent would take, proposed before anything runs.

    Cached per goal: this costs a model call, and the answer does not change
    between two clicks on a tab. It is a proposal — the session's own tasks
    replace it the moment one starts.
    """
    trajdir = Path(trajdir)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(goal_id or "")):
        return {"ok": False, "error": "unknown goal"}
    cache = trajdir / "plans" / f"{goal_id}.json"
    if cache.is_file():
        try:
            return {"ok": True, **json.loads(cache.read_text())}
        except (OSError, ValueError):
            pass
    if not generate:
        return {"ok": True, "goal_id": goal_id, "steps": []}
    goals, _ = GM.load(trajdir)
    GM.sanitize(goals)
    briefing = AE.goal_context(trajdir, goals, goal_id)
    if not briefing:
        return {"ok": False, "error": "unknown goal"}
    try:
        config = json.loads((trajdir / "config.json").read_text())
        from . import goal_synth as GS, providers as PR
        provider = PR.make(config.get("synth_provider")
                           or config["extract_provider"], "synthesize")
        steps = GS.plan(provider, briefing)
    except Exception as error:                       # noqa: BLE001
        return {"ok": False, "error": str(error)[:200]}
    record = {"goal_id": goal_id, "steps": steps,
              "generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        SIO.secure_dir(cache.parent, trajdir)
        SIO.atomic_write_json(cache, record, root=trajdir)
    except OSError:
        pass                     # a plan is a convenience, not state to lose
    return {"ok": True, **record}


def conversation_rows(trajdir, limit=200):
    """The conversations this vault actually holds, analyzed or not.

    The artifact ships a sample list for its own demo; this replaces it, so a
    count on screen is a count of the user's own history.
    """
    from . import discover as D
    rows, analyzed = [], {}
    convdir = Path(trajdir) / "conversations"
    if convdir.is_dir():
        for path in convdir.glob("*.json"):
            try:
                extracted = json.loads(path.read_text()).get("extracted", {})
            except (OSError, ValueError):
                continue
            analyzed[path.stem] = extracted
    try:
        sessions = D.discover(30)
    except Exception:                        # noqa: BLE001 - advisory listing
        sessions = []
    # Which goal a conversation fed is not a guess: goals cite its turns as
    # evidence. Prefer the most specific goal that cites it most often.
    goals, _ = GM.load(trajdir)
    GM.sanitize(goals)
    depth_of = {g["id"]: GM.depth(goals, g["id"]) for g in goals["goals"]}
    cited = {}
    for goal in goals["goals"]:
        for evidence_id in goal.get("evidence_ids") or []:
            prefix = str(evidence_id).split("#", 1)[0]
            if prefix:
                cited.setdefault(prefix, {}).setdefault(goal["id"], 0)
                cited[prefix][goal["id"]] += 1
    titles = {g["id"]: g.get("title", "") for g in goals["goals"]}
    for session in sessions[:limit]:
        sid = session["session_id"]
        extracted = analyzed.get(sid)
        turns = session.get("turns") or []
        first = next((t["text"] for t in turns if t.get("role") == "user"), "")
        title = ""
        if extracted:
            title = (_first(extracted.get("apparent_objectives"))
                     or _first(extracted.get("projects_or_topics")))
        counts_for = cited.get(sid[:8], {})
        goal_id = max(counts_for,
                      key=lambda gid: (counts_for[gid], depth_of.get(gid, 0)),
                      default=None)
        rows.append({
            "id": sid,
            "goalId": goal_id,
            "goalLine": ("Goal: " + titles[goal_id]) if goal_id
                        else "No goal drawn from this yet",
            "title": (title or first or "Untitled conversation")[:90],
            "meta": f"{session.get('date', '')} · {len(turns)} messages",
            "turns": len(turns),
            "goal": titles.get(goal_id, "")[:60],
            "repo": Path(session.get("cwd") or "").name,
            "done": sid in analyzed,
            "thread": thread_rows(turns, limit=6, chars=200),
        })
    # Biggest first: length is the closest thing to how much a conversation
    # carries, and it is the order the extractor works in, so the list reads
    # top-down as the analysis moves. Date breaks ties.
    rows.sort(key=lambda row: (row["turns"], row["meta"]), reverse=True)
    return rows


def setup_state(trajdir):
    """What onboarding still has to answer, read from the vault itself.

    The UI asks the questions; this reports the answers already on disk, so a
    reinstall or a second browser never re-asks something already settled.
    """
    from .. import global_vault
    from . import state as ST
    try:
        storage = global_vault.is_enabled()
    except Exception:                      # noqa: BLE001 - absent is "not yet"
        storage = False
    provider = _configured_provider(trajdir)
    analysis = {"ollama": "local", "claude": "claude"}.get(provider)
    counts = {"total": 0, "analyzed": 0, "pending": 0}
    if storage:
        try:
            snap = ST.snapshot()
            counts = {"total": snap["total"], "analyzed": snap["analyzed"],
                      "pending": snap["newer_pending"]}
        except Exception:                  # noqa: BLE001 - progress is advisory
            pass
    goals, _ = GM.load(trajdir)
    processing = ST.processing()
    rows = conversation_rows(trajdir) if storage else []
    current = None
    if processing and processing.get("current"):
        sid = str(processing["current"])
        title = next((r["title"] for r in rows if r["id"] == sid), "")
        current = {"id": sid, "title": title}
    return {
        "sv": 9,
        "storage": bool(storage),
        "analysis": analysis,
        # Settled once capture is chosen and either analysis ran or was declined.
        "done": bool(storage and (analysis or goals.get("goals"))),
        "conversations": counts,
        "goals": len(goals.get("goals", [])),
        "running": bool(processing) or ST.worker_active(),
        # Which conversation the worker has open right now, so the UI can name
        # it instead of animating an anonymous bar.
        "current": current,
        "phase": (processing or {}).get("phase") if processing else None,
        # Which conversations are in flight, not how many: the UI marks rows
        # by id, so a filtered or reordered list still marks the right ones.
        # Extraction runs eight at a time, and showing one understated it by
        # the size of the pool.
        "active": [str(sid) for sid in (processing or {}).get("active") or []],
        "inflight": len((processing or {}).get("active") or []),
        "convos": rows,
    }


def _configured_provider(trajdir):
    """Which analysis provider this vault uses, for the artifact's setup state."""
    try:
        config = json.loads((Path(trajdir) / "config.json").read_text())
    except (OSError, ValueError):
        return None
    return config.get("synth_provider") or config.get("extract_provider")


def _goal_revision(goals, important):
    def stable(value):
        if isinstance(value, dict):
            return {
                key: stable(nested)
                for key, nested in value.items()
                if key not in ("generated_at", "updated_at")
            }
        if isinstance(value, list):
            return [stable(nested) for nested in value]
        return value

    payload = json.dumps(
        stable({"goals": goals, "important": important}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# What Claude has been told about this chat's goals, read from the two files
# that already record it: the context snapshot (the exact document the model
# was last handed) and the session manifest (whether /goals-ui is on). Nothing
# here writes; the numbers are characters, and the browser labels them "~ tok"
# because a token count is an estimate this side cannot make honestly.
#
# Every point at which cli.py hands this chat's goal document to the model:
# a session start (which re-sends the whole document), a user prompt, a
# subagent start, and a tool batch (the last three carry a delta). Listing
# only the three per-message ones understated it -- a session start is the
# largest send there is, and a reader counting what Claude has been given
# would have been counting short.
INJECTION_READS = ("session start", "prompt", "subagent", "task")


def _injection_state(session_id, root):
    """What this chat has sent of its goal document, and what is pending.

    ``cached`` is true once the document has actually been handed over, which
    is the only thing the snapshot proves -- it records what was *rendered*
    into a turn, not what the model read, so every label derived from it says
    "sent". ``last_at`` is when that happened. ``last_delta_chars`` is the
    size of the change *since* then -- the text a next message would carry,
    0 when nothing has changed -- and None when nothing has been sent yet,
    because there is no base to diff against.

    Read-only, and called outside the session lock: see ``_payload``.
    """
    snapshot = CS.load_context_snapshot(session_id, root)
    previous = snapshot.get("text")
    cached = isinstance(previous, str) and bool(snapshot.get("sha256"))
    delta = None
    if cached:
        try:
            current = CS.paths(session_id, root).goal_context.read_text(
                encoding="utf-8")
        except (OSError, ValueError):
            current = previous
        if current == previous:
            delta = 0
        else:
            delta = len("\n".join(difflib.unified_diff(
                previous.splitlines(), current.splitlines(),
                fromfile="goals (as you last saw them)", tofile="goals (now)",
                lineterm="", n=1)))
    at = snapshot.get("at")
    return {
        "cached": cached,
        "last_delta_chars": delta,
        "last_at": at if isinstance(at, str) else None,
        "active": CS.goals_ui_active(session_id, root),
        "reads": list(INJECTION_READS),
    }


def _payload(trajdir=None, chat_scoped=None):
    chat_scoped = trajdir is not None if chat_scoped is None else chat_scoped
    trajdir = _scope(trajdir)
    # Set under the lock, read after it: the injection card is computed from
    # two read-only files, and the hook that writes the snapshot waits only
    # half a second for this same lock before dropping its injection. A poll
    # every 1.5s per open tab must not be one of the things it waits behind.
    identity = None
    with _state_access(trajdir, chat_scoped):
        goals, important = _load_goals(trajdir, chat_scoped)
        GM.sanitize(goals)
        ana = {}
        analyzer = None
        # What the Claude session behind this workspace has done lately. A
        # global vault stands behind no one session, so there is nothing it
        # could report -- the field is still present, so the browser reads
        # one shape in both scopes.
        notices = []
        # The tab needs a name, and only this side knows which conversation
        # the window belongs to.
        session = None
        # Present in both scopes so the browser reads one shape; a global
        # vault stands behind no chat, so nothing is injected for it.
        injection = {"cached": False, "last_delta_chars": None,
                     "last_at": None, "active": False,
                     "reads": list(INJECTION_READS)}
        try:
            ana = json.loads((trajdir / "analysis.json").read_text())
        except (OSError, ValueError):
            pass
        if chat_scoped:
            session_id, root = _chat_identity(trajdir)
            analyzer = CS.get_analyzer_state(session_id, root)
            notices = CS.load_notices(session_id, root)
            session = session_id
            identity = (session_id, root)
        # Agent execution state is scoped to the goal tree it was launched
        # against; chat-scoped goal ids live in a different namespace.
        runs, claim = ({}, None) if chat_scoped else (
            AE.plans(trajdir), AE.pending_claim(trajdir))
        payload = {"goals": goals["goals"], "items": important["items"],
                   "prompts": _load_prompts(trajdir, chat_scoped),
                   "generated_at": goals.get("generated_at", ""),
                   "sessions": ana.get("sessions_analyzed"),
                   "analyzer": analyzer,
                   "notices": notices,
                   "session_id": session,
                   "injection": injection,
                   "agent_runs": runs,
                   "agent_claim": claim,
                   "scope": "chat" if chat_scoped else "global",
                   "provider": _configured_provider(trajdir),
                   "revision": _goal_revision(goals, important)}
    if identity is not None:
        payload["injection"] = _injection_state(*identity)
    return payload


def _apply(op, trajdir=None, chat_scoped=None):
    result = _apply_locked(op, trajdir, chat_scoped)
    deferred = result.get("__deferred__") if isinstance(result, dict) else None
    if not deferred:
        return result
    from . import build as BUILD
    kind, session_id, root, goal_id, op = deferred
    if kind == "build_todos":
        ids = op.get("ids")
        return BUILD.start(session_id, root, goal_id,
                           ids if isinstance(ids, list) else [])
    return BUILD.answer(session_id, root, goal_id,
                        str(op.get("id") or ""), str(op.get("answer") or ""))


def _generate_prompt(session_id, root, goals, important, goal):
    """Ask Claude for a prompt for this goal, from the tree the plugin holds.

    Synchronous and bounded by the provider's own timeout: the rail shows
    "Generating…" until it lands. Empty on any failure -- the caller says so.
    """
    from . import providers as PROVIDERS
    tree = CS._goal_context_text(session_id, goals, important,
                                 CS.load_prompts(session_id, root))
    title = " ".join(str(goal.get("title") or "Untitled").split())
    todos = str(goal.get("todos_md") or "").strip()
    notes = str(goal.get("notes") or "").strip()
    ask = [
        "You write prompts for a coding agent (Claude Code) working in the",
        "user's repository. Below is the user's whole goal tree for this",
        "chat, then the ONE goal to write a prompt for.",
        "",
        "Write the prompt the user would send to have this goal (and only",
        "this goal) implemented. Be concrete: name the outcome, the",
        "constraints that follow from the rest of the tree, what to leave",
        "alone, and how to verify. Second person, addressed to the agent.",
        "No preamble, no headings about yourself, no quotation marks",
        "around the whole thing. Under 250 words. Output the prompt only.",
        "",
        "# Goal tree", "", tree.rstrip(), "",
        "# The goal to write the prompt for", "",
        f"{goal.get('id')} · {title}",
    ]
    if todos:
        ask += ["", "Its TODOs:", todos]
    if notes:
        ask += ["", "The user's notes on it:", notes[:3000]]
    try:
        provider = PROVIDERS.make(
            os.environ.get("HC_CHAT_PROVIDER", "claude"), "synthesize")
        text = provider.generate("\n".join(ask) + "\n")
    except Exception:  # noqa: BLE001 - any provider failure is "no prompt"
        return ""
    return str(text or "").strip()


def _apply_locked(op, trajdir=None, chat_scoped=None):
    chat_scoped = trajdir is not None if chat_scoped is None else chat_scoped
    trajdir = _scope(trajdir)
    with _state_access(trajdir, chat_scoped):
        goals, important = _load_goals(trajdir, chat_scoped)
        GM.sanitize(goals)
        kind = op.get("op")
        # Checked before any scope or goal reasoning: a disconnected op gives
        # the same answer everywhere, and never half-applies on the way out.
        if kind in EXPERIMENTAL_OPS and not _experimental_enabled():
            return {"ok": False, "error": EXPERIMENTAL_ERROR}
        g = GM.by_id(goals, op.get("goal_id", ""))
        # Execution-state ops touch the agent-run store only: choosing to work
        # on a goal must not rewrite the goal itself.
        if kind in ("enable_capture", "start_analysis"):
            if chat_scoped:
                return {"ok": False, "error": "global scope only"}
            from .. import global_vault
            if kind == "enable_capture":
                # Nothing is read until the user asks for it here.
                if op.get("enabled") is False:
                    global_vault.disable_always_on()
                    return {"ok": True, **setup_state(trajdir)}
                global_vault.enable_always_on()
                counts = global_vault.backfill()
                return {"ok": True, "imported": counts.get("imported", 0),
                        **setup_state(trajdir)}
            choice = op.get("provider")
            if choice not in ("local", "claude", "none"):
                return {"ok": False, "error": "unknown analysis provider"}
            if choice == "none":
                return {"ok": True, **setup_state(trajdir)}
            if not global_vault.is_enabled():
                return {"ok": False, "error": "enable capture first"}
            _spawn_analysis("ollama" if choice == "local" else "claude", trajdir)
            return {"ok": True, **setup_state(trajdir)}
        if kind in ("start_agent_run", "cancel_agent_run", "launch_agent_run",
                    "resume_agent_run"):
            if chat_scoped:
                return {"ok": False, "error": "agent runs attach to Vault goals"}
            if kind == "cancel_agent_run":
                AE.clear_claim(trajdir)
                return {"ok": True}
            if not g:
                return {"ok": False, "error": "goal not found"}
            if kind == "resume_agent_run":
                # Open the session that already exists, rather than starting
                # another one against the same goal.
                session = str(op.get("session_id") or "")
                run = next((r for r in AE.load_runs(trajdir)
                            if r.get("claude_session_id") == session
                            and r.get("vault_goal_id") == g["id"]), None)
                if run is None:
                    return {"ok": False, "error": "no such session for this goal"}
                # The session is already open somewhere; surfacing that
                # window is what the reader means by "open the conversation".
                # Resuming into a new one is the fallback, not the intent.
                if AE.raise_window(str(run.get("terminal_window") or "")):
                    return {"ok": True, "raised": True,
                            "session_id": session}
                cwd = run.get("cwd") or AE.goal_cwd(trajdir, goals, g["id"])
                if not cwd:
                    return {"ok": False, "error": "that session has no recorded directory"}
                try:
                    script = AE.write_launch_script(
                        trajdir, g["id"], cwd,
                        ["claude", "-r", session], send=True)
                    app = AE.open_terminal(script)
                except (OSError, RuntimeError, ValueError) as exc:
                    return {"ok": False, "error": str(exc)[:200]}
                return {"ok": True, "resumed": True, "terminal": app,
                        "cwd": cwd, "command": f"claude -r {session}"}
            if kind == "start_agent_run":
                return {"ok": True, "command": f"hc work {g['id']}",
                        "claim": AE.arm(trajdir, g["id"], g.get("title", ""))}
            # Open a real interactive session in the goal's own project, with
            # the goal as its opening message. Nothing runs unattended.
            cwd = AE.goal_cwd(trajdir, goals, g["id"])
            if not cwd:
                return {"ok": False, "error":
                        "no project directory is recorded for this goal yet"}
            confirmed = op.get("confirmed") is True
            # --start gives Claude the opening message as an argument, so the
            # session begins on its own. Without it the command is typed into
            # a shell and waits, which is the other honest option.
            command = ["hc", "work", g["id"]] + (["--start"] if confirmed else [])
            prompt = AE.launch_prompt(goals, g["id"])
            try:
                script = AE.write_launch_script(
                    trajdir, g["id"], cwd, command, prompt, send=confirmed)
                window = []
                app = AE.open_terminal(script, opened_window=window)
                if window and window[0]:
                    AE.remember_window(trajdir, g["id"], window[0])
            except (OSError, RuntimeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)[:200],
                        "command": f"cd {cwd} && hc work {g['id']} --start"}

            AE.clear_claim(trajdir)
            return {"ok": True, "launched": True, "terminal": app, "cwd": cwd,
                    "sent": confirmed,
                    "command": f"cd {cwd} && hc work {g['id']} --start"}
        if kind in ("build_todos", "answer_todo", "generate_prompt"):
            # The rail's build and generate: chat scope only, since both run
            # against the chat's own project and goal tree. The build ops are
            # handed back to _apply to run OUTSIDE this lock -- build.py takes
            # the same lock for its own writes, and a child process must
            # never be spawned while a request still holds it.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            if not g:
                return {"ok": False, "error": "goal not found in this chat"}
            session_id, root = _chat_identity(trajdir)
            if kind == "generate_prompt":
                text = _generate_prompt(session_id, root, goals, important, g)
                if not text:
                    return {"ok": False, "error":
                            "the prompt could not be generated (is the claude "
                            "CLI on PATH?)"}
                g["prompt_md"] = text
                g["updated_at"] = GM._now()
                _save_goals(trajdir, goals, important, chat_scoped)
                return {"ok": True, "prompt": text}
            return {"__deferred__": (kind, session_id, root, g["id"], op)}
        if kind == "rename_goal" and g and op.get("title", "").strip():
            g["title"] = op["title"].strip()[:120]
        elif kind == "set_status" and g and op.get("status") in ("active", "in_progress", "completed", "abandoned"):
            g["status"] = op["status"]
        elif kind == "set_priority" and g and op.get("priority") in ("urgent", "high", "normal"):
            g["priority"] = op["priority"]
        elif kind == "set_notes" and g:
            # The goal's whole markdown document: stored as written.
            g["notes"] = str(op.get("notes", ""))
        elif kind == "set_sources" and g:
            raw = op.get("sources")
            if not isinstance(raw, list):
                return {"ok": False, "error": "sources must be a list"}
            # Accepts plain strings or the typed rows the artifact edits.
            g["sources"] = GM.normalize_sources(raw)
            g["updated_at"] = GM._now()
        elif kind == "set_opening" and g:
            g["opening"] = str(op.get("opening", "")).strip()[:400]
            g["updated_at"] = GM._now()
        elif kind == "set_description" and g:
            g["description"] = str(op.get("description", ""))[:600]
        elif kind == "toggle_todo" and g:
            kids = [c for c in goals["goals"] if c.get("parent_goal_id") == g["id"]]
            try:
                child = kids[int(op.get("index", -1))]
            except (IndexError, ValueError, TypeError):
                return {"ok": False, "error": "no such subgoal"}
            child["status"] = ("active" if child["status"] == "completed"
                               else "completed")
            child["updated_at"] = GM._now()
        elif kind == "add_todo" and g and op.get("text", "").strip():
            goals["goals"].append(GM.new_goal(
                GM.child_goal_id(goals, g["id"]),
                op["text"].strip()[:120], g["id"], origin="user"))
        elif kind in ("attach_prompt", "detach_prompt"):
            if not g:
                return {"ok": False, "error": "goal not found in this chat"}
            prompt_id = op.get("prompt_id")
            valid = {p["id"] for p in _load_prompts(trajdir, chat_scoped)}
            if not isinstance(prompt_id, str) or prompt_id not in valid:
                return {"ok": False, "error": "prompt not found in this chat"}
            links = g.setdefault("prompt_ids", [])
            removed = g.setdefault("detached_prompt_ids", [])
            automatic = g.setdefault("auto_prompt_ids", [])
            # Both directions are the user overruling inference, so both clear
            # the machine label: an auto link the user keeps becomes theirs,
            # and one they drop must not be re-linked by the next analysis.
            g["auto_prompt_ids"] = [pid for pid in automatic if pid != prompt_id]
            if kind == "attach_prompt":
                if prompt_id not in links:
                    links.append(prompt_id)
                g["detached_prompt_ids"] = [pid for pid in removed
                                            if pid != prompt_id]
            else:
                g["prompt_ids"] = [pid for pid in links if pid != prompt_id]
                if prompt_id not in removed:
                    removed.append(prompt_id)
            g["updated_at"] = GM._now()
        elif kind == "add_goal":
            parent = op.get("parent_goal_id") or None
            if parent and not GM.by_id(goals, parent):
                return {"ok": False, "error": "parent not found"}
            gid = GM.next_goal_id(goals)
            goals["goals"].append(GM.new_goal(
                gid, (op.get("title") or "Untitled").strip()[:120], parent,
                origin="user"))
        else:
            return {"ok": False, "error": "unknown or invalid op"}
        GM.sanitize(goals)
        _save_goals(trajdir, goals, important, chat_scoped)
        return {"ok": True}


# The colour the dressed chat workspace lands on: the artifact paints its
# `.hc` shell on it. The mask and the workspace meet on the same pixel at
# reveal, so anything near-but-not-equal is a seam -- and the bridge holds the
# same ground once the unpack has taken this mask away, from one constant.
CHAT_GROUND = "#0d1117"


def preboot_mask(chat_scoped):
    """Hide the artifact's own first frame, on the ground it will land on.

    Every /goals-ui opens a fresh port, so a chat workspace is always a new
    origin with no saved theme: following the operating system there means a
    white page in front of a dark workspace, which is the flash it was meant
    to remove. A chat opens dark unless the reader chose otherwise here.
    """
    ground = CHAT_GROUND if chat_scoped else "#fff"
    other = "#fff" if chat_scoped else CHAT_GROUND
    want = "light" if chat_scoped else "dark"
    return (
        '<style id="hc-preboot">html{visibility:hidden!important}'
        'html,body{background:%s!important}</style>'
        '<script>(function(){'
        'try{var saved=null;'
        'try{saved=JSON.parse(localStorage.getItem("hc-vault-ui-v1")||"null");}'
        'catch(e){}'
        'if(saved&&saved.themeMode==="%s"){'
        'var s=document.getElementById("hc-preboot");'
        'if(s){s.textContent="html{visibility:hidden!important}"'
        '+"html,body{background:%s!important}";}}}catch(e){}'
        'setTimeout(function(){var s=document.getElementById("hc-preboot");'
        'if(s&&s.parentNode){s.parentNode.removeChild(s);}},2500);'
        '})();</script>'
    ) % (ground, want, other)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):                  # quiet
        pass

    def _request_origin(self):
        return f"http://{self.server.expected_host}"

    def _begin_request(self):
        """Validate the browser boundary, then mark this request active.

        Host validation blocks DNS rebinding.  Origin validation and the JSON
        media-type check in ``do_POST`` block cross-site, CORS-simple writes.
        Missing Origin remains valid for the launcher health check and other
        local non-browser clients.
        """
        hosts = self.headers.get_all("Host", [])
        if hosts != [self.server.expected_host]:
            self._send(403, {"ok": False, "error": "invalid host"})
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self._request_origin()]:
            self._send(403, {"ok": False, "error": "cross-origin request denied"})
            return False
        with self.server.activity_lock:
            if self.server.idle_expired:
                closing = True
            else:
                closing = False
                self.server.active_requests += 1
                self.server.last_activity = time.monotonic()
        if closing:
            self._send(503, {"ok": False, "error": "server is shutting down"})
            return False
        return True

    def _finish_request(self):
        with self.server.activity_lock:
            self.server.active_requests -= 1
            self.server.last_activity = time.monotonic()

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._begin_request():
            return
        try:
            # Before the scope logic: whether this build exposes the route at
            # all is a question that comes ahead of which vault it would read.
            if _experimental_route(self.path) and not _experimental_enabled():
                self._send(200, {"ok": False, "error": EXPERIMENTAL_ERROR})
            elif self.path in ("/", "/index.html"):
                html = resources.files("human_compact.trajectory").joinpath(
                    "web/goals_bundle.html").read_text(encoding="utf-8")
                # The artifact ships its own pre-hydration body: a rust splash
                # and the raw template, unresolved {{ bindings }} and the
                # global onboarding dialog included. It paints that for a frame
                # before it unpacks the template and replaces documentElement.
                # Reading it is worse than reading nothing -- it shows setup
                # questions this release does not ask. Hide the document until
                # the unpack, which removes this style along with the rest of
                # the original head. The timer is the failsafe: if the unpack
                # never happens, the page is shown anyway rather than staying
                # blank.
                html = html.replace(
                    "</head>",
                    preboot_mask(self.server.chat_scoped) + "</head>", 1)
                # Parse the artifact's template island before running the
                # bridge, while still running the bridge synchronously before
                # DOMContentLoaded lets the artifact unpack that template.
                html = html.replace(
                    "</body>", '<script src="/bridge.js"></script>\n</body>', 1)
                self._send(200, html.encode(), "text/html; charset=utf-8")
            elif self.path == "/bridge.js":
                js = resources.files("human_compact.trajectory").joinpath(
                    "web/bridge.js").read_bytes()
                self._send(200, js, "application/javascript")
            elif self.path == "/api/state":
                self._send(200, _payload(
                    self.server.trajdir, self.server.chat_scoped))
            elif (self.path == "/api/briefing"
                  or self.path.startswith("/api/briefing?")):
                # Exactly what a session launched on this goal would receive,
                # so the context is inspectable before it is spent.
                from urllib.parse import urlparse, parse_qs
                goal_id = parse_qs(urlparse(self.path).query).get("goal", [""])[0]
                if self.server.chat_scoped:
                    self._send(200, {"ok": False,
                                     "error": "briefings are for Vault goals"})
                else:
                    trajdir = self.server.trajdir
                    goals, _ = GM.load(trajdir)
                    GM.sanitize(goals)
                    text = AE.goal_context(trajdir, goals, goal_id)
                    dirs, refs = AE.goal_sources(goals, goal_id)
                    parts = AE.prompt_sections(trajdir, goals, goal_id) or {}
                    self._send(200, {
                        "ok": bool(text),
                        "goal_id": goal_id,
                        "briefing": text,
                        "intro": parts.get("intro", []),
                        "sections": parts.get("sections", []),
                        "footer": parts.get("footer", []),
                        "opening": AE.launch_prompt(goals, goal_id),
                        "cwd": AE.goal_cwd(trajdir, goals, goal_id),
                        "add_dirs": dirs,
                        "references": refs,
                    })
            elif self.path == "/api/briefings":
                # Every goal's briefing in one call. The panels are baked into
                # the artifact's state before it boots, so fetching them one
                # goal at a time afterwards was too late to be rendered.
                if self.server.chat_scoped:
                    self._send(200, {"ok": False, "goals": {}})
                else:
                    trajdir = self.server.trajdir
                    goals, _ = GM.load(trajdir)
                    GM.sanitize(goals)
                    out = {}
                    for goal in goals.get("goals", []):
                        parts = AE.prompt_sections(trajdir, goals, goal["id"])
                        if not parts:
                            continue
                        dirs, refs = AE.goal_sources(goals, goal["id"])
                        out[goal["id"]] = {
                            "sections": parts.get("sections", []),
                            "cwd": AE.goal_cwd(trajdir, goals, goal["id"]),
                            "add_dirs": dirs, "references": refs,
                        }
                    self._send(200, {"ok": True, "goals": out})
            elif self.path.startswith("/api/plan"):
                from urllib.parse import urlparse, parse_qs
                goal_id = parse_qs(urlparse(self.path).query).get("goal", [""])[0]
                if self.server.chat_scoped:
                    self._send(200, {"ok": False, "error": "Vault goals only"})
                else:
                    self._send(200, plan_preview(self.server.trajdir, goal_id))
            elif self.path.startswith("/api/conversation"):
                from urllib.parse import urlparse, parse_qs
                sid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                thread = (None if self.server.chat_scoped
                          else conversation_thread(self.server.trajdir, sid))
                self._send(200, {"ok": thread is not None,
                                 "id": sid, "thread": thread or []})
            elif self.path == "/api/setup":
                self._send(200, {"ok": True, **setup_state(self.server.trajdir)}
                           if not self.server.chat_scoped
                           else {"ok": False, "error": "global scope only"})
            elif self.path.startswith("/api/review"):
                from urllib.parse import urlparse, parse_qs
                goal_id = parse_qs(urlparse(self.path).query).get("goal", [""])[0]
                if self.server.chat_scoped:
                    self._send(200, {"ok": False, "runs": []})
                else:
                    goals, _ = GM.load(self.server.trajdir)
                    GM.sanitize(goals)
                    self._send(200, AE.review(self.server.trajdir, goals, goal_id))
            elif self.path == "/api/health":
                self._send(200, {
                    "ok": True,
                    "scope": "chat" if self.server.chat_scoped else "global",
                    "version": _version(),
                    "session_id": (self.server.trajdir.name
                                   if self.server.chat_scoped else None),
                })
            else:
                self._send(404, {"error": "not found"})
        finally:
            self._finish_request()

    def do_POST(self):
        if not self._begin_request():
            return
        try:
            content_types = self.headers.get_all("Content-Type", [])
            if (len(content_types) != 1 or
                    content_types[0].split(";", 1)[0].strip().lower()
                    != "application/json"):
                self._send(415, {"ok": False, "error": "application/json required"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            try:
                n = int(lengths[0]) if len(lengths) == 1 else -1
            except (ValueError, TypeError):
                n = -1
            if n < 0:
                self._send(400, {"ok": False, "error": "invalid content length"})
                return
            if n > MAX_JSON_BYTES:
                self._send(413, {"ok": False, "error": "request body too large"})
                return
            try:
                body = json.loads(self.rfile.read(n))
            except (ValueError, TypeError):
                self._send(400, {"ok": False, "error": "bad json"})
                return
            if self.path == "/api/op":
                if not isinstance(body, dict):
                    self._send(400, {"ok": False, "error": "expected an operation"})
                    return
                with self.server.state_lock:
                    result = _apply(
                        body, self.server.trajdir, self.server.chat_scoped)
                self._send(200, result)
            elif self.path == "/api/import":
                if not isinstance(body, dict):
                    self._send(400, {
                        "ok": False,
                        "error": "revisioned import payload required",
                    })
                    return
                nested = body.get("goals")
                expected_revision = body.get("base_revision")
                if not isinstance(expected_revision, str):
                    self._send(400, {
                        "ok": False,
                        "error": "base_revision is required",
                    })
                    return
                with self.server.state_lock:
                    result = _import(
                        nested,
                        self.server.trajdir,
                        self.server.chat_scoped,
                        expected_revision=expected_revision,
                    )
                self._send(409 if result.get("conflict") else 200, result)
            else:
                self._send(404, {"error": "not found"})
        finally:
            self._finish_request()


def _configure_server(server, trajdir, chat_scoped):
    server.trajdir = trajdir
    server.chat_scoped = chat_scoped
    server.state_lock = threading.RLock()
    server.expected_host = f"127.0.0.1:{server.server_address[1]}"
    server.activity_lock = threading.Lock()
    server.last_activity = time.monotonic()
    server.active_requests = 0
    server.idle_expired = False


def _resolved_idle_timeout(value, chat_scoped):
    if value is None and not chat_scoped:
        return None
    if value is None:
        value = os.environ.get("HC_CHAT_UI_IDLE_SECONDS", DEFAULT_CHAT_IDLE_SECONDS)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(DEFAULT_CHAT_IDLE_SECONDS)
    return seconds if seconds > 0 else None


def _watch_idle(server, timeout, stop):
    interval = min(60.0, max(0.05, timeout / 4))
    while not stop.wait(interval):
        with server.activity_lock:
            expired = (
                not server.active_requests
                and time.monotonic() - server.last_activity >= timeout
            )
            if expired:
                # Prevent a new request from starting between this decision and
                # shutdown. Existing requests are never interrupted.
                server.idle_expired = True
        if expired:
            server.shutdown()
            return


def run(port=8765, open_browser=True, trajdir=None, ready_callback=None,
        label="Vault goals", idle_timeout=None, replace=True):
    chat_scoped = trajdir is not None
    trajdir = _scope(trajdir)
    from .secure_io import secure_dir, secure_existing_tree
    if chat_scoped:
        secure_dir(trajdir, trajdir.parent)
    else:
        secure_existing_tree(trajdir, trajdir.parent)
    replaced = None
    if replace and not chat_scoped:
        # One scope, one server. A second one would serve its own snapshot of
        # the code at whatever port happened to be free.
        replaced = stop_existing(trajdir)
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            _configure_server(srv, trajdir, chat_scoped)
            break
        except OSError:
            continue
    else:
        print("  no free port found"); return
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    if not chat_scoped:
        _write_registry(trajdir, url)
    # Detached launchers redirect this output to a private diagnostic log.
    # Flush before readiness so a blocked child still exposes its last stage.
    print(f"\n  {label} · {url}", flush=True)
    if replaced:
        print(f"  replaced the server at {replaced['url']} "
              f"(was version {replaced['version']})", flush=True)
    print("  Ctrl-C to stop\n", flush=True)
    idle_stop = threading.Event()
    idle_thread = None
    # Being replaced arrives as SIGTERM, whose default action skips every
    # cleanup path below. Turn it into the interrupt this loop already knows
    # how to end on, so the socket closes and the registry is released.
    previous_term = None
    if threading.current_thread() is threading.main_thread():
        import signal
        try:
            previous_term = signal.signal(
                signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
        except (ValueError, OSError):
            previous_term = None
    try:
        if ready_callback is not None:
            ready_callback(url, srv)
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        timeout = _resolved_idle_timeout(idle_timeout, chat_scoped)
        if timeout is not None:
            idle_thread = threading.Thread(
                target=_watch_idle,
                args=(srv, timeout, idle_stop),
                daemon=True,
            )
            idle_thread.start()
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        idle_stop.set()
        if previous_term is not None:
            import signal
            try:
                signal.signal(signal.SIGTERM, previous_term)
            except (ValueError, OSError):
                pass
        srv.server_close()
        if not chat_scoped:
            owner = _read_registry(trajdir)
            if isinstance(owner, dict) and owner.get("pid") == os.getpid():
                _registry_path(trajdir).unlink(missing_ok=True)
        if idle_thread is not None and idle_thread is not threading.current_thread():
            idle_thread.join(timeout=1)


def _merge_todo_items(posted, previous):
    """The browser's rows with the server's build state laid back over them.

    A row is matched by id. Text, depth and order are whatever the browser
    sent (that is the edit); status and question are whatever the server had
    for that id (that is the run). A row the browser no longer sends is gone;
    a row it sends that the server never saw starts blank. A browser that
    posted no list at all (an older cached page) keeps the server's rows.
    """
    if not isinstance(posted, list):
        return GM.normalize_todo_items(previous)
    held = {row.get("id"): row for row in GM.normalize_todo_items(previous)}
    out = []
    for row in GM.normalize_todo_items(posted):
        was = held.get(row["id"])
        if was is not None:
            row["status"] = was.get("status", "")
            row["question"] = was.get("question", "")
        else:
            row["status"] = ""
            row["question"] = ""
        out.append(row)
    return out


def _import(nested, trajdir=None, chat_scoped=None, expected_revision=None):
    """Map the Claude Design app's nested node tree back into the goals model.
    Node ids are preserved; legacy `t:<gid>:<i>` nodes from a browser cached
    before todos became goals are resolved to their promoted child. Nodes
    missing from the payload are marked abandoned (history kept, never
    destroyed). Evidence links and important-item associations survive."""
    if not isinstance(nested, list):
        return {"ok": False, "error": "expected a list of nodes"}
    chat_scoped = trajdir is not None if chat_scoped is None else chat_scoped
    trajdir = _scope(trajdir)
    with _state_access(trajdir, chat_scoped):
        goals, important = _load_goals(trajdir, chat_scoped)
        GM.sanitize(goals)
        current_revision = _goal_revision(goals, important)
        if (expected_revision is not None
                and expected_revision != current_revision):
            return {
                "ok": False,
                "conflict": True,
                "error": "goal state changed; refresh and merge before importing",
                "revision": current_revision,
            }
        old = {g["id"]: g for g in goals["goals"]}
        seen, out = set(), []

        used = set(old)

        def resolve(nid, title, parent_gid):
            """Give a legacy todo node a real goal id.

            A browser cached before promotion still sends `t:<gid>:<i>` nodes.
            They are goals now, so reuse the promoted child already carrying
            this title under this parent rather than creating a duplicate.
            """
            if not nid.startswith("t:"):
                return nid
            match = next((g for g in goals["goals"]
                          if g.get("parent_goal_id") == parent_gid
                          and g.get("title") == title), None)
            if match is not None and match["id"] not in seen:
                return match["id"]
            minted = GM.child_goal_id({"goals": [{"id": i} for i in used]},
                                      parent_gid or "g")
            used.add(minted)
            return minted

        def walk(node, parent_gid):
            title = (node.get("title") or "Untitled").strip()[:120]
            nid = resolve(str(node.get("id", "")), title, parent_gid)
            seen.add(nid)
            prev = old.get(nid, {})
            done = bool(node.get("done"))
            status = ("abandoned" if done and prev.get("status") == "abandoned" else
                      "completed" if done else
                      "in_progress" if node.get("status") == "inprog" else "active")
            out.append({"id": nid, "title": title, "status": status,
                        "parent_goal_id": parent_gid,
                        "evidence_ids": prev.get("evidence_ids", []),
                        "todos": [],
                        "important_item_ids": prev.get("important_item_ids", []),
                        "prompt_ids": prev.get("prompt_ids", []),
                        "sources": prev.get("sources", []),
                        "opening": prev.get("opening", ""),
                        "auto_prompt_ids": prev.get("auto_prompt_ids", []),
                        "detached_prompt_ids": prev.get("detached_prompt_ids", []),
                        "priority": node.get("prio") if node.get("prio") in
                            ("urgent", "high", "normal") else "normal",
                        "notes": str(node.get("notes") or ""),
                        # Rows come from the browser -- text, depth, order --
                        # but their build state is the server's: a run that
                        # marked a row done or asking must not be undone by a
                        # tree the browser posted from an older copy.
                        "todo_items": _merge_todo_items(
                            node.get("todo_items"), prev.get("todo_items")),
                        "todos_md": "",
                        "prompt_md": str(node["prompt_md"] if node.get("prompt_md")
                                         is not None else prev.get("prompt_md") or ""),
                        "description": str(node.get("desc") or "")[:600],
                        "origin": prev.get("origin", "ui"),
                        "updated_at": prev.get("updated_at", GM._now())}
                       )
            for ch in node.get("children") or []:
                walk(ch, nid)

        for n in nested:
            walk(n, None)
        for g in out:
            prev = old.get(g["id"])
            if not prev:
                g["updated_at"] = GM._now()
                continue
            if (g["title"], g["status"], g["parent_goal_id"], g["priority"],
                g["notes"], GM.render_todos(g["todo_items"]), g["prompt_md"],
                g["description"]) != \
               (prev.get("title"), prev.get("status"), prev.get("parent_goal_id"),
                prev.get("priority", "normal"), prev.get("notes", ""),
                GM.render_todos(prev.get("todo_items")), prev.get("prompt_md", ""),
                prev.get("description", "")):
                g["updated_at"] = GM._now()
        # anything the app deleted -> abandoned, kept
        for gid, prev in old.items():
            if gid not in seen:
                prev = dict(prev)
                if prev.get("status") != "abandoned":
                    prev["status"] = "abandoned"
                    prev["updated_at"] = GM._now()
                out.append(prev)
        goals["goals"] = out
        GM.sanitize(goals)
        _save_goals(trajdir, goals, important, chat_scoped)
        saved_goals, saved_important = _load_goals(trajdir, chat_scoped)
        return {
            "ok": True,
            "goals": len(out),
            "revision": _goal_revision(saved_goals, saved_important),
        }
