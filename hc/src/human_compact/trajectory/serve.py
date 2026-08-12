"""Local viewer: stdlib HTTP server for the Goal Trajectory Map. Serves the
analysis, resolves turn-level evidence, and records user corrections into a
file that regeneration never touches."""
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlparse, parse_qs


def run(trajdir: Path, port=7710, open_browser=True):
    web = resources.files("human_compact").joinpath("trajectory/web")
    index_html = web.joinpath("index.html").read_text()
    corrections_file = trajdir / "corrections.json"

    def load(name):
        p = trajdir / name
        return json.loads(p.read_text()) if p.is_file() else {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                self._send(index_html, "text/html; charset=utf-8")
            elif u.path.startswith("/static/"):
                name = u.path.split("/")[-1]
                try:
                    data = web.joinpath("static/" + name).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_response(404); self.end_headers()
            elif u.path == "/api/graph":
                self._send(load("graph.json"))
            elif u.path == "/api/analysis":
                self._send({"analysis": load("analysis.json"),
                            "corrections": load("corrections.json")})
            elif u.path == "/api/evidence":
                ids = parse_qs(u.query).get("ids", [""])[0].split(",")
                idx = load("evidence_index.json")
                self._send({i: idx.get(i) for i in ids if i})
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            if urlparse(self.path).path != "/api/correction":
                self.send_response(404); self.end_headers(); return
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n))
            cur = load("corrections.json") or {}
            cur[rec.get("target", "unknown")] = rec
            corrections_file.write_text(json.dumps(cur, indent=1))
            self._send({"ok": True})

    srv = None
    for p in range(port, port + 11):
        try:
            ThreadingHTTPServer.allow_reuse_address = True
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"  ports {port}-{port+10} all busy — close an old Trajectory tab "
              f"or run: lsof -ti :{port} | xargs kill")
        return
    url = f"http://127.0.0.1:{port}/"
    print(f"  Trajectory map: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
