"""Repository package-manager evidence and bounded local runtime checks."""
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess

MANAGERS=('npm','bun','pnpm','yarn')
LOCKS={'package-lock.json':'npm','npm-shrinkwrap.json':'npm','bun.lock':'bun','bun.lockb':'bun','pnpm-lock.yaml':'pnpm','yarn.lock':'yarn'}

def evidence(root,cwd):
    root=Path(root).resolve();cwd=Path(cwd).resolve()
    while cwd.is_relative_to(root):
        manifest=cwd/'package.json';package={}
        if manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size<=100000:
            package=json.loads(manifest.read_text())
        declared=package.get('packageManager','')
        locks=[name for name in LOCKS if (cwd/name).is_file()]
        if declared or locks:
            match=re.fullmatch(r'(npm|bun|pnpm|yarn)@([^+]+)(?:\+.*)?',declared) if isinstance(declared,str) else None
            managers=sorted({LOCKS[name] for name in locks})
            return {'cwd':str(cwd.relative_to(root)),'declared':declared,'manager':match[1] if match else managers[0] if len(managers)==1 else None,
                    'version':match[2] if match else None,'lockfiles':locks,'conflict':bool(declared and not match) or not match and len(managers)>1}
        if cwd==root:break
        cwd=cwd.parent
    return {'manager':None,'lockfiles':[]}

def check_choice(root,cwd,manager):
    facts=evidence(root,cwd)
    if facts.get('conflict'):raise ValueError('Conflicting or unsupported package-manager declarations; review the repository configuration.')
    if facts['manager'] and manager!=facts['manager']:
        raise ValueError('Repository declares '+facts['manager']+'; do not substitute '+manager+'. Preserve its package manager and lockfile.')
    return facts

def check_runtime(root,cwd,argv,env):
    if not argv or argv[0] not in MANAGERS:return
    manager=argv[0];facts=check_choice(root,cwd,manager)
    env=launch_env(root,cwd,env)
    executable=shutil.which(manager,path=env.get('PATH',''))
    if not executable and manager=='bun':raise MissingBun(facts.get('version'))
    if not executable:raise ValueError('Required package manager '+manager+' is not installed or not on the launch PATH. Install it, then retry; no npm fallback was used.')
    # Corepack shims must not download a runtime as a side effect of a version probe.
    try:
        checked=subprocess.run([executable,'--version'],cwd=cwd,env={**env,'COREPACK_ENABLE_NETWORK':'0','COREPACK_ENABLE_DOWNLOAD_PROMPT':'0'},capture_output=True,text=True,timeout=15)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise ValueError('Could not verify local '+manager+'; check its installation and retry.') from exc
    actual=checked.stdout.strip()
    if checked.returncode or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[\w.-]+)?',actual):raise ValueError('Could not verify the local '+manager+' version. Make the repository runtime available and retry.')
    expected=facts.get('version')
    if expected and actual!=expected and manager=='bun':raise MissingBun(expected)
    if expected and actual!=expected:raise ValueError('Repository requires '+manager+'@'+expected+'; installed version is '+actual+'. Install the declared version and retry.')
    return {'manager':manager,'version':actual}

def railpack_commands(plan):
    return [{'stage':s.get('name'),'commands':[x['cmd'] for x in s.get('commands',[]) if isinstance(x,dict) and isinstance(x.get('cmd'),str)][:8]} for s in plan.get('steps',[])[:8]]


def preserve_railpack(plan,railpack,cwd):
    managers=set()
    for step in railpack_commands(railpack):
        if step['stage']!='install':continue
        for command in step['commands']:
            tokens=shlex.split(command)
            if len(tokens)>=2 and tokens[0] in MANAGERS and tokens[1] in ('install','ci'):
                managers.add(tokens[0])
    if len(managers)!=1:return
    expected=next(iter(managers))
    for step in plan['preparation']+plan['services']:
        if Path(step['cwd']).resolve()==Path(cwd).resolve() and step['argv'][0] in MANAGERS and step['argv'][0]!=expected:
            raise ValueError('Railpack selected '+expected+' for this component; do not substitute '+step['argv'][0]+'. Reconcile contradictory repository evidence explicitly.')

