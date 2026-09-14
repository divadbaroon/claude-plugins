"""Authenticated loopback control of a run through the supervisor that owns it."""
import json
import secrets
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

ACTIVE = {'installing','building','starting','running','setup_planning','setup_running','awaiting_approval'}


def ticket(port):
    return {'url':f'http://127.0.0.1:{port}', 'token':secrets.token_hex(32)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call(owner, id, action, **arguments):
    url=urlsplit(owner.get('url',''))
    if url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.path or url.query or url.fragment or url.username:
        raise ValueError('Invalid project supervisor address.')
    request=Request(owner['url']+'/api/op',data=json.dumps({'op':'project_owner_control','id':id,
        'token':owner['token'],'action':action,**arguments}).encode(),
        headers={'Content-Type':'application/json','Origin':owner['url']})
    try:
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=20) as response:
            data=response.read(4*1024*1024+1)
        if len(data)>4*1024*1024:raise ValueError('Supervisor response exceeded limit.')
        result=json.loads(data)
    except Exception:
        raise ValueError('The supervisor owning this run is unavailable. No process was stopped or replaced.') from None
    if not result.get('ok'):raise ValueError(result.get('error','The owning supervisor rejected the request.'))
    return result
