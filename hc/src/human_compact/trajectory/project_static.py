"""Host-owned loopback server for plain static projects; no package installation."""
import argparse
import errno
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
from urllib.parse import unquote, urlsplit

CONFIGS={'package.json','requirements.txt','pyproject.toml','Cargo.toml','go.mod','Gemfile','pom.xml','deno.json','composer.json','Caddyfile','Staticfile','staticfile','Dockerfile','compose.yaml','compose.yml','docker-compose.yaml','docker-compose.yml'}


def eligible(path):
    path=Path(path)
    return (path/'index.html').is_file() and not (path/'index.html').is_symlink() and not any((path/name).exists() for name in CONFIGS)


def plan(path,root):
    if not eligible(path):raise ValueError('Built-in static serving requires index.html and no build or server configuration')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    return {'status':'plan','summary':'Serve the existing static files locally; no dependency installation or build is required.',
            'evidence':[str((Path(path)/'index.html').relative_to(root))+':1'],
            'preparation':[],'services':[{'id':'static','kind':'static','cwd':str(Path(path).relative_to(root)),
                'argv':['hc-static'],'dependsOn':[],'healthUrl':f'http://127.0.0.1:{port}/'}],'entryService':'static'}


class Handler(SimpleHTTPRequestHandler):
    def list_directory(self,path):
        self.send_error(404);return None

    def send_head(self):
        path=unquote(urlsplit(self.path).path)
        parts=Path(path.lstrip('/')).parts
        # Explicitly reject hidden files, traversal and symlinks, even within the root.
        root=Path(self.directory).resolve();target=root
        for part in parts:
            if part.startswith('.') or '\\' in part or '\x00' in part:
                self.send_error(404);return None
            target=target/part
            if target.is_symlink():self.send_error(404);return None
        if not target.resolve().is_relative_to(root):self.send_error(404);return None
        if target.is_dir():
            for name in ('index.html','index.htm'):
                if (target/name).is_symlink():self.send_error(404);return None
        return super().send_head()

    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        super().end_headers()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',required=True);parser.add_argument('--port',type=int,required=True);parser.add_argument('--allow-port-fallback',action='store_true');parser.add_argument('--ready-file')
    args=parser.parse_args();root=Path(args.directory).resolve(strict=True)
    if not 1024<=args.port<=65535 or not eligible(root):raise SystemExit('Invalid static service configuration')
    def handler(*a,**kw):return Handler(*a,directory=str(root),**kw)
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),handler)
    except OSError as exc:
        if not args.allow_port_fallback or exc.errno not in (errno.EADDRINUSE,10048):raise
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    port=server.server_address[1]
    if args.ready_file:
        target=Path(args.ready_file);temporary=target.with_suffix('.tmp')
        fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:json.dump({'port':port},stream)
        os.replace(temporary,target)
    print(f'Static site ready: http://127.0.0.1:{port}/',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
