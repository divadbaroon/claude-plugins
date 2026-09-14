"""Bounded, read-only runtime assessment and semantic repair checks.

Wheel coverage ranks candidates; it is not a claim that source builds or application
code work. No package code is executed and no project files are written here.
"""
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen, Request
from urllib.parse import quote
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from packaging.tags import sys_tags, cpython_tags, compatible_tags
from packaging.utils import parse_wheel_filename

VERSIONS = ('3.10','3.11','3.12','3.13','3.14')
MANIFESTS = ('requirements.txt','pyproject.toml','.python-version','uv.lock','poetry.lock',
             'Pipfile','Pipfile.lock','package.json','package-lock.json','yarn.lock','pnpm-lock.yaml')
MAX_FILE = 4000000


def snapshot(root, cwd):
    """Fingerprint relevant files, including bounded nested pip requirements."""
    from .project_setup import within
    root=Path(root).resolve();cwd=Path(cwd).resolve();files={};unknown=[]
    def read(path):
        name=str(path.relative_to(root))
        if name in files:return files[name]
        p=within(root,name)
        if not p.is_file() or p.stat().st_size>MAX_FILE or len(files)>=24 or sum(len(v) for v in files.values())>16000000:
            raise ValueError('Compatibility manifest exceeds the read budget: '+name)
        files[name]=p.read_text();return files[name]
    directory=cwd
    while directory.is_relative_to(root):
        for name in MANIFESTS:
            path=directory/name
            if path.exists():read(path)
        if directory==root:break
        directory=directory.parent
    requirements=[];visited=set()
    def collect(path):
        if path in visited:return
        visited.add(path)
        for raw in read(path).splitlines():
            line=raw.split(' #',1)[0].strip()
            if not line or line.startswith('#'):continue
            if line.startswith(('-r ','--requirement ','-c ','--constraint ')):
                collect(path.parent/line.split(maxsplit=1)[1]);continue
            try:
                requirement=Requirement(line)
                if requirement.url:raise ValueError('URL dependency')
                requirements.append(requirement)
            except Exception:unknown.append('Unsupported requirement syntax; dependency assessment is partial')
    if (cwd/'requirements.txt').exists():collect(cwd/'requirements.txt')
    declaration=None;constraint=None
    for name in ('.python-version','pyproject.toml'):
        directory=cwd
        while directory.is_relative_to(root):
            path=directory/name
            if path.exists():
                value=read(path)
                if name=='.python-version':declaration=value.strip()
                else:
                    data=tomllib.loads(value);project=data.get('project',{})
                    constraint=project.get('requires-python')
                    if constraint:SpecifierSet(constraint)
                    if not requirements:
                        for value in project.get('dependencies',[]):
                            r=Requirement(value)
                            if r.url:unknown.append('URL dependency is not assessed')
                            else:requirements.append(r)
                    if 'dependencies' in project.get('dynamic',[]):unknown.append('Dynamic dependencies are not assessed')
                break
            if directory==root:break
            directory=directory.parent
    digest=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    return {'fingerprint':digest,'declaration':declaration,'constraint':constraint,
            'requirements':requirements,'unknown':sorted(set(unknown)),
            'files':list(files)}


def metadata(name, version):
    """Public exact-release metadata only, no credentials or project-controlled URLs."""
    url='https://pypi.org/pypi/'+quote(name,safe='')+'/'+quote(version,safe='')+'/json'
    with urlopen(Request(url,headers={'User-Agent':'Engelbart compatibility assessment'}),timeout=5) as response:
        raw=response.read(1000001)
    if len(raw)>1000000:raise ValueError('Package metadata exceeds budget')
    return json.loads(raw)


def allows(spec, version):
    """A minor-only request is not ruled out solely by a patch-level lower bound."""
    if not spec:return True
    parsed=SpecifierSet(spec)
    if len(version.split('.'))==3:return parsed.contains(version,prereleases=True)
    patches={0,1,999}
    for clause in parsed:
        try:
            boundary=Version(clause.version.rstrip('.*')).release
            if len(boundary)>2 and '.'.join(map(str,boundary[:2]))==version:
                patches.update(max(0,boundary[2]+offset) for offset in (-1,0,1))
        except Exception:pass
    return any(parsed.contains(version+'.'+str(patch),prereleases=True) for patch in patches)


