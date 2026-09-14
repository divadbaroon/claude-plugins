"""Pre-execution run-order assessment. Never launches project commands."""
import json
import re
import shlex
from pathlib import Path
import threading
import time
from . import project_agent_trace as Trace
import uuid
from . import project_components as PC, project_analysis as PA, project_setup as S
from . import project_run as R, project_environment as PE, preview as PV, providers

_LOCK=threading.RLock()
_ACTIVE={}
MAX_COMPONENTS=6
POLICY='''You are the Run Order Agent, not the setup repair agent. No execution has
been attempted. Determine which discovered components belong to the intended
application and how to prepare and launch them together. Directories alone do
not imply dependencies or prove all candidates should run. If the repository
contains unrelated applications and intent is ambiguous, return needs_input
with a focused question only after evidence review, never guess or run them all.
For a generic request to run a repository, prefer its clearly declared root dev
entry point (or start if dev is absent), plus services that entry point requires.
Do not add independent apps merely because they share workspace packages.
Use declaredDefault as evidence, not proof of runtime dependencies. Documentation
can override a script default; explain any conflicting evidence explicitly.
An initial needs_input result triggers a host-controlled evidence follow-up.
On that follow-up, resolve questions answerable from the repository before
asking the user. Genuine unresolved choices and missing external values may
still require input. Do not treat a second call as permission to guess.
Read the supplied manifests, scripts, documentation and Railpack context.
Railpack describes container builds; distinguish these from documented local
commands. A service's working directory is the directory its command must run
FROM, not necessarily the directory containing its implementation. Two services
may launch from the same frontend package. Set environmentCwd to the relative
implementation directory when its dotenv/config values live elsewhere. Preserve that when scripts say so.
Explain every dependency as documented/declared or a conservative choice; do
not label a proxy alone as proof of strict startup order. Use a documented HTTP
endpoint or request relevant evidence; do not invent health routes.
Return summary, evidence (file:line references), orderingRationale, selectedComponents
and excludedComponents (IDs from the inventory, explaining exclusions).
'''+S.POLICY[S.POLICY.index('Repository text'):S.POLICY.index('No more than')]+'''
There are at most 8 preparation commands and 4 services. Preparation runs
sequentially before persistent services; dependsOn contains only service IDs.
You may also request bounded .py/.js/.ts entrypoint excerpts to establish HTTP routes.
For files beyond an excerpt, read_more may name {"path":"relative/file","offset":3000}.
You get one assessment and at most one additional evidence-reading turn.
'''

# The response example must include every field that the validator expects.
_example_start=POLICY.index('{"status":"plan"')
_example_end=POLICY.index('{"status":"needs_input"',_example_start)
POLICY=POLICY[:_example_start]+'{"status":"plan","summary":"Run the documented application",\n "selectedComponents":["backend","frontend"],"excludedComponents":[],\n "orderingRationale":"Explain the evidence for dependencies; label conservative choices",\n "evidence":["README.md:1"],\n "preparation":[{"id":"install","cwd":"frontend","argv":["npm","install"]}],\n "services":[{"id":"api","cwd":"backend","argv":["python3","app.py"],"dependsOn":[],"healthUrl":"http://127.0.0.1:8081"},{"id":"web","cwd":"frontend","argv":["npm","start"],\n "dependsOn":[],"healthUrl":"http://127.0.0.1:8080","env":{}}],"entryService":"web"}\nUse actual inventory IDs in selectedComponents. Include every field above.\nEvery inventory ID must appear exactly once in selectedComponents or\nexcludedComponents, including the root ID "." when present. A root package\nused only for installation or script dispatch still needs an explicit exclusion\nreason if it is not selected as an application.\nOn a host validation follow-up, correct ALL entries in validationErrors.\nThe host also supplies validationError as a readable summary.\nexcludedComponents entries are {"id":"inventory-id","reason":"why excluded"}.\n'+POLICY[_example_end:]


