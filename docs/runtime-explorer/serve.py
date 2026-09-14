"""Loopback reading page + durable questions sent to one existing Codex thread."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from thought_store import AnswerReader, Store, queue_prompt

PUBLIC_FILES = {"/": "index.html", "/index.html": "index.html", "/README.md": "README.md",
                "/validation.json": "validation.json", "/browser-validation.json": "browser-validation.json"}


def default_codex():
    # Use the app's matching CLI when available: its queue targets the open app.
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    return str(bundled) if bundled.is_file() else shutil.which("codex")


class Bridge:
    def __init__(self, directory, data_dir, thread, transcript, codex, cwd):
        self.directory = Path(directory).resolve()
        self.store = Store(data_dir)
        held = self.store.meta("thread")
        if held and held != thread:
            raise ValueError("The question store belongs to another Codex thread")
        self.store.set_meta("thread", thread)
        self.thread, self.codex, self.cwd = thread, codex, str(Path(cwd).resolve())
        self.token = secrets.token_urlsafe(32)
        self.reader = AnswerReader(self.store, transcript)
        self.stop = threading.Event()
        self.reader_error = ""
        self.delivery_error = ""
        for node in self.store.all():
            if node["status"] == "sending":
                self.store.status(node["id"], "uncertain", "The service restarted during delivery. Check this Codex conversation before retrying.")

    def deliver(self, node):
        try:
            done = subprocess.run([self.codex, "queue", "--thread", self.thread,
                                   "--message", queue_prompt(node), "--cd", self.cwd],
                                  capture_output=True, text=True, timeout=30)
            if done.returncode:
                self.delivery_error = (done.stderr or done.stdout or "Codex could not queue the question")[-1600:]
                self.store.status(node["id"], "failed", self.delivery_error)
            else:
                self.delivery_error = ""
                # An answer/receipt may arrive before the queue process returns.
                current = self.store.get(node["id"])
                if current["status"] == "sending":
                    self.store.status(node["id"], "queued")
        except subprocess.TimeoutExpired:
            self.delivery_error = "Codex did not confirm delivery in time. Check the conversation before retrying; the message may already be queued."
            self.store.status(node["id"], "uncertain", self.delivery_error)
        except OSError as exc:
            self.delivery_error = f"Could not reach Codex: {exc}"
            self.store.status(node["id"], "failed", self.delivery_error)

    def run(self):
        while not self.stop.is_set():
            try:
                self.reader.poll()
                self.reader_error = ""
                node = self.store.claim_next()
                if node:
                    self.deliver(node)
            except (OSError, ValueError) as exc:
                self.reader_error = "Cannot read this Codex conversation: " + str(exc)
            self.stop.wait(0.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def bridge(self):
        return self.server.bridge

    def allowed_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def send(self, status, payload, content_type="application/json; charset=utf-8", download=False):
        body = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, (dict, list)) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="engelbart-questions.json"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {"error": "Local host required"})
        path = urlsplit(self.path).path
        if path == "/api/thoughts":
            error = self.bridge.reader_error or self.bridge.delivery_error
            return self.send(200, {"questions": self.bridge.store.all(), "token": self.bridge.token,
                                   "connected": not bool(error), "error": error})
        if path == "/api/export":
            return self.send(200, {"format": "engelbart-questions-v1", "questions": self.bridge.store.all()}, download=True)
        file = PUBLIC_FILES.get(path)
        if not file or not (self.bridge.directory / file).is_file():
            return self.send(404, {"error": "Not found"})
        content_type = "text/html; charset=utf-8" if file.endswith(".html") else "application/json; charset=utf-8" if file.endswith(".json") else "text/plain; charset=utf-8"
        return self.send(200, (self.bridge.directory / file).read_bytes(), content_type)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        expected = "http://" + self.headers.get("Host", "")
        if not self.allowed_host() or origin != expected or not secrets.compare_digest(self.headers.get("X-Thought-Token", ""), self.bridge.token):
            return self.send(403, {"error": "Open this page locally before submitting a question"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "Expected JSON"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 100000:
                raise ValueError("Question request is empty or too large")
            raw = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            if path == "/api/questions":
                node = self.bridge.store.create(raw)
            elif path.startswith("/api/questions/") and path.endswith("/retry"):
                node = self.bridge.store.retry(path.split("/")[3])
            else:
                return self.send(404, {"error": "Not found"})
            self.send(200, {"question": node})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--codex", default=default_codex())
    parser.add_argument("--cwd", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    if not args.thread or not args.codex:
        parser.error("An existing Codex --thread and installed codex CLI are required")
    if not args.transcript.is_file() or args.thread not in args.transcript.name:
        parser.error("--transcript must be this thread's existing rollout JSONL file")
    bridge = Bridge(args.directory, args.data_dir or args.directory / ".thoughts", args.thread,
                    args.transcript, args.codex, args.cwd)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.bridge = bridge
    thread = threading.Thread(target=bridge.run, name="codex-questions", daemon=True)
    thread.start()
    print(f"http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
