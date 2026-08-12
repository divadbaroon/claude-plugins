"""hc ui — localhost goal browser. Reads and writes the SAME goals.json
through the goals model (goal_context.md stays in sync for SessionStart
injection). Stdlib only; localhost only; Ctrl-C to stop."""
import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import goals as GM, state


def _payload():
    trajdir = state.trajdir()
    goals, important = GM.load(trajdir)
    GM.sanitize(goals)
    ana = {}
    try:
        ana = json.loads((trajdir / "analysis.json").read_text())
    except (OSError, ValueError):
        pass
    return {"goals": goals["goals"], "items": important["items"],
            "generated_at": goals.get("generated_at", ""),
            "sessions": ana.get("sessions_analyzed")}


def _apply(op):
    trajdir = state.trajdir()
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
    elif kind == "add_goal":
        parent = op.get("parent_goal_id") or None
        if parent and not GM.by_id(goals, parent):
            return {"ok": False, "error": "parent not found"}
        gid = GM.next_goal_id(goals)
        goals["goals"].append({"id": gid, "title": (op.get("title") or "Untitled").strip()[:120],
                               "status": "active", "parent_goal_id": parent,
                               "evidence_ids": [], "todos": [], "important_item_ids": [],
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
                "web/goals.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, _payload())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/op":
            self._send(404, {"error": "not found"}); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            op = json.loads(self.rfile.read(n))
        except (ValueError, TypeError):
            self._send(400, {"ok": False, "error": "bad json"}); return
        self._send(200, _apply(op))


def run(port=8765, open_browser=True):
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
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
