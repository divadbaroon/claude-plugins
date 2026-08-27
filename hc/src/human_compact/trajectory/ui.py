"""hc ui — localhost goal browser. Reads and writes the SAME goals.json
through the goals model (goal_context.md stays in sync for SessionStart
injection). Stdlib only; localhost only; Ctrl-C to stop."""
import difflib
import hashlib
import json
import os
import re
import socketserver
import sys
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
from . import autosync as AUTOSYNC
from . import project_store as PS
from . import secure_io as SIO
from . import setup_chat as SETUP


DEFAULT_CHAT_IDLE_SECONDS = 8 * 60 * 60
MAX_JSON_BYTES = 2 * 1024 * 1024
SERVER_REGISTRY = "server.json"

# Screenshots pasted into a TODO row arrive on /api/attachment as raw image
# bytes and are kept under the workspace's own directory. A retina capture
# runs to several megabytes; twenty-five is room for any of them and a wall
# against anything else.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ATTACHMENT_TYPES = {"image/png": ".png", "image/jpeg": ".jpg",
                    "image/gif": ".gif", "image/webp": ".webp"}


def _store_attachment(trajdir, data, ext):
    """Write one pasted image under <scope>/attachments, named by the moment
    and a nonce so two pastes never share a file. Returns the absolute path."""
    import secrets
    folder = SIO.secure_dir(_scope(trajdir) / "attachments")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{secrets.token_hex(4)}{ext}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return path.resolve()

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
# The operations that edit one goal and are refused when that goal is not in
# this workspace. Kept beside the branch that applies them so the refusal can
# name the real reason rather than call the operation itself unknown.
GOAL_OPS = frozenset({
    "rename_goal", "set_status", "set_priority", "set_notes", "set_sources",
    "set_opening", "set_description", "toggle_todo", "set_relevance",
    "add_todo", "set_understanding",
})
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


def _code_stamp():
    """The newest edit time among this package's own Python files, or 0.0."""
    newest = 0.0
    try:
        for path in Path(__file__).resolve().parent.glob("*.py"):
            newest = max(newest, path.stat().st_mtime)
    except OSError:                       # noqa: BLE001 - a hint, never logic
        return 0.0
    return newest


# When this process read its own code. The browser's half of the workspace is
# re-read from disk on every page load and this half is not, so editing the
# plugin with a workspace open leaves a new page talking to an old server --
# which answers the controls added since with "unknown operation". The page
# cannot tell that from a bug unless it is told, so it is.
_CODE_STAMP = _code_stamp()


def _server_is_stale():
    """Whether this package has been edited since this process started."""
    now = _code_stamp()
    # A second of slack: a file copied into place can carry a timestamp a
    # hair past the read that took it, and a workspace should not open under
    # a warning about an edit nobody made.
    return bool(now and _CODE_STAMP and now > _CODE_STAMP + 1.0)


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


# --- linked chats: other sessions whose prompts join this workspace --------
#
# A linked chat is a PROMPT SOURCE, nothing more: its transcript is followed
# with the same cursor machinery as the workspace's own, and its user turns
# join the related-prompts pool, tagged with the chat's label. No goals are
# read from it and nothing is written to it.
#
# A link has a scope. Linked from the header it is GLOBAL: its prompts are
# offered to every goal. Linked from a goal's own pane it is scoped to that
# goal: offered to that goal and the goals under it, never to the goals
# above it -- a chat brought in while working on a subgoal belongs to that
# branch, not to the parent it hangs from. One row per (session, scope).

def _linked_path(session_id, root):
    return CS.paths(session_id, root).session_dir / "linked_chats.json"


def _load_linked(session_id, root):
    try:
        value = json.loads(_linked_path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = value.get("chats") if isinstance(value, dict) else None
    out, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("session_id") or "")
        try:
            CS.paths(sid, root)
        except ValueError:
            continue
        goal_id = row.get("goal_id")
        goal_id = goal_id if isinstance(goal_id, str) and goal_id else None
        if (sid, goal_id) in seen:
            continue
        seen.add((sid, goal_id))
        entry = {"session_id": sid, "label": str(row.get("label") or sid[:8])}
        if goal_id:
            entry["goal_id"] = goal_id
        out.append(entry)
    return out


def _linked_sessions(chats):
    """Each linked session once, with the goal ids it is scoped to -- or
    None when any of its links is global, which covers every goal."""
    scopes = {}
    labels = {}
    for chat in chats:
        sid = chat["session_id"]
        labels.setdefault(sid, chat["label"])
        goal_id = chat.get("goal_id")
        if sid not in scopes:
            scopes[sid] = set() if goal_id else None
        if scopes[sid] is not None:
            if goal_id:
                scopes[sid].add(goal_id)
            else:
                scopes[sid] = None
    return [(sid, labels[sid], None if goals is None else sorted(goals))
            for sid, goals in scopes.items()]


def _save_linked(session_id, root, chats):
    from .secure_io import atomic_write_json
    path = _linked_path(session_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"chats": chats})