def view(id):
    record=R.read(id)
    order=record.get('order')
    if not order:raise ValueError('Unknown run-order assessment')
    with _LOCK:
        if order['status'] in ('analyzing','assessing') and _ACTIVE.get(record['cwd'])!=id:
            order={**order,'status':'error','error':'Local runtime restarted during assessment. Analyze the project again.'}
    return {**order,'id':id,'path':record['cwd']}


def _redactor(root, components):
    values=[]
    for directory in [root]+[Path(c['path']) for c in components]:
        values.extend(PE.saved(directory).values())
        for name in ('.env','.env.local','.env.production','.env.production.local'):
            p=directory/name
            if p.is_file() and not p.is_symlink():values.extend(PE.dotenv(PE.read(p)).values())
    def redact(text):
        text=str(text)
        for v in sorted(set(values),key=len,reverse=True):
            if v:text=text.replace(v,'[redacted]')
        return text
    return redact


def _excerpt(root,request,redact):
    name=request if isinstance(request,str) else request.get('path')
    offset=0 if isinstance(request,str) else request.get('offset',0)
    if not isinstance(offset,int) or not 0<=offset<=48000:raise ValueError('Invalid excerpt offset')
    p=S.within(root,name)
    if not p.is_file() or any(part in PE.SKIP for part in Path(name).parts) or (p.name not in S.FILES and p.suffix not in ('.py','.js','.ts')):raise ValueError('Only setup manifests, README and bounded source excerpts may be requested')
    with p.open('rb') as stream:
        before=stream.read(offset);data=stream.read(3000)
    return {'path':name,'startLine':before.count(b'\n')+1,'offset':offset,
            'text':S.scrub(data.decode('utf-8','replace'),redact),'truncated':p.stat().st_size>offset+3000}


def declared_default(root):
    """Trace literal root npm aliases; never execute package scripts to discover intent."""
    try:
        path=root/'package.json'
        if path.is_symlink() or path.stat().st_size>100000:return None
        scripts=json.loads(path.read_text()).get('scripts',{})
        name='dev' if scripts.get('dev') else 'start' if scripts.get('start') else None
        if not name:return None
        chain=[];current=name
        for _ in range(8):
            if current in [x['script'] for x in chain]:return None
            command=scripts.get(current)
            if not isinstance(command,str):return None
            chain.append({'script':current,'command':command})
            tokens=shlex.split(command)
            if len(tokens)==3 and tokens[:2]==['npm','run']:current=tokens[2];continue
            result={'script':name,'chain':chain,'evidence':'package.json','targetComponent':None}
            if tokens and tokens[0]=='vite' and '--config' in tokens:
                index=tokens.index('--config')+1
                if index<len(tokens) and not re.search(r'[;&|`$]',command):
                    config=(root/tokens[index]).resolve()
                    if config.is_relative_to(root) and config.is_file():
                        result.update(targetComponent=config.parent.relative_to(root).as_posix(),config=config.relative_to(root).as_posix())
            return result
    except (OSError,ValueError,TypeError,AttributeError):return None


def followup_evidence(root, discovery, brief, redact):
    seen={f['path'] for f in brief['files']}
    default=brief.get('declaredDefault') or {}
    names=[default.get('config')]+sorted(discovery['documentation'],key=lambda n:(len(Path(n).parts),n))
    names += [e['file'] for c in discovery['components'] for e in c['evidence'] if Path(e['file']).name in S.FILES]
    excerpts=[]
    for name in dict.fromkeys(n for n in names if n):
        if name in seen:continue
        try:excerpts.append(_excerpt(root,name,redact))
        except (OSError,ValueError):continue
        if len(excerpts)==3:break
    return excerpts


