"""hc ui — localhost goal browser. Reads and writes the SAME goals.json
through the goals model (goal_context.md stays in sync for SessionStart
injection). Stdlib only; localhost only; Ctrl-C to stop."""
import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import goals as GM, state


def _scope(trajdir=None):
    """Resolve legacy global UI scope or an explicitly bound chat scope."""
    return Path(trajdir) if trajdir is not None else state.trajdir()


def _load_prompts(trajdir):
    """Read assignable human prompts from this chat only.

    Accept the early list-shaped store defensively; the durable store is
    ``{"prompts": [...]}``. Malformed/incomplete rows never reach the UI.
    """
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


def _payload(trajdir=None):
    trajdir = _scope(trajdir)
    goals, important = GM.load(trajdir)
    GM.sanitize(goals)
    ana = {}
    try:
        ana = json.loads((trajdir / "analysis.json").read_text())
    except (OSError, ValueError):
        pass
    return {"goals": goals["goals"], "items": important["items"],
            "prompts": _load_prompts(trajdir),
            "generated_at": goals.get("generated_at", ""),
            "sessions": ana.get("sessions_analyzed")}


def _apply(op, trajdir=None):
    trajdir = _scope(trajdir)
    goals, important = GM.load(trajdir)
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
        valid = {p["id"] for p in _load_prompts(trajdir)}
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
    GM.save(trajdir, goals, important)          # also rewrites goal_context.md
    return {"ok": True}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):                  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
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
            self._send(200, _payload(self.server.trajdir))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
        except (ValueError, TypeError):
            self._send(400, {"ok": False, "error": "bad json"}); return
        if self.path == "/api/op":
            with self.server.state_lock:
                result = _apply(body, self.server.trajdir)
            self._send(200, result)
        elif self.path == "/api/import":
            with self.server.state_lock:
                result = _import(body, self.server.trajdir)
            self._send(200, result)
        else:
            self._send(404, {"error": "not found"})


def run(port=8765, open_browser=True, trajdir=None):
    trajdir = _scope(trajdir)
    trajdir.mkdir(parents=True, exist_ok=True)
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            srv.trajdir = trajdir
            srv.state_lock = threading.RLock()
            break
        except OSError:
            continue
    else:
        print("  no free port found"); return
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"\n  Vault goals · {url}")
    print("  Ctrl-C to stop\n")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        srv.server_close()


def _import(nested, trajdir=None):
    """Map the Claude Design app's nested node tree back into the goals model.
    Node ids are preserved; `t:<gid>:<i>` nodes are that goal's todos. Nodes
    missing from the payload are marked abandoned (history kept, never
    destroyed). Evidence links and important-item associations survive."""
    if not isinstance(nested, list):
        return {"ok": False, "error": "expected a list of nodes"}
    trajdir = _scope(trajdir)
    goals, important = GM.load(trajdir)
    GM.sanitize(goals)
    old = {g["id"]: g for g in goals["goals"]}
    seen, out = set(), []

    def walk(node, parent_gid):
        nid = str(node.get("id", ""))
        title = (node.get("title") or "Untitled").strip()[:120]
        if nid.startswith("t:"):
            gid = nid.split(":")[1] if nid.count(":") >= 2 else parent_gid
            host = next((g for g in out if g["id"] == gid), None) or                    next((g for g in out if g["id"] == parent_gid), None)
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
            [(t["text"], t["done"]) for t in g["todos"]]) !=            (prev.get("title"), prev.get("status"), prev.get("parent_goal_id"),
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
    GM.save(trajdir, goals, important)
    return {"ok": True, "goals": len(out)}