def _discover_chats(own_session_id, root, limit=30):
    """Sessions with transcripts under Claude's projects directory, newest
    first: enough to point at a chat, no more."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    rows = []
    for path in (home / "projects").glob("*/*.jsonl"):
        sid = path.stem
        if sid == own_session_id:
            continue
        try:
            CS.paths(sid, root)
        except ValueError:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if not stat.st_size:
            continue
        # Where the chat worked, when its manifest says: the project
        # switcher groups chats by directory, and a name alone ("plugins")
        # cannot tell two repositories with the same last segment apart.
        cwd = _manifest_cwd(sid, root)
        rows.append({"session_id": sid,
                     "project": path.parent.name.split("-")[-1] or path.parent.name,
                     "cwd": cwd,
                     "mtime": stat.st_mtime, "size": stat.st_size})
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:limit]


def _load_prompts(trajdir, chat_scoped=False):
    """Read assignable human prompts for this scope.

    Chat scope has a per-session prompt store; the global tree derives its
    prompts from the evidence index, whose user turns are the same records
    goals already cite. Malformed/incomplete rows never reach the UI.
    """
    if chat_scoped:
        session_id, root = _chat_identity(trajdir)
        rows = list(CS.load_prompts(session_id, root))
        # Linked chats ride along, each row tagged with where it came from
        # so the picker can say, and -- for a goal-scoped link -- with the
        # goals it is for, so the picker offers it there and below and
        # nowhere else. A global link carries no chat_goals. Their stores
        # are read-only here.
        for sid, label, goal_ids in _linked_sessions(_load_linked(session_id, root)):
            try:
                for row in CS.load_prompts(sid, root):
                    if not isinstance(row, dict):
                        continue
                    row = dict(row, chat=label)
                    if goal_ids is not None:
                        row["chat_goals"] = goal_ids
                    rows.append(row)
            except (OSError, ValueError):
                continue
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


MAX_TREE_ENTRIES = 4000
_TREE_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".next"}


def project_tree(root, depth=3):
    """The project's own files, bounded, for the workspace's file rail.

    Containment is the job: every path is resolved and checked to be inside
    *root* before it is named, so a symlink out of the project cannot be
    listed and nothing above the project is reachable.
    """
    try:
        base = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    if not base.is_dir():
        return []
    budget = [MAX_TREE_ENTRIES]

    def walk(directory, level):
        if level > depth or budget[0] <= 0:
            return []
        try:
            entries = sorted(directory.iterdir(),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return []
        out = []
        for entry in entries:
            if budget[0] <= 0:
                break
            if entry.name.startswith(".") and entry.name != ".claude-plugin":
                continue
            if entry.name in _TREE_SKIP:
                continue
            try:
                entry.resolve().relative_to(base)
            except (OSError, ValueError, RuntimeError):
                continue
            budget[0] -= 1
            out.append({"n": entry.name + "/", "kids": walk(entry, level + 1)}
                       if entry.is_dir() else {"n": entry.name})
        return out

    return walk(base, 1)


def _adopt_server_for_project(trajdir, session_id, root) -> str:
    """Settle which window the chat that just bound should be reading.

    A chat stands its workspace up before it has a project -- it is about to
    be asked which -- so the window is registered under the chat. What
    happens next depends on whether the project it named already has one:

    * It does: the reader is sent there, and the URL to send them to is what
      comes back. Taking the project's window over instead pointed every
      other chat of it at this ad-hoc one, and there were two ports for one
      tree again.
    * It does not: this window becomes the project's, so the next chat to
      join finds it rather than opening another.

    Returns where to send the reader, or "" to leave them where they are.
    """
    try:
        from .. import cli
        from . import project_store as PS
        mine = CS.paths(session_id, root).session_dir
        record = cli._read_server_registry(mine)
        home = CS.bound_project(session_id, root)
        if home:
            standing = _running_workspace(PS.server_record(root, home))
            if standing:
                here = (record or {}).get("url")
                return "" if here == standing["url"] else str(standing["url"])
            if record:
                PS.set_server_record(root, home, record)
        if record:
            held = CS.tree_session(session_id, root)
            if held and held != session_id:
                cli._write_server_registry(
                    CS.paths(held, root).session_dir, record)
        return ""
    except Exception:  # noqa: BLE001 - a binding must not fail over a registry
        return ""


def _ask_which_project(trajdir, session_id, root, goals) -> bool:
    """Whether to put this chat through onboarding -- and if not, say so once.

    Binding arrived after every chat that already exists, so "has no binding"
    describes the whole existing world rather than the new chats onboarding
    is for. What separates the two is work: a chat with a tree of its own has
    been used, and could only have been used before there was anything to
    ask. That is written down rather than merely returned -- a conclusion
    recomputed on every poll would be reached again after the reader
    answered, and answering once is the whole point.

    The directory is deliberately NOT part of this. It was, and it meant that
    a new chat started anywhere somebody had once made a project of joined
    that project unasked: a chat opened in a home directory landed in a tree
    of hundreds of goals belonging to everything else, with no choice offered
    and no way back. Where a chat sits is the suggestion onboarding opens on,
    never the answer.
    """
    if not CS.needs_project_onboarding(session_id, root):
        return False
    if not (goals or {}).get("goals"):
        return True
    try:
        CS.mark_project_migrated(session_id, root)
    except Exception:  # noqa: BLE001 - asking twice beats failing to answer
        pass
    return False


def _project_identity(trajdir, chat_scoped, session_id):
    """The directory this chat works in, named for the reader.

    Empty rather than guessed: a workspace that cannot say where it is should
    say nothing rather than invent a repository. The project is the Claude
    Code project: the directory the chat was started in, which is what its
    manifest recorded. Every chat in that directory is a chat of the same
    project, and what the reader writes about the project (its objective)
    is kept once per directory, not once per chat.
    """
    empty = {"cwd": "", "name": "", "branch": "", "remote": "",
             "objective": "", "description": "", "sources": [], "saved": [],
             "working_dir": "", "worktrees": []}
    if not (chat_scoped and session_id):
        return empty
    try:
        _sid, root = _chat_identity(trajdir)
    except (OSError, ValueError):
        return empty
    # The chat says which project it is for; the directory it was started in
    # is only the suggestion the onboarding opens on. Before the rename of
    # this rule a chat could not be moved between projects at all, because
    # every chat in a folder was the same project by definition.
    cwd = CS.bound_project(str(session_id), root) or _manifest_cwd(str(session_id), root)
    if not cwd:
        return empty
    # One spelling, the same one the project records and the switcher use.
    # Answering "where am I" in a different spelling from "here is every
    # project" meant the reader's own project was not marked as theirs.
    try:
        cwd = PS.repo_home(cwd)
    except Exception:  # noqa: BLE001 - a path git cannot read is still a path
        pass
    path = Path(str(cwd))
    record = _load_project(root, str(path))
    # Named by the reader when they have named it; the directory's own name
    # is the fallback, not the answer -- a project can be renamed without
    # moving.
    # Which checkout of this repository the builds run in, and every
    # checkout there is to choose from. The branch reported is that
    # directory's, not the main worktree's: what the reader is confirming
    # is that Engelbart is on the branch their own Claude Code is on, and
    # the main worktree's branch would answer a question nobody asked.
    where = str(record.get("working_dir") or "") or str(path)
    try:
        trees = PS.worktrees(str(path))
    except Exception:  # noqa: BLE001 - a repository git cannot read is still one
        trees = []
    return {"cwd": str(path), "name": record.get("name") or path.name,
            "branch": AE._git_branch(where) or "",
            "working_dir": str(record.get("working_dir") or ""),
            "worktrees": trees,
            "remote": _git_remote(str(path)),
            "objective": record.get("objective", ""),
            # Written beside the objective and read back here: the overview
            # offers a field for it, and a description kept only in the
            # project file would be invisible until that file was opened.
            "description": record.get("description", ""),
            # What the reader attached to the project: the overview lists
            # them beside the repository and reads each one on demand.
            "sources": record.get("sources", []),
            # The shelf beside the context, and never mixed into it: what
            # the Saved page lists is read by nobody but that page.
            "saved": record.get("saved", [])}


def _all_projects(root, active=""):
    """What the switcher lists: the projects somebody made.

    It used to also list every directory this machine had ever run Claude
    Code in, which is a list of where you have been rather than a list of
    what you are working on -- ~/Downloads was on it. A project is made by
    hand now; the one being looked at is on the list whether or not it has
    a record yet, because leaving it off would say it does not exist.
    """
    rows = {PS._resolved(row["cwd"]): row for row in PS.list_projects(root)}
    here = PS._resolved(active) if active else ""
    # Unless it was just deleted: putting the directory this window happens
    # to sit in back on the list is how a deletion looked like it had failed.
    if here and PS.deleted(root, active):
        here = ""
    if here and here not in rows:
        rows[here] = {"cwd": active, "name": Path(here).name or here,
                      "objective": "", "description": "",
                      "generated_at": "", "goals": 0, "chats": 0}
    return sorted(rows.values(),
                  key=lambda row: (row.get("generated_at") or "",
                                   row.get("name") or ""), reverse=True)


def _manifest_cwd(session_id, root):
    """The directory a chat's manifest names, read as written.

    Read from the file rather than through load_manifest: that loader hands
    back a blank default when the manifest's own session_id disagrees with
    the directory's, which a seeded or copied workspace does -- and the
    directory it was started in is still the directory it was started in.
    """
    try:
        path = CS.paths(str(session_id), root).manifest
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    cwd = value.get("cwd") if isinstance(value, dict) else None
    return cwd if isinstance(cwd, str) else ""


# The project's own record: one file per directory, under the vault base
# beside the chat sessions, so two chats in one repository read and write
# the same objective -- and read the same goals, which the same file holds
# for every chat started there. Its shape and its writers live in
# project_store; these are the names this module already called them by.
PROJECT_OBJECTIVE_LIMIT = PS.PROJECT_OBJECTIVE_LIMIT
_project_path = PS.project_path
_load_project = PS.load_project
_save_project = PS.save_project


PROJECT_FILE_LIMIT = 256 * 1024


def project_file(root, relpath):
    """One text file of the project, for the overview's README pane.

    The same containment rule as the tree: the path is resolved and checked
    to be inside *root* before it is read, so `../` and symlinks out of the
    project read nothing. Bounded, and text only: a binary answers as such
    rather than as mojibake.
    """
    try:
        base = Path(root).expanduser().resolve(strict=True)
        target = (base / str(relpath)).resolve(strict=True)
        target.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return {"ok": False, "error": "no such file in the project"}
    if not target.is_file():
        return {"ok": False, "error": "not a file"}
    try:
        with open(target, "rb") as handle:
            raw = handle.read(PROJECT_FILE_LIMIT + 1)
    except OSError:
        return {"ok": False, "error": "unreadable"}
    if b"\0" in raw[:8192]:
        return {"ok": False, "error": "binary file"}
    truncated = len(raw) > PROJECT_FILE_LIMIT
    text = raw[:PROJECT_FILE_LIMIT].decode("utf-8", errors="replace")
    return {"ok": True, "path": str(target.relative_to(base)),
            "text": text, "truncated": truncated}


def source_body(root, cwd, source):
    """What one attached source says, for the overview's right-hand pane.

    Each kind is read the way that kind can be read, and a kind that cannot
    be read from here says so rather than being faked:

    * a document is a file of the project, under the same containment rule
      the file pane uses -- a path outside the project reads nothing;
    * a conversation is a chat of this vault, shown as its turns;
    * a repository is a name and a link. Nothing is fetched over the
      network: this pane reads the disk.
    """
    kind = str((source or {}).get("type") or "")
    label = str((source or {}).get("label") or "")
    if kind == "chat":
        session = label.strip()
        try:
            CS.paths(session, root)
        except ValueError:
            return {"ok": False, "kind": kind,
                    "error": "that is not a chat of this vault"}
        rows = CS.load_prompts(session, root)
        turns = [{"role": str(r.get("role") or "user"),
                  "text": str(r.get("text") or "")[:2000],
                  "created_at": str(r.get("created_at") or "")}
                 for r in rows if isinstance(r, dict)][-40:]
        if not turns:
            return {"ok": False, "kind": kind,
                    "error": "that chat has no turns yet"}
        return {"ok": True, "kind": kind, "turns": turns,
                "total": len(rows)}
    if kind == "github":
        return {"ok": True, "kind": kind, "label": label}
    if not cwd:
        return {"ok": False, "kind": kind, "error": "no project"}
    if label.startswith(("http://", "https://")):
        return {"ok": True, "kind": kind, "label": label}
    # An absolute path inside the project is named relative to it; one
    # outside is refused by project_file's own containment check.
    rel = label
    try:
        base = Path(cwd).expanduser().resolve()
        here = Path(label).expanduser()
        if here.is_absolute():
            rel = str(here.resolve().relative_to(base))
    except (OSError, ValueError, RuntimeError):
        return {"ok": False, "kind": kind,
                "error": "that file is outside the project"}
    return dict(project_file(cwd, rel), kind=kind)


def project_json(root, cwd, full=False):
    """The project's own record, as text, for the settings panel's Data tab.

    The file as it stands on disk, byte for byte: what a reader opening it
    in an editor would see, indentation and all. A directory whose file has
    never been written -- no chat in it has saved a goal yet -- gets the
    record it would hold, built now and not saved, marked ``written``
    false, so the pane shows the project rather than an absence.

    Bounded like the README pane unless ``full`` is asked for: a project of
    many chats can outgrow what a pane should hand a browser, and a bound
    that says it was reached is better than one that quietly is not. The
    panel's copy button asks for the whole file -- a clipboard has no such
    bound, and a record cut at a quarter is not JSON.
    """
    if not cwd:
        return {"ok": False, "error": "no project"}
    path = PS.project_path(root, cwd)
    written = True
    try:
        with open(path, "rb") as handle:
            raw = handle.read(-1 if full else PROJECT_FILE_LIMIT + 1)
    except OSError:
        written = False
        try:
            raw = (json.dumps(PS.build(root, cwd), indent=1)
                   + "\n").encode("utf-8")
        except (OSError, ValueError, TypeError):
            return {"ok": False, "error": "unreadable"}
    cut = not full and len(raw) > PROJECT_FILE_LIMIT
    text = raw[:PROJECT_FILE_LIMIT] if cut else raw
    return {"ok": True, "path": str(path), "written": written,
            "truncated": cut,
            "text": text.decode("utf-8", errors="replace")}


README_NAMES = ("README.md", "readme.md", "README.markdown", "README")


def project_readme(cwd):
    """The project's front page, under whatever name it was given.

    Every repository has one and almost none of them agree on the case, so
    the few spellings that actually turn up are tried in order and the first
    that reads is the one shown -- nothing is merged, and a project that has
    no front page says so rather than answering with an empty pane.
    """
    if not cwd:
        return {"ok": False, "error": "no project"}
    for name in README_NAMES:
        found = project_file(cwd, name)
        if found.get("ok"):
            return found
    return {"ok": False, "error": "this project has no README"}


# What one question is allowed to carry, and how much of a source is read
# to answer it. Both are bounds on what leaves this machine, not on what a
# model could hold: a question is a sentence and a README is not a corpus.
ASK_QUESTION_LIMIT = 2000
ASK_CONTEXT_LIMIT = 60 * 1024


def ask_source(root, cwd, source, question, engine=None):
    """Answer one question about one piece of this project's context.

    The context is that one source and nothing else -- the repository's
    README and where it sits, a document's text, or a conversation's turns.
    A question asked in front of one pane is answered from that pane, so an
    answer can be checked against what is on screen beside it.

    Nothing is stored: the question goes to the configured provider and the
    answer comes back to the caller. A source that cannot be read from here
    (a link, a repository nobody has cloned) is refused rather than asked
    about from its name alone.
    """
    words = " ".join(str(question or "").split())[:ASK_QUESTION_LIMIT]
    if not words:
        return {"ok": False, "error": "ask something first"}
    if source is None:
        if not cwd:
            return {"ok": False, "error":
                    "this chat has no project directory to read"}
        body = project_readme(cwd)
        parts = ["# The repository", "", "directory: " + str(cwd)]
        if body.get("ok"):
            parts += ["", "## " + str(body.get("path")), "",
                      str(body.get("text"))]
        else:
            parts += ["", "It has no README to read."]
        text = "\n".join(parts)
    else:
        body = source_body(root, cwd, source)
        if not body.get("ok"):
            return {"ok": False,
                    "error": str(body.get("error") or "could not read it")}
        if str(body.get("kind")) == "chat":
            text = "\n\n".join(
                ["# The conversation", ""] +
                ["%s: %s" % (str(turn.get("role")), str(turn.get("text")))
                 for turn in body.get("turns") or []])
        elif isinstance(body.get("text"), str):
            text = "\n".join(["# " + str(body.get("path")
                                          or source.get("label") or "the file"),
                              "", str(body.get("text"))])
        else:
            return {"ok": False, "error":
                    "that is a link, not something this pane can read"}
    ask = [
        "You are answering ONE question about ONE piece of a project's",
        "context, quoted in full below. Answer from that text and from",
        "nothing else: where it does not say, say that it does not say",
        "rather than filling the gap from what projects like this usually",
        "do. Be brief -- a few sentences or a short list. Markdown is fine.",
        "No preamble, no restating of the question.",
        "", "# The context", "", text[:ASK_CONTEXT_LIMIT], "",
        "# The question", "", words,
    ]
    out = _answer(ask, engine)
    if not out.get("ok"):
        return out
    return {"ok": True, "asked": words, "answer": out["answer"]}


def _answer(lines, engine=None, read_dirs=None, search_dir=""):
    """Put one assembled prompt to the configured provider.

    Nothing here decides what a question is worth asking about -- the caller
    has already built the prompt. This is only the round trip, and the ways
    it can come back with no answer said the same way in both places that
    ask: what the provider said went wrong when it could name it, and one
    guess at the usual cause when it could not.

    *read_dirs* is for the one prompt whose material is not in it: a scenario
    drafted from screenshots names files, and the provider has to be allowed
    to open them.

    *search_dir* is for the prompt that does not know where its material is:
    a question about how the project behaves is answered out of the project,
    so the call is made in that directory with the tools to search it.
    """
    from . import providers as PROVIDERS
    try:
        engine = engine or PROVIDERS.make(
            os.environ.get("HC_CHAT_PROVIDER", "claude"), "synthesize")
        # A question is a question, not an agent turn. Providers that know
        # the difference are asked for the plain round trip; a test double
        # with one method keeps being called the one way it has.
        if search_dir:
            speak = getattr(engine, "generate_searching", None)
            answer = (speak("\n".join(lines) + "\n", search_dir) if speak
                      else engine.generate("\n".join(lines) + "\n"))
        elif read_dirs:
            speak = getattr(engine, "generate_reading", None)
            answer = (speak("\n".join(lines) + "\n", read_dirs) if speak
                      else engine.generate("\n".join(lines) + "\n"))
        else:
            speak = getattr(engine, "generate_plain", None) or engine.generate
            answer = speak("\n".join(lines) + "\n")
    except PROVIDERS.ProviderError as exc:
        # What went wrong, in the provider's own words. A CLI that is not
        # installed, one that ran out of time and one whose login was
        # refused used to read identically here, and want three different
        # things from the person looking at the message.
        return {"ok": False, "error": " ".join(str(exc).split())[:200]}
    except Exception:  # noqa: BLE001 - any other provider failure is "no answer"
        return {"ok": False, "error":
                "the answer could not be generated (is the claude CLI on "
                "PATH?)"}
    answer = str(answer or "").strip()
    if not answer:
        return {"ok": False, "error": "the model answered with nothing"}
    return {"ok": True, "answer": answer}


# What one highlighted passage may carry, and how much of the goal around it
# travels with the question. A passage is what a cursor was dragged over and
# a goal is not a corpus: both are bounds on what leaves this machine.
ASK_SELECTION_LIMIT = 4000
ASK_GOAL_LIMIT = 24 * 1024

# How much of a panel's own conversation travels with a follow-up. "What
# about the second one?" is only a question if what came before it is there
# too -- but a panel is a conversation, not a transcript to replay whole.
ASK_TURN_LIMIT = 6
ASK_ANSWER_LIMIT = 2000


def _lineage(goals, goal_id):
    """*goal_id* and every goal above it, outermost first.

    An empty list when there is no such goal -- a passage highlighted
    somewhere the tree does not reach is still a passage, and gets asked
    about on its own rather than under a goal invented for it.
    """
    by_id = {}
    for goal in goals or []:
        if isinstance(goal, dict) and goal.get("id") is not None:
            by_id[str(goal["id"])] = goal
    chain, seen = [], set()
    at = by_id.get(str(goal_id or ""))
    while isinstance(at, dict) and str(at.get("id")) not in seen:
        seen.add(str(at.get("id")))
        chain.append(at)
        at = by_id.get(str(at.get("parent_goal_id") or ""))
    chain.reverse()
    return chain


def _row_depth(row):
    try:
        return max(0, min(8, int(row.get("depth") or 0)))
    except (TypeError, ValueError):
        return 0


def goal_context(goals, goal_id, objective=""):
    """The goal a highlighted passage sits in, written out for a model.

    Its ancestors by title alone -- they are where it sits, not what it is
    about -- and then the goal itself whole: what it is called, how it
    stands, the notes someone wrote on it, and its TODO rows with the state
    the builder left each one in. The objective leads, when the project has
    one, because half the questions worth asking about a goal are about how
    it serves that.
    """
    chain = _lineage(goals, goal_id)
    if not chain:
        return ""
    parts = []
    objective = " ".join(str(objective or "").split())
    if objective:
        parts += ["# The objective", "", objective, ""]
    if len(chain) > 1:
        parts += ["# Where it sits", ""]
        parts += ["%s- %s" % ("  " * n, str(g.get("title") or "Untitled"))
                  for n, g in enumerate(chain[:-1])]
        parts += [""]
    goal = chain[-1]
    parts += ["# The goal", "",
              "title: " + str(goal.get("title") or "Untitled"),
              "status: " + str(goal.get("status") or "active")]
    notes = str(goal.get("notes") or "").strip()
    if notes:
        parts += ["", "## Its notes", "", notes]
    rows = [r for r in (goal.get("todo_items") or []) if isinstance(r, dict)]
    if rows:
        parts += ["", "## Its TODO rows", ""]
        for row in rows:
            state = str(row.get("status") or "").strip()
            parts.append("%s- %s%s" % (
                "  " * _row_depth(row), str(row.get("text") or ""),
                (" [%s]" % state) if state else ""))
    return "\n".join(parts)[:ASK_GOAL_LIMIT]


def _earlier_turns(turns):
    """The panel's own conversation so far, oldest first, as prompt lines.

    Only pairs that finished: a question whose answer never arrived is not
    something the next answer can build on, and a half turn quoted back at
    the model reads as an answer it already gave.

    Two panels keep their turns and they spell them differently -- the ask
    panel holds a conversation it will throw away, the Understanding tab
    holds one it writes onto the goal. Both spellings are read here rather
    than one of them being renamed to suit the other.
    """
    said = []
    for turn in list(turns or [])[-ASK_TURN_LIMIT:]:
        if not isinstance(turn, dict):
            continue
        asked = " ".join(str(turn.get("question") or turn.get("q") or ""
                             ).split())[:ASK_QUESTION_LIMIT]
        answer = str(turn.get("answer") or turn.get("a")
                     or "").strip()[:ASK_ANSWER_LIMIT]
        if asked and answer:
            said += ["Q: " + asked, "", "A: " + answer, ""]
    return said


def ask_selection(goals, goal_id, selection, question, objective="",
                  turns=None, engine=None):
    """Answer one question about a passage someone highlighted.

    A goal, a subgoal, a TODO row, a line of notes: whatever the cursor was
    dragged over is the subject, and the goal it sits in is quoted around it
    so the answer knows what the passage belongs to. Nothing else of the
    workspace travels -- the other goals in the tree are not the question.

    Unlike a question about a document, this one may be a question about
    what to do next, so the model is allowed to suggest -- and told to say
    which part is a suggestion rather than something the workspace records.

    A question is rarely the only one. *turns* is what the same panel has
    already asked and been told, so a follow-up can be a follow-up rather
    than a question that has to restate everything before it.

    Nothing is stored: the answer goes back to the caller and the goal's own
    record is untouched. The conversation lives in the panel that is holding
    it and dies with it. This is a way to think beside the tree, not a way
    to write into it.
    """
    words = " ".join(str(question or "").split())[:ASK_QUESTION_LIMIT]
    if not words:
        return {"ok": False, "error": "ask something first"}
    passage = str(selection or "").strip()[:ASK_SELECTION_LIMIT]
    if not passage:
        return {"ok": False, "error": "highlight something first"}
    around = goal_context(goals, goal_id, objective)
    ask = [
        "You are answering ONE question about a passage someone highlighted",
        "in the goal workspace of a project. The passage is quoted below,",
        "with the goal it sits in around it.",
        "",
        "Answer from that. Where the workspace does not say, say that it",
        "does not say rather than filling the gap from what projects like",
        "this usually do. When the question asks you to brainstorm, suggest",
        "or weigh options, do that -- and mark plainly which part is your",
        "suggestion rather than something the workspace already records.",
        "",
        "Be brief -- a few sentences or a short list. Markdown is fine. No",
        "preamble, no restating of the question.",
    ]
    said = _earlier_turns(turns)
    if said:
        ask += ["",
                "This is a conversation already under way. Everything below",
                "was said in it, about the same passage; the last question",
                "is the one to answer, and it may lean on what came before",
                "without repeating it."]
    if around:
        ask += ["", "# The goal it sits in", "", around]
    ask += ["", "# The highlighted passage", "", passage]
    if said:
        ask += ["", "# What has been asked and answered so far", ""] + said
    ask += ["", "# The question", "", words]
    out = _answer(ask, engine)
    if not out.get("ok"):
        return out
    return {"ok": True, "asked": words, "selection": passage,
            "answer": out["answer"]}


# What is said to a call that has nothing but the prompt: no project on this
# machine to look in, or a workspace that is somebody else's.
SCENARIO_NO_TOOLS = [
    "You have no tools on this call: nothing to search with, nothing to run,",
    "no file to open. Everything the answer may come from is quoted below.",
    "Do not write a tool call, a command or a search -- there is nothing here",
    "to run it, and what you write is kept on the goal as the answer.",
]

# The shape every answer about a scenario comes back in. It was GIVEN / WHEN
# / THEN, on the reasoning that cases compare when they are written the same
# way -- but a reader asking "what happens to the second build" wants the
# answer, and reading it back out of three capitalised clauses is work the
# form was supposed to save. The shape belongs to the scenario, which is one
# situation written once; the answers are ordinary answers.
SCENARIO_SHAPE = [
    "Answer in plain prose -- a few sentences, or a short paragraph or two",
    "where the question needs them. No heading, no bullets, no preamble, no",
    "closing offer of help, and no restating of the question: the first",
    "sentence is already the answer.",
]

# Where the answer is allowed to come from when the prompt is all there is.
SCENARIO_FROM_WORDS = [
    "Answer from the scenario and the goal around it. Where they do not say,",
    "do not fill the gap from what projects like this usually do: say what",
    "you are assuming, in the sentence that rests on it. If the question",
    "cannot be answered from what is here at all, say so in one sentence and",
    "then say what would settle it.",
]

SCENARIO_FORM = (SCENARIO_NO_TOOLS + [""] + SCENARIO_SHAPE + [""]
                 + SCENARIO_FROM_WORDS)


def scenario_from_code(where):
    """What is said when the project the scenario is about is on this disk.

    Half the questions a reader types into the tab are questions about what
    the code already does -- what happens to the second build, which of two
    edits wins -- and the honest answer to those is not an assumption, it is
    the file that decides it. So the call is given the repository and told to
    go and look, and told that UNCLEAR is what you write after looking and
    not instead of it.
    """
    return [
        "You are in the project this goal belongs to: %s. You can look" % where,
        "through it -- Glob for files, Grep for the names the scenario uses,",
        "Read what you find. Nothing here writes or runs anything.",
        "",
        "Where the question turns on what the code actually does, go and find",
        "out before you answer. Do not answer such a question from what a",
        "project like this usually does while the code that settles it is",
        "sitting in front of you.",
        "",
        "Then answer from what you read, plus the scenario and the goal around",
        "it. Name your evidence: a sentence that comes from the code ends with",
        "the file and line it came from in parentheses, like",
        "(src/build.py:212). Where nothing in the project or the scenario",
        "settles a point, say what you are assuming, in the sentence that",
        "rests on it.",
        "",
        "Say plainly what is still open after you have looked, and what would",
        "settle it -- but never that you could not check the code, because you",
        "can.",
    ]

# A reply that is not an answer at all but a tool call written out as text: a
# provider that reached for a tool it was not given prints the call instead of
# running it, and a call kept on the goal is read as the answer for as long as
# the goal lives. Prose is prose, so there is no shape left to check -- this
# is the one thing still worth refusing.
SCENARIO_TOOL_CALL = re.compile(
    r'^(?:\*\*|#{1,6}\s*)?tool(?:\s+(?:call|use|name))?\s*(?:\*\*)?\s*:'
    r'|^\{\s*"(?:command|tool|tool_name|name|input|parameters)"\s*:', re.I)


def scenario_answer(text) -> str:
    """What came back, as the tab keeps it -- or "" when it is not an answer.

    A whole reply wrapped in one code fence is unwrapped: the fence is the
    model's packaging, not the reader's answer. A fence around part of a reply
    is left alone, because there it is usually the code being quoted back.

    Only the opening line is weighed against a tool call. A printed-out call
    always opens with one; an answer that quotes a line of JSON in the middle
    of itself is an answer, and refusing that would be refusing the answers
    that went and looked.
    """
    body = str(text or "").strip()
    fenced = re.match(r"^```[^\n]*\n(.*)\n```$", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    if not body or SCENARIO_TOOL_CALL.match(body):
        return ""
    return body


# What is said to a provider that wrote a tool call out instead of answering.
# Its own reply is not quoted back: what went wrong with it is that it was not
# an answer, and what an answer is is already above.
SCENARIO_AGAIN = [
    "",
    "# Your last reply was not an answer",
    "",
    "It was a tool call, printed out. You have no tools on this call and",
    "nothing to look at beyond what is quoted above: answer from the scenario",
    "and the goal, in prose, and nothing else. Where they do not say, say",
    "what you are assuming in the sentence that rests on it.",
]

# The same, said to a call that does have the project to look in. Its looking
# was never the problem -- so it is not told to stop, only to run the search
# rather than print it.
SCENARIO_AGAIN_IN_CODE = [
    "",
    "# Your last reply was not an answer",
    "",
    "It was a tool call, printed out rather than run. What you write now is",
    "kept on the goal as the answer, so write the answer itself: prose, and",
    "nothing else around it. You really can search this project -- make the",
    "call, read what comes back, and answer from it.",
]


def scenario_cwd(cwd):
    """The project directory a question may be answered out of, or "".

    A directory decides what a subprocess is allowed to open, so it is taken
    from the workspace's own record of where this chat works rather than from
    anything a browser posted -- and it is still only used when it is really
    a directory on this machine, which a shared workspace's is not.
    """
    where = str(cwd or "").strip()
    if not where:
        return ""
    try:
        path = Path(where).expanduser().resolve()
    except OSError:
        return ""
    return str(path) if path.is_dir() else ""


def ask_scenario(goals, goal_id, scenario, question, objective="",
                 turns=None, cwd="", engine=None):
    """Answer one question about the scenario a goal's work is for.

    The Understanding tab's question boxes come here. What is being asked
    about is the situation the reader described, with the goal it belongs to
    quoted around it, and what comes back is an ordinary answer in prose. The
    GIVEN/WHEN/THEN shape lives on the scenario itself -- see
    ``draft_scenario`` -- where it is written once and read many times; an
    answer is read once, by the person who asked.

    *cwd* is the project this chat works in, when it is on this machine. Most
    of what a reader asks the tab is a question about what the code already
    does, and the answer to that is in the code: the call is put in that
    directory and allowed to search it, rather than being asked to guess and
    reporting back that it could not check. Without one -- a workspace shared
    from another machine -- the question is answered from its own words, as
    it always was.

    *turns* is the thread this question already has, so a follow-up can lean
    on the answer above it. Unlike ``ask_selection``, what comes back does get
    kept -- the tab writes it onto the goal through ``set_understanding``, and
    a build of the goal's rows opens on the answers along with the questions.
    """
    words = " ".join(str(question or "").split())[:ASK_QUESTION_LIMIT]
    if not words:
        return {"ok": False, "error": "ask something first"}
    said = str(scenario or "").strip()[:GM.MAX_SCENARIO]
    if not said:
        return {"ok": False, "error": "describe the scenario first"}
    around = goal_context(goals, goal_id, objective)
    where = scenario_cwd(cwd)
    ask = [
        "You are answering ONE question about a scenario someone wrote in the",
        "goal workspace of a project: the situation a goal's work is for, in",
        "their own words, quoted below with the goal it belongs to.",
        "",
    ] + (scenario_from_code(where) + [""] + SCENARIO_SHAPE if where
         else SCENARIO_FORM)
    earlier = _earlier_turns(turns)
    if earlier:
        ask += ["",
                "This question has been asked and answered already and what",
                "follows is a follow-up. Everything below was said about this",
                "same scenario; the last question is the one to answer, and it",
                "may lean on what came before without repeating it."]
    if around:
        ask += ["", "# The goal it belongs to", "", around]
    ask += ["", "# The scenario", "", said]
    if earlier:
        ask += ["", "# What has been asked and answered so far", ""] + earlier
    ask += ["", "# The question", "", words]
    out = _answer(ask, engine, search_dir=where)
    if not out.get("ok"):
        return out
    said_back = scenario_answer(out["answer"])
    if not said_back:
        # Asked once more, told what was wrong with the first reply. What
        # comes back here is kept on the goal and opens every build of its
        # rows, so a reply that is a printed-out tool call is worth a second
        # call -- and, if the second is no better, worth refusing rather than
        # writing down.
        out = _answer(ask + (SCENARIO_AGAIN_IN_CODE if where
                             else SCENARIO_AGAIN), engine, search_dir=where)
        if not out.get("ok"):
            return out
        said_back = scenario_answer(out["answer"])
    if not said_back:
        return {"ok": False, "error":
                "the model wrote a tool call instead of an answer -- ask again"}
    return {"ok": True, "asked": words, "answer": said_back[:GM.MAX_ANSWER]}


def scenario_shots(trajdir, shots):
    """The posted screenshots that are really this workspace's, resolved.

    A path arrives from a browser and decides what a subprocess is allowed to
    open, so it is not taken on its word: only files that are already under
    this workspace's own attachments directory survive, which is every file
    /api/attachment ever handed out and nothing else.
    """
    try:
        folder = Path(_scope(trajdir) / "attachments").resolve()
    except OSError:
        return []
    out = []
    for shot in GM.normalize_shots(shots):
        try:
            path = Path(shot["path"]).expanduser().resolve()
        except OSError:
            continue
        if path.parent != folder or not path.is_file():
            continue
        out.append({"path": str(path), "name": shot["name"]})
    return out


# The shape a scenario is written in. Not the answers -- those are prose now
# -- but the situation itself, which is written once and then read by every
# build of the goal's rows: what is true before anything happens, what
# happens, and what follows. A line whose keyword the reader's words do not
# fill is left standing empty rather than invented, and comes back with a
# question beside it.
SCENARIO_MAP_FORM = [
    "Map what they wrote onto GIVEN / WHEN / THEN and write nothing else.",
    "One or more blocks, each line beginning with its word in capitals:",
    "",
    "GIVEN <what is true before anything happens>",
    "WHEN <what happens, or what someone does>",
    "THEN <what follows from it>",
    "",
    "AND on its own line continues the GIVEN, WHEN or THEN above it. Write a",
    "second block when their words cover a case the first does not.",
    "",
    "Use their own words wherever they gave you words to use. This is a",
    "mapping, not a rewrite: nothing about what should be built, no task",
    "list, no solution, nothing they did not say.",
    "",
    "Where their words do not say what belongs on a line, do not invent it",
    "and do not leave the line out. Write the keyword alone on its line, and",
    "add one line beginning ASK, saying what you would need them to tell you",
    "to fill it in -- one ASK for each empty line, in the order they appear.",
    "If their words map onto nothing at all, write no GIVEN, WHEN or THEN and",
    "only the ASK lines.",
]

# The words a mapped scenario is built out of, and the question beside a line
# it could not fill.
SCENARIO_MAP_KEYWORD = re.compile(r"^(?:GIVEN|WHEN|THEN|AND)\b")
SCENARIO_MAP_ASK = re.compile(r"^ASK\b[\s:>-]*")


def _scenario_line(line) -> str:
    """One line with the markdown a model reached for taken off.

    A keyword bulleted or set in bold is a keyword the tab cannot see at the
    head of the line. What the form asked for is the bare line, and this is
    the bare line.
    """
    line = re.sub(r"^(?:[-*+]\s+|#{1,6}\s+|>\s+)", "", str(line).strip())
    return line.replace("**", "").replace("__", "").strip()


def scenario_mapped(text):
    """A mapped scenario out of what came back: (lines, questions).

    The keyword lines are the scenario, in the order they were written. The
    ASK lines are not part of it -- they are what the reader has to fill in
    themselves, and the tab shows them under the box rather than writing them
    into it, because a question is not a situation.
    """
    lines, asks = [], []
    for raw in str(text or "").splitlines():
        line = _scenario_line(raw)
        if not line or line.startswith("```"):
            continue
        if SCENARIO_MAP_ASK.match(line):
            words = SCENARIO_MAP_ASK.sub("", line).strip()
            if words and len(asks) < GM.MAX_QUESTIONS:
                asks.append(words[:ASK_QUESTION_LIMIT])
        elif SCENARIO_MAP_KEYWORD.match(line):
            lines.append(line)
    return "\n".join(lines)[:GM.MAX_SCENARIO], asks


def draft_scenario(trajdir, text, shots, objective="", engine=None):
    """Write the scenario field from what the reader has to hand.

    Screenshots of the thing, a few rough words about it, or both. The
    screenshots are the point: a person who can show you the screen they mean
    should not have to describe it first, and the model is given the files to
    open rather than a description of them.

    What comes back is their own natural language mapped onto GIVEN / WHEN /
    THEN, plus the questions for whatever would not map -- an empty THEN
    comes back as an empty THEN and a question beside it, not as a plausible
    sentence somebody has to notice was invented.

    Nothing here is saved: the tab puts the lines in the box and the box is
    still theirs to edit.
    """
    notes = str(text or "").strip()[:GM.MAX_SCENARIO]
    files = scenario_shots(trajdir, shots)
    if not notes and not files:
        return {"ok": False,
                "error": "paste a screenshot or type something first"}
    ask = [
        "You are writing the SCENARIO field of one goal in a project's goal",
        "workspace: the situation that goal's work is for, so that anyone",
        "picking the work up knows what it is for before they read a single",
        "task.",
        "",
        "A scenario is a situation in the present tense -- who is doing what,",
        "where they are doing it, and what is true while they do.",
        "",
    ] + SCENARIO_MAP_FORM
    objective = " ".join(str(objective or "").split())
    if objective:
        ask += ["", "# The project's objective", "", objective]
    if files:
        ask += ["", "# Screenshots of the situation", "",
                "Open every one of these with Read before you write. They are",
                "the material, not decoration.", ""]
        ask += ["- " + shot["path"] for shot in files]
    if notes:
        ask += ["", "# What the reader typed", "", notes]
    out = _answer(ask, engine,
                  read_dirs=sorted({str(Path(s["path"]).parent)
                                    for s in files}) if files else None)
    if not out.get("ok"):
        return out
    mapped, asks = scenario_mapped(out["answer"])
    if not mapped and not asks:
        # Neither a line nor a question came back. Refused rather than
        # emptying the box the reader typed into: what they wrote is still
        # the best scenario anyone has.
        return {"ok": False, "error":
                "that did not map onto GIVEN / WHEN / THEN -- write it here"}
    return {"ok": True, "scenario": mapped, "asks": asks, "shots": files}


def _git_remote(cwd):
    """The origin URL as git records it, or "" -- never a placeholder."""
    try:
        text = (Path(cwd).expanduser().resolve() / ".git" / "config").read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    block = re.search(r'\[remote "origin"\]([^\[]*)', text)
    if not block:
        return ""
    url = re.search(r"url\s*=\s*(\S+)", block.group(1))
    return url.group(1) if url else ""


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
# was last handed) and the session manifest (whether /bart is on). Nothing
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


def _remember_name(SB, name, root):
    """Store the chosen name, if one was given and we are signed in.

    Best effort on purpose: failing to record a name must not turn a
    successful sign-in into a failed one.
    """
    text = str(name or "").strip()
    if not text:
        return
    try:
        SB.set_display_name(text, root)
    except SB.SupabaseError:
        pass
    except (OSError, ValueError):
        pass


def _supabase_status(SB, root, cwd=None):
    """The panel's view of the connection, with the name it signed up as."""
    state = dict(SB.status(root), ok=True)
    state["autosync"] = AUTOSYNC.state(root, cwd)
    if state.get("signed_in"):
        try:
            state["display_name"] = SB.display_name(root)
        except (SB.SupabaseError, OSError, ValueError):
            state["display_name"] = ""
    else:
        state["display_name"] = ""
    return state