class MissingBun(ValueError):
    def __init__(self,version=None):
        self.version=version
        super().__init__('Bun'+('@'+version if version else '')+' is required but is not available to this launch.')


def bun_home(version):
    from . import project_runtime as RT
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise ValueError('Bun installation requires an exact release version.')
    return RT.home()/'package-managers'/'bun'/version


def bun_bin(version):
    import os
    return bun_home(version)/'node_modules'/'bun'/'bin' if os.name=='nt' else bun_home(version)/'bin'


def launch_env(root,cwd,env):
    import os
    from . import project_runtime as RT
    facts=evidence(root,cwd)
    if facts.get('manager')!='bun':return env
    version=facts.get('version')
    if not version:
        marker=RT.home()/'package-managers'/'bun'/'default-version'
        if marker.is_file():version=marker.read_text().strip()
    if version and re.fullmatch(r'\d+\.\d+\.\d+',version):
        directory=bun_bin(version)
        if (directory/('bun.exe' if os.name=='nt' else 'bun')).is_file():
            return {**env,'PATH':str(directory)+os.pathsep+env.get('PATH','')}
    return env


def bun_request(version=None):
    import urllib.request
    if not version:
        with urllib.request.urlopen('https://registry.npmjs.org/bun/latest',timeout=15) as response:
            version=json.loads(response.read(1000000))['version']
    target=bun_home(version)
    if not shutil.which('npm'):raise ValueError('Installing Bun requires an installed npm executable.')
    return {'manager':'bun','version':version,'directory':str(target)}


def install_bun(request,cancelled=lambda:False):
    """Host-owned installer; never accepts model-provided commands or project env."""
    import os
    import time
    from . import project_runtime as RT
    version=request['version'];target=bun_home(version)
    target.mkdir(parents=True,exist_ok=True)
    npm=shutil.which('npm')
    if not npm:raise ValueError('Installing Bun requires npm on PATH.')
    command=[npm,'install','--global','--prefix',str(target),'bun@'+version,
             '--registry=https://registry.npmjs.org','--no-audit','--no-fund','--ignore-scripts=false']
    env=RT.tool_env()
    for name in ('user.npmrc','global.npmrc'):(target/name).write_text('')
    env.update(NPM_CONFIG_USERCONFIG=str(target/'user.npmrc'),NPM_CONFIG_GLOBALCONFIG=str(target/'global.npmrc'))
    # A file avoids pipe deadlocks while keeping the installer log out of model evidence.
    with (target/'install.log').open('w+') as log:
        proc=subprocess.Popen(command,cwd=target,env=env,stdout=log,stderr=log,start_new_session=os.name!='nt')
        deadline=time.monotonic()+180
        try:
            while proc.poll() is None:
                if cancelled():raise ValueError('Bun installation cancelled.')
                if time.monotonic()>deadline:raise ValueError('Bun installation timed out. Retry installation.')
                time.sleep(.1)
            if proc.returncode:raise ValueError('Bun installation failed. Installer log: '+str(target/'install.log'))
        finally:
            if proc.poll() is None:
                if os.name!='nt':
                    import signal
                    os.killpg(proc.pid,signal.SIGKILL)
                else:proc.kill()
                proc.wait()
    executable=bun_bin(version)/('bun.exe' if os.name=='nt' else 'bun')
    checked=subprocess.run([str(executable),'--version'],env=env,capture_output=True,text=True,timeout=15)
    if checked.returncode or checked.stdout.strip()!=version:raise ValueError('Installed Bun version could not be verified.')
    (target.parent/'default-version').write_text(version)
    return str(executable)
