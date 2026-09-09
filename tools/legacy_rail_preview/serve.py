#!/usr/bin/env python3
"""Serve the real legacy rail with an isolated, editable demonstration store.

Stdlib only. Does not import the Engelbart backend, read credentials, execute
commands, or contact models. The browser assets come from this checkout.
"""
import argparse
import copy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
WEB = HERE.parents[1] / 'hc/src/human_compact/trajectory/web'
SESSION = '11111111-2222-4333-8444-555555555555'


def sample():
    texts = ['Bring the legacy editor into Engelbart',
             'Keep Enter, Tab and arrow-key editing',
             'Keep selection steady while edits save',
             'Try a selected build with Cmd+Enter',
             'Inspect the original rail behavior']
    return {'revision': '1', 'scope': 'chat', 'session_id': SESSION,
            'prompts': [], 'agent_runs': {}, 'build_runs': {},
            'project': {'name': 'Rail reference', 'cwd': ''},
            'goals': [{'id': 'rail-reference', 'title': 'Rail reference',
                       'status': 'active', 'parent_goal_id': None,
                       'prompt_ids': [], 'sources': [],
                       'notes': '# Editor notes\n\nKeep the list continuous. Editing a row should not replace the whole rail.\n',
                       'understanding': {
                           'scenario': 'I am turning an idea into a small set of implementation steps.',
                           'shots': [], 'questions': [
                               {'id': 'q12345678', 'text': 'Which behavior must survive the move?',
                                'shots': [], 'thread': []}]},
                       'todo_items': [{'id': 't%08d' % i, 'text': text,
                                       'depth': 1 if i in (1, 2) else 0,
                                       'status': 'done' if i == 4 else '', 'question': ''}
                                      for i, text in enumerate(texts)]}]}


class DemoStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.pending = {}
        self.state = json.loads(self.path.read_text()) if self.path.exists() else sample()
        for goal in self.state['goals']:
            for row in goal.get('todo_items', []):
                if row.get('status') in ('building', 'queued'):
                    row['status'] = ''
        self.save()

    def save(self):
        self.state['generated_at'] = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state, indent=2) + '\n')
        temp.chmod(0o600)
        os.replace(temp, self.path)

    def changed(self):
        self.state['revision'] = str(int(self.state['revision']) + 1)
        self.save()

    def finish_builds(self, now=None):
        with self.lock:
            now = time.monotonic() if now is None else now
            ready = [key for key, due in self.pending.items() if due <= now]
            for gid, rid in ready:
                for goal in self.state['goals']:
                    if goal['id'] == gid:
                        for row in goal.get('todo_items', []):
                            if row['id'] == rid and row.get('status') == 'building':
                                row['status'] = 'done'
                del self.pending[(gid, rid)]
            if ready:
                self.changed()

    def snapshot(self):
        with self.lock:
            self.finish_builds()
            return copy.deepcopy(self.state)

    def import_tree(self, body):
        with self.lock:
            if body.get('base_revision') != self.state['revision']:
                return {'ok': False, 'error': 'stale revision', 'revision': self.state['revision']}
            roots = body.get('goals')
            if not isinstance(roots, list):
                return {'ok': False, 'error': 'Expected a goal list'}
            nodes = {}

            def walk(items):
                for node in items:
                    if isinstance(node, dict) and isinstance(node.get('id'), str):
                        nodes[node['id']] = node
                        walk(node.get('children', []))
            walk(roots)
            # The reference has one fixed goal. Imports may edit its rows and
            # notes; they cannot turn this server into a project manager.
            for goal in self.state['goals']:
                node = nodes.get(goal['id'])
                if node is None:
                    continue
                rows = node.get('todo_items', [])
                if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                    return {'ok': False, 'error': 'Expected todo rows'}
                held = {r['id']: r for r in goal['todo_items']}
                cleaned = []
                for row in rows:
                    if not isinstance(row.get('id'), str) or not isinstance(row.get('text'), str):
                        return {'ok': False, 'error': 'Each row needs an id and text'}
                    value = copy.deepcopy(row)
                    if (goal['id'], row['id']) in self.pending:
                        value['status'] = held[row['id']]['status']
                    cleaned.append(value)
                goal['todo_items'] = cleaned
                if isinstance(node.get('notes'), str):
                    goal['notes'] = node['notes']
            self.changed()
            return {'ok': True, 'revision': self.state['revision']}

    def op(self, body):
        with self.lock:
            name = body.get('op')
            if name == 'prompt_preview':
                return {'ok': True, 'prompt': ''}
            if name == 'build_log':
                return {'ok': True, 'lines': ['Reference demo: no agent or shell is connected.']}
            if name == 'check_understanding':
                return {'ok': True, 'raised': False}
            goal = next((g for g in self.state['goals'] if g['id'] == body.get('goal_id')), None)
            if name == 'set_understanding' and goal:
                goal['understanding'] = {k: copy.deepcopy(body.get(k, '' if k == 'scenario' else []))
                                         for k in ('scenario', 'shots', 'questions')}
                self.changed()
                return {'ok': True}
            if name == 'build_todos' and goal:
                ids = body.get('ids', [])
                if not isinstance(ids, list):
                    return {'ok': False, 'error': 'Expected selected todo ids'}
                for row in goal['todo_items']:
                    if row['id'] in ids and row['text'].strip() and row.get('status') != 'done':
                        row['status'] = 'building'
                        self.pending[(goal['id'], row['id'])] = time.monotonic() + 2
                self.changed()
                return {'ok': True, 'simulated': True}
            if name == 'cancel_todos' and goal:
                ids = body.get('ids', [])
                if not isinstance(ids, list):
                    return {'ok': False, 'error': 'Expected selected todo ids'}
                for row in goal['todo_items']:
                    if row['id'] in ids:
                        self.pending.pop((goal['id'], row['id']), None)
                        row['status'] = ''
                self.changed()
                return {'ok': True, 'simulated': True}
            return {'ok': False, 'error': 'This reference runs no agents. Edit and save locally; builds are simulated.'}


