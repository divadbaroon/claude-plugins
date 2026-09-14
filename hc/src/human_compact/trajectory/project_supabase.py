"""Host-owned, opt-in local Supabase provisioning. No remote CLI operations."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from . import project_environment as PE, project_runtime as RT
try:
    import tomllib
except ImportError:
    import tomli as tomllib

_LOCK=threading.RLock()
_JOBS={}
_INSTALL_LOCK=threading.Lock()
# Map only conventional names actually found by the environment inventory.
class DockerUnavailable(ValueError):
    pass


def lease(path):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    handle=path.open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close();return None
    return handle


KINDS={'URL':'API_URL','ANON_KEY':'ANON_KEY','PUBLISHABLE_KEY':'ANON_KEY',
       'SERVICE_ROLE_KEY':'SERVICE_ROLE_KEY','SECRET_KEY':'SERVICE_ROLE_KEY',
       'BUCKET_URL':'STORAGE_URL','DB_URL':'DB_URL'}


def location(path):
    root=PE.project(path)
    key=hashlib.sha256(str(root).encode()).hexdigest()[:24]
    target=RT.home()/'supabase'/key
    if target.resolve().is_relative_to(root) or target.is_symlink():
        raise ValueError('Local services storage must be outside the repository.')
    return root,target


def detect(path,repository_root=None):
    root=PE.project(path);boundary=PE.repository_boundary(root,repository_root)
    for parent in (root,*root.parents):
        if not parent.is_relative_to(boundary):break
        config=parent/'supabase'/'config.toml'
        if config.is_file():
            tomllib.loads(PE.read(config))
            return {'available':True,'source':str(parent),'evidence':str(config),
                    'summary':'Create an isolated local database, auth and storage using this repository’s Supabase configuration.'}
    return {'available':False}


def read_state(target):
    p=target/'state.json'
    return json.loads(PE.read(p)) if p.exists() else {}


def write_state(target,state):
    target.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(dir=target)
    try:
        with os.fdopen(fd,'w') as f:json.dump(state,f)
        os.replace(name,target/'state.json')
    finally:
        if os.path.exists(name):os.unlink(name)


def command(argv,cwd,env=None,timeout=30):
    # Output may contain generated secrets. Never publish it to the browser/model.
    with tempfile.TemporaryFile() as out:
        p=subprocess.Popen([str(x) for x in argv],cwd=cwd,env=env or tool_env(),
                           stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=os.name!='nt')
        try:p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name!='nt':
                import signal
                os.killpg(p.pid,signal.SIGKILL)
            else:p.kill()
            p.wait();raise ValueError('Local service operation timed out. Retry after checking Docker.')
        out.seek(0);output=out.read(1024*1024).decode('utf-8',errors='replace')
        if p.returncode:
            fd,log=tempfile.mkstemp(prefix='local-service-error-',suffix='.log',dir=cwd)
            with os.fdopen(fd,'w') as f:f.write(output)
            raise ValueError('Local service command failed ('+' '.join(str(x) for x in argv[:3])+'). Private diagnostic log: '+log+'. No remote repair was attempted.')
        return output


def tool_env():
    env=RT.tool_env()
    for key in ('USERPROFILE','APPDATA','LOCALAPPDATA','PATHEXT','COMSPEC','ProgramFiles'):
        if key in os.environ:env[key]=os.environ[key]
    return env


def local_env(target):
    env=docker_env()
    private=target/'tool-home';private.mkdir(parents=True,exist_ok=True,mode=0o700)
    env.update(HOME=str(private),USERPROFILE=str(private),XDG_CONFIG_HOME=str(private/'.config'))
    return env


def docker_binary():
    paths=[shutil.which('docker'),'/Applications/Docker.app/Contents/Resources/bin/docker',
           str(Path.home()/'.docker/bin/docker'),'/usr/local/bin/docker',
           str(Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Docker/Docker/resources/bin/docker.exe'),
           str(Path(os.environ.get('LOCALAPPDATA','C:/Users/Default/AppData/Local'))/'Programs/DockerDesktop/resources/bin/docker.exe')]
    return next((str(p) for p in paths if p and Path(p).is_file()),None)


def docker_env():
    binary=docker_binary()
    if not binary:raise DockerUnavailable('Docker is not installed.')
    env=tool_env();env['PATH']=str(Path(binary).parent)+os.pathsep+env.get('PATH','')
    try:contexts=json.loads(command([binary,'context','inspect'],RT.home(),env))
    except ValueError as exc:raise DockerUnavailable('Docker context is not yet available. Complete Docker startup.') from exc
    endpoint=contexts[0]['Endpoints']['docker']['Host']
    if not endpoint.startswith(('unix://','npipe://')):
        raise ValueError('Select a local Docker context. Remote Docker endpoints are not allowed for local setup.')
    env['DOCKER_HOST']=endpoint
    try:command([binary,'info','--format','{{.ServerVersion}}'],RT.home(),env,15)
    except ValueError as exc:raise DockerUnavailable('Docker is installed but its local engine is not accessible.') from exc
    return env


def download(url,target,limit):
    req=urllib.request.Request(url,headers={'User-Agent':'Engelbart-local-setup'})
    with urllib.request.urlopen(req,timeout=60) as response, target.open('wb') as f:
        total=0
        while True:
            chunk=response.read(1024*1024)
            if not chunk:break
            total+=len(chunk)
            if total>limit:raise ValueError('Installer exceeds the download size limit.')
            f.write(chunk)


def install_cli():
    tools=RT.home()/'supabase-tools';tools.mkdir(exist_ok=True)
    binary=tools/('supabase.exe' if os.name=='nt' else 'supabase')
    if binary.is_file():return str(binary)
    system={'Darwin':'darwin','Linux':'linux','Windows':'windows'}.get(platform.system())
    arch={'arm64':'arm64','aarch64':'arm64','x86_64':'amd64','AMD64':'amd64'}.get(platform.machine())
    if not system or not arch:raise ValueError('No supported Supabase CLI binary for this platform.')
    with tempfile.TemporaryDirectory(dir=tools) as tmp:
        tmp=Path(tmp);meta=tmp/'release.json'
        download('https://api.github.com/repos/supabase/cli/releases/latest',meta,2*1024*1024)
        release=json.loads(meta.read_text());version=release['tag_name']
        if not re.fullmatch(r'v\d+\.\d+\.\d+',version):raise ValueError('Invalid Supabase release version.')
        name=f'supabase_{version[1:]}_{system}_{arch}.tar.gz'
        base='https://github.com/supabase/cli/releases/download/'+version+'/'
        archive=tmp/name;checksums=tmp/'checksums'
        download(base+name,archive,150*1024*1024)
        download(base+'checksums.txt',checksums,1024*1024)
        expected=next((line.split()[0] for line in checksums.read_text().splitlines() if line.split()[-1]==name),None)
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise ValueError('Supabase CLI checksum did not match.')
        with tarfile.open(archive) as tar:
            member=tar.getmember(binary.name)
            if not member.isfile() or member.size>200*1024*1024:raise ValueError('Invalid CLI archive.')
            with tar.extractfile(member) as source, (tmp/binary.name).open('wb') as dest:shutil.copyfileobj(source,dest)
        (tmp/binary.name).chmod(0o700);os.replace(tmp/binary.name,binary)
    command([binary,'--version'],tools)
    return str(binary)


def install_docker():
    system=platform.system()
    if system=='Darwin':
        app=Path('/Applications/Docker.app')
        if not app.exists():
            arch='arm64' if platform.machine()=='arm64' else 'amd64'
            with tempfile.TemporaryDirectory(prefix='engelbart-docker-') as tmp:
                tmp=Path(tmp);dmg=tmp/'Docker.dmg';mount=tmp/'mount';mount.mkdir()
                download(f'https://desktop.docker.com/mac/main/{arch}/Docker.dmg',dmg,2*1024**3)
                command(['/usr/bin/hdiutil','attach',dmg,'-nobrowse','-mountpoint',mount],tmp,timeout=120)
                try:
                    installer=mount/'Docker.app'
                    command(['/usr/sbin/spctl','--assess','--type','execute',installer],tmp,timeout=120)
                    # Fixed host-generated installer path; AppleScript handles only the OS authorization dialog.
                    script='do shell script '+json.dumps("'"+str(installer/'Contents/MacOS/install')+"'")+' with administrator privileges'
                    command(['/usr/bin/osascript','-e',script],tmp,timeout=600)
                finally:command(['/usr/bin/hdiutil','detach',mount],tmp,timeout=60)
        command(['/usr/bin/open','-a',str(app)],RT.home())
    elif system=='Windows':
        if not docker_binary():
            winget=shutil.which('winget')
            if not winget:raise ValueError('Windows App Installer (winget) is needed to install Docker Desktop.')
            command([winget,'install','--exact','--id','Docker.DockerDesktop','--source','winget','--interactive'],RT.home(),timeout=1200)
        app=Path(os.environ.get('ProgramFiles','C:/Program Files'))/'Docker/Docker/Docker Desktop.exe'
        if app.is_file():subprocess.Popen([str(app)],env=RT.tool_env(),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    elif system=='Linux':
        if not docker_binary():
            distro=Path('/etc/os-release').read_text()
            if not re.search(r'^ID=(?:"?)(ubuntu|debian)(?:"?)$',distro,re.M):
                raise ValueError('Automatic Docker installation currently supports Debian/Ubuntu, macOS and Windows. Configure a local Docker-compatible runtime on this distribution.')
            pkexec=shutil.which('pkexec')
            if not pkexec:raise ValueError('A desktop authorization agent (pkexec) is required to install Docker. Install a local Docker runtime on headless systems.')
            command([pkexec,'/usr/bin/apt-get','update'],RT.home(),timeout=600)
            command([pkexec,'/usr/bin/apt-get','install','-y','docker.io'],RT.home(),timeout=1200)
            command([pkexec,'/usr/bin/systemctl','start','docker'],RT.home(),timeout=120)
        # Never change socket permissions or silently grant docker-group root-equivalent access.
    else:raise ValueError('Automatic Docker installation is unavailable on this platform.')


def mapping(names):
    result={}
    for name in names:
        m=re.fullmatch(r'(?:NEXT_PUBLIC_|VITE_|PUBLIC_|REACT_APP_)?SUPABASE_('+'|'.join(KINDS)+r')(?:_DEV|_LOCAL)?',name)
        if m:result[name]=KINDS[m[1]]
    return result


def prepare(root,target,source):
    """Copy reviewed local inputs only; omit remote links, credentials and functions."""
    folder=source/'supabase'
    if folder.is_symlink():raise ValueError('Supabase inputs cannot be symlinks.')
    config=tomllib.loads(PE.read(folder/'config.toml'))
    files={};warnings=['External auth providers, auth hooks and edge functions are disabled in the isolated preview.']
    for sub in ('migrations','seeds'):
        where=folder/sub
        if where.is_symlink():raise ValueError('Supabase input directories cannot be symlinks.')
        for p in where.rglob('*') if where.exists() else []:
            if p.is_symlink():raise ValueError('Supabase inputs cannot contain symlinks.')
            if p.is_file():files[str(p.relative_to(folder))]=PE.read(p)
    for name in ('seed.sql','roles.sql'):
        if (folder/name).exists():files[name]=PE.read(folder/name)
    if not any(n.startswith('migrations/') for n in files):
        raise ValueError('No Supabase migrations were found. Supply a local schema before creating this preview.')
    if len(files)>1000 or sum(len(t) for t in files.values())>20*1024*1024:raise ValueError('Supabase schema exceeds the local setup limit.')
    for content in files.values():
        if re.search(r'\b(dblink|postgres_fdw|http_post|http_get|http_request|http_put)\b|\bnet\s*\.',content,re.I):
            raise ValueError('Schema or seed SQL contains network operations. Review these before running an isolated database.')
    previous=read_state(target);ports=previous.get('ports',{})
    used=set()
    for saved in target.parent.glob('*/state.json'):
        if saved.parent!=target:
            used.update(json.loads(PE.read(saved)).get('ports',{}).values())
    sockets=[]
    def port(key):
        if key not in ports:
            for _ in range(100):
                s=socket.socket();s.bind(('127.0.0.1',0));sockets.append(s)
                candidate=s.getsockname()[1]
                if candidate not in used:
                    ports[key]=candidate;used.add(candidate);break
            else:raise ValueError('No unreserved local service port was found.')
        return ports[key]
    try:
        # A small native TOML writer preserves parsed values without executable interpolation.
        config['project_id']='engelbart-'+target.name
        for section,keys in {'api':['port'],'db':['port','shadow_port'],'studio':['port'],'inbucket':['port','smtp_port','pop3_port'],'analytics':['port'],'edge_runtime':['inspector_port']}.items():
            if section in config:
                for key in keys:config[section][key]=port(section+'.'+key)
        if 'pooler' in config.get('db',{}):config['db']['pooler']['port']=port('db.pooler.port')
        config.setdefault('api',{})['port']=port('api.port')
        config['api']['enabled']=True
        for section in ('analytics','edge_runtime'):
            config[section]={'enabled':False}
        auth=config.setdefault('auth',{})
        auth['site_url']='http://127.0.0.1:3000';auth['additional_redirect_urls']=['http://127.0.0.1:*','http://localhost:*']
        for section in ('external','hook'):
            auth.pop(section,None)
        auth.pop('sms',None);auth.get('email',{}).pop('smtp',None)
        config.pop('functions',None)
        config.pop('experimental',None)
        config.get('studio',{}).pop('openai_api_key',None)
        seed=config.setdefault('db',{}).setdefault('seed',{})
        patterns=seed.get('sql_paths',['./seed.sql']);found=[]
        for pattern in patterns:
            if not isinstance(pattern,str) or Path(pattern).is_absolute() or '..' in Path(pattern).parts:raise ValueError('Seed paths must stay inside supabase/.')
            import fnmatch
            matches=[name for name in files if fnmatch.fnmatch(name,pattern.removeprefix('./'))]
            if not matches:warnings.append('Missing seed input '+pattern+'; the preview may have no sample data.')
            found.extend(matches)
        seed['sql_paths']=['./'+n for n in sorted(set(found))];seed['enabled']=bool(found)
        rendered=toml(config)
        if re.search(r'env\s*\(',rendered):raise ValueError('Supabase configuration still requires external environment values. Review the config before local setup.')
        # Never retain paths to repository secrets or remote services.
        if re.search(r'https?://(?!localhost[:/\"]|127\.0\.0\.1[:/\"])[^\s\"]+',rendered):
            raise ValueError('Supabase configuration contains a non-local URL. Review it before local setup.')
        digest=hashlib.sha256(json.dumps(files,sort_keys=True).encode()+rendered.encode()).hexdigest()
        if previous.get('schemaDigest') and previous['schemaDigest']!=digest:
            raise ValueError('Local schema/config changed. Review and migrate the existing local database before restarting; its data was preserved.')
        work=target/'workspace';destination=work/'supabase';destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        for name,content in files.items():
            p=destination/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
        (destination/'config.toml').write_text(rendered)
        return work,ports,digest,warnings
    finally:
        for s in sockets:s.close()


def toml(data):
    lines=[]
    def table(obj,path):
        if path:lines.append('['+'.'.join(json.dumps(x) for x in path)+']')
        for k,v in obj.items():
            if isinstance(v,dict):continue
            if isinstance(v,list) and any(isinstance(x,dict) for x in v):raise ValueError('Array-of-table Supabase config needs review.')
            lines.append(json.dumps(k)+' = '+json.dumps(v))
        for k,v in obj.items():
            if isinstance(v,dict):table(v,[*path,k])
    table(data,[])
    result='\n'.join(lines)+'\n';tomllib.loads(result);return result


def credentials(status,names,api_port):
    url=status.get('API_URL','')
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.port!=api_port:
        raise ValueError('Supabase returned a non-local or unexpected API address.')
    for k in ('ANON_KEY','SERVICE_ROLE_KEY'):
        if not isinstance(status.get(k),str) or not status[k] or status[k]=='undefined':raise ValueError('Supabase did not return valid local credentials.')
    status={**status,'STORAGE_URL':url.rstrip('/')+'/storage/v1'}
    if status.get('DB_URL'):
        db=urlsplit(status['DB_URL'])
        if db.hostname not in ('127.0.0.1','localhost'):raise ValueError('Supabase returned a non-local database URL.')
    mapped=mapping(names)
    if not any(v=='API_URL' for v in mapped.values()):raise ValueError('Cannot map the app’s Supabase URL variable. Add a supported variable mapping before local setup.')
    return {name:status[k] for name,k in mapped.items() if k in status}


def launch_values(root):
    _,target=location(str(root));state=read_state(target)
    if not state.get('selected'):return {}
    if state.get('status')!='ready':raise ValueError('Local Supabase is stopped or needs attention. Resume it in Environment check.')
    # Override any repository or saved production Supabase values on every launch.
    values=json.loads(PE.read(target/'credentials.json'))
    names={row['name'] for row in PE.scan(str(root))['variables']}
    unknown=[n for n in names if 'SUPABASE' in n and re.search(r'(URL|KEY)',n) and n not in mapping(names)]
    if unknown:raise ValueError('Review unmapped Supabase configuration before launching locally: '+', '.join(sorted(unknown)))
    return {**{n:'' for n in names if 'SUPABASE' in n},**values,'NEXT_PUBLIC_SUPABASE_ENV':'PROD'}


def inspect(path,repository_root=None):
    root,target=location(path)
    try:facts=detect(path,repository_root)
    except ValueError:
        facts={'available':False,'reason':'The repository’s Supabase configuration could not be parsed.'}
    state=read_state(target)
    if state.get('status')=='working':
        handle=lease(target/'operation.lock')
        if handle is not None:
            handle.close();state.update(status='needs_input',reason='Setup was interrupted. Resume to continue; local data is preserved.')
    return {**facts,**state,'ok':True,'path':str(root)}


def start(path,repository_root=None,action='start'):
    root,target=location(path)
    if action not in ('start','stop'):raise ValueError('Unknown local services action.')
    facts=detect(path,repository_root)
    if not facts['available']:raise ValueError('No local Supabase configuration found.')
    with _LOCK:
        if str(target) in _JOBS:return inspect(path,repository_root)
        handle=lease(target/'operation.lock')
        if handle is None:return inspect(path,repository_root)
        state=read_state(target);state.update(selected=True,status='working',reason='Stopping local services' if action=='stop' else 'Checking local Supabase prerequisites')
        write_state(target,state);_JOBS[str(target)]=True
    def worker():
        install_handle=None
        def update(**patch):
            with _LOCK:state.update(patch);write_state(target,state)
        try:
            if action=='stop':
                cli=RT.home()/'supabase-tools'/('supabase.exe' if os.name=='nt' else 'supabase')
                if (target/'workspace/supabase/config.toml').is_file():
                    owned=tomllib.loads(PE.read(target/'workspace/supabase/config.toml'))
                    if owned.get('project_id')!='engelbart-'+target.name:raise ValueError('Local service identity changed; refusing to stop another project.')
                    command([cli,'stop','--workdir',target/'workspace'],target,local_env(target),180)
                update(status='stopped',reason='Local services stopped. Database data is preserved.');return
            names={r['name'] for r in PE.scan(str(root))['variables']}
            if not any(v=='API_URL' for v in mapping(names).values()):raise ValueError('No supported Supabase URL variable was detected; map the app’s configuration first.')
            work,ports,digest,warnings=prepare(root,target,Path(facts['source']))
            update(ports=ports,schemaDigest=digest,warnings=warnings)
            with _INSTALL_LOCK:
                install_handle=lease(RT.home()/'supabase-tools/install.lock')
                if install_handle is None:raise ValueError('Another local setup is installing tools. Resume when that installation completes.')
                try:env=docker_env()
                except DockerUnavailable:
                    update(reason='Installing or opening Docker. Complete any system or first-launch prompts.')
                    install_docker()
                    try:env=docker_env()
                    except DockerUnavailable:
                        update(status='needs_input',reason='Complete Docker’s first-launch prompts and start its local engine, then Resume. On Linux, your account needs access to the Docker socket; a new login may be required.');return
                update(reason='Installing the verified Supabase CLI')
                cli=install_cli()
                install_handle.close();install_handle=None
            update(reason='Starting local Supabase and applying repository migrations. The first run downloads container images.')
            env=local_env(target)
            command([cli,'start','--workdir',work],target,env,900)
            raw=command([cli,'status','--output','json','--workdir',work],target,env)
            # CLI diagnostics may precede JSON; accept only a full JSON object suffix.
            start_index=raw.find('{');status=json.loads(raw[start_index:])
            values=credentials(status,names,ports['api.port'])
            fd,name=tempfile.mkstemp(dir=target)
            with os.fdopen(fd,'w') as f:json.dump(values,f)
            os.replace(name,target/'credentials.json')
            PE.save(str(root),values)
            update(status='ready',configured=True,reason='Local Supabase is ready. Local credentials are saved for this app.',variableNames=sorted(values))
        except Exception as exc:
            # Do not return raw subprocess output or credential-bearing exception data.
            update(status='needs_input',reason=str(exc)[:700] if isinstance(exc,ValueError) else 'Local setup failed. Check Docker and repository configuration, then Resume.')
        finally:
            with _LOCK:_JOBS.pop(str(target),None)
            handle.close()
            if install_handle is not None:install_handle.close()
    threading.Thread(target=worker,daemon=True).start()
    return inspect(path,repository_root)