# The shared workspaces this process has opened, by project. One server
# per project rather than one per click: opening the same project twice
# should be the same window, not a second one on a second port.
_SHARED_SERVERS = {}
_SHARED_GUARD = threading.Lock()


def open_shared(project_id, trajdir=None):
    """Start a shared workspace for one project, or hand back the one that
    is already up."""
    pid = str(project_id)
    with _SHARED_GUARD:
        held = _SHARED_SERVERS.get(pid)
        if held and held.get("thread") and held["thread"].is_alive():
            return {"ok": True, "url": held["url"], "already": True}
        started = run_shared(pid, open_browser=False, trajdir=trajdir)
        if not started:
            return {"ok": False, "error": "no free port for a shared workspace"}
        _SHARED_SERVERS[pid] = started
        return {"ok": True, "url": started["url"], "already": False}


_PROJECT_SERVERS = {}


def _running_workspace(record, session_id=None):
    """The workspace a record names, if it is actually still there.

    A record is a claim: the process it names may be gone, and handing the
    reader a dead port is worse than opening a new window. Probed through
    the launcher's own check so every place that asks "is it up?" asks the
    same question.
    """
    if not isinstance(record, dict) or not record.get("url"):
        return None
    try:
        from .. import cli
    except Exception:  # noqa: BLE001 - without the launcher, believe nothing
        return None
    who = str(session_id or record.get("session_id") or "")
    return record if cli._healthy_chat_server(record, who) else None