def export_page(output):
    output.mkdir(parents=True, exist_ok=True)
    html = (WEB / 'goals_bundle.html').read_text()
    html = html.replace('</body>', '<script src="/bridge.js"></script>\n<script src="/cutout.js"></script>\n</body>', 1)
    (output / 'index.html').write_text(html)
    shutil.copy2(WEB / 'bridge.js', output / 'bridge.js')
    for name in ('cutout.js', 'cutout.css', 'README.md'):
        shutil.copy2(HERE / name, output / name)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def local_host(self):
        if self.headers.get('Host') != '127.0.0.1:%s' % self.server.server_port:
            self.send({'ok': False, 'error': 'foreign host'}, 403)
            return False
        return True

    def send(self, value, status=200, mime='application/json'):
        data = json.dumps(value).encode() if mime == 'application/json' else value
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.local_host():
            return
        path = urlsplit(self.path).path
        if path == '/api/health':
            return self.send({'ok': True, 'scope': 'chat', 'session_id': SESSION, 'version': 'rail-reference'})
        if path == '/api/state':
            return self.send(self.server.store.snapshot())
        if path.startswith('/api/'):
            return self.send({'ok': True, 'configured': False, 'signed_in': False, 'items': [], 'active': False})
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/bridge.js': ('bridge.js', 'application/javascript'),
                 '/cutout.js': ('cutout.js', 'application/javascript'),
                 '/cutout.css': ('cutout.css', 'text/css'),
                 '/README.md': ('README.md', 'text/plain; charset=utf-8')}
        if path == '/favicon.ico':
            return self.send(b'', 204, 'image/x-icon')
        if path not in files:
            return self.send({'ok': False, 'error': 'not found'}, 404)
        name, mime = files[path]
        self.send((self.server.output / name).read_bytes(), mime=mime)

    def do_POST(self):
        if not self.local_host():
            return
        origin = self.headers.get('Origin')
        expected = 'http://127.0.0.1:%s' % self.server.server_port
        if origin and origin != expected:
            return self.send({'ok': False, 'error': 'foreign origin'}, 403)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError('invalid size')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('expected JSON object')
            if self.path == '/api/import':
                result = self.server.store.import_tree(body)
                return self.send(result, 200 if result['ok'] else 409)
            if self.path == '/api/op':
                return self.send(self.server.store.op(body))
            return self.send({'ok': False, 'error': 'not found'}, 404)
        except (ValueError, TypeError, KeyError):
            self.send({'ok': False, 'error': 'invalid request'}, 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--output', type=Path,
                        default=Path.home() / 'Desktop/Codex Readings/engelbart-legacy-rail')
    args = parser.parse_args()
    export_page(args.output)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.output = args.output
    server.store = DemoStore(args.output / 'demo-state.json')
    meta = {'pid': os.getpid(), 'url': 'http://127.0.0.1:%s/' % server.server_port}
    (args.output / 'server.json').write_text(json.dumps(meta) + '\n')
    print(meta['url'], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
