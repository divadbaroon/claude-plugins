"""Staged, bounded dataset collections. File bytes never pass through JSON or a model."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import unicodedata
import uuid
from . import project_store as PS


def policy():
    def setting(name, default):
        return max(1, int(os.environ.get(name, default)))
    return {'maxBytes': setting('HC_DATASET_MAX_BYTES', 8 * 1024**3),
            'maxFileBytes': setting('HC_DATASET_MAX_FILE_BYTES', 1024**3),
            'maxFiles': setting('HC_DATASET_MAX_FILES', 5000)}


def relative(value):
    value = unicodedata.normalize('NFC', str(value))
    parts = value.split('/')
    if (not value or len(value) > 500 or any(p in ('', '.', '..') for p in parts)
            or re.search(r'[\\:\x00-\x1f]', value)
            or any(p.endswith((' ', '.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p) for p in parts)):
        raise ValueError('Unsafe dataset relative path')
    return value


def location(cwd, rid):
    if not re.fullmatch(r'import-[a-f0-9]{32}', rid): raise ValueError('Invalid import session')
    base = Path(cwd).resolve()
    folder = base / '.engelbart-resources'
    if folder.is_symlink(): raise ValueError('Dataset storage cannot be a symlink')
    folder.mkdir(mode=0o700, exist_ok=True)
    folder = folder / rid
    if folder.is_symlink(): raise ValueError('Import cannot be a symlink')
    return folder


def secure_file(folder, name):
    if folder.is_symlink(): raise ValueError('Dataset symlinks are not allowed')
    target = folder
    for segment in relative(name).split('/'):
        target = target / segment
        if target.is_symlink(): raise ValueError('Dataset symlinks are not allowed')
        if target.exists() and not (target.is_file() or target.is_dir()): raise ValueError('Special files are not allowed')
    return target


def lock(root, cwd, rid='resources'):
    from . import chat_state as CS
    return CS.session_lock(rid + '-' + hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:24], root, wait_s=10)


def write_json(path, value):
    if path.is_symlink(): raise ValueError('Unsafe metadata file')
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.replace(tmp, path)


def read_session(cwd, rid):
    folder = location(cwd, rid)
    path = secure_file(folder, 'session.json')
    if path.stat().st_size > 8 * 1024**2: raise ValueError('Import metadata exceeds policy')
    value = json.loads(path.read_text(encoding='utf-8'))
    if time.time() - value['createdAt'] > 86400 and value['status'] != 'ready':
        raise ValueError('Import session expired; choose the folder again')
    return folder, value


def begin(root, cwd, name, files, source=None):
    cleanup(root,cwd)
    limits = policy()
    if not isinstance(files, list) or not files or len(files) > limits['maxFiles']: raise ValueError('Dataset file count exceeds configured local policy')
    entries, seen, total = [], set(), 0
    for item in files:
        p = relative(item.get('path', ''))
        if Path(p).name in ('.DS_Store', 'Thumbs.db'): continue
        key = p.casefold()
        if key in seen or any('/'.join(p.split('/')[:i]).casefold() in seen for i in range(1,len(p.split('/')))):
            raise ValueError('Duplicate normalized dataset path')
        seen.add(key)
        size = item.get('size')
        if type(size) is not int or size < 0 or size > limits['maxFileBytes']: raise ValueError('Dataset file exceeds configured local policy')
        total += size
        if total > limits['maxBytes']: raise ValueError('Dataset collection exceeds configured local policy')
        entries.append({'path':p, 'size':size, 'format':Path(p).suffix.lower().lstrip('.')})
    if not entries: raise ValueError('No dataset files were selected')
    # Catch file/directory conflicts regardless of enumeration order.
    for e in entries:
        if any('/'.join(e['path'].split('/')[:i]).casefold() in seen for i in range(1,len(e['path'].split('/')))):
            raise ValueError('Dataset file conflicts with directory')
    if shutil.disk_usage(cwd).free < total + 256*1024**2: raise ValueError('Not enough local disk space for this collection')
    rid = 'import-' + uuid.uuid4().hex
    folder = location(cwd, rid); folder.mkdir(mode=0o700)
    (folder / 'files').mkdir(mode=0o700)
    value = {'id':rid, 'name':str(name or 'Dataset')[:200], 'status':'acquiring', 'createdAt':time.time(),
             'source':source or {'type':'local_folder' if len(entries)>1 else 'local_file'}, 'files':entries, 'totalBytes':total, 'uploaded':{}}
    write_json(folder/'session.json', value)
    ignore = Path(cwd)/'.gitignore'
    if ignore.is_symlink(): raise ValueError('Unsafe project ignore file')
    with lock(root,cwd):
        old = ignore.read_text() if ignore.exists() else ''
        if '/.engelbart-resources/' not in old.splitlines(): ignore.write_text(old + ('\n' if old and not old.endswith('\n') else '') + '/.engelbart-resources/\n')
    return {'id':rid, 'status':'acquiring', 'policy':limits, 'fileCount':len(entries), 'totalBytes':total}


def put(root, cwd, rid, name, stream, size):
    from . import resources as R
    name = relative(name)
    with lock(root,cwd,rid):
        folder, session = read_session(cwd,rid)
        if session['status'] != 'acquiring': raise ValueError('Import no longer accepts files')
        entry = next((e for e in session['files'] if e['path']==name), None)
        if entry is None or size != entry['size']: raise ValueError('File size/path differs from import manifest')
        path = secure_file(folder/'files', name); path.parent.mkdir(parents=True,exist_ok=True)
        if name in session['uploaded']:
            # Retrying is explicit; never trust a partial transfer as a complete file.
            raise ValueError('File already uploaded; resume using import status')
        tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
        remaining, digest, started = size, hashlib.sha256(), time.monotonic()
        try:
            fd = os.open(tmp, os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            with os.fdopen(fd,'wb') as dest:
                while remaining:
                    chunk = stream.read(min(65536,remaining))
                    if not chunk: raise ValueError('Incomplete file transfer; retry this file')
                    if len(chunk)>remaining or time.monotonic()-started>1800: raise ValueError('Dataset transfer exceeded local policy')
                    dest.write(chunk); digest.update(chunk); remaining-=len(chunk)
            os.replace(tmp,path)
            session['uploaded'][name] = digest.hexdigest()
            write_json(folder/'session.json',session)
        finally:
            if tmp.exists(): tmp.unlink()
    return {'ok':True,'path':name}


def status(cwd,rid):
    _, s = read_session(cwd,rid)
    return {'id':rid,'status':s['status'],'uploaded':list(s['uploaded']), 'error':s.get('error',''), 'resource':s.get('resource')}


def cancel(root,cwd,rid):
    with lock(root,cwd,rid):
        folder,s=read_session(cwd,rid)
        if s['status']=='ready': return {'ok':True}
        # Never delete an activated resource, even when a caller races completion.
        shutil.rmtree(folder/'files')
        s.update(status='failed',error='Import cancelled');write_json(folder/'session.json',s)
    return {'ok':True}


def prepare_manifest(cwd, folder, session, files_root=None):
    from . import resources as R
    files, inspected, directories = [], [], set()
    for entry in session['files']:
        p = secure_file(files_root or folder/'files',entry['path'])
        if not p.is_file() or p.stat().st_size != entry['size']: raise ValueError('Dataset file is missing or incomplete')
        details = dict(entry, sha256=session.get('uploaded', {}).get(entry['path'], ''))
        parts=entry['path'].split('/')
        directories.update('/'.join(parts[:i]) for i in range(1,len(parts)))
        if p.suffix.lower() in R.UPLOAD_FORMATS:
            if len(inspected)<24:
                try:
                    info=R.inspect_table(p)
                    inspected.append(dict(info,path=str(p if files_root else p.relative_to(cwd)),relativePath=entry['path']))
                    details.update(role='table',schema=info['columns'],rowCount=info.get('rowCount'))
                except Exception as exc:
                    details.update(role='table',inspection='Could not inspect ('+type(exc).__name__+')')
            else: details.update(role='table',inspection='Available for incremental inspection')
        else:
            details['role']='metadata' if re.search('readme|metadata|datasheet',p.name,re.I) else 'artifact'
            if details['role']=='metadata' and p.suffix.lower() in ('.md','.txt','.yaml','.yml'):
                with p.open(encoding='utf-8',errors='replace') as stream: details['metadata']={'excerpt':stream.read(2048)}
        files.append(details)
    if not inspected: raise ValueError('Could not read this spreadsheet.' if len(files)==1 and files[0]['format']=='xlsx' else 'Could not inspect any supported data file; original dataset remains active')
    # Full metadata lives beside the files, not in normal project/model context.
    manifest={'version':1,'root':session['name'],'fileCount':len(files),'folderCount':len(directories),
              'totalBytes':session['totalBytes'],'directories':sorted(directories),'files':files}
    return manifest,inspected


def finish(root,cwd,rid,activate=True):
    from . import resources as R
    cwd=Path(cwd).resolve()
    with lock(root,cwd,rid):
        folder,s=read_session(cwd,rid)
        if s['status']=='ready': return s['resource']
        if len(s['uploaded']) != len(s['files']): raise ValueError('Import is incomplete; resume file transfer before preparation')
        try:
            manifest, inspected=prepare_manifest(cwd,folder,s)
            digest=hashlib.sha256(json.dumps([(e['path'],e['size'],e['sha256']) for e in sorted(manifest['files'],key=lambda e:e['path'])],ensure_ascii=False).encode()).hexdigest()
            write_json(folder/'manifest.json',manifest)
            summary={k:v for k,v in manifest.items() if k not in ('files','directories')}
            summary['files']=[{k:v for k,v in f.items() if k not in ('schema',)} for f in manifest['files'][:64]]
            summary['previewTruncated']=len(manifest['files'])>64
            selected=[]
            for info in inspected[:2]:
                if len(json.dumps(selected+[info],ensure_ascii=False))<21000: selected.append(info)
            r={'id':rid,'kind':'dataset','name':s['name'],'status':'ready','error':'', 'source':s['source'],
               'manifest':summary,'metadata':{'files':selected,'sha256':digest},
               'access':{'localPath':str((folder/'files').relative_to(cwd)),'manifestPath':str((folder/'manifest.json').relative_to(cwd)),
                         'primaryFiles':[i['path'] for i in selected], 'originalFile':str((folder/'files'/s['files'][0]['path']).relative_to(cwd))},
               'provenance':{'providedBy':'user','uploadedAt':s['createdAt'],'manifestDigest':digest}}
            if not activate:
                s.update(status='ready',resource=r);write_json(folder/'session.json',s)
                return r
            with lock(root,cwd):
                project=PS.load_project(root,cwd);records=project.get('resources') or []
                duplicate=next((x for x in records if x.get('metadata',{}).get('sha256')==digest and R.cached_ready(cwd,x)),None)
                if duplicate:
                    r=duplicate
                    replacements=[x for x in records if x['kind']=='dataset' and x['id']!=r['id'] and x['status'] in ('selected','needs_user','failed')]
                    r.setdefault('provenance',{})['replaces']=[{'id':x['id'],'name':x['name'],'source':x.get('source',{}),'fallbackOf':x.get('provenance',{}).get('fallbackOf')} for x in replacements[:6]]
                else:
                    if len(records)>=12: raise ValueError('Project resource history is full')
                    replaced=[x for x in records if x['kind']=='dataset' and (x['id']==project.get('activeDatasetId') or x['status'] in ('selected','needs_user','failed'))]
                    r['provenance']['replaces']=[{'id':x['id'],'name':x['name'],'source':x.get('source',{}),'fallbackOf':x.get('provenance',{}).get('fallbackOf')} for x in replaced[:6]]
                    records.append(r)
                PS.save_project(root,cwd,{'resources':records,'activeDatasetId':r['id']})
            # Activation is the commit point; a failed receipt write cannot make
            # an already committed import appear failed to its caller.
            s.update(status='ready',resource=r)
            try:
                write_json(folder/'session.json',s)
                if r['id'] != rid: shutil.rmtree(folder/'files')
            except OSError: pass
            return r
        except Exception as exc:
            s.update(status='failed',error=str(exc)[:300]);write_json(folder/'session.json',s)
            return {'id':rid,'kind':'dataset','name':s['name'],'status':'failed','error':s['error'],'source':s['source'],'metadata':{},'access':{},'provenance':{}}


def single(root,cwd,name,stream,size):
    from . import resources as R
    if Path(name).suffix.lower() not in R.UPLOAD_FORMATS: raise ValueError('Upload a CSV, TSV, Parquet, XLSX or JSON file')
    if size<=0: raise ValueError('The file is empty')
    name=relative(name)
    if '/' in name: raise ValueError('Use a single filename')
    s=begin(root,cwd,name,[{'path':name,'size':size}],{'type':'local_file','kind':'upload','originalFilename':name})
    put(root,cwd,s['id'],name,stream,size)
    return finish(root,cwd,s['id'])


def acquire(root,cwd,resource,fetch):
    """Acquire only manifest-listed files under the selected provider root."""
    from . import resources as R
    from urllib.parse import quote
    source=resource['source']; manifest=resource.get('manifest') or {}
    files=manifest.get('files') or []
    if manifest.get('truncated') or len(files)!=manifest.get('fileCount'):
        raise R.NeedsUser('Needs local folder: remote manifest is incomplete')
    if source.get('provider') not in ('github','anonymous_github','supabase'):
        raise R.NeedsUser('Needs local folder: this provider has no collection downloader')
    root_path=source.get('rootPath') or ''
    if root_path: relative(root_path)
    repo=source.get('repo') or ''
    if source['provider']=='github':
        if not re.fullmatch(r'[\w.-]+/[\w.-]+',repo) or not re.fullmatch(r'[a-fA-F0-9]{40}',source.get('commit','')):
            raise R.NeedsUser('Needs local folder: GitHub revision is not pinned')
    elif source['provider']=='anonymous_github' and not re.fullmatch(r'[\w.-]+',repo): raise ValueError('Invalid anonymous repository identifier')
    if any(type(f.get('size')) is not int for f in files): raise R.NeedsUser('Needs local folder: provider did not supply bounded file sizes')
    started=begin(root,cwd,resource['name'],files,source); rid=started['id']
    folder,_=read_session(cwd,rid)
    try:
        for file in files:
            path=relative(file['path']); full='/'.join(filter(None,[root_path,path]))
            if source['provider']=='github': url=f"https://raw.githubusercontent.com/{repo}/{source['commit']}/{quote(full,safe='/')}"
            elif source['provider']=='anonymous_github': url=f"https://anonymous.4open.science/api/repo/{quote(repo,safe='')}/file/{quote(full,safe='/')}"
            else:
                url=file.get('downloadUrl') or ''
                if not url: raise R.NeedsUser('Dataset download link expired or is missing; retry setup or upload the folder locally')
            temp=folder/'remote.part'
            fetch(url,temp,min(file['size'] or 1,policy()['maxFileBytes']))
            if temp.stat().st_size!=file['size']: raise ValueError('Remote collection file size changed')
            if source['provider']=='github' and file.get('sha'):
                digest=hashlib.sha1(('blob '+str(file['size'])+'\0').encode())
                with temp.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(65536),b''): digest.update(chunk)
                if digest.hexdigest()!=file['sha']: raise ValueError('Remote file does not match pinned GitHub manifest')
            with temp.open('rb') as stream: put(root,cwd,rid,path,stream,file['size'])
            temp.unlink()
        out=finish(root,cwd,rid,activate=False)
        if out['status']!='ready': raise ValueError(out['error'])
        out.update(id=resource['id'],provenance={**resource.get('provenance',{}),'acquiredFrom':source,'manifestDigest':out['metadata']['sha256']})
        return out
    except Exception:
        cancel(root,cwd,rid)
        raise


def relevant_files(cwd, resource, project):
    """Deterministic, bounded attention hints, recomputed as the experiment changes."""
    from . import resources as R
    manifest=resource.get('manifest') or {}
    try:
        p=R.safe_path(cwd,resource.get('access',{}).get('manifestPath',''))
        if p.is_file() and p.stat().st_size<=8*1024**2: manifest=json.loads(p.read_text(encoding='utf-8'))
    except (ValueError,OSError): pass
    task={k:v for k,v in project.items() if k not in ('resources','activeDatasetId')}
    words=set(re.findall(r'[\w]{3,}',json.dumps(task,ensure_ascii=False)[:20000].lower()))
    candidates=[f for f in manifest.get('files',[]) if f.get('role')=='table' or Path(f.get('path','')).suffix in R.UPLOAD_FORMATS]
    def score(f):
        tokens=set(re.findall(r'[\w]{3,}',(f['path']+' '+json.dumps(f.get('schema',[]))).lower().replace('_',' ')))
        return len(tokens & words)
    candidates.sort(key=lambda f:(-score(f),f.get('size') or 0,f['path']))
    return [{'path':f['path'],'columns':f.get('schema',[])[:12],
             'reason':'Matches current experiment terms' if score(f) else 'Small supported table to inspect first; relevance is not yet established'} for f in candidates[:4]]


def json_sample(stream):
    """Read the first ten records of a large JSON array with a bounded buffer."""
    decoder=json.JSONDecoder(); buffer=stream.read(65536).lstrip(); out=[]
    if not buffer.startswith('['): raise ValueError('Expected a JSON array of records')
    buffer=buffer[1:]
    while len(out)<10:
        buffer=buffer.lstrip()
        if buffer.startswith(']'): return out
        try:
            value,end=decoder.raw_decode(buffer)
            if not isinstance(value,dict): raise ValueError('Expected JSON object records')
        except json.JSONDecodeError:
            if len(buffer)>1024**2: raise ValueError('JSON record exceeds bounded inspection policy')
            more=stream.read(65536)
            if not more: raise ValueError('Incomplete JSON record')
            buffer+=more;continue
        out.append(value);buffer=buffer[end:].lstrip()
        if buffer.startswith(','): buffer=buffer[1:]
        elif buffer.startswith(']'): return out
        elif len(out)<10:
            more=stream.read(65536)
            buffer+=more
            buffer=buffer.lstrip()
            if buffer.startswith(','): buffer=buffer[1:]
            elif buffer.startswith(']'): return out
            else: raise ValueError('Malformed JSON array')
    return out


def cleanup(root,cwd):
    """Quarantine expired, unreferenced staging data on the next import."""
    base=Path(cwd).resolve()/'.engelbart-resources'
    if base.is_symlink(): raise ValueError('Dataset storage cannot be a symlink')
    if not base.exists(): return
    records=PS.load_project(root,cwd).get('resources') or []
    active_paths=[r.get('access',{}).get('localPath','') for r in records]
    for folder in list(base.glob('import-*'))[:100]:
        if folder.is_symlink() or not re.fullmatch('import-[a-f0-9]{32}',folder.name): continue
        if any(p.startswith('.engelbart-resources/'+folder.name+'/') for p in active_paths): continue
        with lock(root,cwd,folder.name):
            meta=secure_file(folder,'session.json')
            if not meta.is_file() or meta.stat().st_size>8*1024**2: continue
            session=json.loads(meta.read_text(encoding='utf-8'))
            if session['status']=='ready' or time.time()-session['createdAt']<86400: continue
            files=folder/'files'
            if files.is_symlink(): raise ValueError('Unsafe staging directory')
            if files.exists(): shutil.rmtree(files)
            session.update(status='failed',error='Import expired; choose the folder again')
            write_json(meta,session)


def local_root(value):
    value = str(value)
    if not value or len(value)>2000 or any(ord(c)<32 for c in value): raise ValueError('Invalid local dataset path')
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or path.is_symlink(): raise ValueError('Use an absolute dataset folder path, not a symlink')
    path = path.resolve()
    if not path.is_dir():
        from .resources import NeedsUser
        raise NeedsUser('Local dataset folder is missing on this computer. Restore it or supply the folder in Dataset.')
    if path == Path(path.anchor): raise ValueError('Choose a dataset folder, not a filesystem root')
    return path


def link_local(root, cwd, resource):
    """Inspect an explicitly supplied folder in place; never copy or upload its bytes."""
    folder = local_root(resource['source'].get('path', ''))
    limits = policy(); entries = []; total = 0; seen = set(); visited = 0
    for parent, dirs, names in os.walk(folder, followlinks=False):
        visited += 1
        if visited > limits['maxFiles']: raise ValueError('Dataset folder count exceeds local policy')
        dirs.sort(); names.sort()
        for name in dirs + names:
            p = Path(parent)/name
            if p.is_symlink(): raise ValueError('Dataset symlinks are not allowed')
            if not (p.is_dir() or stat.S_ISREG(p.lstat().st_mode)): raise ValueError('Special dataset files are not allowed')
        for name in names:
            if name in ('.DS_Store','Thumbs.db'): continue
            p = Path(parent)/name; rel = relative(p.relative_to(folder).as_posix())
            if rel.casefold() in seen: raise ValueError('Duplicate normalized dataset path')
            seen.add(rel.casefold()); size = p.stat().st_size; total += size
            if size > limits['maxFileBytes'] or total > limits['maxBytes'] or len(entries) >= limits['maxFiles']:
                raise ValueError('Dataset exceeds configured local policy')
            entries.append({'path':rel,'size':size,'format':p.suffix.lower().lstrip('.')})
    metadata_folder = Path(cwd)/'.engelbart-resources'/resource['id']
    manifest, inspected = prepare_manifest(Path(cwd), metadata_folder, {'files':entries,'name':resource['name'],'totalBytes':total}, files_root=folder)
    write_json(metadata_folder/'manifest.json', manifest)
    selected=[]
    for info in inspected[:2]:
        if len(json.dumps(selected+[info],ensure_ascii=False))<21000: selected.append(info)
    if not selected: raise ValueError('No bounded dataset preview could be prepared')
    summary={k:v for k,v in manifest.items() if k not in ('files','directories')}
    summary.update(files=manifest['files'][:64],previewTruncated=len(entries)>64)
    return {'status':'ready','error':'','source':dict(resource['source'],path=str(folder)),
            'manifest':summary,'metadata':{'files':selected},
            'access':{'localPath':str(folder),'linked':True,'manifestPath':str((metadata_folder/'manifest.json').relative_to(cwd)),
                      'primaryFiles':[i['path'] for i in selected]}}
