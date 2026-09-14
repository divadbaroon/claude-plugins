"""Probe only loopback sockets owned by the launched service's process tree."""
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
import psutil


def listeners(proc, port):
    try:
        parent=psutil.Process(proc.process.pid)
        processes=[parent,*parent.children(recursive=True)]
    except psutil.Error:return set()
    addresses=set()
    for process in processes:
        try:
            for connection in process.net_connections(kind='tcp'):
                if connection.status==psutil.CONN_LISTEN and connection.laddr.port==port:
                    addresses.add(connection.laddr.ip)
        except (psutil.AccessDenied,psutil.NoSuchProcess):
            # macOS restricts socket introspection; lsof can inspect the user's own processes.
            if sys.platform!='darwin':continue
            try:
                result=subprocess.run(['lsof','-nP','-a','-p',str(process.pid),'-iTCP:'+str(port),'-sTCP:LISTEN','-Ftn'],capture_output=True,text=True,timeout=2)
                family=None
                for line in result.stdout.splitlines():
                    if line.startswith('t'):family=line[1:]
                    if line.startswith('n'):
                        host,_,number=line[1:].rpartition(':')
                        if number==str(port):
                            host=host.strip('[]')
                            if host=='*':host={'IPv4':'0.0.0.0','IPv6':'::'}.get(family)
                            if host:addresses.add(host)
            except (OSError,subprocess.TimeoutExpired):pass
    return addresses


def probe(proc, expected):
    endpoint=urlsplit(expected);port=endpoint.port or (443 if endpoint.scheme=='https' else 80)
    owned=listeners(proc,port)
    requested=endpoint.hostname
    hosts=list(dict.fromkeys([requested,'127.0.0.1','::1']))
    for host in hosts:
        if host=='localhost':continue  # Select the concrete address whose listener we verified.
        if host not in ('127.0.0.1','::1'):continue
        wildcard='0.0.0.0' if host=='127.0.0.1' else '::'
        if host not in owned and wildcard not in owned:continue
        netloc=('['+host+']' if ':' in host else host)+':'+str(port)
        actual=urlunsplit((endpoint.scheme,netloc,endpoint.path,endpoint.query,''))
        proc.url=actual;proc.probe()
        if proc.healthy and proc.alive():return actual
    proc.healthy=False
    return None
