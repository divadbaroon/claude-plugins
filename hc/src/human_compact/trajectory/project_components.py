"""Bounded app discovery and declared startup graph; no commands or config execute."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from . import project_environment as PE, project_static as PS

MANIFESTS={'package.json':'node','requirements.txt':'python','pyproject.toml':'python','Cargo.toml':'rust','go.mod':'go','Gemfile':'ruby','pom.xml':'java','deno.json':'deno','composer.json':'php'}
COMPOSE={'compose.yaml','compose.yml','docker-compose.yaml','docker-compose.yml'}


def order(components, edges):
    ids={c['id'] for c in components}
    remaining=set(ids);levels=[];issues=[]
    for edge in edges:
        if edge['dependency'] not in ids:
            issues.append('Unresolved dependency: '+edge['service']+' → '+edge['dependency'])
    while remaining:
        ready=sorted(i for i in remaining if all(e['dependency'] in ids-remaining for e in edges if e['service']==i))
        if not ready:break
        levels.append(ready);remaining.difference_update(ready)
    if remaining:issues.append('Cycle or unresolved prerequisites block: '+', '.join(sorted(remaining)))
    return {'levels':levels,'blocked':sorted(remaining),'issues':issues}


def discover(directory):
    root=PE.project(directory);components={};edges=[];hints=[];warnings=[];docs=[];compose=[];count=0;total=0
    def content(p):
        nonlocal total
        s=PE.read(p);total+=len(s.encode())
        if total>4*1024*1024:raise ValueError('discovery byte budget reached')
        return s
    for base,dirs,files in os.walk(root,followlinks=False):
        path=Path(base);relative=path.relative_to(root)
        count+=1
        if count>2000 or total>4*1024*1024:
            warnings.append('Discovery budget reached; some directories were not inspected.');break
        dirs[:]=sorted(d for d in dirs if d not in PE.SKIP and not (path/d).is_symlink())
        if len(relative.parts)>=6:
            if dirs:warnings.append('Depth limit reached at '+str(relative))
            dirs[:]=[]
        found=sorted(f for f in set(files)&MANIFESTS.keys() if not (path/f).is_symlink())
        if not found and PS.eligible(path):
            id=relative.as_posix()
            components[id]={'id':id,'path':str(path),'name':path.name,'types':['staticfile'],
                            'evidence':[{'file':(relative/'index.html').as_posix(),'kind':'static-entrypoint'}],'ports':[]}
            # Child HTML pages are routes/assets of this site, not separate applications.
            dirs[:]=[]
        if found:
            id=relative.as_posix();evidence=[{'file':(relative/f).as_posix(),'kind':'manifest'} for f in found]
            row={'id':id,'path':str(path),'name':path.name,'types':sorted({MANIFESTS[f] for f in found}),'evidence':evidence,'ports':[]}
            components[id]=row
            if 'package.json' in found:
                try:
                    package=json.loads(content(path/'package.json'))
                    scripts=package.get('scripts',{})
                    row['scripts']=sorted(scripts)
                    for name,command in scripts.items():
                        if not isinstance(command,str):continue
                        for match in re.finditer(r'\bPORT\s*=\s*(\d+)|--port(?:=|\s+)(\d+)',command):
                            row['ports'].append(int(match[1] or match[2]))
                        # A literal cd target is a relationship, not proof of start ordering.
                        for match in re.finditer(r'\bcd\s+([.\w/-]+)\s*&&',command):
                            target=(path/match[1]).resolve()
                            if target.is_relative_to(root):hints.append({'service':id,'targetPath':str(target.relative_to(root)),'kind':'script-reference','source':(relative/'package.json').as_posix(),'script':name})
                    proxy=package.get('proxy')
                    if isinstance(proxy,str):
                        parsed=urlsplit(proxy)
                        if parsed.hostname in ('localhost','127.0.0.1','::1'):
                            row['proxyPort']=parsed.port or 80
                except (OSError,ValueError,TypeError,AttributeError):warnings.append('Could not inspect '+(relative/'package.json').as_posix())
            for filename in ('base.py','app.py','main.py'):
                if filename not in files:continue
                try:
                    text=content(path/filename)
                    row['ports']+= [int(m[1]) for m in re.finditer(r'\.run\([^)]*\bport\s*=\s*(\d+)',text)]
                except (OSError,ValueError,UnicodeError):warnings.append('Could not inspect '+str(relative/filename))
        for filename in files:
            if filename in COMPOSE:compose.append(path/filename)
            if filename.lower()=='readme.md':docs.append((relative/filename).as_posix())
    # Read declarative Compose dependency semantics without executing Docker or interpolation.
    for file in sorted(compose):
        rel=file.relative_to(root).as_posix()
        try:
            import yaml
            text=content(file)
            if len(text)>65536:raise ValueError('Compose size limit')
            # Reject aliases/tags rather than allowing expansion bombs or custom constructors.
            tokens=list(yaml.scan(text))
            if len(tokens)>12000 or any(type(t).__name__ in ('AliasToken','AnchorToken','TagToken') for t in tokens):raise ValueError('Unsupported YAML aliases or tags')
            config=yaml.safe_load(text)
            services=config.get('services',{})
            if not isinstance(services,dict) or len(services)>100:raise ValueError('Invalid service mapping')
            for name,spec in services.items():
                if not isinstance(name,str) or not isinstance(spec,dict):raise ValueError('Invalid service')
                id=rel+'#'+name
                build=spec.get('build');context=build.get('context','.') if isinstance(build,dict) else build
                local=None
                if isinstance(context,str) and not any(x in context for x in ('${','://')):
                    target=(file.parent/context).resolve()
                    if target.is_relative_to(root) and target.is_dir():local=str(target)
                components[id]={'id':id,'name':name,'path':local,'types':['compose-service'],'evidence':[{'file':rel,'kind':'compose-service'}],'ports':[], 'requiresContainer':True}
                dependencies=spec.get('depends_on',{})
                if isinstance(dependencies,list):dependencies={d:{'condition':'service_started'} for d in dependencies}
                if not isinstance(dependencies,dict):raise ValueError('Invalid depends_on')
                for dep,options in dependencies.items():
                    condition=options.get('condition','service_started') if isinstance(options,dict) else 'service_started'
                    if condition not in ('service_started','service_healthy','service_completed_successfully'):raise ValueError('Unknown dependency condition')
                    if isinstance(options,dict) and options.get('required') is False:
                        hints.append({'service':id,'dependencies':[rel+'#'+str(dep)],'kind':'optional-dependency','source':rel});continue
                    edges.append({'service':id,'dependency':rel+'#'+str(dep),'condition':condition,'source':rel,'kind':'declared'})
                if any(k in spec for k in ('extends','profiles')):warnings.append('Compose extends/profiles need review: '+id)
        except (ImportError,OSError,ValueError,TypeError,AttributeError) as exc:
            warnings.append('Compose configuration needs review: '+rel)
        except Exception:
            warnings.append('Could not parse Compose configuration: '+rel)
    # Keep source-folder candidates alongside container services, without pretending
    # a container context is an independently runnable application or vice versa.
    if len(components)>200:warnings.append('Component limit reached; discovery is incomplete.')
    rows=sorted(components.values(),key=lambda r:r['id'])[:200]
    for row in rows:
        if row.get('proxyPort'):
            targets=[c['id'] for c in rows if c['id']!=row['id'] and row['proxyPort'] in c['ports']]
            hints.append({'service':row['id'],'dependencies':targets,'port':row['proxyPort'],'kind':'functional-proxy','source':row['evidence'][0]['file'], 'note':'Requests need this endpoint; this does not establish startup order.'})
    graph=order(rows,edges)
    return {'ok':True,'root':str(root),'components':rows,'dependencies':edges,'relationships':hints,'order':graph,'warnings':warnings,'documentation':sorted(docs),
            'note':'Only declared dependencies constrain these groups. Unrelated candidates are not proven independent. README instructions, script references and proxies are evidence for review, not executable startup rules. Container services require a container-capable executor; the current native runner does not run them.'}