def assess(root, cwd, preferred, installed, lookup=metadata):
    source=snapshot(root,cwd)
    pins={}
    for r in source['requirements']:
        exact=[s.version for s in r.specifier if s.operator=='==' and '*' not in s.version]
        if len(exact)==1:pins[(r.name,exact[0])]=None
    keys=list(pins)[:32]
    def fetch(key):
        try:return key,lookup(*key)
        except Exception:return key,None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for key,data in pool.map(fetch,keys):pins[key]=data
    versions=list(dict.fromkeys([preferred]+([source['declaration']] if source['declaration'] in VERSIONS or source['declaration'] and source['declaration'].count('.')==2 else [])+list(VERSIONS)))
    candidates=[];platforms=list(dict.fromkeys(t.platform for t in sys_tags()))
    for version in versions:
        try:parsed=Version(version)
        except Exception:continue
        blocked=[];gaps=[];unknown=list(source['unknown']);wheels=0
        declared=source['declaration']
        if declared and not (version==declared or version.startswith(declared+'.')):
            blocked.append('Conflicts with .python-version '+declared)
        if not allows(source['constraint'],version):blocked.append('Conflicts with requires-python '+str(source['constraint']))
        target=tuple(parsed.release[:2]);interpreter='cp'+''.join(map(str,target))
        tags=set(cpython_tags(target,platforms=platforms))|set(compatible_tags(target,interpreter=interpreter,platforms=platforms))
        for requirement in source['requirements']:
            environment={'python_version':'.'.join(version.split('.')[:2]),'python_full_version':version if len(parsed.release)==3 else version+'.0'}
            if requirement.marker and not requirement.marker.evaluate(environment):continue
            exact=[s.version for s in requirement.specifier if s.operator=='==' and '*' not in s.version]
            label=str(requirement)
            if len(exact)!=1:unknown.append('Unpinned dependency: '+requirement.name);continue
            data=pins.get((requirement.name,exact[0]))
            if not data:unknown.append('Metadata unavailable: '+label);continue
            if not allows(data.get('info',{}).get('requires_python'),version):blocked.append('Requires-Python excludes candidate: '+label);continue
            matching=False;has_wheel=False
            for artifact in data.get('urls',[]):
                if artifact.get('packagetype')!='bdist_wheel' or artifact.get('yanked'):continue
                has_wheel=True
                try:
                    if parse_wheel_filename(artifact['filename'])[3]&tags and allows(artifact.get('requires_python'),version):matching=True
                except (ValueError,KeyError):continue
            if matching:wheels+=1
            elif has_wheel:gaps.append('No matching wheel: '+label)
            else:unknown.append('Source-only release: '+label)
        if not source['requirements']:unknown.append('No static dependencies to assess')
        candidates.append({'version':version,'status':'excluded' if blocked else 'partial',
                           'reasons':blocked,'wheelGaps':gaps,'wheelMatches':wheels,'unknown':unknown[:12],
                           'installed':version in installed or any(v.startswith(version+'.') for v in installed)})
    eligible=[c for c in candidates if c['status']!='excluded']
    # Prefer demonstrable wheel coverage; installation and the original choice break ties.
    eligible.sort(key=lambda c:(len(c['wheelGaps']),-c['wheelMatches'],not c['installed'],c['version']!=preferred,tuple(-n for n in Version(c['version']).release)))
    release_constraints=[]
    for r in source['requirements']:
        exact=next((s.version for s in r.specifier if s.operator=='==' and '*' not in s.version),None)
        data=pins.get((r.name,exact))
        if data and data.get('info',{}).get('requires_python'):
            release_constraints.append({'requirement':str(r),'specifier':data['info']['requires_python']})
    return {'cwd':str(Path(cwd).resolve().relative_to(Path(root).resolve())),
            'manifestFingerprint':source['fingerprint'],'files':source['files'],
            'constraint':source['constraint'],'declaration':source['declaration'],
            'releaseConstraints':release_constraints,
            'recommendedVersion':eligible[0]['version'] if eligible else None,
            'candidates':candidates,'scope':'Declared constraints and direct pinned release metadata; transitive resolution and source builds remain unverified.'}


