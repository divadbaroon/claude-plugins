"""Python runtime operations owned by hc, never model-supplied shell commands."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess


def home():
    p=Path(os.environ.get('HUMAN_COMPACT_HOME') or Path.home()/'.human-compact')/'project-environments'
    p.mkdir(parents=True,exist_ok=True,mode=0o700)
    return p.resolve()


def tool_env():
    env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','TMPDIR','TEMP','TMP','SYSTEMROOT','WINDIR','LANG')}
    env.update(UV_PYTHON_INSTALL_DIR=str(home()/'python'),UV_CACHE_DIR=str(home()/'cache'),UV_NO_CONFIG='1',UV_PYTHON_DOWNLOADS='never')
    return env


def find(version):
    # uv discovery never downloads and ignores project configuration.
    uv=shutil.which('uv')
    if uv:
        r=subprocess.run([uv,'python','find','--no-project','--system',version],cwd=home(),env=tool_env(),capture_output=True,text=True,timeout=10)
        if r.returncode==0:
            p=Path(r.stdout.strip())
            if p.is_absolute() and p.is_file():return str(p)
    p=shutil.which('python'+version)
    if p:
        r=subprocess.run([p,'--version'],cwd=home(),env=tool_env(),capture_output=True,text=True,timeout=5)
        if r.returncode==0 and re.search(r'Python '+re.escape(version)+r'(?:\.|\s|$)',r.stdout+r.stderr):return p
    return None


def inventory():
    versions=[]
    for version in ('3.10','3.11','3.12','3.13','3.14'):
        try:
            p=find(version)
            if p:versions.append({'version':version,'executable':p})
        except (OSError,subprocess.SubprocessError):pass
    return {'python':versions,'canDownloadPython':bool(shutil.which('uv')),'downloadRequiresApproval':True}


def validate(root, requests):
    from .project_setup import within
    if not isinstance(requests,list) or len(requests)>4:raise ValueError('At most four Python runtimes are supported')
    seen=set();out=[]
    for r in requests:
        if not isinstance(r,dict) or set(r)-{'cwd','version'}:raise ValueError('Invalid Python runtime request')
        cwd=str(within(Path(root).resolve(),r.get('cwd'),directory=True))
        version=r.get('version')
        if not isinstance(version,str) or not re.fullmatch(r'3\.(?:10|11|12|13|14)(?:\.[0-9]{1,3})?',version):raise ValueError('Unsupported Python version; select 3.10 through 3.14')
        if cwd in seen:raise ValueError('Duplicate Python runtime component')
        seen.add(cwd);out.append({'cwd':cwd,'version':version})
    return out


def missing(requests):
    return [r for r in requests if not find(r['version'])]


def download_command(version):
    uv=shutil.which('uv')
    if not uv:raise ValueError('Python download is unavailable: install uv locally, then retry. Approval cannot install uv.')
    return [uv,'python','install',version,'--no-bin','--no-registry']


def environment_path(root,cwd,version,token):
    key=hashlib.sha256((str(root)+'\0'+cwd).encode()).hexdigest()[:24]
    return home()/key/(version+'-'+token)


def python_path(venv):
    return str(venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python'))


def bind(step,requests,paths):
    """Translate only known venv commands; never rewrite opaque package scripts."""
    argv=step['argv'];cwd=step['cwd']
    if cwd not in paths:return argv,{}
    venv=paths[cwd];executable=python_path(venv)
    if argv[:3] in (['python3','-m','venv'],['python','-m','venv']):return None,{}
    if argv[0] in ('.venv/bin/python','.venv/Scripts/python.exe','python','python3'):
        argv=[executable]+argv[1:]
    return argv,{'VIRTUAL_ENV':str(venv),'PATH':str(Path(executable).parent)+os.pathsep+os.environ.get('PATH','')}


def initial_requests(root,plan):
    """Manage direct Python services from their first venv creation.

    Opaque npm wrappers keep their existing semantics; no script is rewritten.
    """
    requests=[]
    for step in plan['preparation']:
        creates=step['argv'] in (['python3','-m','venv','.venv'],['python','-m','venv','.venv'])
        installs=step['argv'][0] in ('.venv/bin/python','.venv/Scripts/python.exe') and step['argv'][1:]==['-m','pip','install','-r','requirements.txt']
        if not (creates or installs):continue
        cwd=step['cwd']
        services=[s for s in plan['services'] if s.get('environmentCwd')==str(Path(cwd).relative_to(root)) or s['cwd']==cwd]
        if not services or any(s['argv'][0] not in ('python','python3','.venv/bin/python','.venv/Scripts/python.exe') for s in services):continue
        declaration=Path(cwd)/'.python-version'
        if declaration.is_file() and not declaration.is_symlink():
            value=declaration.read_text()[:100].strip()
            m=re.fullmatch(r'(3\.(?:10|11|12|13|14))(?:\.\d+)?',value)
            if not m:raise ValueError('The declared Python version needs manual review')
            version=value
        else:
            executable=shutil.which(step['argv'][0] if creates else 'python3')
            if not executable:raise ValueError('Python is not installed; select a Python runtime before retrying')
            r=subprocess.run([executable,'--version'],cwd=home(),env=tool_env(),capture_output=True,text=True,timeout=5)
            m=re.search(r'Python (3\.(?:10|11|12|13|14))(?:\.|\s|$)',r.stdout+r.stderr)
            if not m:raise ValueError('The local Python version is unsupported by managed setup')
            version=m[1]
        if not any(r['cwd']==cwd for r in requests):requests.append({'cwd':cwd,'version':version})
    return requests
