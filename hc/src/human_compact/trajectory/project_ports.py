"""Local port checks. Never stop unrelated listeners or guess application rewrites."""
import errno
import socket
import os
from urllib.parse import urlsplit


def automatic_static(plan,service):
    # Without a connection contract, a multi-service plan may embed this URL elsewhere.
    return service.get('kind')=='static' and len(plan['services'])==1


def conflicts(plan):
    result=[];seen={}
    for service in plan['services']:
        if automatic_static(plan,service):continue  # Its server binds and reports atomically.
        endpoint=urlsplit(service['healthUrl'])
        addresses=('127.0.0.1','::1') if endpoint.hostname=='localhost' else (endpoint.hostname,)
        for address in addresses:
            key=(address,endpoint.port)
            reason=None
            if key in seen:reason='Also requested by service '+seen[key]
            else:
                family=socket.AF_INET6 if ':' in address else socket.AF_INET
                try:
                    with socket.socket(family,socket.SOCK_STREAM) as probe:
                        # A terminated HTTP server can leave TIME_WAIT connections.
                        # Match POSIX server restart semantics without permitting an
                        # active listener to be shared (never enable SO_REUSEPORT).
                        if os.name!='nt':probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                        elif hasattr(socket,'SO_EXCLUSIVEADDRUSE'):probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                        probe.bind(key)
                except OSError as exc:
                    if exc.errno in (errno.EAFNOSUPPORT,errno.EADDRNOTAVAIL) and endpoint.hostname=='localhost':continue
                    reason='Port is occupied or unavailable'
            seen[key]=service['id']
            if reason:
                result.append({'service':service['id'],'healthUrl':service['healthUrl'],'reason':reason,
                    'dependents':[s['id'] for s in plan['services'] if service['id'] in s.get('dependsOn',[])]})
                break
    return result


def available_port():
    """A candidate, not a reservation; preflight and launch still handle races."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        return probe.getsockname()[1]


def reusable_preparation(step, stages, failure, root):
    """Reuse successful unchanged preparation within a port-only recovery."""
    import shlex
    from pathlib import Path
    text=' '.join(str(failure.get(k,'')) for k in ('reason','stderr'))
    if not ('EADDRINUSE' in text or 'port is already occupied' in text.lower()):return False
    if step.get('stage')!='prepare' or step.get('_runtime') or step.get('env') or step.get('environmentCwd'):return False
    if step['argv'] not in (['npm','install'],['npm','ci'],['npm','run','build']):return False
    cwd=Path(step['cwd']).resolve()
    return any(s.get('status')=='done' and s.get('command')==shlex.join(step['argv'])
               and Path(s.get('cwd') or root).resolve()==cwd for s in stages)