def configuration(root, step, requests, services=()):
    cwd=Path(step['cwd']).resolve();runtime=next((r['version'] for r in requests if Path(r['cwd']).resolve()==cwd),None)
    argv=list(step['argv'])
    if runtime and argv[0] in ('python','python3','.venv/bin/python','.venv/Scripts/python.exe'):argv[0]='managed-python'
    entrypoint=None
    if len(argv)==2 and argv[1].endswith('.py'):
        from .project_setup import within
        p=within(Path(root).resolve(),str((cwd/argv[1]).relative_to(Path(root).resolve())))
        if p.stat().st_size<=MAX_FILE:entrypoint=hashlib.sha256(p.read_bytes()).hexdigest()
    return {'cwd':str(cwd.relative_to(Path(root).resolve())), 'argv':argv,'entrypointFingerprint':entrypoint,
            'env':step.get('env',{}),'runtime':runtime,'environmentCwd':step.get('environmentCwd'),
            'dependencies':sorted([configuration(root,s,requests) for s in services if s['id'] in step.get('dependsOn',[])],key=lambda c:json.dumps(c,sort_keys=True)),
            'manifestFingerprint':snapshot(root,cwd)['fingerprint'],
            'healthUrl':step.get('healthUrl')}


def validate_repair(root, plan, requests, attempts, assessments=()):
    configs=[configuration(root,s,requests,plan['services']) for s in plan['preparation']+plan['services']]
    for previous in attempts:
        failure=previous.get('failure') or {}
        failed=failure.get('configuration')
        if not failed:continue  # Legacy records do not prove a semantic match.
        if not any(c['cwd']==failed['cwd'] for c in configs):
            raise ValueError('Repair removes the failed component instead of repairing it')
        logs=str(failure.get('stdout',''))+'\n'+str(failure.get('stderr',''))
        native_build=('Failed building wheel' in logs or 'Failed to build' in logs) and any(token in logs for token in ('cpython','Python.h','_Py','struct _'))
        if native_build:
            old_report=next((a for a in (failure.get('compatibility') or []) if a['cwd']==failed['cwd']),None)
            new_report=next((a for a in assessments if a['cwd']==failed['cwd']),None)
            candidate=next((c for c in configs if c['cwd']==failed['cwd'] and c['argv']==failed['argv']),None)
            if old_report and new_report and candidate and candidate['manifestFingerprint']==failed['manifestFingerprint']:
                before=next((c for c in old_report['candidates'] if c['version']==failed['runtime']),None)
                after=next((c for c in new_report['candidates'] if c['version']==candidate['runtime']),None)
                if before and after and not (set(before['wheelGaps'])-set(after['wheelGaps'])):
                    raise ValueError('Runtime change has no demonstrated improvement for the failed native dependency build. Choose a candidate with matching wheels, change dependencies, or request manual review.')
        def equivalent(candidate):
            old=dict(failed);new=dict(candidate)
            if failure.get('exitCode') not in (None,0):
                old.pop('healthUrl',None);new.pop('healthUrl',None)
            a=old.pop('runtime',None);b=new.pop('runtime',None)
            same_runtime=a==b or bool(a and b and (a.startswith(b+'.') or b.startswith(a+'.')))
            return same_runtime and old==new
        if any(equivalent(c) for c in configs):
            raise ValueError('Repair repeats the failed component configuration (command, runtime and manifests). Change a relevant input before retrying.')
    return configs


def verify_actual(report, actual):
    if not allows(report['constraint'],actual):
        raise ValueError('Installed Python '+actual+' violates the project requires-python constraint')
    for item in report.get('releaseConstraints',[]):
        r=Requirement(item['requirement'])
        if r.marker and not r.marker.evaluate({'python_version':'.'.join(actual.split('.')[:2]),'python_full_version':actual}):continue
        if not allows(item['specifier'],actual):
            raise ValueError('Installed Python '+actual+' violates Requires-Python for '+r.name)