def open_project(cwd, trajdir=None):
    """Bring up another project's workspace beside this one.

    A workspace is chat-scoped -- it serves one session's goals -- so the
    newest chat started in that directory is the one opened. A directory the
    vault has never held a chat for has nothing to serve, and says so rather
    than opening an empty page.
    """
    root = None
    if trajdir is not None:
        try:
            _, root = _chat_identity(_scope(trajdir))
        except Exception:
            root = None
    key = PS._resolved(cwd)
    sessions = PS.project_sessions(root, cwd)
    fresh = False
    if not sessions:
        # A project nobody has worked in has no chat, and a workspace serves
        # one chat's goals. This used to refuse and say "run claude there",
        # which made creating a project a dead end: the reader clicked the
        # thing they had just made and was told to go elsewhere. Make the
        # workspace instead. It is empty, which is what a new project is.
        where = Path(str(cwd)).expanduser()
        if not where.is_dir():
            return {"ok": False, "cwd": str(cwd),
                    "error": "that directory is not there any more"}
        try:
            sessions = [CS.open_workspace_for(where, root)]
            fresh = True
        except (OSError, ValueError) as exc:
            return {"ok": False, "cwd": str(cwd),
                    "error": "could not start a workspace there: "
                             + str(exc)[:120]}
    # The project's own tree, not whichever session sorted last: a reader
    # clicking into a project wants the goals of that project, and sessions
    # are named with UUIDs, so "last" was arbitrary.
    session_id = CS.tree_session(sessions[-1], root) or sessions[-1]
    with _SHARED_GUARD:
        # What the project says is running, before anything this process
        # remembers: a window opened by another process -- the detached one
        # /bart starts -- is invisible to a dictionary kept in this one, and
        # asking only ourselves is how a second port appeared for one tree.
        noted = _running_workspace(PS.server_record(root, cwd))
        if noted:
            return {"ok": True, "url": noted["url"], "already": True,
                    "session_id": str(noted.get("session_id") or session_id)}
        held = _PROJECT_SERVERS.get(key)
        if held and held.get("thread") and held["thread"].is_alive():
            return {"ok": True, "url": held["url"], "already": True,
                    "session_id": held["session_id"]}
        started = _serve_session(session_id, root)
        if not started:
            return {"ok": False, "error": "no free port for a workspace"}
        started["session_id"] = session_id
        _PROJECT_SERVERS[key] = started
        # Written where every other chat of the project looks, so the next
        # one to ask finds this window instead of opening another.
        try:
            PS.set_server_record(root, cwd, {
                "schema_version": 1, "session_id": session_id,
                "pid": os.getpid(), "url": started["url"],
                "started_at": time.time()})
        except Exception:  # noqa: BLE001 - a note is not worth failing over
            pass
        return {"ok": True, "url": started["url"], "already": False,
                "session_id": session_id, "fresh": fresh}


def _serve_session(session_id, root, port=8870):
    """A second chat-scoped server, in a thread of this process."""
    try:
        session_dir = CS.paths(str(session_id), root).session_dir
    except ValueError:
        return None
    holder = {}
    ready = threading.Event()

    def note(url, _srv):
        holder["url"] = url
        ready.set()

    thread = threading.Thread(
        target=lambda: run(port=port, open_browser=False, trajdir=session_dir,
                           ready_callback=note, label="Project",
                           idle_timeout=None, replace=False),
        daemon=True)
    thread.start()
    if not ready.wait(12) or not holder.get("url"):
        return None
    return {"url": holder["url"], "thread": thread}


def _looks_like_a_path(text):
    """Whether what was typed names a place on disk rather than a project.

    A name is the ordinary way in, so only text that could not be one --
    text with a separator in it, or a leading ``~`` or ``.`` -- is read as a
    directory. "My redesign" is a name; "~/Projects/redesign" is a path.
    """
    return (text.startswith(("~", "/", "./", "../"))
            or "/" in text or os.sep in text)


# A repository is written one of three ways -- an http(s) URL, an ssh or git
# one, or the scp-like ``git@host:owner/repo`` -- and a repository is checked
# for before a path is, because every one of these has a slash in it and
# would otherwise be read as a folder that is not there.
_REPO_URL = re.compile(
    r"^(?:https?://|ssh://|git://|git\+ssh://)[^\s]+$|"
    r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s]+$")


def _looks_like_a_repo(text):
    """Whether what was typed is a repository to clone rather than a name.

    Only the transports git is asked to speak here. ``ext::`` and ``file::``
    are URLs git understands and are not among them: the first runs a command
    of the URL's choosing, and neither is what anybody means by "the repo".
    """
    words = str(text or "").strip()
    if not words or words.startswith("-"):
        return False
    if words.lower().startswith(("ext::", "file::", "file://")):
        return False
    return bool(_REPO_URL.match(words))


def _repo_name(url):
    """The repository's own name, off the end of its URL: the folder a clone
    would have made, without the ``.git``."""
    text = str(url or "").strip().rstrip("/")
    text = text.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return text[:-4] if text.endswith(".git") else text


def _clone(url, into):
    """Run the clone, and answer with what went wrong, or "" if nothing did.

    The argument list is git's, never a shell's, and ``--`` ends the options
    so a URL that begins with a dash is a URL rather than a flag. Credential
    prompts are turned off: this is run by a server nobody is looking at, and
    a private repository must fail saying so rather than hang on a password
    question asked into a terminal that is not there.
    """
    import subprocess

    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="",
               SSH_ASKPASS="", GCM_INTERACTIVE="never")
    try:
        done = subprocess.run(["git", "clone", "--", str(url), str(into)],
                              capture_output=True, text=True, env=env,
                              timeout=15 * 60)
    except FileNotFoundError:
        return "git is not installed on this machine"
    except subprocess.TimeoutExpired:
        return "that clone took too long — try it in a terminal"
    except OSError as exc:
        return str(exc)[:200]
    if done.returncode:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        return said[-1][:200] if said else "git could not clone that"
    return ""


def _taken(taken):
    """The answer for a name somebody already used.

    A refusal rather than the project that has it: the reader is making a
    second thing and would not be able to tell the two apart afterwards.
    ``duplicate`` is what the box reads to put the cursor back in the name.
    """
    return {"ok": False, "duplicate": True,
            "name": str(taken.get("name") or ""),
            "cwd": str(taken.get("cwd") or ""),
            "error": 'a project is already called "%s" — name this one '
                     'something else' % str(taken.get("name") or "")}


def _made(root, where, name, **extra):
    """The answer for a project that now exists: where it is, what it is
    called, and whether it has anything in it yet.

    ``setup`` is the whole of the onboarding question: a project with no
    chats and no objective knows nothing about itself that the reader has
    not typed, so the two questions worth asking are asked at once, here,
    rather than waiting for a chat to be started in it.
    """
    record = PS.load_project(root, where)
    chats = len(PS.project_sessions(root, where))
    return dict({"ok": True, "cwd": str(where),
                 "name": record.get("name") or name,
                 "chats": chats,
                 "setup": not chats and not record.get("objective")}, **extra)


def clone_project(url, name="", root=None):
    """Make a project out of a repository: clone it, and let where it landed
    be the project's directory.

    The clone goes into a home of its own beside the other projects rather
    than anywhere on the reader's disk, for the same reason a named project
    does -- nobody asked to choose a parent folder, and a repository that is
    a project is one whether or not it sits under ~/Projects.
    """
    address = str(url or "").strip()
    if not _looks_like_a_repo(address):
        return {"ok": False, "error": "that is not a repository URL"}
    text = str(name or "").strip()[:PS.PROJECT_NAME_LIMIT] or _repo_name(address)
    home = PS.workspace_home(root, text)
    if home is None:
        return {"ok": False, "error": "give the project a name"}
    taken = PS.project_named(root, text)
    if taken:
        return _taken(taken)
    try:
        if home.exists() and any(home.iterdir()):
            return _taken({"name": text, "cwd": str(home)})
        home.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}
    trouble = _clone(address, home)
    if trouble:
        return {"ok": False, "error": trouble}
    PS.save_project(root, str(home), {"name": text})
    return _made(root, str(home), text, cloned=address)


def new_project(name, cwd=None, root=None, repo=None, parent=None):
    """Make a project: from a name, from a repository, or from a directory
    that already exists.

    Typing a name is the ordinary way -- a project is somewhere to keep an
    objective, goals and sources, and most have no directory to point at
    when they are made, so one is minted for them inside the vault. Give a
    repository and it is cloned into that home instead, so the project is
    the code from the first moment. A path is still taken when one is typed,
    so a repository already on disk can become the project it is; that
    directory has to exist, since a mistyped path should be reported rather
    than quietly created.

    Either half of what the browser sends is accepted in either field: a
    reader who types a path -- or a repository URL -- into the name box means
    that, and the box has one line in it.
    """
    text = str(name or cwd or "").strip()
    address = str(repo or "").strip()
    if address or _looks_like_a_repo(text):
        url = address or text
        # The name box holds the URL itself when that is all that was typed;
        # when a name was typed beside it, that is what to call the project.
        called = "" if text == url or _looks_like_a_repo(text) else text
        return clone_project(url, called, root)
    if not text:
        return {"ok": False, "error": "give the project a name"}
    if not _looks_like_a_path(text):
        taken = PS.project_named(root, text)
        if taken:
            return _taken(taken)
        # A parent the reader chose is where they want the project to live;
        # the vault is only where one goes when nobody said.
        seat = str(parent or "").strip()
        if seat:
            where = PS.create_under(root, seat, text)
            if not where:
                return {"ok": False,
                        "error": "could not make " + text + " in " + seat}
            return _made(root, where, text)
        where = PS.create_named(root, text)
        if not where:
            return {"ok": False, "error": "give the project a name"}
        return _made(root, where, text)
    try:
        where = Path(text).expanduser()
    except (OSError, RuntimeError, ValueError):
        return {"ok": False, "error": "that is not a directory path"}
    if not where.is_dir():
        return {"ok": False, "error": "no such directory: " + str(where)}
    PS.touch(root, str(where))
    return _made(root, str(where.resolve()), where.name)


def project_setup(cwd, objective=None, description=None, root=None):
    """Answer the two questions a new project is asked: what it is for, and
    what is worth knowing before anything starts.

    Written against the project that was just made rather than the one this
    workspace serves -- the new one has no chat yet, so no workspace of its
    own to write them in, and asking now is the difference between a project
    that knows what it is for and one that waits for a chat to say.
    """
    where = str(cwd or "").strip()
    if not where:
        return {"ok": False, "error": "which project?"}
    if not PS.read_file(root, where).get("project"):
        return {"ok": False, "error": "that project has not been made yet"}
    record = PS.load_project(root, where)
    if objective is not None:
        if not isinstance(objective, str):
            return {"ok": False, "error": "objective must be text"}
        record["objective"] = objective.strip()[:PROJECT_OBJECTIVE_LIMIT]
    if description is not None:
        if not isinstance(description, str):
            return {"ok": False, "error": "description must be text"}
        record["description"] = description.strip()[
            :PS.PROJECT_DESCRIPTION_LIMIT]
    _save_project(root, where, record)
    saved = PS.load_project(root, where)
    return {"ok": True, "cwd": where,
            "objective": saved.get("objective", ""),
            "description": saved.get("description", "")}


