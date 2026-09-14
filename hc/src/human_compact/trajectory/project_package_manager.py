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
    executable=shutil.which(manager,path=env.get('PATH',''))
    if not executable:raise ValueError('Required package manager '+manager+' is not installed or not on the launch PATH. Install it, then retry; no npm fallback was used.')
    # Corepack shims must not download a runtime as a side effect of a version probe.
    try:
        checked=subprocess.run([executable,'--version'],cwd=cwd,env={**env,'COREPACK_ENABLE_NETWORK':'0','COREPACK_ENABLE_DOWNLOAD_PROMPT':'0'},capture_output=True,text=True,timeout=15)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise ValueError('Could not verify local '+manager+'; check its installation and retry.') from exc
    actual=checked.stdout.strip()
    if checked.returncode or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[\w.-]+)?',actual):raise ValueError('Could not verify the local '+manager+' version. Make the repository runtime available and retry.')
    expected=facts.get('version')
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