def validation_feedback(root, candidates, proposal):
    """Collect independent plan/metadata failures before spending the follow-up."""
    errors=[]
    try:apply_assessment({'order':{}},root,candidates,proposal)
    except (ValueError,TypeError,OSError) as exc:errors.append(str(exc))
    evidence=proposal.get('evidence')
    if isinstance(evidence,list):
        for index,citation in enumerate(evidence[:40]):
            try:validate_citation(root,citation)
            except (ValueError,TypeError,OSError) as exc:
                errors.append(f'evidence[{index}]: {exc}')
    ids={c['id'] for c in candidates}
    selected=proposal.get('selectedComponents')
    excluded=proposal.get('excludedComponents')
    if isinstance(selected,list) and all(isinstance(x,str) for x in selected):
        if len(selected)!=len(set(selected)):errors.append('selectedComponents contains duplicate IDs')
        invalid=set(selected)-ids
        if invalid:errors.append('Unknown selected component IDs: '+', '.join(sorted(invalid)))
        if isinstance(excluded,list):
            listed=[e.get('id') for e in excluded if isinstance(e,dict) and isinstance(e.get('id'),str)]
            missing=ids-set(selected)-set(listed)
            unexpected=set(listed)-(ids-set(selected))
            if missing:errors.append('excludedComponents is missing IDs: '+', '.join(repr(x) for x in sorted(missing))+'. Account for every inventory ID exactly once; a workspace root is still an inventory component even when it only dispatches scripts.')
            if unexpected:errors.append('Invalid or selected IDs in excludedComponents: '+', '.join(sorted(unexpected)))
            if len(listed)!=len(set(listed)):errors.append('excludedComponents contains duplicate IDs')
            for index,e in enumerate(excluded):
                if not isinstance(e,dict) or not isinstance(e.get('reason'),str) or not e['reason'].strip():errors.append(f'excludedComponents[{index}] needs a nonempty reason')
    return list(dict.fromkeys(errors))[:16]


def assess(root, discovery, analyses, redact, observe=None):
    docs=[]
    # Reserve documentation space instead of allowing manifests to crowd it out.
    documentation=sorted(discovery['documentation'],key=lambda n:(len(Path(n).parts),n))
    manifests=list(dict.fromkeys(e['file'] for c in discovery['components'] for e in c['evidence'] if Path(e['file']).name in S.FILES))
    # Root overview plus a shared setup README when present; the remaining slots are manifests.
    shared=[n for n in documentation if sum(c['id'].startswith(str(Path(n).parent)+'/') for c in discovery['components'])>1]
    names=list(dict.fromkeys(documentation[:1]+shared[:1]+manifests+documentation))
    for name in names:
        if len(docs)>=4:break
        try:docs.append(_excerpt(root,name,redact))
        except (OSError,ValueError):pass
    brief={'repositoryRoot':str(root),'components':discovery['components'],
           'relationships':discovery['relationships'],'declaredDependencies':discovery['dependencies'],
           'documentation':discovery['documentation'][:30],'files':docs,'railpack':analyses,
           'declaredDefault':declared_default(root)}
    engine=PV._engine('synthesize',90,root=root)
    for turn in range(2):
        prompt=POLICY+'\nEvidence JSON:\n'+json.dumps(S.scrub_tree(brief,redact),ensure_ascii=False)
        if len(prompt)>36000:raise ValueError('Run-order context budget exceeded. Narrow the selected project directory.')
        raw=Trace.call(engine,prompt,redact,observe)
        if len(raw)>24000:raise ValueError('Run-order response budget exceeded')
        value=providers._last_json_object(raw)
        status=value.get('status')
        target=(brief.get('declaredDefault') or {}).get('targetComponent')
        extra_independent=False
        if status=='plan' and target:
            services=value.get('services',[])
            extra_independent=isinstance(services,list) and len(services)>1 and all(isinstance(x,dict) and not x.get('dependsOn') for x in services)
        validation_error=None
        if turn==0 and status=='plan' and not extra_independent:
            errors=validation_feedback(root,discovery['components'],value)
            if errors:
                brief['validationErrors']=errors
                validation_error='; '.join(errors)[:4000]
        if turn==0 and (status=='needs_input' or extra_independent or validation_error):
            brief['initialAssessment']=json.loads(redact(json.dumps(value)))
            brief['followupReason']='Review repository evidence before requesting input' if status=='needs_input' else 'Check extra independent services against the declared default entry point'
            if validation_error:
                brief['followupReason']='Correct the launch plan using host validation feedback'
                brief['validationError']=validation_error
            brief['requestedFiles']=followup_evidence(root,discovery,brief,redact)
            continue
        if status!='read_more':
            if extra_independent:
                return {'status':'needs_input','reason':'The repository declares a default entry point, but the assessment still proposes multiple independent services. Select the intended application to avoid launching unrelated apps.'}
            return json.loads(redact(json.dumps(value)))
        requests=value.get('files')
        if turn or not isinstance(requests,list) or not 1<=len(requests)<=3:raise ValueError('Run-order evidence request limit reached')
        brief['requestedFiles']=[_excerpt(root,n,redact) for n in requests]
    raise ValueError('Run order remains unresolved')


