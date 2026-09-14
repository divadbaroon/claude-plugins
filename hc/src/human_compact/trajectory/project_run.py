"""Native execution of server-retained Railpack application commands. with bounded failure recovery."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from . import preview as PV, project_environment as PE, project_owner as Owner

FINITE_TIMEOUT=600
START_TIMEOUT=75
_LOCK=threading.RLock()
_JOBS={}


class ProposalRejected(ValueError):
    pass


class NativePlanError(ValueError):
    def __init__(self, reason, stage, command):
        super().__init__(reason);self.stage=stage;self.command=command


def folder():
    p=Path(os.environ.get('HUMAN_COMPACT_HOME') or Path.home()/'.human-compact')/'project-runs'
    p.mkdir(parents=True,exist_ok=True,mode=0o700)
    return p


def write(id,data):
    fd,temp=tempfile.mkstemp(dir=folder())
    try:
        with os.fdopen(fd,'w') as f: json.dump(data,f)
        os.replace(temp,folder()/(id+'.json'))
    finally:
        if os.path.exists(temp):os.unlink(temp)


def read(id):
    if not isinstance(id,str) or not re.fullmatch(r'[a-f0-9]{32}',id):raise ValueError('Unknown analysis')
    return json.loads((folder()/(id+'.json')).read_text())


def retain(result):
    id=uuid.uuid4().hex
    write(id,{'cwd':result['path'],'repositoryRoot':result.get('repositoryRoot',result['path']),'plan':result['plan'],'orderPlan':result.get('nativePlan')})
    return id


def stages(plan):
    # Railpack's container provisioning is not an application command on the host.
    out=[]
    for step in plan.get('steps',[]):
        name=step.get('name','')
        if name.startswith('packages:'):continue
        if name not in ('install','build'):
            raise ValueError('Unsupported native Railpack stage: '+name)
        commands=[]
        for item in step.get('commands',[]):
            if 'cmd' not in item:continue
            command=item['cmd']
            if command=='mkdir -p /app/node_modules/.cache':continue
            if not isinstance(command,str) or not command.strip():raise ValueError('Invalid plan command')
            if '/app/' in command or '/mise/' in command:raise NativePlanError('This command requires Railpack container paths; native execution is unsupported.',name,command)
            commands.append(command)
        for command in commands:out.append({'stage':name,'command':command,'status':'pending','variables':step.get('variables',{})})
    command=plan.get('deploy',{}).get('startCommand')
    if not command:raise ValueError('Railpack did not provide a start command.')
    out.append({'stage':'start','command':command,'status':'pending','variables':plan.get('deploy',{}).get('variables',{})})
    return out


def environment(cwd, inherited_values=None, validate_required=True, skipped=None):
    root=PE.project(cwd)
    report=PE.scan(cwd)
    missing=[r['name'] for r in report['variables'] if r['status']=='missing' and r.get('group')!='other' and r['name'] not in (skipped or []) and r['name'] not in (inherited_values or {})]
    if validate_required and missing:raise ValueError('Required environment values are missing: '+', '.join(missing)+' in '+str(root)+'. Return to Environment check.')
    values=dict(inherited_values or {})
    names=['.env.production.local','.env.local','.env.production','.env'] if report['framework']=='next' else ['.env.production.local','.env.production','.env.local','.env']
    for name in names:
        p=root/name
        if p.exists():
            for k,v in PE.dotenv(PE.read(p)).items():values.setdefault(k,v)
    values.update(PE.saved(root))
    if any('$' in v for v in values.values()):raise ValueError('Environment interpolation needs review before native execution.')
    # Preserve OS tooling, not hc's credentials or unrelated API secrets.
    base={k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','LOGNAME','TMPDIR','TEMP','TMP','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','LANG','LC_ALL')}
    from . import project_package_manager as PM
    base=PM.launch_env(root,root,base)
    base.update(values)
    secrets=sorted({v for v in values.values() if v},key=len,reverse=True)
    def redact(text):
        text=str(text)
        for value in secrets:
            text=text.replace(value,'[redacted]')
            # Hide partial secrets at live stream boundaries as well.
            for n in range(min(len(value)-1,len(text)),0,-1):
                if text.endswith(value[:n]):text=text[:-n]+'[redacted]';break
        return text
    return base,values,redact


def conflict_runs(id, conflicts):
    from urllib.parse import urlsplit
    ports={urlsplit(c['healthUrl']).port for c in conflicts}
    found=[]
    for file in folder().glob('*.json'):
        if file.stem==id or not re.fullmatch(r'[a-f0-9]{32}',file.stem):continue
        candidate=read(file.stem)
        if (candidate.get('run') or {}).get('status')!='running':continue
        state=_view(file.stem)
        if state.get('status')!='running' or not state.get('healthy'):continue
        services=next((a.get('services',[]) for a in reversed(state.get('attempts',[])) if a.get('services')),[])
        urls=[service['healthUrl'] for service in services]+[state.get('url','')]
        matched=sorted(ports & {urlsplit(url).port for url in urls})
        if matched:found.append({'id':file.stem,'cwd':state['cwd'],'ports':matched,'url':state.get('url')})
    return found


def view(id):
    result=_view(id)
    if result.get('status')=='needs_input' and result.get('portConflicts'):
        result['blockingRuns']=conflict_runs(id,result['portConflicts'])
    return result


def _view(id):
    with _LOCK:
        job=_JOBS.get(id)
        if job:
            result=json.loads(json.dumps(job['state']))
            proc=job.get('proc')
            if proc:
                logs=proc.logs()
                result['stages'][job['index']].update(logs)
                result.update(logs,pid=proc.process.pid,started_at=proc.started_at)
                if result['status']=='running' and not proc.alive():
                    result.update(status='failed',healthy=False,reason='process exited after becoming healthy',exitCode=proc.process.poll())
                    job['state']=result;save_job(id,job)
            if result.get('status') == 'running':
                owned = job.get('setupProcs') or {'app': proc}
                result['previewServices'] = [
                    {'id': name, 'url': p.url, 'healthy': bool(p.healthy and p.alive()),
                     'isEntry': p is proc, 'embeddable': getattr(p, 'embeddable', True)}
                    for name, p in owned.items() if p and p.url
                ]
            return result
        record=read(id)
        if record.get('owner') and (record.get('run') or {}).get('status') in Owner.ACTIVE:
            try:return Owner.call(record['owner'],id,'state')['run']
            except ValueError as exc:
                return {**record['run'],'status':'needs_input','healthy':False,'reason':str(exc)}
        if 'run' in record:
            result=record['run']
            if result['status'] not in ('failed','ready','needs_input','unsupported','awaiting_approval'):
                result.update(status='failed',healthy=False,reason='hc restarted; previous process ownership is unavailable. Stop the prior process before retrying.')
            return result
        if record.get('orderPlan'):
            plan=record['orderPlan']
            return {'id':id,'status':'ready','source':'run_order','cwd':record['cwd'],'environmentPaths':(record.get('order') or {}).get('environmentPaths',[]),'stages':[{'stage':step['id'],'cwd':step['cwd'],'command':shlex.join(step['argv']),'status':'pending'} for step in plan['preparation']+plan['services']]}
        if record.get('order'):raise ValueError('Complete run-order assessment before running the project')
        try: steps=stages(record['plan']);reason=''
        except ValueError as exc:steps=[];reason=str(exc)
        return {'id':id,'status':'ready','cwd':record['cwd'],'reason':reason,'stages':[{k:v for k,v in s.items() if k!='variables'} for s in steps]}


def run_worker(id, job, redact, operation):
    """Every asynchronous launch/approval must publish unexpected worker failures."""
    try:
        operation()
    except Exception as exc:
        with _LOCK:
            state = job['state']
            reason = 'Stopped by Reset' if job.get('cancelled') else (
                'Project runner failed (' + type(exc).__name__ + '): ' + redact(str(exc))[:800])
            for proc in list(job.get('setupProcs', {}).values()) + [job.get('proc')]:
                if proc:
                    try:
                        if proc.alive(): proc.stop()
                    except Exception:
                        pass
            state.update(status='failed', healthy=False, reason=reason, workerError=True)
            for stage in state.get('stages', []):
                if stage.get('status') in ('pending', 'running'):
                    stage.update(status='failed', reason=reason)
            for attempt in state.get('attempts', []):
                if attempt.get('status') in ('planning', 'running'):
                    attempt.update(status='failed', reason=reason)
            save_job(id, job)
    finally:
        with _LOCK:
            if _JOBS.get(id) is job:save_job(id,job)


def start(id,retry=False,owner=None,environment_skips=None):
    import hashlib
    root=str(Path(read(id)['cwd']).resolve())
    lock=folder()/('.launch-'+hashlib.sha256(root.encode()).hexdigest())
    try:lock.mkdir()
    except FileExistsError:raise ValueError('Another launch request for this project is in progress. Retry shortly.') from None
    try:return _start(id,retry,owner,environment_skips)
    finally:lock.rmdir()


def _start(id,retry=False,owner=None,environment_skips=None):
    with _LOCK:
        if id in _JOBS and _JOBS[id]['state']['status'] not in ('failed','needs_input','unsupported'):return view(id)
        record=read(id)
        skips={}
        if environment_skips is not None:
            if not isinstance(environment_skips,dict) or len(environment_skips)>50:raise ValueError('Invalid environment skips')
            boundary=Path(record.get('repositoryRoot',record['cwd'])).resolve()
            for directory,names in environment_skips.items():
                if not isinstance(directory,str):raise ValueError('Invalid environment skip directory')
                resolved=Path(directory).resolve()
                if not resolved.is_relative_to(boundary) or not isinstance(names,list) or len(names)>200 or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',n) for n in names):raise ValueError('Invalid environment skips')
                skips[str(resolved)]=names
        # Consult the actual owning supervisor, never infer ownership from a PID or HTTP health.
        for file in folder().glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{32}',file.stem) or file.stem in _JOBS:continue
            candidate=read(file.stem)
            if Path(candidate['cwd']).resolve()!=Path(record['cwd']).resolve():continue
            if (candidate.get('run') or {}).get('status') not in Owner.ACTIVE:continue
            if not candidate.get('owner'):
                raise ValueError('This project has a run from an older supervisor. Stop it in that preview before starting here.')
            existing=Owner.call(candidate['owner'],file.stem,'state')['run']
            if existing['status'] in Owner.ACTIVE:return {**existing,'joinedExistingRun':True}
        if id not in _JOBS and record.get('run',{}).get('status') not in (None,'failed','ready','needs_input','unsupported'):
            raise ValueError('hc restarted. Stop the prior process before starting a fresh analysis; ownership cannot be safely recovered.')
        if record.get('run',{}).get('status')=='failed' and not retry:return view(id)
        for existing_id,j in _JOBS.items():
            if existing_id!=id and Path(j['state']['cwd']).resolve()==Path(record['cwd']).resolve():
                existing=view(existing_id)
                if existing['status'] in ('installing','building','starting','running','setup_planning','setup_running','awaiting_approval'):
                    return {**existing,'joinedExistingRun':True}

        held=PV.running(record['cwd'])
        if held and held.alive():raise ValueError('A process is already running in this directory.')
        base,values,redact=environment(record['cwd'],validate_required=not bool(record.get('orderPlan')),skipped=skips.get(str(Path(record['cwd']).resolve()),[]))
        order_plan=record.get('orderPlan')
        if record.get('order') and not order_plan:raise ValueError('Complete run-order assessment before running the project')
        try:steps=([{'stage':'plan','command':'','status':'pending','variables':{}}] if order_plan else stages(record['plan']));preflight_error=''
        except ValueError as exc:
            steps=[{'stage':getattr(exc,'stage','plan'),'command':getattr(exc,'command',''),'status':'pending','variables':{}}];preflight_error=str(exc)
        state={'id':id,'cwd':record['cwd'],'status':'installing','stage':steps[0]['stage'],'command':redact(steps[0]['command']),'healthy':False,
               'environmentPaths':(record.get('order') or {}).get('environmentPaths',[]),'environmentSkips':skips,
               'attempts':record.get('run',{}).get('attempts',[]),
               'failures':record.get('run',{}).get('failures',[])[-8:],
               'stages':[{k:redact(v) if k=='command' else v for k,v in s.items() if k!='variables'} for s in steps]}
        job={'state':state,'owner':owner,'orderPlan':record.get('orderPlan'),'order':record.get('order'),'plan':record['plan'],'repositoryRoot':record.get('repositoryRoot',record['cwd']),'index':0};_JOBS[id]=job
        def persist():save_job(id,job)
        def work():
            inherited = dict(base)
            previous=record.get('run',{})
            if retry and previous.get('status') in ('failed','unsupported','needs_input') and previous.get('failures'):
                state.update(previous['failures'][-1])
                state['stages']=previous.get('stages',[])
                state['status']='setup_planning'
                recover(id,job,redact);return
            if order_plan:
                state.update(source='run_order',status='setup_running',stages=[])
                recover(id,job,redact,initial_plan=order_plan);return
            try:
                if preflight_error:raise ValueError(preflight_error)
                from . import project_package_manager as PM
                for step in steps:
                    PM.check_runtime(job['repositoryRoot'],state['cwd'],shlex.split(step['command']),base)
                for i,step in enumerate(steps):
                    with _LOCK:
                        if job.get('cancelled'): raise ValueError('Stopped by Reset')
                        job['index']=i;job['proc']=None;state.update(status={'install':'installing','build':'building','start':'starting'}[step['stage']],stage=step['stage'],command=redact(step['command']))
                        state['stages'][i]['status']='running';persist()
                    inherited.update(step['variables'])
                    env={**inherited,**values}
                    with _LOCK:
                        if job.get('cancelled'): raise ValueError('Stopped by Reset')
                        proc=PV.start_plan_process(state['cwd'],step['command'],env,redact)
                        job['proc']=proc
                    deadline=time.monotonic()+(START_TIMEOUT if step['stage']=='start' else FINITE_TIMEOUT)
                    while proc.alive():
                        if step['stage']=='start':
                            proc.probe()
                            if proc.healthy and proc.alive():
                                time.sleep(.3)
                                if not proc.alive(): continue
                                with _LOCK:
                                    state.update(everHealthy=True,status='running',healthy=True,url=proc.url,pid=proc.process.pid,started_at=proc.started_at)
                                    state['stages'][i].update(status='done',**proc.logs());persist()
                                # Remain the owner after the launch request and health handshake.
                                while proc.alive():
                                    time.sleep(1)
                                proc.thread.join(timeout=3)
                                state['exitCode']=proc.process.poll()
                                raise ValueError('process exited after becoming healthy')
                        if time.monotonic()>deadline:
                            proc.stop();raise ValueError('startup timeout: no healthy local URL found' if step['stage']=='start' else 'finite stage timed out')
                        time.sleep(.15)
                    proc.thread.join(timeout=3)
                    code=proc.process.poll()
                    with _LOCK:state['stages'][i].update(exitCode=code,**proc.logs())
                    if code!=0 or step['stage']=='start':
                        state['exitCode']=code
                        raise ValueError('process exited before a healthy local URL was found' if step['stage']=='start' else 'command exited nonzero')
                    with _LOCK:
                        state['stages'][i]['status']='done'
                        job.setdefault('completedPreparation',[]).append(dict(state['stages'][i]))
                        persist()
            except Exception as exc:
                with _LOCK:
                    proc=job.get('proc')
                    if proc and proc.alive():proc.stop()
                    state.update(status='failed',healthy=False,reason='Stopped by Reset' if job.get('cancelled') else redact(str(exc))[:1000])
                    state['stages'][job['index']]['status']='failed'
                    if proc:state.update(**proc.logs(),exitCode=proc.process.poll())
                    if not job.get('cancelled') and not state.get('everHealthy') and len(state['attempts'])<2:state['status']='setup_planning'
                    persist()
                if not job.get('cancelled') and not state.get('everHealthy'):recover(id,job,redact)
        persist();threading.Thread(target=run_worker,args=(id,job,redact,work),daemon=True).start()
        return view(id)


def reset(id):
    """Stop only this owned run; retain evidence and never undo project files."""
    with _LOCK:
        job=_JOBS.get(id)
        if not job:
            record=read(id)  # Never signal an unowned saved PID.
            if record.get('owner') and (record.get('run') or {}).get('status') in Owner.ACTIVE:
                return Owner.call(record['owner'],id,'reset')
            if record.get('run',{}).get('status')=='awaiting_approval':
                record['run'].update(status='failed',reason='Stopped by Reset')
                record['run']['approval']['status']='cancelled'
                write(id,record)
            return {'ok':True}
        job['cancelled']=True
        procs=list(job.get('setupProcs',{}).values())+[job.get('proc')]
        for proc in procs:
            if proc and proc.alive():
                proc.stop()
        for proc in procs:
            if proc:
                try: proc.process.wait(timeout=5)
                except Exception: raise ValueError('Could not stop the owned process. Retry Reset.')
        job['state'].update(status='failed',healthy=False,reason='Stopped by Reset')
        save_job(id,job)
        return {'ok':True}


def save_job(id,job):
    write(id,{'cwd':job['state']['cwd'],'repositoryRoot':job.get('repositoryRoot',job['state']['cwd']),
              'plan':job['plan'],'orderPlan':job.get('orderPlan'),'order':job.get('order'),'owner':job.get('owner'),'run':job['state']})


def recover(id,job,redact,initial_plan=None,resume=None):
    """Bounded repair attempts after launch. Only the supervisor executes commands."""
    from . import project_setup as S, project_runtime as RT, project_compatibility as PC, project_ports as Ports
    state=job['state']
    try:_,selected_values,_=environment(state['cwd'],validate_required=not bool(job.get('orderPlan')),skipped=state.get('environmentSkips',{}).get(str(Path(state['cwd']).resolve()),[]))
    except Exception as exc:
        with _LOCK:
            state.update(status='needs_input',healthy=False,reason=redact(str(exc))[:1000]);save_job(id,job)
        return
    failure={k:state.get(k) for k in ('stage','command','reason','exitCode','stdout','stderr','componentCwd','runtime','configuration','compatibility')}
    if initial_plan is None and resume is None:state.setdefault('failures',[]).append(failure)
    original_failure=dict((state.get('originalFailure') if resume is not None or failure.get('stage')=='validation' else None) or failure)
    def persist():save_job(id,job)
    def check():
        if job.get('cancelled'):raise ValueError('Stopped by Reset')
    def stop_services():
        for proc in job.get('setupProcs',{}).values():
            if proc.alive():proc.stop()
        for proc in job.get('setupProcs',{}).values():
            try:proc.process.wait(timeout=5)
            except Exception:pass
        for report in job.get('staticReports',[]):
            for path in (report,report.with_suffix('.tmp')):
                try:path.unlink(missing_ok=True)
                except OSError:pass
    def repairs():return sum(a.get('role')!='run_order' for a in state['attempts'])
    while (resume is not None or initial_plan is not None or repairs()<S.MAX_ATTEMPTS) and not job.get('cancelled'):
        with _LOCK:
            resuming=resume is not None
            attempt=state['attempts'][-1] if resuming else {'number':0 if initial_plan is not None else repairs()+1,'role':'run_order' if initial_plan is not None else 'repair','status':'planning'}
            if not resuming:state['attempts'].append(attempt)
            state.update(status='setup_running' if initial_plan is not None else 'setup_planning',healthy=False,stage='setup',reason='Executing assessed run order' if initial_plan is not None else 'Diagnosing the failed setup')
            persist()
        try:
            record={'cwd':state['cwd'],'repositoryRoot':str(Path(job['repositoryRoot']).resolve()),'plan':job['plan'],'acceptedPlan':job.get('orderPlan')}
            def observe(event):
                with _LOCK:
                    trace=attempt.setdefault('agentTrace',{'name':'Setup Repair Agent','calls':[]})
                    if event['status']=='running':trace['calls'].append(event)
                    else:trace['calls'][-1]=event
                    if not job.get('cancelled'):state['reason']=event['phase']
                    persist()
            approved=resume is not None
            proposal=resume if approved else initial_plan if initial_plan is not None else S.propose(record,{**failure,'originalFailure':original_failure},state['attempts'][:-1],redact,observe)
            resume=None
            initial_plan=None
            # Redact before persistence and validation; a literal secret must never become argv.
            proposal=json.loads(redact(json.dumps(proposal)))
            rejected_commands=[{k:step.get(k) for k in ('cwd','argv','env')} for key in ('preparation','services') for step in (proposal.get(key,[]) if isinstance(proposal,dict) and isinstance(proposal.get(key,[]),list) else []) if isinstance(step,dict)]
            signature=json.dumps(rejected_commands,sort_keys=True)
            repeated_rejection=any(a.get('rejectedSignature')==signature for a in state['attempts'][:-1])
            job['proc']=None
            try:
                plan=S.validate(job['repositoryRoot'],proposal)
                if plan['status']=='plan':
                    from . import project_package_manager as PM
                    PM.preserve_railpack(plan,job['plan'],state['cwd'])
            except ValueError as exc:
                attempt['rejectedSignature']=signature
                raise ProposalRejected(str(exc)) from exc
            with _LOCK:
                check();attempt.update(plan)
                job['proc']=None
                if plan['status']!='plan':
                    state.update(status=plan['status'],reason=plan['reason']);persist();return
                state.update(stage='compatibility',command='',stdout='',stderr='',exitCode=None,
                             configuration=None,runtime=None,componentCwd=None,compatibility=[],
                             reason='Checking runtime constraints and dependency metadata')
                persist()
            from . import project_package_manager as PM
            if approved and state.get('approval',{}).get('kind')=='bun':
                state.update(stage='package-manager',reason='Installing Bun into Engelbart local storage');persist()
                PM.install_bun(state['approval']['runtime'],lambda:job.get('cancelled',False))
                approved=False  # Any subsequent Python download needs its own approval.
            try:
                for step in plan['preparation']+plan['services']:
                    PM.check_runtime(job['repositoryRoot'],step['cwd'],step['argv'],environment(step['cwd'],validate_required=False)[0])
            except PM.MissingBun as exc:
                request=PM.bun_request(exc.version)
                with _LOCK:
                    state.update(status='awaiting_approval',stage='package-manager',reason=str(exc),
                        approval={'id':uuid.uuid4().hex,'status':'pending','kind':'bun','runtime':request,
                                  'summary':'Install Bun '+request['version']+' and continue',
                                  'changes':['Install Bun '+request['version']+' in '+request['directory']],
                                  'proposal':proposal})
                    persist()
                return
            except ValueError as exc:
                with _LOCK:
                    attempt.update(status='needs_input',reason=str(exc))
                    state.update(status='needs_input',reason=redact(str(exc)),stage='package-manager')
                    persist()
                return
            stop_services()  # Release only processes owned by this attempt before checking ports.
            conflicts=Ports.conflicts(plan)
            if conflicts and len(plan['services'])>1:
                reason='Port conflict in a multi-service plan. Review dependent API/proxy/CORS configuration before changing service ports.'
                with _LOCK:
                    attempt.update(status='needs_input',reason=reason)
                    state.update(status='needs_input',stage='ports',portConflicts=conflicts,reason=reason)
                    persist()
                return
            if conflicts:
                failure={'stage':'ports','command':'','exitCode':None,'stdout':'','stderr':'',
                         'reason':'Requested health port is occupied; change the launch port and health URL only after checking dependent configuration.',
                         'portConflicts':conflicts,'suggestedPort':Ports.available_port(),
                         'originalFailure':original_failure}
                with _LOCK:
                    attempt.update(status='failed',reason=failure['reason'],failure=failure)
                    state['failures'].append(failure)
                    state.update(**failure,status='setup_planning',healthy=False)
                    persist()
                continue
            with _LOCK:state.update(portConflicts=[],portChanges=[]);persist()
            explicit=bool(plan.get('pythonRuntimes'))
            requests=RT.validate(job['repositoryRoot'],plan.get('pythonRuntimes',[]))
            if not requests:requests=RT.initial_requests(Path(job['repositoryRoot']).resolve(),plan)
            installed=[r['version'] for r in RT.inventory()['python']] if requests else []
            assessments=[]
            for request in requests:
                check()
                assessment=PC.assess(job['repositoryRoot'],request['cwd'],request['version'],installed)
                assessments.append(assessment)
                with _LOCK:
                    state.update(compatibility=assessments,componentCwd=assessment['cwd'])
                    attempt['compatibility']=assessments;persist()
                if not explicit and assessment['recommendedVersion']:
                    request['version']=assessment['recommendedVersion']
                selected=next((c for c in assessment['candidates'] if c['version']==request['version']),None)
                if not selected or selected['status']=='excluded':
                    raise ValueError('Python '+request['version']+' conflicts with declared compatibility constraints for '+assessment['cwd'])
            if requests:
                proposal['pythonRuntimes']=[{**r,'cwd':str(Path(r['cwd']).relative_to(Path(job['repositoryRoot']).resolve()))} for r in requests]
                attempt['pythonRuntimes']=proposal['pythonRuntimes']
            configs=PC.validate_repair(job['repositoryRoot'],plan,requests,state['attempts'][:-1],assessments)
            with _LOCK:
                check()
                # Canonical configurations ignore labels and presentation changes.
                prior=[a for a in state['attempts'][:-1] if a.get('configurations')==configs]
                if prior and attempt['role']!='run_order':raise ValueError('Setup returned the same failed plan; manual review is needed')
                attempt['configurations']=configs
                downloads=RT.missing(requests)
                if approved and any(r['version'] not in state.get('approval',{}).get('downloadVersions',[]) for r in downloads):approved=False
                if downloads and not approved:
                    for request in downloads:RT.download_command(request['version'])
                    token=uuid.uuid4().hex
                    # Store the original relative-path proposal, not executable host commands.
                    state.update(status='awaiting_approval',reason='Approval needed to download Python into Engelbart local storage.',
                        approval={'id':token,'status':'pending','summary':plan['summary'],
                                  'downloadVersions':sorted({r['version'] for r in downloads}),
                                  'changes':['Download Python '+r['version']+' for '+r['cwd'] for r in downloads],
                                  'proposal':proposal})
                    persist();return
                state.update(status='setup_running',reason=plan['summary'])
                job['setupProcs']={};job['proc']=None
                persist()
            paths={r['cwd']:RT.environment_path(job['repositoryRoot'],r['cwd'],r['version'],uuid.uuid4().hex) for r in requests}
            verified_runtimes={}
            runtime_steps=[]
            for n,r in enumerate(requests):
                if r in downloads:runtime_steps.append({'id':'runtime-download-'+str(n),'cwd':str(RT.home()),'stage':'prepare','argv':RT.download_command(r['version']),'env':{},'_runtime':True})
                runtime_steps.append({'id':'runtime-create-'+str(n),'cwd':str(RT.home()),'stage':'prepare','argv':[], 'env':{},'_runtime':True,'_create':r})
            steps=runtime_steps+[{**s,'stage':'prepare','_configuration':configs[n]} for n,s in enumerate(plan['preparation'])]+[{**s,'stage':'service','_configuration':configs[len(plan['preparation'])+n]} for n,s in enumerate(plan['services'])]
            for step in steps:
                check()
                if Ports.reusable_preparation(step,job.get('completedPreparation',[]),original_failure,state['cwd']):
                    continue
                # Revalidate immediately before launch, including filesystem boundaries.
                if not step.get('_runtime'):S.command(Path(job['repositoryRoot']).resolve(),{**step,'cwd':str(Path(step['cwd']).relative_to(Path(job['repositoryRoot']).resolve()))},step['stage']=='prepare')
                environment_cwd=str(S.within(Path(job['repositoryRoot']).resolve(),step['environmentCwd'],directory=True)) if step.get('environmentCwd') else step['cwd']
                if step.get('_runtime'):
                    env,values,local_redact=RT.tool_env(),{},redact
                    if not step.get('_create'):env['UV_PYTHON_DOWNLOADS']='automatic'
                    if step.get('_create'):
                        r=step['_create'];interpreter=RT.find(r['version'])
                        if not interpreter:raise ValueError('Requested Python interpreter is unavailable after provisioning')
                        step['argv']=[interpreter,'-m','venv',str(paths[r['cwd']])]
                else:
                    env,values,local_redact=environment(environment_cwd,selected_values,skipped=state.get('environmentSkips',{}).get(str(Path(environment_cwd).resolve()),[]))
                    env=PM.launch_env(job['repositoryRoot'],step['cwd'],env)
                    if step.get('kind')=='static':
                        from . import project_static as PS
                        from urllib.parse import urlsplit
                        bound=[sys.executable,str(Path(PS.__file__).resolve()),'--directory',step['cwd'],'--port',str(urlsplit(step['healthUrl']).port)]
                        report=folder()/('.static-ready-'+uuid.uuid4().hex+'.json')
                        job.setdefault('staticReports',[]).append(report)
                        step['_portReport']=report
                        bound+=['--ready-file',str(report)]
                        if Ports.automatic_static(plan,step):bound.append('--allow-port-fallback')
                        extra={}
                    else:bound,extra=RT.bind(step,requests,paths)
                    if bound is None:continue  # Managed environment was created above.
                    step={**step,'argv':bound};env.update(extra)
                def mask(text,local_redact=local_redact):return redact(local_redact(text))
                command=shlex.join(step['argv'])
                with _LOCK:
                    check()
                    index=len(state['stages']);job['index']=index;job['proc']=None
                    component=step.get('_create',{}).get('cwd',step['cwd'])
                    component_relative=str(Path(component).relative_to(Path(job['repositoryRoot']).resolve())) if Path(component).is_relative_to(Path(job['repositoryRoot']).resolve()) else None
                    state.update(stage=step['id'],command=mask(command),componentCwd=component_relative,runtime=verified_runtimes.get(component),configuration=step.get('_configuration'),stdout='',stderr='',exitCode=None)
                    stage={'stage':step['id'],'cwd':step['cwd'],'command':mask(command),'status':'running','attempt':attempt['number'],'componentCwd':component_relative,'runtime':verified_runtimes.get(component),'configuration':step.get('_configuration')}
                    state['stages'].append(stage);persist()
                    if step['stage']=='service' and not Ports.automatic_static(plan,step):
                        from urllib.parse import urlsplit
                        import socket
                        endpoint=urlsplit(step['healthUrl'])
                        try:
                            connection=socket.create_connection((endpoint.hostname,endpoint.port),timeout=.3)
                        except OSError:pass
                        else:
                            connection.close();raise ValueError('Health port is already occupied; refusing to claim an unrelated server')
                    proc=PV.start_plan_process(step['cwd'],command,{**env,**step['env']},mask,owner=id+':'+step['id'],argv=step['argv'])
                    job['proc']=proc;job['setupProcs'][step['id']]=proc
                deadline=time.monotonic()+(FINITE_TIMEOUT if step['stage']=='prepare' else START_TIMEOUT)
                while True:
                    check()
                    if not proc.alive():
                        proc.thread.join(timeout=3);code=proc.process.poll()
                        stage.update(exitCode=code,**proc.logs())
                        if step['stage']=='prepare' and code==0:break
                        state['exitCode']=code
                        raise ValueError('Setup command exited before completion' if step['stage']=='prepare' else 'Service exited before HTTP health succeeded')
                    # A dependency must remain alive while later services start.
                    for dep in step.get('dependsOn',[]):
                        if not job['setupProcs'][dep].alive():raise ValueError('Dependency exited: '+dep)
                    if step['stage']=='service':
                        if step.get('kind')=='static' and not stage.get('boundPort'):
                            # A private supervisor-owned status file avoids parsing/redacting log text.
                            report=step['_portReport']
                            if not report.exists():
                                if time.monotonic()>deadline:raise ValueError('Static server did not report its bound port')
                                time.sleep(.05);continue
                            if report.stat().st_size>128:raise ValueError('Invalid static server status')
                            port=json.loads(report.read_text())['port'];report.unlink()
                            if not isinstance(port,int):raise ValueError('Invalid static server port')
                            if not 1024<=port<=65535:raise ValueError('Static server reported an invalid bound port')
                            actual=f'http://127.0.0.1:{port}/';requested=step['healthUrl']
                            if actual!=requested and not Ports.automatic_static(plan,step):raise ValueError('Unexpected static port change')
                            with _LOCK:
                                stage.update(boundPort=port,healthUrl=actual,requestedHealthUrl=requested)
                                step['healthUrl']=actual
                                for service in plan['services']:
                                    if service['id']==step['id']:service['healthUrl']=actual
                                if actual!=requested:
                                    state['portChanges'].append({'service':step['id'],'requestedUrl':requested,'actualUrl':actual})
                                persist()
                        from . import project_health as Health
                        actual=Health.probe(proc,step['healthUrl'])
                        if actual:
                            requested=step['healthUrl'];step['healthUrl']=actual
                            stage.update(healthUrl=actual,requestedHealthUrl=requested)
                            for service in plan['services']:
                                if service['id']==step['id']:service['healthUrl']=actual
                            if actual!=requested:state.setdefault('healthAddressChanges',[]).append({'service':step['id'],'requestedUrl':requested,'actualUrl':actual})
                            break
                    if time.monotonic()>deadline:raise ValueError(('Health check timed out: server running but no owned listener passed HTTP health at '+step['healthUrl']) if step['stage']=='service' else 'Setup stage timed out: '+step['id'])
                    time.sleep(.15)
                if step.get('_create'):
                    r=step['_create'];executable=str(RT.python_path(paths[r['cwd']]))
                    verified=subprocess.run([executable,'--version'],env=RT.tool_env(),cwd=str(RT.home()),capture_output=True,text=True,timeout=10,check=True)
                    match=re.fullmatch(r'Python (3\.\d+\.\d+)\s*',verified.stdout.strip() or verified.stderr.strip())
                    if not match or not (match[1]==r['version'] or match[1].startswith(r['version']+'.')):
                        raise ValueError('Created interpreter does not match the requested Python version')
                    runtime={'cwd':str(Path(r['cwd']).relative_to(Path(job['repositoryRoot']).resolve())),
                             'requestedVersion':r['version'],'actualVersion':match[1],'executable':executable}
                    verified_runtimes[r['cwd']]=runtime
                    attempt.setdefault('executedRuntimes',[]).append(runtime)
                    stage['runtime']=runtime
                    state['runtime']=runtime
                    PC.verify_actual(next(a for a in assessments if a['cwd']==str(Path(r['cwd']).relative_to(Path(job['repositoryRoot']).resolve()))),match[1])
                with _LOCK:stage.update(status='done',**proc.logs());persist()
            with _LOCK:
                check()
                for service in plan['services']:
                    p=job['setupProcs'][service['id']];p.url=service['healthUrl'];p.probe()
                    if not p.alive() or not p.healthy:raise ValueError('Service lost health: '+service['id'])
                entry=job['setupProcs'][plan['entryService']]
                attempt['status']='running'
                state.update(status='running',healthy=True,url=entry.url,pid=entry.process.pid,started_at=entry.started_at,reason='')
                # View must report entry process details, not the last dependency.
                job['proc']=entry
                job['index']=next(i for i,s in enumerate(state['stages']) if s.get('attempt')==attempt['number'] and s['stage']==plan['entryService'])
                persist()
            while not job.get('cancelled') and all(job['setupProcs'][s['id']].alive() for s in plan['services']):time.sleep(.5)
            stop_services()
            with _LOCK:
                state.update(status='failed',healthy=False,reason='Stopped by Reset' if job.get('cancelled') else 'A setup service exited after becoming healthy')
                persist()
            return  # A later runtime exit does not silently trigger another model operation.
        except Exception as exc:
            stop_services()
            if isinstance(exc,ProposalRejected):
                with _LOCK:
                    reason='Repair proposal rejected: '+redact(str(exc))[:800]
                    failure={'stage':'validation','command':'','exitCode':None,'reason':reason,'stdout':'','stderr':'',
                             'validationError':redact(str(exc))[:800],'rejectedCommands':rejected_commands,
                             'componentCwd':state.get('componentCwd'),'originalFailure':original_failure}
                    attempt.update(status='rejected',reason=reason,failure=failure)
                    state['failures'].append(failure)
                    state.update(**failure,healthy=False,status='failed' if repeated_rejection or repairs()>=S.MAX_ATTEMPTS else 'setup_planning')
                    if repeated_rejection:state['reason']='Setup repeated rejected command inputs. '+reason
                    persist()
                if repeated_rejection or job.get('cancelled'):return
                continue
            with _LOCK:
                proc=job.get('proc')
                reason=redact(str(exc))[:1000]
                if proc:
                    state.update(**proc.logs(),exitCode=proc.process.poll())
                    state['stages'][job['index']].update(status='failed',**proc.logs())
                attempt.update(status='failed',reason=reason)
                failure={k:state.get(k) for k in ('stage','command','exitCode','stdout','stderr','componentCwd','runtime','configuration','compatibility')};failure['reason']=reason
                # Keep the latest execution error across subsequent validation failures.
                original_failure=dict(failure)
                state['originalFailure']=original_failure
                attempt['failure']=failure
                state['failures'].append(failure)
                state.update(status='setup_planning' if attempt.get('services') and repairs()<S.MAX_ATTEMPTS else 'failed',healthy=False,reason=reason)
                if job.get('cancelled'):state['reason']='Stopped by Reset'
                persist()
            # Invalid output, missing provider or unsupported commands cannot be executed.
            if not attempt.get('services'):return
    with _LOCK:
        if not job.get('cancelled'):
            state.update(status='failed',healthy=False,reason=f'Setup recovery stopped after {S.MAX_ATTEMPTS} repair attempts. '+str(state.get('reason',''))[:700])
            persist()


def decide_approval(id,approval_id,approve):
    """Claim one exact persisted proposal; retries/double clicks never execute twice."""
    with _LOCK:
        record=read(id);job=_JOBS.get(id)
        if not job and record.get('owner') and (record.get('run') or {}).get('status') in Owner.ACTIVE:
            return Owner.call(record['owner'],id,'approval',approvalId=approval_id,approve=approve)['run']
        state=job['state'] if job else record.get('run',{})
        approval=state.get('approval') or {}
        if approval.get('id')!=approval_id:raise ValueError('This approval is no longer current. Refresh the run.')
        if approval.get('status')!='pending':return view(id)
        if state.get('status')!='awaiting_approval':raise ValueError('This run is no longer waiting for approval')
        if not isinstance(approve,bool):raise ValueError('Approval decision must be explicit')
        if not job:
            job={'state':state,'plan':record['plan'],'orderPlan':record.get('orderPlan'),'order':record.get('order'),
                 'repositoryRoot':record.get('repositoryRoot',record['cwd']),'index':0}
            _JOBS[id]=job
        approval['status']='approved' if approve else 'declined'
        approval['decidedAt']=time.time()
        if not approve:
            state.update(status='needs_input',reason='Repair declined. Previous failure and logs are preserved.')
            save_job(id,job);return view(id)
        _,_,redact=environment(state['cwd'],validate_required=False)
        state.update(status='setup_running',reason='Applying approved repair')
        save_job(id,job)
        threading.Thread(target=run_worker,args=(id,job,redact,lambda: recover(id,job,redact,resume=approval['proposal'])),daemon=True).start()
        return view(id)


def owner_control(server_owner, body):
    import hmac
    with _LOCK:
        job=_JOBS.get(body.get('id'))
        if not job or job.get('owner')!=server_owner or not hmac.compare_digest(str(body.get('token','')),server_owner['token']):
            raise ValueError('This supervisor does not own the requested run.')
        id=body['id']
        if body.get('action')=='state':return {'ok':True,'run':view(id)}
        if body.get('action')=='reset':return reset(id)
        if body.get('action')=='approval':return {'ok':True,'run':decide_approval(id,body.get('approvalId'),body.get('approve'))}
        raise ValueError('Unknown supervisor action.')