def _chooser_command(here):
    """The folder question this machine already knows how to ask, or None.

    Every desktop ships a folder chooser; none of them is the same program.
    The one that belongs to the platform is asked for rather than a file
    browser drawn in the page, because the reader knows their own already --
    its sidebar, its recent places, its search.
    """
    import shutil
    import sys

    if sys.platform == "darwin":
        # AppleScript strings take backslash and quote the way C does.
        def quoted(text):
            body = str(text).replace("\\", "\\\\").replace('"', '\\"')
            return '"' + body + '"'

        ask = ["choose folder with prompt "
               + quoted("Choose a folder for this project")]
        if here:
            ask.append("default location POSIX file " + quoted(here))
        # Brought to the front first, or the dialog opens behind the browser:
        # this is asked for by a server nobody is looking at, not by an app
        # already in front. Wrapped, because a chooser that opens in the
        # background is a smaller problem than one that does not open.
        return ["osascript", "-e", "try\n\tactivate\nend try",
                "-e", "POSIX path of (" + " ".join(ask) + ")"]
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-STA", "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$d.Description = 'Choose a folder for this project'; "
                + (("$d.SelectedPath = '%s'; " % str(here).replace("'", "''"))
                   if here else "")
                + "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"]
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--directory",
                "--title=Choose a folder for this project"] + (
                    ["--filename=" + here.rstrip("/") + "/"] if here else [])
    if shutil.which("kdialog"):
        return ["kdialog", "--getexistingdirectory",
                here or os.path.expanduser("~")]
    return None


def _file_chooser_command(here, prompt):
    """The same question as _chooser_command, asked about files.

    Several of them, because a reader saving what they have been reading
    saves a stack of papers rather than one, and picking them one dialog at
    a time is the thing the picker was meant to end. Each platform reports
    the choice its own way; the caller splits on newlines.
    """
    import shutil
    import sys

    if sys.platform == "darwin":
        def quoted(text):
            body = str(text).replace("\\", "\\\\").replace('"', '\\"')
            return '"' + body + '"'

        ask = ["choose file with prompt " + quoted(prompt),
               "with multiple selections allowed"]
        if here:
            ask.insert(1, "default location POSIX file " + quoted(here))
        # One POSIX path per line. AppleScript's own list text would be
        # comma-joined, and a path may hold a comma.
        return ["osascript", "-e", "try\n\tactivate\nend try",
                "-e", "set picked to (" + " ".join(ask) + ")",
                "-e", "set out to \"\"",
                "-e", "repeat with one in picked\n"
                      "\tset out to out & POSIX path of one & linefeed\n"
                      "end repeat",
                "-e", "return out"]
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-STA", "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                "$d.Multiselect = $true; "
                "$d.Title = '" + str(prompt).replace("'", "''") + "'; "
                + (("$d.InitialDirectory = '%s'; "
                    % str(here).replace("'", "''")) if here else "")
                + "if ($d.ShowDialog() -eq 'OK') { $d.FileNames | Write-Output }"]
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--multiple",
                "--separator=\n", "--title=" + str(prompt)] + (
                    ["--filename=" + here.rstrip("/") + "/"] if here else [])
    if shutil.which("kdialog"):
        return ["kdialog", "--getopenfilename", "--multiple",
                "--separate-output", here or os.path.expanduser("~")]
    return None


def pick_files(start=None, prompt="Choose files to save to this project"):
    """Open this machine's file chooser and report what was picked.

    The folder chooser's twin, and for the same reason: a path is something
    you can point at long before it is something you can spell. Cancelling
    is not a failure -- it comes back cancelled and nothing is attached.
    """
    import subprocess

    here = ""
    if start:
        try:
            spot = Path(str(start)).expanduser()
            here = str(spot) if spot.is_dir() else ""
        except (OSError, RuntimeError, ValueError):
            here = ""
    command = _file_chooser_command(here, str(prompt or "Choose files"))
    if not command:
        return {"ok": False,
                "error": "no file chooser on this machine — type a path"}
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=15 * 60)
    except FileNotFoundError:
        return {"ok": False,
                "error": "no file chooser on this machine — type a path"}
    except subprocess.TimeoutExpired:
        return {"ok": True, "cancelled": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}
    lines = [line.strip() for line in str(done.stdout or "").splitlines()]
    picked = [line for line in lines if line]
    if not picked:
        trouble = str(done.stderr or "").strip()
        if done.returncode and trouble and "-128" not in trouble \
                and "cancel" not in trouble.lower():
            return {"ok": False, "error": trouble.splitlines()[-1][:200]}
        return {"ok": True, "cancelled": True}
    files = []
    for line in picked[:40]:
        where = Path(line).expanduser()
        if not where.is_file():
            continue
        files.append({"path": str(where.resolve()), "name": where.name})
    if not files:
        return {"ok": False, "error": "nothing there to read"}
    return {"ok": True, "files": files}


def pick_directory(start=None):
    """Open this machine's folder chooser and report what was picked.

    Typing a path works for a path you can spell; pointing at one is what
    everybody actually wants, so the platform's own dialog is opened and the
    chosen directory comes back as text for the box to hold. Closing the
    dialog is not a failure -- it comes back cancelled, and the form is left
    exactly as it was.
    """
    import subprocess

    here = ""
    if start:
        try:
            spot = Path(str(start)).expanduser()
            here = str(spot) if spot.is_dir() else ""
        except (OSError, RuntimeError, ValueError):
            here = ""
    command = _chooser_command(here)
    if not command:
        return {"ok": False,
                "error": "no folder chooser on this machine — type a path"}
    try:
        # No deadline worth enforcing is shorter than a person deciding, and
        # a stuck dialog should not outlive the workspace either.
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=15 * 60)
    except FileNotFoundError:
        return {"ok": False,
                "error": "no folder chooser on this machine — type a path"}
    except subprocess.TimeoutExpired:
        return {"ok": True, "cancelled": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:200]}
    picked = str(done.stdout or "").strip()
    if not picked:
        # Cancelling is how every one of these dialogs reports "never mind":
        # an empty answer, a non-zero exit, or both. A chooser that could not
        # open at all says something on the way out, and that is worth
        # repeating rather than passing off as a change of mind.
        trouble = str(done.stderr or "").strip()
        if done.returncode and trouble and "-128" not in trouble \
                and "cancel" not in trouble.lower():
            return {"ok": False, "error": trouble.splitlines()[-1][:200]}
        return {"ok": True, "cancelled": True}
    where = Path(picked).expanduser()
    if not where.is_dir():
        return {"ok": False, "error": "no such directory: " + str(where)}
    return {"ok": True, "cwd": str(where.resolve()), "name": where.name}


def _empty_shared(project_id):
    """What a shared workspace serves when the project will not load: the
    shape the page needs, with nothing in it."""
    from . import shared_state
    payload = shared_state.build({}, None, {})
    payload["shared"]["project_id"] = project_id
    return payload


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
        bound = False
        if chat_scoped:
            session_id, root = _chat_identity(trajdir)
            # Resolved with the same root the rest of this payload uses: a
            # server on a vault of its own must not answer from the default.
            bound = not _ask_which_project(trajdir, session_id, root, goals)
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
                   # Where this chat works. Recorded in the manifest already; the
                   # workspace could only name a project by guessing without it.
                   "project": _project_identity(trajdir, chat_scoped, session),
                   # False is what sends a chat through onboarding. Answered
                   # from the binding alone: every chat has a directory, so a
                   # directory could never tell a new chat from a bound one.
                   "project_bound": bound,
                   "provider": _configured_provider(trajdir),
                   "revision": _goal_revision(goals, important)}
    if identity is not None:
        payload["injection"] = _injection_state(*identity)
        try:
            from . import build as BUILD
            payload["build_session"] = BUILD.session_state(*identity)
            # What a build of one row costs here: the context every build
            # opens on, and what this chat's own finished runs have spent.
            # The rail prices its rows from these two numbers.
            payload["build_cost"] = BUILD.cost(*identity)
            # And what each build is doing right now: a line each, so a row
            # that says "building" can say what that means.
            payload["build_runs"] = BUILD.live(*identity)
        except Exception:  # noqa: BLE001 - the rail can do without it
            payload["build_session"] = None
            payload["build_cost"] = None
            payload["build_runs"] = {}
    return payload


def _handoff(trajdir=None, chat_scoped=None):
    """The hand-off document for this workspace, written and returned.

    The goal tree and prompts are read under the state lock; git runs
    outside it, since a slow remote or `gh` call must not hold up the
    pollers.
    """
    from . import handoff as HO
    chat_scoped = trajdir is not None if chat_scoped is None else chat_scoped
    trajdir = _scope(trajdir)
    session = None
    with _state_access(trajdir, chat_scoped):
        goals, _important = _load_goals(trajdir, chat_scoped)
        GM.sanitize(goals)
        prompts = _load_prompts(trajdir, chat_scoped)
        if chat_scoped:
            session, _root = _chat_identity(trajdir)
    try:
        return HO.build(trajdir, goals["goals"], prompts, chat_scoped,
                        session_id=session,
                        generated_at=goals.get("generated_at", ""))
    except Exception as exc:  # noqa: BLE001 - the button reports, never hangs
        return {"ok": False, "error": str(exc)[:200]}


# A write has landed on disk. Arming the send here rather than in each
# branch that saves means an operation added later is covered by naming it
# in WRITE_OPS, not by remembering a call at the end of its own code path.
def _arm_autosync(op, result, trajdir, chat_scoped):
    if str((op or {}).get("op") or "") not in AUTOSYNC.WRITE_OPS:
        return
    if not chat_scoped or not isinstance(result, dict) or not result.get("ok"):
        return
    try:
        session_id, root = _chat_identity(_scope(trajdir))
        who = _project_identity(_scope(trajdir), chat_scoped, session_id)
        AUTOSYNC.schedule(root, who.get("cwd"))
    except (OSError, ValueError, KeyError):
        # A send that cannot be armed is not a failed edit. The edit is
        # saved; the button in the panel still sends it by hand.
        pass


def _apply(op, trajdir=None, chat_scoped=None):
    # Armed here rather than inside, so the work that happens outside the
    # chat's lock -- building TODOs, cancelling a run -- is covered by the
    # same rule as the edits that happen inside it.
    result = _apply_dispatch(op, trajdir, chat_scoped)
    _arm_autosync(op, result, trajdir, chat_scoped)
    return result


