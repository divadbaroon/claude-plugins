"""hc ui — localhost goal browser. Reads and writes the SAME goals.json
through the goals model (goal_context.md stays in sync for SessionStart
injection). Stdlib only; localhost only; Ctrl-C to stop."""
import json
import os
import threading
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import chat_state as CS, goals as GM, state


DEFAULT_CHAT_IDLE_SECONDS = 8 * 60 * 60
MAX_JSON_BYTES = 2 * 1024 * 1024


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
    """Read assignable human prompts from this chat only.

    Accept the early list-shaped store defensively; the durable store is
    ``{"prompts": [...]}``. Malformed/incomplete rows never reach the UI.
    """
    if chat_scoped:
        session_id, root = _chat_identity(trajdir)
        rows = CS.load_prompts(session_id, root)
    else:
        try:
            raw = json.loads((trajdir / "prompts.json").read_text())
        except (OSError, ValueError, TypeError):
            return []
        rows = raw if isinstance(raw, list) else raw.get("prompts", []) \
            if isinstance(raw, dict) else []
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


def _payload(trajdir=None, chat_scoped=None):
    chat_scoped = trajdir is not None if chat_scoped is None else chat_scoped
    trajdir = _scope(trajdir)
    with _state_access(trajdir, chat_scoped):
        goals, important = _load_goals(trajdir, chat_scoped)
        GM.sanitize(goals)
        ana = {}
        try:
            ana = json.loads((trajdir / "analysis.json").read_text())
        except (OSError, ValueError):
            pass
        return {"goals": goals["goals"], "items": important["items"],
                "prompts": _load_prompts(trajdir, chat_scoped),
                "generated_at": goals.get("generated_at", ""),
                "sessions": ana.get("sessions_analyzed")}


