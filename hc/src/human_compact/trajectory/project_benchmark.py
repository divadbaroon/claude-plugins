"""Local benchmark bookkeeping around the Projects controller; never executes CSV cells."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

HEADERS = ['Git repo URL', 'Paper DOI', 'Paper Keyword(s)', 'What is in the git repo', 'Types of dependencies in the git repo']
MAX_CSV = 1024 * 1024
MAX_CASES = 500
MAX_EVENTS = 40
MAX_EVENT_BYTES = 48000
MAX_FILES = 24
MAX_BATCH_BYTES = 8 * 1024 * 1024
_LOCK = threading.RLock()
_SECRETS = set()
# Preserve protocol keys, while treating arbitrary metadata/trace keys as content.
_SCHEMA_KEYS = set('''id operation stage status request response run order ok error reason
command cwd root path source sourceUrl repoUrl healthy analysisId variables name kind group
requirement blocksContinuation evidence file line publicValues values envValues secretValues
startedAt finishedAt durationSeconds started_at exitCode stdout stderr attempts attempt agentTrace
prompt promptChars responseTruncated provider model effort tools phase timeoutSeconds suppliedFiles
requestedFiles followupReason outcome components requiresContainer framework environmentPaths result
firstFailure actualCommit reviewedCommit matchesReviewedCommit trackedFilesDirty note variableNames
execution requestedRunId observedRunId joinedExistingRun metadata at clientOutcome
'''.split()) | set(HEADERS)
OPERATIONS = {'discover_project_components':'checkout_discovery', 'analyze_project':'assessment',
    'project_order_start':'run_order', 'project_order_state':'run_order',
    'inspect_project_environment':'environment', 'save_project_environment':'environment',
    'project_run_start':'run', 'project_run_state':'run', 'project_run_reset':'reset',
    'project_run_approval':'repair', 'benchmark_client_outcome':'controller'}


def _key(value):
    return re.sub(r'[^a-z0-9]', '', value.lower())


def tags(value):
    return [re.sub(r'[\s-]+','_', part.strip().lower()) for part in value.split(';') if part.strip()]


def repository(value):
    u=urlsplit(value)
    if (u.scheme!='https' or u.netloc!='github.com' or u.username or u.password or u.query or u.fragment
            or not re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+/?',u.path)):
        raise ValueError('Use a GitHub HTTPS owner/repository URL without credentials, query, or fragment.')
    owner,repo=u.path.strip('/').split('/')
    repo=re.sub(r'\.git$','',repo)
    if repo in ('','.', '..'):raise ValueError('Repository name is empty or invalid.')
    return 'https://github.com/'+owner+'/'+repo


def parse_csv(text, name='uploaded.csv'):
    if not isinstance(text,str) or len(text.encode())>MAX_CSV:raise ValueError('Choose a CSV under 1 MB.')
    reader=csv.reader(io.StringIO(text.lstrip('\ufeff'),newline=''),strict=True)
    try:headers=next(reader)
    except (StopIteration,csv.Error):raise ValueError('CSV needs a header row.')
    keys=[_key(h) for h in headers]
    aliases={'gitrepourl':('gitrepourl','repositoryurl','repourl'), 'paperkeywords':('paperkeywords','paperkeyword'),
             'whatisinthegitrepo':('whatisinthegitrepo','artifacttype','artifacttypes'),
             'typesofdependenciesinthegitrepo':('typesofdependenciesinthegitrepo','dependencies','dependencytypes')}
    indices=[]
    for h in HEADERS:
        matches=[i for i,k in enumerate(keys) if k in aliases.get(_key(h),(_key(h),))]
        if len(matches)!=1:raise ValueError('CSV requires exactly one column: '+h)
        indices.append(matches[0])
    if len(set(keys))!=len(keys):raise ValueError('CSV has duplicate header names.')
    cases=[]
    while True:
        line=reader.line_num+1
        try:values=next(reader)
        except StopIteration:break
        except csv.Error as exc:raise ValueError(f'CSV row {line}: {exc}') from exc
        if not values or all(not v.strip() for v in values):continue
        if len(values)!=len(headers):raise ValueError(f'CSV row {line}: expected {len(headers)} cells, found {len(values)}.')
        if len(cases)>=MAX_CASES:raise ValueError(f'CSV row {line}: at most {MAX_CASES} cases per dataset.')
        fields=[values[i].strip() for i in indices]
        try:url=repository(fields[0])
        except ValueError as exc:raise ValueError(f'CSV row {line}: {exc}') from exc
        cid=hashlib.sha256(json.dumps([line,values],ensure_ascii=False).encode()).hexdigest()[:24]
        cases.append({'id':cid,'row':line,'repoUrl':url,'paperDoi':fields[1], 'keywords':tags(fields[2]),
                      'artifactTypes':tags(fields[3]),'dependencies':tags(fields[4]),
                      'metadata':dict(zip(headers,values))})
    if not cases:raise ValueError('CSV contains no cases.')
    digest=hashlib.sha256(text.encode()).hexdigest()
    return {'id':digest,'sha256':digest,'name':str(name)[:200],'cases':cases,'createdAt':time.time()}


def folder():
    path=Path(os.environ.get('HUMAN_COMPACT_HOME') or Path.home()/'.human-compact')/'project-benchmarks'
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    return path


def _read(id):
    if not isinstance(id,str) or not re.fullmatch(r'[a-f0-9]{32}|[a-f0-9]{64}',id):raise ValueError('Unknown benchmark identity.')
    try:return json.loads((folder()/(id+'.json')).read_text())
    except (OSError,ValueError) as exc:raise ValueError('Benchmark record is unavailable.') from exc


def _save(record):
    if 'selection' in record:
        _bound_batch(record)
    fd,path=tempfile.mkstemp(dir=folder())
    try:
        with os.fdopen(fd,'w') as f:json.dump(record,f)
        os.replace(path,folder()/(record['id']+'.json'))
    finally:
        if os.path.exists(path):os.unlink(path)
    # Retain at most 24 datasets and 24 cohorts independently. Cohorts embed their cases.
    files=sorted((p for p in folder().glob('*.json') if len(p.stem)==len(record['id'])), key=lambda p:p.stat().st_mtime,reverse=True)
    for old in files[MAX_FILES:]:old.unlink(missing_ok=True)


def _bound_batch(batch):
    """Bound the whole file, not merely each polling response. Preserve cohort and first failure."""
    batch['evidenceLimits']['batchBytes']=MAX_BATCH_BYTES
    while len(json.dumps(batch).encode())>MAX_BATCH_BYTES:
        candidates=[event for case in batch['cases'] for event in case['events'] if len(json.dumps(event.get('response',{})))>1800]
        if candidates:
            event=max(candidates,key=lambda e:len(json.dumps(e['response'])))
            response=event['response']
            event['response']={'ok':response.get('ok'),'evidenceTruncated':True,'reason':'cohort byte budget',
                               'excerpt':json.dumps(response)[:1200]}
            continue
        case=max(batch['cases'],key=lambda c:len(c['events']))
        if len(case['events'])<=2:raise ValueError('Benchmark metadata exceeds the local report byte limit.')
        case['events'].pop(1);case['droppedEvents']+=1
    batch['evidenceLimits']['batchBytes']=MAX_BATCH_BYTES


def import_csv(text,name='uploaded.csv'):
    with _LOCK:
        data=parse_csv(text,name); _save(data); return data


def create_batch(dataset_id,ids,filters=None):
    with _LOCK:
        data=_read(dataset_id)
        if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):raise ValueError('Select at least one distinct case.')
        lookup={c['id']:c for c in data['cases']}
        if any(id not in lookup for id in ids):raise ValueError('Selection contains an unknown case.')
        batch={'schemaVersion':1,'id':uuid.uuid4().hex,'createdAt':time.time(),
               'host':{'system':platform.system(),'release':platform.release(),'machine':platform.machine(),'python':platform.python_version()},
               'dataset':{k:data[k] for k in ('id','name','sha256')},
               'selection':{'ids':ids,'cohortSize':len(data['cases']),'filters':sanitize(filters or {})},
               'cases':[{**lookup[id],'events':[],'outcome':'pending','droppedEvents':0} for id in ids],
               'evidenceLimits':{'eventsPerCase':MAX_EVENTS,'bytesPerEvent':MAX_EVENT_BYTES,'retainedCohorts':MAX_FILES,
                  'missing':'Controller observations only. Checkout/discovery share one boundary. Subprocess timings and logs appear only when exposed by the existing controller. Unfinished boundary calls may be interrupted. Scientific correctness is not assessed.'}}
        _save(batch);return batch


def _case(batch,case_id):
    for case in batch['cases']:
        if case['id']==case_id:return case
    raise ValueError('Case is outside this benchmark selection.')


def _collect_secrets(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if _key(str(key)) in {'values','envvalues','publicvalues','secretvalues','environmentvalues'} and isinstance(item,dict):
                _SECRETS.update(str(v) for v in item.values() if isinstance(v,(str,int,float)) and str(v))
            else:_collect_secrets(item)
    elif isinstance(value,list):
        for item in value:_collect_secrets(item)


def _saved_secrets():
    # Submitted public values are private too. Reload from authoritative environment storage
    # after server restart; values remain in memory and never enter benchmark files.
    from . import project_environment as PE
    home=folder().parent/'project-environments'
    for file in home.glob('*.json'):
        try:
            values=json.loads(PE.read(file))
            if isinstance(values,dict):_SECRETS.update(str(v) for v in values.values() if isinstance(v,str) and v)
        except (OSError,ValueError):pass


def sanitize(value):
    """Recursively omit value maps and apply existing secret-pattern scrubber, with bounds."""
    from .project_setup import scrub
    variants=set(_SECRETS)
    for secret in _SECRETS:
        variants.add(json.dumps(secret)[1:-1])
        variants.add(json.dumps(secret,ensure_ascii=False)[1:-1])
    replacements=sorted(variants,key=len,reverse=True)
    def mask(text):
        for secret in replacements:
            if len(secret)<4:
                text=re.sub(r'(?<![\w])'+re.escape(secret)+r'(?![\w])','[REDACTED]',text)
            else:text=text.replace(secret,'[REDACTED]')
        return text
    enums={'operation':set(OPERATIONS),'stage':set(OPERATIONS.values())|{'install','build','start','health','plan','validation','compatibility','error'},
           'status':{'pending','running','done','failed','error','ready','needs_input','unsupported','awaiting_approval','stopped','found','missing','optional'}}
    def walk(item,depth=0,field=None):
        if depth>16:return '[truncated: nesting limit]'
        if isinstance(item,str):
            if item in enums.get(field,set()):return item
            text=scrub(item,mask)
            return text if len(text)<=8000 else text[:8000]+' [truncated: string limit]'
        if isinstance(item,dict):
            out={}
            for key,v in list(item.items())[:160]:
                if _key(str(key)) in {'values','envvalues','publicvalues','secretvalues','environmentvalues','authorization','password','token','apikey'}:
                    out[str(key)]='[omitted: configuration values]'
                else:
                    safe_key=str(key) if str(key) in _SCHEMA_KEYS else walk(str(key),depth+1)
                    out[safe_key]=walk(v,depth+1,str(key))
            if len(item)>160:out['_truncated']='dictionary limit'
            return out
        if isinstance(item,list):
            result=[walk(v,depth+1) for v in item[:100]]
            if len(item)>100:result.append({'_truncated':'list limit','omitted':len(item)-100})
            return result
        return item if item is None or isinstance(item,(bool,int,float)) else str(type(item).__name__)
    return walk(value)


def _outcome(case,answer):
    run=answer.get('run') or {}; order=answer.get('order') or {}
    status=run.get('status') or order.get('status')
    if answer.get('ok') is False or status in ('failed','error'):return 'failed'
    if run and (case.get('execution',{}).get('joinedExistingRun') or run.get('id') in case.get('joinedRunIds',[])):return 'blocked'
    if status=='unsupported':return 'unsupported'
    if status in ('needs_input','awaiting_approval'):return 'blocked'
    if any(v.get('status')=='missing' and v.get('blocksContinuation',True) for v in answer.get('variables',[]) if isinstance(v,dict)):return 'blocked'
    if run.get('healthy') and status=='running':
        kinds=case['artifactTypes']
        non_app=any(any(t in kind for t in ('dataset','cli','simulation','library','script')) for kind in kinds)
        explicit_app=any(kind in ('web_application','web_app','application','system') for kind in kinds)
        return 'unsupported' if non_app and not explicit_app else 'healthy_startup'
    if status=='stopped':return 'interrupted'
    if status=='ready' and case.get('outcome') in ('blocked','failed','unsupported'):return case['outcome']
    return 'in_progress'


def _append(case,event):
    case['events'].append(event)
    if len(case['events'])>MAX_EVENTS:
        # First half remains original evidence, second half tracks newest observations.
        case['events'].pop(MAX_EVENTS//2);case['droppedEvents']+=1


def begin(context,operation,body):
    if operation not in OPERATIONS:raise ValueError('Unsupported benchmark operation.')
    with _LOCK:
        batch=_read(context.get('batchId')); case=_case(batch,context.get('caseId'))
        _collect_secrets(body if operation=='save_project_environment' else {})
        _saved_secrets()
        event={'id':uuid.uuid4().hex,'operation':operation,'stage':OPERATIONS[operation], 'startedAt':time.time(),'status':'pending',
               'request':{k:body[k] for k in ('id','path','retry','approve') if k in body}}
        if operation=='save_project_environment':event['variableNames']=list((body.get('values') or {}).keys())
        clean=sanitize(event);clean['id']=event['id']
        _append(case,clean)
        if not operation.endswith('_state'):case['outcome']='in_progress'
        # Apply redaction to existing evidence too if a newly submitted value was present earlier.
        _save(sanitize_batch(batch))
        return {'batchId':batch['id'],'caseId':case['id'],'eventId':event['id'],'startedAt':event['startedAt'],'operation':operation,'requestedRunId':body.get('id') if operation=='project_run_start' else None}


def sanitize_batch(batch):
    # Do not apply list limits to the fixed cohort/selection denominator.
    return {**batch,'dataset':{**batch['dataset'],'name':sanitize(batch['dataset']['name'])},'cases':[{**case,**({'firstFailure':sanitize(case['firstFailure'])} if 'firstFailure' in case else {}),'metadata':sanitize(case['metadata']),'events':[{**sanitize(e),'id':e['id']} for e in case['events']]} for case in batch['cases']]}


def finish(token,answer):
    with _LOCK:
        batch=_read(token['batchId']);case=_case(batch,token['caseId'])
        _collect_secrets(answer);_saved_secrets()
        clean=sanitize(answer)
        raw=json.dumps(clean)
        if len(raw.encode())>MAX_EVENT_BYTES:
            clean={'ok':answer.get('ok'),'evidenceTruncated':True,'excerpt':raw[:MAX_EVENT_BYTES//2]}
        event=next((e for e in case['events'] if e['id']==token['eventId']),None)
        if event is not None:event.update(finishedAt=time.time(),durationSeconds=round(time.time()-token['startedAt'],4),status='done',response=clean)
        run=answer.get('run') or {};order=answer.get('order') or {}
        observed=run.get('id');requested=token.get('requestedRunId')
        if observed:
            case['observedRunId']=observed
            joined=case.setdefault('joinedRunIds',[])
            if run.get('joinedExistingRun'):
                if observed not in joined:joined.append(observed)
                if len(joined)>40:joined.pop(20);case['droppedJoinedRunIds']=case.get('droppedJoinedRunIds',0)+1
                case['execution']={'requestedRunId':requested or case.get('runId'),'observedRunId':observed,'joinedExistingRun':True}
                if requested:case['runId']=requested
            elif token.get('operation')=='project_run_start':
                case['execution']={'requestedRunId':requested,'observedRunId':observed,'joinedExistingRun':observed in joined}
                if observed not in joined:case['runId']=observed
            elif observed not in joined:
                case['runId']=observed
            if event is not None and token.get('operation')=='project_run_start':event['execution']=dict(case.get('execution',{}))
        case['outcome']=(answer['clientOutcome'] if token.get('operation')=='benchmark_client_outcome' and answer.get('clientOutcome') in ('unsupported','failed') else _outcome(case,answer))
        if case['outcome']=='failed' and 'firstFailure' not in case:
            failure=run or order
            case['firstFailure']=sanitize({'at':time.time(),'operation':event.get('operation') if event else 'unknown',
                'error':str(answer.get('error') or failure.get('error') or failure.get('reason') or '')[:1500],
                'stage':failure.get('failedStage') or failure.get('stage'),'command':str(failure.get('command') or '')[:1500],'cwd':failure.get('cwd') or failure.get('path'),'exitCode':failure.get('exitCode'),'stdout':str(failure.get('stdout') or '')[:1500],'stderr':str(failure.get('stderr') or '')[:1500],
                'agentTraceExcerpt':json.dumps(sanitize(failure.get('agentTrace')))[:1600] if failure.get('agentTrace') else None})
        if event and event.get('operation')=='discover_project_components' and answer.get('ok') and answer.get('root'):
            case['checkoutRevision']=checkout_revision(answer['root'],case['metadata'])
        if answer.get('analysisId'):case['runId']=answer['analysisId']
        if isinstance(answer.get('order'),dict):
            if answer['order'].get('id'):case['orderId']=answer['order']['id']
            if answer['order'].get('result',{}).get('analysisId'):case['runId']=answer['order']['result']['analysisId']
        _save(sanitize_batch(batch))


def checkout_revision(root,metadata):
    import subprocess
    reviewed=next((v.strip() for k,v in metadata.items() if _key(k) in ('reviewedcommit','reviewedrevision')),None)
    actual=None;dirty=None
    try:
        result=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True,timeout=5)
        if result.returncode==0 and re.fullmatch(r'[a-f0-9]{40,64}',result.stdout.strip()):actual=result.stdout.strip()
        status=subprocess.run(['git','-C',str(root),'status','--porcelain','--untracked-files=no'],capture_output=True,text=True,timeout=5)
        if status.returncode==0:dirty=bool(status.stdout.strip())
    except (OSError,subprocess.TimeoutExpired):pass
    return {'actualCommit':actual,'reviewedCommit':reviewed,'matchesReviewedCommit':actual==reviewed if actual and reviewed else None,
            'trackedFilesDirty':dirty,'note':'Actual checkout at discovery; reviewed revision is evidence metadata, not an execution pin. Untracked files are not audited.'}


def export_batch(id,refresh=True):
    with _LOCK:
        batch=_read(id)
        if 'selection' not in batch:raise ValueError('Expected a benchmark cohort.')
        if refresh:
            from . import project_run as PR, project_order as PO
            for case in batch['cases']:
                run_id=case.get('observedRunId') if case.get('execution',{}).get('joinedExistingRun') else case.get('runId')
                op='project_run_state' if run_id else 'project_order_state' if case.get('orderId') else None
                if not op:continue
                token=begin({'batchId':id,'caseId':case['id']},op,{})
                try:answer={'ok':True, 'run' if run_id else 'order':PR.view(run_id) if run_id else PO.view(case['orderId'])}
                except (OSError,ValueError,TypeError):answer={'ok':False,'error':'Retained controller evidence unavailable; original observations retained.'}
                finish(token,answer)
            batch=_read(id)
        _saved_secrets();batch=sanitize_batch(batch)
        counts={}
        for case in batch['cases']:counts[case['outcome']]=counts.get(case['outcome'],0)+1
        batch['exportedAt']=time.time()
        batch['summary']={'denominator':len(batch['selection']['ids']),'counts':counts,'healthyStartup':counts.get('healthy_startup',0),'healthyStartupRate':counts.get('healthy_startup',0)/len(batch['selection']['ids']),'scientificCorrectness':'not_assessed'}
        return batch


def latest():
    with _LOCK:
        files=sorted(folder().glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        batch=next((_read(p.stem) for p in files if len(p.stem)==32),None)
        dataset=next((_read(p.stem) for p in files if len(p.stem)==64),None)
        return {'batch':export_batch(batch['id'],refresh=False) if batch else None,'dataset':dataset}


def operation(body):
    action=body.get('action')
    if action=='import':return {'ok':True,'dataset':import_csv(body.get('csv'),body.get('name','uploaded.csv'))}
    if action=='create':return {'ok':True,'batch':create_batch(body.get('datasetId'),body.get('selectedIds'),body.get('filters'))}
    if action=='export':return {'ok':True,'batch':export_batch(body.get('id'))}
    if action=='latest':return {'ok':True,**latest()}
    if action=='client_outcome':
        reasons={'container_required':('unsupported','The discovered component requires a container-capable runtime.'),
                 'controller_error':('failed','The project controller reported a terminal error after a completed service response.')}
        if body.get('code') not in reasons:raise ValueError('Unknown client outcome classification.')
        context=body.get('context') or {}
        with _LOCK:
            case=_case(_read(context.get('batchId')),context.get('caseId'))
            if case['outcome'] in ('failed','blocked','unsupported'):return {'ok':True}
            outcome,error=reasons[body['code']]
            token=begin(context,'benchmark_client_outcome',{})
            finish(token,{'ok':False,'clientOutcome':outcome,'error':error,'source':'client_controller'})
        return {'ok':True}
    if action=='exception':
        token=begin(body.get('context') or {},'analyze_project',{})
        finish(token,{'ok':False,'error':str(body.get('error') or 'Browser request interrupted')[:1000]})
        return {'ok':True}
    if action=='sample':
        # Schema example only; no claim of paper/repository provenance.
        return {'ok':True,'csv':','.join(HEADERS)+'\nhttps://github.com/owner/repository,,example,web_application,python; api_credentials\n','name':'benchmark-template.csv'}
    raise ValueError('Unknown benchmark action.')