def start(directory):
    root=PE.project(directory)
    with _LOCK:
        previous=_ACTIVE.get(str(root))
        if previous:return view(previous)
        id=uuid.uuid4().hex
        record={'cwd':str(root),'repositoryRoot':str(root),'plan':{},'order':{'status':'analyzing','progress':'Discovering components','startedAt':time.time(),'agentTrace':{'name':'Run Order Agent','calls':[]}}}
        R.write(id,record);_ACTIVE[str(root)]=id
    def work():
        redact=str
        try:
            discovery=PC.discover(str(root))
            candidates=discovery['components']
            if any(c.get('requiresContainer') for c in candidates):
                raise ValueError('Declared container services need a container-capable runtime. Native startup will not be inferred over them.')
            if not 2<=len(candidates)<=MAX_COMPONENTS:raise ValueError('Run-order assessment supports two to six local components. Select a narrower project directory.')
            redact=_redactor(root,candidates)
            analysis_context=[];outputs=[]
            record['order']['components']=outputs
            for c in candidates:
                component={'component':c['id'],'status':'running','startedAt':time.time()}
                outputs.append(component)
                record['order']['progress']='Analyzing '+c['id'];R.write(id,record)
                result=PA.analyze(c['path'],str(root))
                component.update(status='done' if result['ok'] else 'error',durationSeconds=round(time.time()-component['startedAt'],1),analysisId=result.get('analysisId'),ok=result['ok'],error=redact(result.get('error',''))[:1000])
                R.write(id,record)
                plan=result.get('plan',{})
                analysis_context.append({'component':c['id'],'ok':result['ok'],
                    'providers':result.get('info',{}).get('detectedProviders',[]),
                    'nativePlan':result.get('nativePlan'),
                    'commands':[{'stage':s.get('name'),'commands':[i['cmd'][:500] for i in s.get('commands',[]) if isinstance(i.get('cmd'),str)][:8]} for s in plan.get('steps',[])[:8]],
                    'startCommand':str(plan.get('deploy',{}).get('startCommand',''))[:500],
                    'error':redact(result.get('error',''))[-1000:]})
            record['order'].update(status='assessing',progress='Assessing service relationships and run order',components=outputs)
            R.write(id,record)
            def observe(event):
                calls=record['order']['agentTrace']['calls']
                if event['status']=='running':calls.append(event)
                else:calls[-1]=event
                record['order']['progress']=event['phase']
                R.write(id,record)
            proposal=assess(root,discovery,analysis_context,redact,observe)
            record['order']['progress']='Validating returned launch plan';R.write(id,record)
            apply_assessment(record,root,candidates,proposal)
            R.write(id,record)
        except Exception as exc:
            record['order'].update(status='error',error=redact(str(exc))[:1000]);R.write(id,record)
        finally:
            with _LOCK:_ACTIVE.pop(str(root),None)
    threading.Thread(target=work,daemon=True).start()
    return view(id)


def validate_citation(root, evidence):
    # Notes may contain colons and quotes. Only the leading file:line locator
    # identifies the source; annotation text is retained for display, not parsed.
    match=re.fullmatch(r'([^:\r\n]+):([1-9][0-9]*)(?:[-–]([1-9][0-9]*))?(?:[ \t]+[^\r\n]*)?',evidence)
    if not match or (match[3] and int(match[3])<int(match[2])):
        raise ValueError('Evidence must cite a file and a positive line or ascending line range; an explanatory note is allowed')
    file=S.within(root,match[1])
    if not file.is_file():raise ValueError('Evidence must refer to a file')
    return {'file':match[1],'startLine':int(match[2]),'endLine':int(match[3] or match[2])}