def _apply_dispatch(op, trajdir=None, chat_scoped=None):
    result = _apply_locked(op, trajdir, chat_scoped)
    deferred = result.get("__deferred__") if isinstance(result, dict) else None
    if not deferred:
        return result
    from . import build as BUILD
    kind, session_id, root, goal_id, op = deferred
    if kind == "setup_say":
        return SETUP.ask(op.get("transcript"), root=root,
                         shown=op.get("shown") or [])
    if kind == "setup_from_chat":
        # The other cold start: a chat with plenty in it and no project. The
        # transcript is the description, so nothing is asked of the reader
        # -- three things worth focusing on are read out of what they have
        # already said, each with its tree, and choosing is their whole part.
        said = str(op.get("session") or "").strip()
        if not said:
            return {"ok": False, "error": "no chat to read"}
        try:
            events = CS.load_events(said, root)
        except (OSError, ValueError):
            events = []
        return SETUP.from_chat(events, root=root)
    if kind == "setup_commit":
        return SETUP.commit(root, op.get("name"), op.get("plan"),
                            op.get("goals"), op.get("chosen"),
                            op.get("todos"), op.get("subgoals") or [],
                            bind=op.get("bind") or "")
    if kind.startswith("dev_"):
        # The fourth slot carries the project directory rather than a goal id:
        # a dev server belongs to the project the work lives in, and two goals
        # in the same checkout share the one server rather than racing for its
        # port.
        from . import dev_server as DEV
        cwd = goal_id or ""
        if kind == "dev_start":
            return DEV.start(session_id, root, cwd, force=bool(op.get("force")))
        if kind == "dev_stop":
            return DEV.stop(session_id, root, cwd)
        if kind == "dev_log":
            return DEV.log(session_id, root, cwd)
        return DEV.status(session_id, root, cwd)
    if kind == "reopen_session":
        return BUILD.reopen(session_id, root)
    if kind == "build_log":
        # What the build has been doing, whole: the rail asks for this only
        # when the reader opens the log, since the state carries the last
        # line already.
        return {"ok": True, "goal_id": goal_id,
                "lines": BUILD.load_activity(session_id, root, goal_id),
                "run": BUILD.live(session_id, root).get(goal_id)}
    if kind == "watch_build":
        return BUILD.watch(session_id, root, goal_id)
    if kind == "build_todos":
        ids = op.get("ids")
        return BUILD.start(session_id, root, goal_id,
                           ids if isinstance(ids, list) else [])
    if kind == "cancel_todos":
        ids = op.get("ids")
        return BUILD.cancel(session_id, root, goal_id,
                            ids if isinstance(ids, list) else [])
    if kind == "reopen_todo":
        return BUILD.reopen(session_id, root, goal_id,
                            str(op.get("id") or ""), str(op.get("note") or ""))
    if kind == "note_todo":
        # A word for a row the build is on, from the pane Enter opens under
        # it: into the build's session, not onto the list.
        return BUILD.note(session_id, root, goal_id,
                          str(op.get("id") or ""), str(op.get("note") or ""))
    if kind == "set_build_settings":
        # The Builds tab: which model, at what effort -- for a build, and
        # for the restart check that follows one. Vault-wide.
        return BUILD.save_settings(
            session_id, root,
            {k: op.get(k) for k in ("model", "effort", "check",
                                    "check_model", "check_effort") if k in op})
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
        if kind == "bind_project":
            # What the onboarding's last step does, and the only thing that
            # ends it: this chat is for that project, and stays for it across
            # resumes until it is bound somewhere else.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            where = op.get("cwd")
            if not isinstance(where, str) or not where.strip():
                return {"ok": False, "error": "which project?"}
            try:
                session_id, root = _chat_identity(trajdir)
                home = CS.bind_project(session_id, where, root)
                # This window was stood up for a chat that had no project
                # yet, so it is registered under the chat. Now that the chat
                # has one, the registration moves to the project's store --
                # otherwise the next chat to join looks there, finds nothing,
                # and opens a second window onto the same tree.
                elsewhere = _adopt_server_for_project(
                    trajdir, session_id, root)
            except (OSError, ValueError, TypeError, TimeoutError) as exc:
                return {"ok": False, "error": str(exc)[:200]}
            out = {"ok": True, "cwd": home,
                   "project": _project_identity(trajdir, True, session_id)}
            # The project already had a window: the reader belongs in it, not
            # on this page, which was only ever somewhere to be asked.
            if elsewhere:
                out["open"] = elsewhere
            return out
        if kind == "forget_project":
            # Deletion, and named as one on the screen: the reader types the
            # project's name to get here. Everything the vault keeps for it
            # goes -- its records, its window, and the goals, TODO rows,
            # notes and prompts of every chat in it. The repository itself is
            # untouched.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            where = op.get("cwd")
            if not isinstance(where, str) or not where:
                return {"ok": False, "error": "which project?"}
            try:
                _, root = _chat_identity(trajdir)
            except Exception:                                # noqa: BLE001
                root = None
            # The chats first: a chat left naming a project that is gone
            # reads an empty tree and is never asked why. Cut loose, it goes
            # back through onboarding and the reader says where it belongs.
            # Done before the directories go, so a chat whose store survives
            # -- one bound from elsewhere -- is asked rather than stranded.
            members = CS.chats_in_project(where, root)
            freed = [sid for sid in members if CS.unbind_project(sid, root)]
            # Named to the deletion: an unbound chat no longer says which
            # project it was in, so the list has to travel with the call.
            gone = PS.delete_project(root, where, members)
            if not gone:
                return {"ok": False, "error": "no such project"}
            return {"ok": True, "cwd": gone["cwd"], "freed": len(freed),
                    "chats": gone["chats"], "goals": gone["goals"]}
        if kind == "open_project":
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            where = op.get("cwd")
            if not isinstance(where, str) or not where:
                return {"ok": False, "error": "which project?"}
            return open_project(where, trajdir)
        if kind == "new_project":
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            # Written where the switcher reads: the list is built with the
            # root this chat's session directory sits in, so a project
            # written under any other one would be invisible the moment it
            # was made.
            try:
                _, root = _chat_identity(trajdir)
            except Exception:                                # noqa: BLE001
                root = None
            return new_project(op.get("name"), op.get("cwd"), root,
                               op.get("repo"), op.get("parent"))
        if kind == "project_setup":
            # The two questions a project that has never been worked in is
            # asked, answered against that project rather than this one: it
            # has no chat yet, and so no workspace of its own to answer in.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            try:
                _, root = _chat_identity(trajdir)
            except Exception:                                # noqa: BLE001
                root = None
            return project_setup(op.get("cwd"), op.get("objective"),
                                 op.get("description"), root)
        if kind == "setup_open_terminal":
            # Offered on top of the copy rows, never instead of them: a
            # machine with no terminal this can drive still has to be told
            # what to type. Answered here rather than deferred -- it opens a
            # window and returns, and spawns nothing that outlives it.
            return SETUP.open_terminal(op.get("command"), op.get("cwd"))
        if kind in ("setup_say", "setup_commit", "setup_from_chat"):
            # The cold-start conversation. Its whole transcript comes from
            # the browser and goes back to it: setup is not a chat of the
            # vault's, it is a page, and nothing is written down until the
            # reader approves what it produced.
            #
            # Handed back to _apply to run OUTSIDE this lock, for the reason
            # the build ops are: `say` spawns a claude subprocess and waits
            # on it, and a request holding the state lock for three minutes
            # is a workspace nobody else can save into.
            if kind == "setup_say" and not isinstance(op.get("transcript"),
                                                      list):
                return {"ok": False, "error": "transcript must be a list"}
            try:
                _sid, root = _chat_identity(trajdir)
            except Exception:                                # noqa: BLE001
                root = None
            return {"__deferred__": (kind, "", root, None, op)}
        if kind == "set_project_objective":
            # What the project is for, in the reader's words: kept once per
            # project directory, so every chat in it reads the same line.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            session_id, root = _chat_identity(trajdir)
            who = _project_identity(trajdir, chat_scoped, session_id)
            if not who["cwd"]:
                return {"ok": False, "error": "this chat has no project directory"}
            text = op.get("objective")
            if not isinstance(text, str):
                return {"ok": False, "error": "objective must be text"}
            record = _load_project(root, who["cwd"])
            record["objective"] = text.strip()[:PROJECT_OBJECTIVE_LIMIT]
            _save_project(root, who["cwd"], record)
            return {"ok": True, "objective": record["objective"]}
        if kind == "set_project_meta":
            # The rest of what belongs to the project rather than to any one
            # chat: a longer description, and the sources saved to it. Kept
            # beside the objective in the same per-directory file, so every
            # chat started there reads them.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            session_id, root = _chat_identity(trajdir)
            who = _project_identity(trajdir, chat_scoped, session_id)
            if not who["cwd"]:
                return {"ok": False, "error": "this chat has no project directory"}
            record = _load_project(root, who["cwd"])
            if "name" in op:
                # What the reader calls this project, which is not the same
                # question as which directory it sits in. Blanking it falls
                # back to the directory's own name rather than leaving a
                # project with no name at all.
                text = op.get("name")
                if not isinstance(text, str):
                    return {"ok": False, "error": "name must be text"}
                record["name"] = text.strip()[:PS.PROJECT_NAME_LIMIT]
            if "description" in op:
                text = op.get("description")
                if not isinstance(text, str):
                    return {"ok": False, "error": "description must be text"}
                record["description"] = text.strip()[
                    :PS.PROJECT_DESCRIPTION_LIMIT]
            if "sources" in op:
                if not isinstance(op.get("sources"), list):
                    return {"ok": False, "error": "sources must be a list"}
                record["sources"] = GM.normalize_sources(op["sources"])
            if "working_dir" in op:
                # Which checkout of the repository the builds run in. Only
                # a worktree of this same project survives the write (see
                # project_store.chosen_dir), so a path the reader cannot
                # have meant comes back as "the project's own home".
                text = op.get("working_dir")
                if not isinstance(text, str):
                    return {"ok": False, "error": "working_dir must be text"}
                record["working_dir"] = text.strip()
            if "saved" in op:
                # The Saved page's whole list, posted back the way the
                # sources are. Kept apart from them on purpose: nothing
                # that assembles context for a model reads this key.
                if not isinstance(op.get("saved"), list):
                    return {"ok": False, "error": "saved must be a list"}
                record["saved"] = PS.normalize_saved(op["saved"])
            _save_project(root, who["cwd"], record)
            saved = _load_project(root, who["cwd"])
            return {"ok": True, "description": saved.get("description", ""),
                    # The name as it now reads, which is the directory's own
                    # when nothing was written for it.
                    "name": saved.get("name") or Path(who["cwd"]).name,
                    "sources": saved.get("sources", []),
                    "saved": saved.get("saved", []),
                    # Read back rather than echoed: a checkout that is not
                    # this repository's was refused on the way in, and the
                    # reader is told what actually stands.
                    "working_dir": saved.get("working_dir", ""),
                    "branch": AE._git_branch(
                        saved.get("working_dir") or who["cwd"]) or ""}
        if kind in ("set_supabase_config", "supabase_login",
                    "supabase_logout"):
            # Connecting the workspace to the reader's own Supabase, from
            # the settings panel rather than a hand-edited file. The URL and
            # the anon key are kept; the PASSWORD IS NOT -- it is exchanged
            # once for tokens on its way through and never written down.
            from . import supabase_client as SB
            root = None
            if chat_scoped:
                _, root = _chat_identity(trajdir)
            try:
                if kind == "set_supabase_config":
                    SB.save_config(op.get("url"), op.get("anon_key"),
                                   op.get("email") or "", root)
                    _remember_name(SB, op.get("display_name"), root)
                elif kind == "supabase_logout":
                    SB.sign_out(root)
                else:
                    email = op.get("email")
                    password = op.get("password")
                    if not isinstance(email, str) or not isinstance(password, str):
                        return {"ok": False,
                                "error": "an email and a password are needed"}
                    if not email.strip() or not password:
                        return {"ok": False,
                                "error": "an email and a password are needed"}
                    SB.sign_in(email.strip(), password, root)
                    # Once signed in, the name can be written: it belongs
                    # to the account, and there was no account before now.
                    _remember_name(SB, op.get("display_name"), root)
            except SB.SupabaseError as exc:
                return {"ok": False, "error": str(exc)}
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)[:200]}
            # Always answered with the state, never with what was sent: the
            # panel redraws from this, and a reply that echoed a password
            # back would put it somewhere new.
            return _supabase_status(SB, root)
        if kind in ("redeem_invite", "open_shared", "shared_projects"):
            # Joining someone else's project, and opening it. The workspace
            # a code leads to is a second window on a second port -- never
            # this one, which is the reader's own.
            from . import supabase_client as SB
            root = None
            if chat_scoped:
                _, root = _chat_identity(trajdir)
            try:
                if kind == "shared_projects":
                    return SB.shared_projects(root)
                if kind == "redeem_invite":
                    joined = SB.redeem(str(op.get("code") or ""), root)
                    opened = open_shared(joined["project_id"], trajdir)
                    return dict(joined, **{"url": opened.get("url", ""),
                                           "opened": opened.get("ok", False)})
                pid = op.get("project_id")
                if not isinstance(pid, str) or not pid:
                    return {"ok": False, "error": "which project?"}
                return open_shared(pid, trajdir)
            except SB.SupabaseError as exc:
                return {"ok": False, "error": str(exc)}
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)[:200]}
        if kind in ("create_share", "list_shares", "revoke_share"):
            # Handing this project to someone with no account here. The
            # token comes back from Postgres once and is not stored on
            # either side -- only its hash is -- so the reply below is the
            # only time the workspace ever holds it.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            session_id, root = _chat_identity(trajdir)
            who = _project_identity(trajdir, chat_scoped, session_id)
            if not who["cwd"] and kind != "revoke_share":
                return {"ok": False, "error": "this chat has no project directory"}
            from . import supabase_client as SB
            try:
                if kind == "create_share":
                    days = op.get("expires_in_days")
                    if days is not None:
                        try:
                            days = max(1, min(365, int(days)))
                        except (TypeError, ValueError):
                            days = 30
                    wanted = op.get("role")
                    if wanted not in ("reader", "editor"):
                        wanted = "reader"
                    return SB.create_share(
                        root, who["cwd"], str(op.get("label") or "")[:120],
                        days, kind="invite", role=wanted)
                if kind == "list_shares":
                    return SB.list_shares(root, who["cwd"])
                share_id = op.get("id")
                if not isinstance(share_id, str) or not share_id:
                    return {"ok": False, "error": "which share?"}
                return SB.revoke_share(root, share_id)
            except SB.SupabaseError as exc:
                return {"ok": False, "error": str(exc)}
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)[:200]}
        if kind == "sync_supabase":
            # The project, up to the reader's own Supabase. Everything the
            # send needs is on disk already: the keys in the vault, the
            # rows built from the same stores the pane reads. Failures come
            # back as a sentence rather than a traceback -- the button is
            # where they will be read.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            session_id, root = _chat_identity(trajdir)
            who = _project_identity(trajdir, chat_scoped, session_id)
            if not who["cwd"]:
                return {"ok": False, "error": "this chat has no project directory"}
            from . import supabase_client as SB
            try:
                # Holds the project for the length of the send: a timer that
                # fires mid-flight would prune rows this one is still writing.
                with AUTOSYNC.hold(root, who["cwd"]):
                    return SB.sync_project(root, who["cwd"])
            except SB.SupabaseError as exc:
                return {"ok": False, "error": str(exc)}
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"could not send: {exc}"}
        if kind in ("link_chat", "unlink_chat"):
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            session_id, root = _chat_identity(trajdir)
            sid = str(op.get("session_id") or "")
            try:
                CS.paths(sid, root)
            except ValueError:
                return {"ok": False, "error": "invalid session id"}
            if sid == session_id:
                return {"ok": False, "error": "this workspace already follows "
                                              "its own chat"}
            # The scope: a goal id for a link made from that goal's pane,
            # nothing for one made from the header. A goal-scoped link
            # must name a goal this tree has, or it would be offered to
            # no one and unlinkable from nowhere.
            goal_id = str(op.get("goal_id") or "") or None
            if goal_id and not any(g.get("id") == goal_id
                                   for g in goals["goals"]):
                return {"ok": False, "error": "no such goal"}
            chats = _load_linked(session_id, root)

            def same(c):
                return c["session_id"] == sid and c.get("goal_id") == goal_id
            if kind == "link_chat":
                if not any(same(c) for c in chats):
                    label = (" ".join(str(op.get("label") or "").split())[:60]
                             or next((c["label"] for c in chats
                                      if c["session_id"] == sid), sid[:8]))
                    # The linked chat's own store, so ingestion has somewhere
                    # to keep its cursor and prompts.
                    CS.paths(sid, root).session_dir.mkdir(parents=True, exist_ok=True)
                    entry = {"session_id": sid, "label": label}
                    if goal_id:
                        entry["goal_id"] = goal_id
                    chats.append(entry)
            else:
                chats = [c for c in chats if not same(c)]
            _save_linked(session_id, root, chats)
            return {"ok": True, "linked": [c["session_id"] for c in chats]}
        if kind in ("dev_status", "dev_start", "dev_stop", "dev_log"):
            # Chat scope only, as the build ops are, and handed back to
            # _apply for the same reason: starting a dev server spawns a
            # process and asking whether a port answers blocks on a socket,
            # and neither belongs inside the chat's lock. What this side does
            # resolve is where -- which needs the goal tree, and so has to
            # happen here.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            session_id, root = _chat_identity(trajdir)
            from . import build as BUILD
            cwd = BUILD._cwd_for(session_id, root, goals, g["id"] if g else "")
            return {"__deferred__": (kind, session_id, root, cwd, op)}
        if kind in ("build_todos", "answer_todo", "cancel_todos",
                    "reopen_todo", "note_todo", "generate_prompt",
                    "prompt_preview", "reopen_session", "build_log",
                    "watch_build", "set_build_settings"):
            # The rail's build and generate: chat scope only, since both run
            # against the chat's own project and goal tree. The build ops are
            # handed back to _apply to run OUTSIDE this lock -- build.py takes
            # the same lock for its own writes, and a child process must
            # never be spawned while a request still holds it.
            if not chat_scoped:
                return {"ok": False, "error": "chat scope only"}
            session_id, root = _chat_identity(trajdir)
            if kind in ("reopen_session", "set_build_settings"):
                return {"__deferred__": (kind, session_id, root, None, op)}
            if not g:
                return {"ok": False, "error": "goal not found in this chat"}
            if kind == "prompt_preview":
                # Read-only, and answered here rather than deferred: it
                # composes a string from the goals already loaded under this
                # lock and spawns nothing. The rail prints it above the
                # reader's own words, so the context a build opens on is
                # visible before the build rather than only inside it.
                from . import build as BUILD
                ids = op.get("ids")
                return {"ok": True,
                        "prompt": BUILD.preview(
                            session_id, root, goals, important, g,
                            ids if isinstance(ids, list) else []),
                        # The same string with no rows in it. The rail prices
                        # each TODO against this, so the number in a row's
                        # corner and the count above the field are one
                        # measurement rather than two.
                        "context_tokens": BUILD.preview_context_tokens(
                            session_id, root, goals, important, g)}
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
        elif kind == "set_understanding" and g:
            # The scenario this goal's work is for and the questions asked
            # about it, as the Understanding tab holds them. Written whole:
            # the tab owns both halves and posts both together, so a save is
            # never half a scenario.
            g["understanding"] = GM.normalize_understanding(op)
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
        elif kind == "set_relevance" and g:
            # Promoting a goal out of the fold. The verdict is a model's
            # judgement; the reader's overrules it, and is stamped against
            # the objective standing now so it is not later called stale.
            wanted = op.get("relevance")
            if wanted not in ("core", "supporting", "unrelated"):
                return {"ok": False, "error": "unknown relevance"}
            g["relevance"] = wanted
            g["relevance_why"] = "set by hand"
            try:
                from . import chat_synth as CSY
                session_id, root = _chat_identity(trajdir)
                cwd = CS.load_manifest(session_id, root).get("cwd")
                g["relevance_for"] = CSY.project_objective(cwd, root)
            except Exception:  # noqa: BLE001 - the verdict still stands
                pass
            g["updated_at"] = GM._now()
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
                # A prompt of theirs tied to the goal is work begun on it.
                if g.get("status") == "active":
                    g["status"] = "in_progress"
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
            # Which project this goal's work belongs to. A subgoal takes it
            # from the goal above it; a root goal takes what the page said,
            # and says nothing when the project is the one this chat was
            # started in -- the ordinary case, and the one that must stay
            # empty so a moved chat still builds where it now lives.
            where = ""
            if parent:
                where = str(GM.by_id(goals, parent).get("project_cwd") or "")
            else:
                asked = str(op.get("project_cwd") or "").strip()
                # Asked for by name here rather than taken from a variable
                # this function never had: written that way, every root goal
                # raised before it could be appended.
                if asked:
                    mine = ""
                    if chat_scoped:
                        try:
                            said, _root = _chat_identity(trajdir)
                            mine = str(_project_identity(
                                trajdir, True, said).get("cwd") or "")
                        except (OSError, ValueError, TypeError):
                            mine = ""
                    if asked != mine:
                        where = asked
            goals["goals"].append(GM.new_goal(
                gid, (op.get("title") or "Untitled").strip()[:120], parent,
                origin="user", project_cwd=where))
        else:
            # Two different failures used to wear one message. "Unknown" is
            # an operation this build has never had -- most often a page
            # newer than the process answering it, since the browser's half
            # is re-read from disk on every load and this half is not. A
            # goal-scoped operation whose goal is gone is a different thing,
            # and neither is helped by being told the other's story.
            if kind in GOAL_OPS:
                return {"ok": False,
                        "error": ("goal not found in this workspace" if not g
                                  else "nothing to apply to that goal")}
            return {"ok": False,
                    "error": "unknown operation: " + str(kind or "")[:60]}
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

    Every /bart opens a fresh port, so a chat workspace is always a new
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
        # Nothing this server sends may be held. It carries no validators --
        # no ETag, no Last-Modified -- so a browser is free to guess a
        # freshness lifetime for the page and the bundle, and a guess of a
        # few minutes serves yesterday's workspace out of the cache after
        # the server has been restarted with today's. The JSON routes ask
        # for no-store on their own; the page and the script could not.
        self.send_header("Cache-Control", "no-store, must-revalidate")
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
            elif self.path.split("?", 1)[0] == "/api/tree":
                # The project's files, for the overview's file pane. Where
                # the project is comes from the chat's manifest; a workspace
                # that does not know answers with an empty tree, not a guess.
                trajdir = self.server.trajdir
                session = (_chat_identity(trajdir)[0]
                           if self.server.chat_scoped else None)
                who = _project_identity(trajdir, self.server.chat_scoped, session)
                self._send(200, {"ok": True, "root": who["cwd"],
                                 "tree": (project_tree(who["cwd"])
                                          if who["cwd"] else [])})
            elif self.path.split("?", 1)[0] == "/api/source":
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(self.path).query)
                want = (query.get("id") or [""])[0]
                trajdir = self.server.trajdir
                session = (_chat_identity(trajdir)[0]
                           if self.server.chat_scoped else None)
                root = (_chat_identity(trajdir)[1]
                        if self.server.chat_scoped else None)
                who = _project_identity(trajdir, self.server.chat_scoped, session)
                found = None
                for row in who.get("sources") or []:
                    if str(row.get("id")) == want:
                        found = row
                        break
                if found is None:
                    self._send(200, {"ok": False, "error": "no such source"})
                else:
                    self._send(200, source_body(root, who["cwd"], found))
            elif self.path.split("?", 1)[0] == "/api/file":
                # One text file of the project, named relative to its root.
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(self.path).query)
                relpath = (query.get("path") or [""])[0]
                trajdir = self.server.trajdir
                session = (_chat_identity(trajdir)[0]
                           if self.server.chat_scoped else None)
                who = _project_identity(trajdir, self.server.chat_scoped, session)
                if not who["cwd"] or not relpath:
                    self._send(200, {"ok": False, "error": "no project"})
                else:
                    self._send(200, project_file(who["cwd"], relpath))
            elif self.path == "/api/readme":
                # The project's front page, for the overview's repository
                # pane. One route rather than the browser guessing at four
                # spellings of README, one round trip at a time.
                trajdir = self.server.trajdir
                session = (_chat_identity(trajdir)[0]
                           if self.server.chat_scoped else None)
                who = _project_identity(trajdir, self.server.chat_scoped, session)
                self._send(200, project_readme(who["cwd"]))
            elif self.path == "/api/supabase":
                from . import supabase_client as SB
                root = None
                cwd = None
                if self.server.chat_scoped:
                    session_id, root = _chat_identity(self.server.trajdir)
                    cwd = _project_identity(
                        self.server.trajdir, True, session_id).get("cwd")
                self._send(200, _supabase_status(SB, root, cwd))
            elif self.path == "/api/models":
                # What the Builds tab offers: the models the installed CLI
                # names, the efforts, and what is chosen. Chat scope, since
                # builds are.
                if not self.server.chat_scoped:
                    self._send(200, {"ok": False, "error": "chat scope only"})
                else:
                    from . import build as BUILD
                    self._send(200, BUILD.models(
                        *_chat_identity(self.server.trajdir)))
            elif self.path.split("?", 1)[0] == "/api/project.json":
                # The project's own record: one file per directory, holding
                # every goal of every chat started there. Read from the vault
                # base, not from the project directory -- it is what the
                # workspace knows about the project, not a file of it.
                # ?full=1 is the copy button asking for the whole file.
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(self.path).query)
                full = (query.get("full") or [""])[0] in ("1", "true")
                trajdir = self.server.trajdir
                session, root = ((_chat_identity(trajdir))
                                 if self.server.chat_scoped else (None, None))
                who = _project_identity(trajdir, self.server.chat_scoped, session)
                self._send(200, project_json(root, who["cwd"], full=full))
            elif self.path.split("?")[0] in ("/setup", "/setup/"):
                # What opens after `npx engelbart-cli`, before there is a
                # chat or a project to open anything else on. Served from
                # this process because it is the one that answers the ops
                # the page posts; it needs nothing else of the workspace.
                page = resources.files("human_compact.trajectory").joinpath(
                    "web/setup.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path.split("?")[0] == "/setup.who":
                # Which chat opened this page, and whether it has anything in
                # it. The page cannot know either: it is served by whichever
                # workspace happened to answer, and a chat with a transcript
                # behind it gets a different first screen from a cold one.
                session, root = ((_chat_identity(self.server.trajdir))
                                 if self.server.chat_scoped else ("", None))
                events = 0
                bound = True
                if session:
                    try:
                        events = len(CS.load_events(session, root))
                        bound = CS.project_bound(session, root)
                    except (OSError, ValueError):
                        pass
                self._send(200, json.dumps({
                    "session": session, "events": events,
                    "bound": bool(bound)}).encode("utf-8"),
                    "application/json")
            elif self.path.split("?")[0] == "/setup.js":
                # The query is ignored rather than matched: a cache-buster
                # on this URL used to fall through to the 404 body, which
                # reads as a page that loaded and did nothing.
                js = resources.files("human_compact.trajectory").joinpath(
                    "web/setup.js").read_bytes()
                self._send(200, js, "application/javascript")
            elif self.path == "/bridge.js":
                js = resources.files("human_compact.trajectory").joinpath(
                    "web/bridge.js").read_bytes()
                # Written by the process that will have to answer this page's
                # operations, before the page's own script runs: whether the
                # two halves of the workspace came from the same edit is
                # something only this side knows.
                stamp = ("window.__hcServerStale = %s;\n"
                         % ("true" if _server_is_stale() else "false"))
                self._send(200, stamp.encode("utf-8") + js,
                           "application/javascript")
            elif self.path == "/api/projects":
                if not self.server.chat_scoped:
                    self._send(200, {"ok": False, "error": "chat scope only"})
                else:
                    session_id, root = _chat_identity(self.server.trajdir)
                    who = _project_identity(
                        self.server.trajdir, True, session_id)
                    self._send(200, {"ok": True,
                                     "active": who["cwd"],
                                     "projects": _all_projects(
                                         root, who["cwd"])})
            elif self.path == "/api/chats":
                if not self.server.chat_scoped:
                    self._send(200, {"ok": False, "error": "chat scope only"})
                else:
                    session_id, root = _chat_identity(self.server.trajdir)
                    linked = _load_linked(session_id, root)
                    self._send(200, {
                        "ok": True,
                        "linked": linked,
                        "available": _discover_chats(session_id, root),
                    })
            elif self.path.split("?", 1)[0] == "/api/state":
                shared = getattr(self.server, "shared_project", None)
                if shared:
                    # A shared workspace stands on no vault: its state is
                    # the rows Postgres has, in the shape the tree reads.
                    from . import supabase_client as SB
                    try:
                        self._send(200, SB.shared_payload(
                            shared,
                            force="fresh=1" in self.path))
                    except SB.SupabaseError as exc:
                        self._send(200, dict(_empty_shared(shared),
                                             shared_error=str(exc)))
                else:
                    self._send(200, _payload(
                        self.server.trajdir, self.server.chat_scoped))
            elif self.path == "/api/handoff":
                # The workspace as one markdown file for a teammate's agent:
                # every goal's notes, prompt and TODO rows with their build
                # states, plus the repository's git and GitHub metadata,
                # under a prompt that has the agent render and open it.
                self._send(200, _handoff(
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

    def _take_attachment(self):
        """A screenshot pasted into a TODO row: the image bytes, as sent.

        Raw bytes rather than the JSON op channel, because a retina capture
        is bigger than an op is allowed to be and base64 would only make it
        bigger. Only images, only up to MAX_ATTACHMENT_BYTES; written under
        the workspace's own directory, and the absolute path handed back for
        the row to remember.
        """
        content_types = self.headers.get_all("Content-Type", [])
        ctype = (content_types[0].split(";", 1)[0].strip().lower()
                 if len(content_types) == 1 else "")
        ext = ATTACHMENT_TYPES.get(ctype)
        if not ext:
            self._send(415, {"ok": False, "error": "an image is required"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            n = int(lengths[0]) if len(lengths) == 1 else -1
        except (ValueError, TypeError):
            n = -1
        if n <= 0:
            self._send(400, {"ok": False, "error": "invalid content length"})
            return
        if n > MAX_ATTACHMENT_BYTES:
            self._send(413, {"ok": False, "error": "image too large"})
            return
        data = self.rfile.read(n)
        name = " ".join(str(self.headers.get("X-HC-Name") or "").split())[:200]
        try:
            path = _store_attachment(self.server.trajdir, data, ext)
        except OSError as exc:
            self._send(500, {"ok": False, "error": str(exc)[:200]})
            return
        self._send(200, {"ok": True, "path": str(path),
                         "name": name or path.name})

    def do_POST(self):
        if not self._begin_request():
            return
        try:
            if self.path == "/api/attachment":
                self._take_attachment()
                return
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
                if getattr(self.server, "shared_project", None):
                    # A shared workspace writes through one door. Everything
                    # else is still refused here rather than left for each
                    # operation to discover on its own.
                    if body.get("op") == "shared_edit_goal":
                        from . import shared_edit as SE
                        from . import supabase_client as SB
                        try:
                            self._send(200, SE.update_goal(
                                str(body.get("id") or ""),
                                body.get("expect"),
                                body.get("fields") or {},
                                project_id=self.server.shared_project))
                        except SB.SupabaseError as exc:
                            self._send(200, {"ok": False, "error": str(exc)})
                        except (OSError, ValueError) as exc:
                            self._send(200, {"ok": False,
                                             "error": str(exc)[:200]})
                        return
                    self._send(200, {"ok": False, "error":
                                     "this is a shared workspace: only your "
                                     "own goals can be edited here"})
                    return
                if body.get("op") == "pick_directory":
                    # Outside the state lock on purpose: the chooser waits on
                    # a person browsing their own disk, and nothing else in
                    # the workspace should stop while they look. It reads no
                    # goals and writes none, so there is nothing to hold.
                    if not self.server.chat_scoped:
                        self._send(200, {"ok": False,
                                         "error": "chat scope only"})
                        return
                    self._send(200, pick_directory(body.get("start")))
                    return
                if body.get("op") == "pick_files":
                    # Same reasoning as the folder chooser above: it waits on
                    # a person, reads no goals, and writes none.
                    if not self.server.chat_scoped:
                        self._send(200, {"ok": False,
                                         "error": "chat scope only"})
                        return
                    self._send(200, pick_files(
                        body.get("start"),
                        body.get("prompt")
                        or "Choose files to save to this project"))
                    return
                with self.server.state_lock:
                    result = _apply(
                        body, self.server.trajdir, self.server.chat_scoped)
                self._send(200, result)
            elif self.path == "/api/ask":
                if not isinstance(body, dict):
                    self._send(400, {"ok": False, "error": "expected a question"})
                    return
                if getattr(self.server, "shared_project", None):
                    self._send(200, {"ok": False, "error":
                                     "a shared workspace has no files here to "
                                     "read"})
                    return
                # Outside the state lock on purpose: this waits on a model
                # for as long as the provider's own deadline allows, and a
                # workspace whose every other request stops for three
                # minutes is a workspace that looks broken.
                trajdir = self.server.trajdir
                session, root = ((_chat_identity(trajdir))
                                 if self.server.chat_scoped else (None, None))
                who = _project_identity(trajdir, self.server.chat_scoped, session)
                want = str(body.get("id") or "")
                found = None
                if want:
                    for row in who.get("sources") or []:
                        if str(row.get("id")) == want:
                            found = row
                            break
                    if found is None:
                        self._send(200, {"ok": False, "error": "no such source"})
                        return
                self._send(200, ask_source(root, who["cwd"], found,
                                           body.get("question")))
            elif self.path == "/api/ask_selection":
                # A question about a passage highlighted in the tree, the
                # rail or the notes. Reads goals, not files, so a shared
                # workspace can ask it too -- and, like /api/ask, waits on a
                # model outside the state lock so nothing else stops for it.
                if not isinstance(body, dict):
                    self._send(400, {"ok": False, "error": "expected a question"})
                    return
                shared = getattr(self.server, "shared_project", None)
                if shared:
                    from . import supabase_client as SB
                    try:
                        state = SB.shared_payload(shared)
                    except SB.SupabaseError as exc:
                        self._send(200, {"ok": False, "error": str(exc)[:200]})
                        return
                else:
                    state = _payload(self.server.trajdir,
                                     self.server.chat_scoped)
                self._send(200, ask_selection(
                    state.get("goals") or [], body.get("goal"),
                    body.get("text"), body.get("question"),
                    objective=str((state.get("project") or {}).get(
                        "objective") or ""),
                    turns=body.get("turns")))
            elif self.path == "/api/ask_scenario":
                # One question from the Understanding tab, answered in prose.
                # Reads goals like /api/ask_selection does,
                # and waits on a model outside the state lock for the same
                # reason -- the answer is written back by the tab afterwards,
                # through the ordinary op, not from in here.
                if not isinstance(body, dict):
                    self._send(400, {"ok": False, "error": "expected a question"})
                    return
                shared = getattr(self.server, "shared_project", None)
                if shared:
                    from . import supabase_client as SB
                    try:
                        state = SB.shared_payload(shared)
                    except SB.SupabaseError as exc:
                        self._send(200, {"ok": False, "error": str(exc)[:200]})
                        return
                else:
                    state = _payload(self.server.trajdir,
                                     self.server.chat_scoped)
                # Most of what the tab is asked is a question about what this
                # project already does, so the answer is looked for in it.
                # A shared workspace's directory is on somebody else's disk:
                # whatever sits at that path here is not the project, so that
                # question is answered from its own words instead.
                self._send(200, ask_scenario(
                    state.get("goals") or [], body.get("goal"),
                    body.get("scenario"), body.get("question"),
                    objective=str((state.get("project") or {}).get(
                        "objective") or ""),
                    turns=body.get("turns"),
                    cwd="" if shared else str((state.get("project") or {}).get(
                        "cwd") or "")))
            elif self.path == "/api/draft_scenario":
                # A scenario written from screenshots, rough words, or both.
                # The images live on this machine, so a workspace that is
                # somebody else's has none of them to open.
                if not isinstance(body, dict):
                    self._send(400, {"ok": False, "error": "expected a draft"})
                    return
                if getattr(self.server, "shared_project", None):
                    self._send(200, {"ok": False, "error":
                                     "a shared workspace has no screenshots "
                                     "here to read"})
                    return
                state = _payload(self.server.trajdir, self.server.chat_scoped)
                self._send(200, draft_scenario(
                    self.server.trajdir, body.get("text"), body.get("shots"),
                    objective=str((state.get("project") or {}).get(
                        "objective") or "")))
            elif self.path == "/api/import":
                if not isinstance(body, dict):
                    self._send(400, {
                        "ok": False,
                        "error": "revisioned import payload required",
                    })
                    return
                nested = body.get("goals")
                shared = getattr(self.server, "shared_project", None)
                if shared:
                    from . import shared_edit as SE
                    from . import supabase_client as SB
                    try:
                        out = SE.apply_tree(shared, nested)
                    except SB.SupabaseError as exc:
                        out = {"ok": False, "error": str(exc)}
                    except (OSError, ValueError) as exc:
                        out = {"ok": False, "error": str(exc)[:200]}
                    self._send(409 if out.get("conflict") else 200, out)
                    return
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


def _configure_server(server, trajdir, chat_scoped, follow=True,
                      shared_project=None):
    server.trajdir = trajdir
    server.chat_scoped = chat_scoped
    # A shared workspace has no vault behind it. trajdir still points
    # somewhere real -- the static files and the lock live there -- but the
    # state comes from Postgres and nothing here writes.
    server.shared_project = shared_project
    server.state_lock = threading.RLock()
    server.expected_host = f"127.0.0.1:{server.server_address[1]}"
    server.activity_lock = threading.Lock()
    server.last_activity = time.monotonic()
    server.active_requests = 0
    server.idle_expired = False
    server.follow_stop = threading.Event()
    server.follow_thread = None
    if chat_scoped and follow:
        # The prompts this workspace offers are the chat's own turns. They
        # used to arrive only through the hooks -- which go quiet the moment
        # a plugin path moves or a session is resumed under a stale hook
        # config, and the list then froze at wherever the last hook left it.
        # The server can read the transcript itself: follow it.
        server.follow_thread = threading.Thread(
            target=_follow_transcript, args=(server, server.follow_stop),
            daemon=True, name="hc-follow-transcript")
        server.follow_thread.start()


def _find_transcript(session_id, manifest):
    """Where the chat's transcript is now.

    The manifest remembers the path the hooks last reported; a session that
    was closed and reopened keeps its id and its file, but if that file is
    gone (moved project, cleared history), look it up by id under Claude's
    own projects directory and take the newest.
    """
    recorded = str((manifest or {}).get("transcript_path") or "")
    if recorded and Path(recorded).is_file():
        return Path(recorded)
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    candidates = sorted(
        (home / "projects").glob(f"*/{session_id}.jsonl"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True)
    return candidates[0] if candidates else None


def _follow_transcript(server, stop, interval=None):
    """Ingest the chat's transcript as it grows, without waiting for a hook.

    Cheap when nothing changed: one stat per tick. Ingestion is the same
    incremental, cursor-and-fingerprint read the hooks use, so a rewritten or
    truncated file is replayed from the top, never misread.
    """
    interval = float(interval if interval is not None else
                     os.environ.get("HC_CHAT_FOLLOW_SECONDS", "2"))
    seen = {}
    first = True
    while not stop.wait(0.2 if first else interval):
        first = False
        try:
            session_id, root = _chat_identity(server.trajdir)
            # Each linked session once, however many scopes link it: its
            # transcript has one cursor.
            targets = [session_id] + [sid for sid, _label, _goals
                                      in _linked_sessions(_load_linked(session_id, root))]
        except (OSError, ValueError):
            continue
        for target in targets:
            try:
                manifest = CS.load_manifest(target, root)
                transcript = _find_transcript(target, manifest)
                if transcript is None:
                    seen[target] = ("", 0, 0)
                    continue
                stat = transcript.stat()
                mark = (str(transcript), stat.st_size, stat.st_mtime_ns)
                if mark == seen.get(target):
                    continue
                source = manifest.get("source") or {}
                if (str(source.get("path") or "") == str(transcript)
                        and int(source.get("cursor") or 0) == stat.st_size
                        and int(source.get("mtime_ns") or 0) == stat.st_mtime_ns):
                    seen[target] = mark   # a hook already took this much
                    continue
                CS.ingest_transcript(target, transcript, root=root)
                seen[target] = mark
                # Having noticed the work, ask for it to be done. Ingestion
                # marks the analyzer "pending" -- a state that means "there
                # is work and nobody is doing it", and so should never be
                # somewhere the system rests. Until now only a hook started
                # a worker, so a chat whose hooks were quiet ingested every
                # turn faithfully and analysed none of them.
                #
                # spawn_refresh coalesces: with a worker already running this
                # only records the request, so a busy chat does not pile up
                # processes.
                _request_analysis(target, root)
            except (OSError, ValueError, TypeError, TimeoutError):
                # The lock was busy, or the file moved between stat and
                # read: the next tick tries again from the manifest's cursor.
                continue
            except Exception:  # noqa: BLE001 - never take the server down
                continue


def _request_analysis(session_id, root):
    """Start a worker for freshly ingested turns, if one is not already up.

    Best effort by design: inference is worth having and never worth taking
    the workspace down for, so every failure here is swallowed and the next
    tick tries again from the same pending state.
    """
    try:
        from . import chat_synth
    except Exception:      # noqa: BLE001
        return
    try:
        chat_synth.spawn_refresh(session_id, root)
    except Exception:      # noqa: BLE001 - a stalled analyzer, not a dead server
        pass


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


def _builds_running(server):
    """Whether any build of this workspace still has a process out.

    Both halves matter. ``build._RUNS`` holds the runs this process started;
    the records on disk hold the ones a previous process did, which outlive
    it. Restarting on top of either is how a build loses its reader and
    leaves its rows saying "building" for ever.
    """
    try:
        from . import build as BUILD
    except Exception:                                    # noqa: BLE001
        return False
    try:
        with BUILD._RUNS_GUARD:
            if any(run.alive() for run in BUILD._RUNS.values()):
                return True
    except Exception:                                    # noqa: BLE001
        return True          # unreadable is not the same as finished
    if not getattr(server, "chat_scoped", False):
        return False
    try:
        session_id, root = _chat_identity(server.trajdir)
        folder = BUILD._builds_dir(session_id, root)
        for record in folder.glob("*.json"):
            if record.name in ("later.json", "usage.json"):
                continue
            try:
                held = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(held, dict) and held.get("status") == "running"
                    and _pid_alive(held.get("pid"))):
                return True
    except (OSError, ValueError):
        return False
    return False


def _watch_code(server, stop, interval=5.0):
    """Re-exec this server once its own code has moved on without it.

    The workspace serves the plugin, and the plugin is what a build of this
    project edits -- so the process answering the page is routinely older
    than the code the page was drawn from. Every control added by that edit
    fails against it, which the page says out loud; this makes the saying
    unnecessary most of the time.

    Three things hold it back, and each of them has cost somebody a
    workspace before: a request in flight, a build with a process out, and
    a run of edits that has not settled. The last is why the stamp must be
    unchanged for two ticks -- an editor part-way through writing a package
    is not a version to restart onto.
    """
    seen = None
    settled = 0
    while not stop.wait(interval):
        if not _server_is_stale():
            seen, settled = None, 0
            continue
        stamp = _code_stamp()
        if stamp != seen:
            seen, settled = stamp, 1
            continue
        settled += 1
        if settled < 2:
            continue
        with server.activity_lock:
            busy = bool(server.active_requests)
        if busy or _builds_running(server):
            continue
        print("\n  code changed on disk · restarting", flush=True)
        try:
            server.follow_stop.set()
        except Exception:                                # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:                                # noqa: BLE001
            pass
        # Same argv, same port, same pid: the browser's next poll lands on
        # the replacement. execv drops this image, so nothing below runs.
        try:
            os.execv(sys.executable, [sys.executable, "-m", "human_compact.cli"]
                     + sys.argv[1:])
        except OSError:
            # Could not become the new server; the old one is already
            # unbound, so end rather than serve from a closed socket.
            server.shutdown()
            return


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


def run_shared(project_id, port=8850, open_browser=True, trajdir=None,
               ready_callback=None, label="Shared goals"):
    """One shared project, on its own local port.

    Deliberately a separate server rather than a mode of the personal one:
    two workspaces, two windows, two addresses. Which tree you are looking
    at should never be a thing you have to work out.
    """
    trajdir = _scope(trajdir)
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), H)
            _configure_server(server, trajdir, False, follow=False,
                              shared_project=str(project_id))
            break
        except OSError:
            continue
    else:
        print("  no free port found")
        return None
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"\n  {label} · {url}", flush=True)
    print("  read-only · Ctrl-C to stop\n", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="hc-shared")
    thread.start()
    if ready_callback:
        ready_callback(url, server)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:      # noqa: BLE001 - a printed url still works
            pass
    return {"url": url, "server": server, "thread": thread}


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
        # A workspace that serves the plugin outlives its own code the
        # moment a build edits it. Watch for that and become the new
        # version, rather than asking the reader to.
        if str(os.environ.get("HC_AUTO_RELOAD", "")).strip() not in ("0", "off",
                                                                     "no"):
            threading.Thread(target=_watch_code, args=(srv, idle_stop),
                             daemon=True).start()
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        idle_stop.set()
        srv.follow_stop.set()
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


# The states in which a row is with the builder and its text is the build's:
# "failed" is not among them -- a row that came back needing another go is
# the reader's again, to reword or to clear.
OUT_WITH_BUILDER = ("queued", "building", "asking")


def _merge_todo_items(posted, previous):
    """The browser's rows with the server's build state laid back over them.

    A row is matched by id. Text, depth and order are whatever the browser
    sent (that is the edit); status, question, what the build spent and the
    row's run history are
    whatever the server had for that id (that is the run). A row the browser
    no longer sends is gone; a row it sends that the server never saw starts
    blank. A browser that posted no list at all (an older cached page) keeps
    the server's rows.

    One exception, and it is about the row rather than the edit: while a row
    is out with the builder its text is what was SENT, and a blank posted
    over it is a page that had not yet learned what the row says -- an import
    composed before the reader finished typing it, arriving after the build
    that carried the finished text. Taking that blank leaves a row that says
    "building" with nothing written on it, and the browser then lays that
    blank back over the reader's own copy. Take the edit here only when there
    is one to take.
    """
    if not isinstance(posted, list):
        return GM.normalize_todo_items(previous)
    held = {row.get("id"): row for row in GM.normalize_todo_items(previous)}
    out = []
    for row in GM.normalize_todo_items(posted):
        was = held.get(row["id"])
        row.pop("tokens", None)
        row.pop("history", None)
        if was is not None:
            row["status"] = was.get("status", "")
            row["question"] = was.get("question", "")
            if (row["status"] in OUT_WITH_BUILDER
                    and not row["text"].strip()
                    and str(was.get("text") or "").strip()):
                row["text"] = was["text"]
            if was.get("tokens"):
                row["tokens"] = was["tokens"]
            if was.get("history"):
                row["history"] = was["history"]
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
            # A tombstone is sticky: nothing restores a deleted goal today,
            # so a posted tree that carries one as active is a stale echo --
            # an in-flight merge computed before the delete -- not a restore.
            if prev.get("status") == "abandoned":
                status = "abandoned"
            out.append({"id": nid, "title": title, "status": status,
                        "parent_goal_id": parent_gid,
                        "evidence_ids": prev.get("evidence_ids", []),
                        "todos": [],
                        "important_item_ids": prev.get("important_item_ids", []),
                        "prompt_ids": prev.get("prompt_ids", []),
                        "sources": prev.get("sources", []),
                        # Carried, not recomputed. The browser posts the
                        # whole tree back on every edit and this rebuilds
                        # each goal from a fixed field list -- so a field
                        # missing here is not merely unsaved, it is erased
                        # by the next thing the reader types.
                        "relevance": prev.get("relevance", "core"),
                        "relevance_why": prev.get("relevance_why", ""),
                        "relevance_for": prev.get("relevance_for", ""),
                        "project_cwd": prev.get("project_cwd", ""),
                        "opening": prev.get("opening", ""),
                        # The Understanding tab's, written through its own op
                        # and never posted with the tree: carried, or the next
                        # thing typed in the artifact would erase it.
                        "understanding": (prev.get("understanding")
                                          or {"scenario": "", "shots": [],
                                              "questions": []}),
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