def _apply(op, trajdir=None, chat_scoped=None):
    chat_scoped = trajdir is not None if chat_scoped is None else chat_scoped
    trajdir = _scope(trajdir)
    with _state_access(trajdir, chat_scoped):
        goals, important = _load_goals(trajdir, chat_scoped)
        GM.sanitize(goals)
        kind = op.get("op")
        g = GM.by_id(goals, op.get("goal_id", ""))
        if kind == "rename_goal" and g and op.get("title", "").strip():
            g["title"] = op["title"].strip()[:120]
        elif kind == "set_status" and g and op.get("status") in ("active", "in_progress", "completed", "abandoned"):
            g["status"] = op["status"]
        elif kind == "set_priority" and g and op.get("priority") in ("urgent", "high", "normal"):
            g["priority"] = op["priority"]
        elif kind == "set_notes" and g:
            g["notes"] = str(op.get("notes", ""))[:4000]
        elif kind == "set_description" and g:
            g["description"] = str(op.get("description", ""))[:600]
        elif kind == "toggle_todo" and g:
            try:
                t = g["todos"][int(op.get("index", -1))]
                t["done"] = not t["done"]
            except (IndexError, ValueError, TypeError):
                return {"ok": False, "error": "no such todo"}
        elif kind == "add_todo" and g and op.get("text", "").strip():
            g["todos"].append({"text": op["text"].strip()[:160], "done": False,
                               "evidence_ids": []})
        elif kind in ("attach_prompt", "detach_prompt"):
            if not g:
                return {"ok": False, "error": "goal not found in this chat"}
            prompt_id = op.get("prompt_id")
            valid = {p["id"] for p in _load_prompts(trajdir, chat_scoped)}
            if not isinstance(prompt_id, str) or prompt_id not in valid:
                return {"ok": False, "error": "prompt not found in this chat"}
            links = g.setdefault("prompt_ids", [])
            if kind == "attach_prompt" and prompt_id not in links:
                links.append(prompt_id)
            elif kind == "detach_prompt":
                g["prompt_ids"] = [pid for pid in links if pid != prompt_id]
            g["updated_at"] = GM._now()
        elif kind == "add_goal":
            parent = op.get("parent_goal_id") or None
            if parent and not GM.by_id(goals, parent):
                return {"ok": False, "error": "parent not found"}
            gid = GM.next_goal_id(goals)
            goals["goals"].append({"id": gid, "title": (op.get("title") or "Untitled").strip()[:120],
                                   "status": "active", "parent_goal_id": parent,
                                   "evidence_ids": [], "todos": [], "important_item_ids": [],
                                   "prompt_ids": [],
                                   "priority": "normal", "notes": "",
                                   "updated_at": GM._now(), "origin": "user"})
        else:
            return {"ok": False, "error": "unknown or invalid op"}
        GM.sanitize(goals)
        _save_goals(trajdir, goals, important, chat_scoped)
        return {"ok": True}


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
            if self.path in ("/", "/index.html"):
                html = resources.files("human_compact.trajectory").joinpath(
                    "web/goals_bundle.html").read_text(encoding="utf-8")
                html = html.replace("<body>",
                                    "<body>\n<script src=\"/bridge.js\"></script>", 1)
                self._send(200, html.encode(), "text/html; charset=utf-8")
            elif self.path == "/bridge.js":
                js = resources.files("human_compact.trajectory").joinpath(
                    "web/bridge.js").read_bytes()
                self._send(200, js, "application/javascript")
            elif self.path == "/api/state":
                self._send(200, _payload(
                    self.server.trajdir, self.server.chat_scoped))
            elif self.path == "/api/health":
                self._send(200, {
                    "ok": True,
                    "scope": "chat" if self.server.chat_scoped else "global",
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
                with self.server.state_lock:
                    result = _import(
                        body, self.server.trajdir, self.server.chat_scoped)
                self._send(200, result)
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
        label="Vault goals", idle_timeout=None):
    chat_scoped = trajdir is not None
    trajdir = _scope(trajdir)
    trajdir.mkdir(parents=True, exist_ok=True)
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
    print(f"\n  {label} · {url}")
    print("  Ctrl-C to stop\n")
    idle_stop = threading.Event()
    idle_thread = None
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
        srv.server_close()
        if idle_thread is not None and idle_thread is not threading.current_thread():
            idle_thread.join(timeout=1)


def _import(nested, trajdir=None, chat_scoped=None):
    """Map the Claude Design app's nested node tree back into the goals model.
    Node ids are preserved; `t:<gid>:<i>` nodes are that goal's todos. Nodes
    missing from the payload are marked abandoned (history kept, never
    destroyed). Evidence links and important-item associations survive."""
    if not isinstance(nested, list):
        return {"ok": False, "error": "expected a list of nodes"}
    chat_scoped = trajdir is not None if chat_scoped is None else chat_scoped
    trajdir = _scope(trajdir)
    with _state_access(trajdir, chat_scoped):
        goals, important = _load_goals(trajdir, chat_scoped)
        GM.sanitize(goals)
        old = {g["id"]: g for g in goals["goals"]}
        seen, out = set(), []

        def walk(node, parent_gid):
            nid = str(node.get("id", ""))
            title = (node.get("title") or "Untitled").strip()[:120]
            if nid.startswith("t:"):
                gid = nid.split(":")[1] if nid.count(":") >= 2 else parent_gid
                host = next((g for g in out if g["id"] == gid), None) or \
                       next((g for g in out if g["id"] == parent_gid), None)
                if host is not None:
                    host["todos"].append({"text": title, "done": bool(node.get("done")),
                                          "evidence_ids": host.pop("_tev", {}).get(
                                              nid, []) if False else
                                          old.get(host["id"], {}).get("_", []) or []})
                for ch in node.get("children") or []:
                    walk(ch, parent_gid)
                return
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
                        "priority": node.get("prio") if node.get("prio") in
                            ("urgent", "high", "normal") else "normal",
                        "notes": str(node.get("notes") or "")[:4000],
                        "description": str(node.get("desc") or "")[:600],
                        "origin": prev.get("origin", "ui"),
                        "updated_at": prev.get("updated_at", GM._now())}
                       )
            for ch in node.get("children") or []:
                walk(ch, nid)

        for n in nested:
            walk(n, None)
        # preserve todo evidence where text still matches the old todo
        for g in out:
            prev = old.get(g["id"])
            if not prev:
                g["updated_at"] = GM._now()
                continue
            oldtd = {t["text"]: t for t in prev.get("todos", [])}
            for t in g["todos"]:
                if t["text"] in oldtd:
                    t["evidence_ids"] = oldtd[t["text"]].get("evidence_ids", [])
            if (g["title"], g["status"], g["parent_goal_id"], g["priority"],
                g["notes"], g["description"],
                [(t["text"], t["done"]) for t in g["todos"]]) != \
               (prev.get("title"), prev.get("status"), prev.get("parent_goal_id"),
                prev.get("priority", "normal"), prev.get("notes", ""),
                prev.get("description", ""),
                [(t["text"], t["done"]) for t in prev.get("todos", [])]):
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
        return {"ok": True, "goals": len(out)}