def apply_assessment(record, root, candidates, proposal):
    validated=S.validate(root,proposal)
    if validated['status']!='plan':
        record['order'].update(status=validated['status'],reason=validated['reason'])
    else:
        ids={c['id'] for c in candidates}
        selected=proposal.get('selectedComponents')
        if selected is None:
            covered={str(Path(step['cwd']).relative_to(root)) for step in validated['services']}
            covered.update(step['environmentCwd'] for step in validated['services'] if step.get('environmentCwd'))
            if ids<=covered:
                selected=sorted(ids)
                record['order']['metadataNote']='Component selection recovered from service/configuration directories; the agent omitted selection metadata.'
        if not isinstance(selected,list) or not selected or not all(isinstance(x,str) for x in selected) or len(selected)!=len(set(selected)) or not set(selected)<=ids:raise ValueError('Run-order plan must identify supported component IDs')
        rationale=proposal.get('orderingRationale')
        if not isinstance(rationale,str) or not rationale.strip():
            waits=[step['id']+' waits for '+', '.join(step['dependsOn']) for step in validated['services'] if step['dependsOn']]
            rationale='The agent omitted its ordering rationale. '+('; '.join(waits)+'. ' if waits else 'No service dependencies were specified. ')+'These are the proposed dependencies, not verified strict startup requirements.'
        if not validated.get('evidence'):raise ValueError('Run-order plan needs evidence')
        citations=[];citation_notes=[]
        for evidence in validated['evidence']:
            try:validate_citation(root,evidence);citations.append(evidence)
            except (ValueError,TypeError,OSError):citation_notes.append(evidence)
        if not citations:
            raise ValueError('Evidence must cite at least one existing file with a positive line or ascending line range')
        validated['evidence']=citations
        if citation_notes:
            record['order']['evidenceNotes']=citation_notes
            record['order']['metadataNote']='Some agent references could not be validated as file:line citations; they are retained as unvalidated notes. Valid citations and execution validation are still required.'
        excluded=proposal.get('excludedComponents')
        if excluded is None and ids==set(selected):excluded=[]
        if not isinstance(excluded,list) or len(excluded)!=len(ids-set(selected)) or {e.get('id') for e in excluded if isinstance(e,dict)}!=ids-set(selected) or any(not isinstance(e,dict) or not isinstance(e.get('reason'),str) or not e['reason'] for e in excluded):raise ValueError('Explain which components were excluded and why')
        # Persist the relative plan for validation again immediately before execution.
        relative={**validated,'preparation':[], 'services':[]}
        for kind in ('preparation','services'):
            relative[kind]=[{**s,'cwd':str(Path(s['cwd']).relative_to(root))} for s in validated[kind]]
        record['orderPlan']=relative
        record['order'].update(status='done',summary=validated['summary'],plan=relative,
            orderingRationale=rationale[:2000],selectedComponents=selected,
            environmentPaths=[str(root/c) for c in selected],
            excludedComponents=[{'id':e['id'],'reason':e['reason'][:1000]} for e in excluded])
    record['order'].pop('error',None)


def revalidate_saved(id):
    """Recover a response-format rejection without another inference or execution."""
    record=R.read(id)
    order=record.get('order') or {}
    if order.get('status')!='error' or not (str(order.get('error','')).startswith('Evidence must cite') or order.get('error') in ('Run-order plan must identify supported component IDs','Run-order plan needs ordering rationale and evidence','Invalid or duplicate setup step ID')):
        raise ValueError('Only a saved metadata validation failure can be recovered this way')
    calls=order.get('agentTrace',{}).get('calls',[])
    if not calls or calls[-1].get('status')!='done' or calls[-1].get('responseTruncated'):
        raise ValueError('No complete saved response is available')
    proposal=providers._last_json_object(calls[-1]['response'])
    root=PE.project(record['cwd'])
    apply_assessment(record,root,PC.discover(str(root))['components'],proposal)
    record['order']['progress']='Saved response revalidated; no new model request'
    R.write(id,record)
    return view(id)
