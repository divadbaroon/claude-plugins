import assert from 'node:assert/strict';
import {createStore,initialState} from './store.js';
import {createActions} from './actions.js';
import {renderNewProject} from './new-project.js';
import {services} from './services.js';
import {renderHeader} from './components/header.js';
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.classList={add(){}};}
  set innerHTML(value){this.content={firstElementChild:new Element('svg')};}
  setAttribute(k,v){this.attrs[k]=v;}
  append(child){this.children.push(child);}
}
globalThis.Node=Element;
globalThis.document={activeElement:{focus(){}},createElement:tag=>new Element(tag),createTextNode:text=>({text})};
globalThis.window={addEventListener(){}};
const store=createStore({...initialState(),status:'ready'});
let resolve, calls=0;
const actions=createActions(store,{
  discoverProjectComponents:async({path})=>({ok:true,root:path,components:[]}),
  analyzeProject({path}){assert.equal(path,'/local/repo');calls++;return new Promise(r=>resolve=r);},
  chooseProjectDirectory:async()=>({ok:true,cwd:'/local/repo'})
});
function text(n){return n ? (n.text||'')+(n.children||[]).map(text).join(' ') : '';}
function find(n,p){if(p(n))return n;for(const c of n.children||[]){const r=find(c,p);if(r)return r;}}
const header=renderHeader(store.get(),actions);
find(header,n=>n.tag==='button'&&text(n)==='New Project').onclick();
assert.ok(store.get().newProject);
await actions.setProjectAutoContinue(false);
await actions.chooseNewProjectFolder();
assert.equal(store.get().newProject.path,'/local/repo');
let modal=renderNewProject(store.get(),actions);
assert.equal(find(modal,n=>n.attrs?.role==='dialog').attrs['aria-modal'],'true');
const pending=actions.analyzeNewProject();
await actions.analyzeNewProject();assert.equal(calls,1);
assert.match(text(renderNewProject(store.get(),actions)),/Analyzing project…/);
actions.closeNewProject();assert.ok(store.get().newProject);
resolve({ok:true,name:'repo',path:'/local/repo',plan:{steps:[]},info:{detectedProviders:['node']},stdout:'Detected Node',rawPlan:'{"steps":[]}',rawInfo:'{}'});
await pending;
modal=renderNewProject(store.get(),actions);
assert.ok(store.get().newProject);assert.match(text(modal),/Detected Node/);
assert.match(text(find(modal,n=>n.tag==='details')),/\{"steps":\[\]\}/);
assert.match(text(modal),/Continue/);
actions.toggleNewProjectRaw(true);
assert.equal(find(renderNewProject(store.get(),actions),n=>n.tag==='details').attrs.open,'');
actions.editNewProjectPath('/local/repo');assert.equal(store.get().newProject.result,null);
const failed=actions.analyzeNewProject();await Promise.resolve();resolve({ok:false,error:'Railpack failed'});await failed;
assert.match(text(renderNewProject(store.get(),actions)),/Railpack failed/);
actions.closeNewProject();assert.equal(store.get().newProject,null);
let request;
globalThis.fetch=async(path,options)=>{request={path,...options};return {ok:true,json:async()=>({ok:true})};};
await services.analyzeProject({path:'/local/repo'});
assert.equal(request.path,'/api/op');assert.deepEqual(JSON.parse(request.body),{op:'analyze_project',path:'/local/repo'});
console.log('Modal, loading, success, raw output, failure, retry and local boundary passed');
// The second page preserves the plan and only sends values to local hc.
const envStore=createStore({...initialState(),newProject:{path:'/local/repo',result:{ok:true,path:'/local/repo'},busy:false}});
let envResolve;
const environment={ok:true,variables:[{name:'TOKEN',status:'missing',requirement:'required',evidence:[{file:'env.schema.json',line:2,kind:'schema'}]}],warnings:[],limitations:'Partial scan'};
const envActions=createActions(envStore,{
  inspectProjectEnvironment:()=>new Promise(r=>envResolve=r),
  saveProjectEnvironment:async({path,values})=>{assert.equal(path,'/local/repo');assert.equal(values.TOKEN,'secret');return {...environment,variables:[{...environment.variables[0],status:'found'}]};}
});
const inspecting=envActions.inspectProjectEnvironment();
assert.match(text(renderNewProject(envStore.get(),envActions)),/Checking environment/);
envResolve(environment);await inspecting;
let envModal=renderNewProject(envStore.get(),envActions);
assert.match(text(envModal),/Missing and required/);assert.match(text(envModal),/env.schema.json:2/);
assert.equal(find(envModal,n=>n.tag==='input'&&n.attrs['aria-label']==='TOKEN').attrs.type,'password');
envActions.editEnvironmentValue('TOKEN','secret');await envActions.saveProjectEnvironment();
assert.deepEqual(envStore.get().newProject.envValues,{});
assert.match(text(renderNewProject(envStore.get(),envActions)),/Found locally/);
envActions.backToProjectPlan();assert.equal(envStore.get().newProject.result.path,'/local/repo');
await services.saveProjectEnvironment({path:'/local/repo',values:{TOKEN:'secret'}});
assert.equal(request.path,'/api/op');assert.equal(JSON.parse(request.body).op,'save_project_environment');
// Run page stays deterministic: stages, actual errors/logs, retry and healthy URL.
const runStore=createStore({...initialState(),newProject:{busy:false,result:{analysisId:'id',path:'/local/repo'}}});
const ready={status:'ready',cwd:'/local/repo',stages:[{stage:'install',command:'npm install',status:'pending'},{stage:'build',command:'npm run build',status:'pending'},{stage:'start',command:'npm run start',status:'pending'}]};
let runAnswer=ready,startRetry;
const runActions=createActions(runStore,{
  projectRunState:async()=>({ok:true,run:runAnswer}),
  startProjectRun:async({retry})=>{startRetry=retry;return {ok:true,run:runAnswer};}
});
await runActions.openProjectRun();
assert.match(text(renderNewProject(runStore.get(),runActions)),/npm install/);
assert.match(text(renderNewProject(runStore.get(),runActions)),/Run project/);
runAnswer={...ready,status:'failed',stage:'build',exitCode:1,reason:'command exited nonzero',stderr:'actual build error'};
await runActions.startProjectRun();
assert.match(text(renderNewProject(runStore.get(),runActions)),/actual build error/);
assert.match(text(renderNewProject(runStore.get(),runActions)),/Retry/);
runAnswer={...ready,status:'running',stage:'start',healthy:true,url:'http://127.0.0.1:3000'};
await runActions.startProjectRun();assert.equal(startRetry,true);
assert.match(text(renderNewProject(runStore.get(),runActions)),/Project is running/);
assert.ok(!find(renderNewProject(runStore.get(),runActions),n=>n.tag==='iframe'));
runActions.showLivePreview();
assert.equal(find(renderNewProject(runStore.get(),runActions),n=>n.tag==='a').attrs.href,'http://127.0.0.1:3000');
assert.ok(!text(renderNewProject(runStore.get(),runActions)).includes('Show logs'));
await runActions.navigateProjectStep('run');
runStore.set({newProject:{...runStore.get().newProject,run:{...ready,status:'installing',stage:'install'}}});
assert.match(text(renderNewProject(runStore.get(),runActions)),/Setting up project/);
assert.match(text(renderNewProject(runStore.get(),runActions)),/Executing install/);
let resetId;
const resetActions=createActions(runStore,{resetProjectRun:async({id})=>{resetId=id;return {ok:true};}});
await resetActions.chooseAnotherProject();
assert.equal(resetId,'id');assert.equal(runStore.get().newProject.page,'plan');assert.equal(runStore.get().newProject.path,'');assert.equal(runStore.get().newProject.result,null);
// A second analysis adopts the owned run ID for polling and Reset.
const joinedStore=createStore({...initialState(),newProject:{page:'run',busy:false,result:{analysisId:'new-analysis',path:'/local/repo'},run:ready}});
let polledId,joinedResetId;
const joinedActions=createActions(joinedStore,{
  startProjectRun:async()=>({ok:true,run:{...ready,id:'owned-run',status:'running',healthy:true,url:'http://127.0.0.1:4100',joinedExistingRun:true}}),
  projectRunState:async({id})=>{polledId=id;return {ok:true,run:{...ready,id,status:'running',healthy:true,url:'http://127.0.0.1:4100'}};},
  resetProjectRun:async({id})=>{joinedResetId=id;return {ok:true};}
});
await joinedActions.startProjectRun();
assert.equal(joinedStore.get().newProject.result.analysisId,'owned-run');
assert.equal(polledId,'owned-run');
assert.match(text(renderNewProject(joinedStore.get(),joinedActions)),/Showing the existing run/);
await joinedActions.chooseAnotherProject();assert.equal(joinedResetId,'owned-run');
const discovery={ok:true,root:'/repo',components:[{id:'web',name:'web',path:'/repo/web',types:['node'],evidence:[]},{id:'api',name:'api',path:'/repo/api',types:['python'],evidence:[]}],dependencies:[],relationships:[],order:{levels:[['api','web']],issues:[]},warnings:[],documentation:[],note:'Partial discovery'};
const componentStore=createStore({...initialState(),newProject:{path:'/repo',busy:false}});
let analyzedPath,orderStarted;
const launchPlan={preparation:[],services:[{id:'backend',cwd:'system/frontend',argv:['npm','run','backend'],dependsOn:[]},{id:'frontend',cwd:'system/frontend',argv:['npm','start'],dependsOn:['backend']}],evidence:['system/README.md:1']};
const orderResult={status:'done',id:'order-id',path:'/repo',summary:'Run the connected services',orderingRationale:'Backend first is conservative',plan:launchPlan,components:[]};
const componentActions=createActions(componentStore,{
  discoverProjectComponents:async()=>discovery,
  startProjectOrder:async({path})=>{orderStarted=path;return {ok:true,order:{id:'order-id'}};},
  projectOrderState:async()=>({ok:true,order:orderResult}),
  analyzeProject:async({path})=>{analyzedPath=path;return {ok:true,path,info:{},plan:{}};}
});
await componentActions.analyzeNewProject();assert.equal(analyzedPath,undefined);assert.equal(orderStarted,'/repo');
assert.equal(componentStore.get().newProject.result.analysisId,'order-id');
const orderText=text(renderNewProject(componentStore.get(),componentActions));
assert.match(orderText,/Project run order/);assert.match(orderText,/npm run backend/);assert.match(orderText,/npm start/);
assert.equal((orderText.match(/Run from: system\/frontend/g)||[]).length,2);
assert.doesNotMatch(orderText,/Analyze component/);
componentActions.resetProjectOrder();assert.equal(componentStore.get().newProject.path,'');
// Recovery is a distinct phase; preserve failure evidence and human-input states.
runStore.set({newProject:{page:'run',busy:false,result:{analysisId:'id',path:'/local/repo'},run:{...ready,status:'setup_planning',stage:'setup',attempts:[{number:1,status:'planning'}],failures:[{stage:'build',stderr:'original evidence'}]}}});
assert.match(text(renderNewProject(runStore.get(),runActions)),/Reviewing the setup failure/);
assert.match(text(renderNewProject(runStore.get(),runActions)),/Setup attempt 1/);
assert.match(text(renderNewProject(runStore.get(),runActions)),/original evidence/);
runStore.set({newProject:{...runStore.get().newProject,run:{...ready,status:'needs_input',reason:'Missing configuration'}}});
assert.match(text(renderNewProject(runStore.get(),runActions)),/Setup needs your input/);
assert.match(text(renderNewProject(runStore.get(),runActions)),/Environment check/);
const trace={name:'Run Order Agent',calls:[{status:'running',startedAt:Date.now()/1000-12,timeoutSeconds:90,model:'sonnet',provider:'claude',effort:'low',tools:'disabled',promptChars:123,prompt:'Exact redacted prompt'}]};
componentStore.set({newProject:{page:'order',path:'/repo',busy:true,order:{status:'assessing',agentTrace:trace}}});
let inspected=text(renderNewProject(componentStore.get(),componentActions));
assert.match(inspected,/Waiting for model response/);assert.match(inspected,/90s limit/);assert.match(inspected,/Exact redacted prompt/);
componentActions.toggleAgentDetails('order-prompt-0',true);
assert.equal(componentStore.get().newProject.agentDetails['order-prompt-0'],true);
trace.calls[0]={...trace.calls[0],status:'timed_out',durationSeconds:90,error:'claude CLI timed out after 90s'};
componentStore.set({newProject:{...componentStore.get().newProject,busy:false,order:{status:'error',agentTrace:trace}}});
inspected=text(renderNewProject(componentStore.get(),componentActions));assert.match(inspected,/Timed out/);assert.match(inspected,/Exact redacted prompt/);

// Copy includes closed diagnostic sections, preserves raw output, and excludes
// both saved/unsaved secret input values and the copy button's own feedback.
const {modalText,copyModalButton}=await import('./modal-copy.js');
const txt=value=>({nodeType:3,textContent:value});
const element=(tag,...children)=>({tagName:tag,childNodes:children});
const copyRoot=element('SECTION',element('H2',txt('Project run order')),
  element('DETAILS',element('SUMMARY',txt('Prompt sent')),element('PRE',txt('Line one\n  Line two'))),
  {...element('INPUT'),value:'never-copy-this-secret'},
  element('BUTTON',txt('Copy all')));
copyRoot.querySelector=()=>null;
assert.equal(modalText(copyRoot),'Project run order\n\nPrompt sent\n\nLine one\n  Line two');
let clipboard='';
Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async value=>{clipboard=value;}}}});
const copyButton=copyModalButton();copyButton.closest=()=>copyRoot;
await copyButton.onclick({currentTarget:copyButton});
assert.equal(clipboard,modalText(copyRoot));assert.equal(copyButton.textContent,'Copied');
navigator.clipboard.writeText=async()=>{throw new Error('Denied');};
await copyButton.onclick({currentTarget:copyButton});
assert.match(copyButton.textContent,/Copy failed/);
assert.ok(find(renderNewProject({...store.get(),newProject:{path:'/local/repo',page:'plan'}},actions),n=>n.tag==='button'&&text(n)==='Copy all'));
const approvalState={...store.get(),newProject:{page:'run',result:{analysisId:'saved',path:'/local/repo'},run:{status:'awaiting_approval',stages:[],reason:'Download requires approval',approval:{id:'token',summary:'Use Python 3.11',changes:['Download Python 3.11'],proposal:{pythonRuntimes:[{cwd:'.',version:'3.11'}]}}}}};
let decision;
const approvalModal=renderNewProject(approvalState,{...actions,decideProjectRepair:value=>decision=value});
find(approvalModal,n=>n.tag==='button'&&text(n)==='Approve and retry').onclick();assert.equal(decision,true);
approvalState.newProject.run.approval.kind='bun';
assert.match(text(renderNewProject(approvalState,actions)),/Install Bun and continue/);
find(approvalModal,n=>n.tag==='button'&&text(n)==='Decline').onclick();assert.equal(decision,false);
assert.match(text(approvalModal),/Exact repair plan/);
// GitHub discovery must hand the local repository root to analysis, even without components.
const githubStore=createStore({...initialState(),newProject:{path:'https://github.com/example/repo',busy:false}});
let localAnalysis;
const githubActions=createActions(githubStore,{
  discoverProjectComponents:async({path})=>{assert.equal(path,'https://github.com/example/repo');return {ok:true,root:'/managed/example/repo',components:[],checkout:{reused:true}};},
  analyzeProject:async(args)=>{localAnalysis=args;return {ok:false,error:'fixture'};}
});
await githubActions.analyzeNewProject();
assert.deepEqual(localAnalysis,{path:'/managed/example/repo',repositoryRoot:'/managed/example/repo'});
assert.equal(githubStore.get().newProject.path,'/managed/example/repo');
assert.match(text(renderNewProject(githubStore.get(),githubActions)),/Reusing local checkout/);
// Opt-in advances through every component's environment, then launches once.
async function autoFixture(statuses,enabled=true,multi=false){
  const state=createStore({...initialState(),newProject:{path:'/repo',autoContinue:enabled,busy:false}});
  const inspected=[];let starts=0;
  const a=createActions(state,{
    discoverProjectComponents:async()=>({ok:true,root:'/repo',components:multi?[{id:'backend'},{id:'frontend'}]:[]}),
    startProjectOrder:async()=>({ok:true,order:{id:'auto'}}),
    projectOrderState:async()=>({ok:true,order:{status:'done',path:'/repo',plan:{},environmentPaths:['/repo/backend','/repo/frontend']}}),
    analyzeProject:async()=>({ok:true,path:'/repo',analysisId:'auto',environmentPaths:['/repo/backend','/repo/frontend'],info:{}}),
    inspectProjectEnvironment:async({path})=>{inspected.push(path);return {ok:true,variables:statuses[path]||[],warnings:[],limitations:''};},
    projectRunState:async()=>({ok:true,run:{id:'auto',cwd:'/repo',status:starts?'running':'ready',healthy:!!starts,url:starts?'http://127.0.0.1:59999/':null,environmentPaths:['/repo/backend','/repo/frontend']}}),
    startProjectRun:async()=>{starts++;return {ok:true,run:{id:'auto',cwd:'/repo',status:'running'}};}
  });
  await a.analyzeNewProject();return {state,a,inspected,starts:()=>starts};
}
const automatic=await autoFixture({});
assert.deepEqual(automatic.inspected,['/repo/backend','/repo/frontend']);
assert.equal(automatic.starts(),1);assert.equal(automatic.state.get().newProject.page,'run');
assert.equal(automatic.state.get().newProject.viewStep,'preview');
await automatic.a.navigateProjectStep('run');
assert.equal(automatic.state.get().newProject.viewStep,'run');
assert.equal(automatic.starts(),1,'reviewing run does not restart it');
automatic.a.showLivePreview();
assert.equal(automatic.state.get().newProject.viewStep,'preview');
await automatic.a.autoAdvanceProject();assert.equal(automatic.starts(),1);
const missing=await autoFixture({'/repo/backend':[{name:'TOKEN',status:'missing'}]});
assert.equal(missing.starts(),0);assert.equal(missing.state.get().newProject.page,'environment');
assert.deepEqual(missing.inspected,['/repo/backend']);
const uncertain=await autoFixture({'/repo/frontend':[{name:'TOKEN',status:'uncertain'}]});
assert.equal(uncertain.starts(),1,'Unresolved references without a required classification do not block automatic launch');
const optionalOnly=await autoFixture({'/repo/backend':[{name:'OPTIONAL_TOKEN',requirement:'optional',status:'uncertain'}],'/repo/frontend':[{name:'TIMEOUT',status:'optional'}]});
assert.equal(optionalOnly.starts(),1);
assert.deepEqual(optionalOnly.inspected,['/repo/backend','/repo/frontend']);
const requiredUncertain=await autoFixture({'/repo/backend':[{name:'REQUIRED_TOKEN',requirement:'required',status:'uncertain'}]});
assert.equal(requiredUncertain.starts(),0,'An unresolved required value must still pause');
const manual=await autoFixture({},false);assert.equal(manual.inspected.length,0);assert.equal(manual.starts(),0);
await manual.a.setProjectAutoContinue(true);assert.equal(manual.starts(),1);
// Explicit approvals are never submitted by auto-continue.
manual.state.set({newProject:{...manual.state.get().newProject,run:{status:'awaiting_approval'}}});
await manual.a.autoAdvanceProject();assert.equal(manual.starts(),1);
console.log('Auto-continue: all component environments, manual default, blockers, and single launch passed');

const multiAuto=await autoFixture({},true,true);
assert.equal(multiAuto.starts(),1);assert.deepEqual(multiAuto.inspected,['/repo/backend','/repo/frontend']);
const restartStore=createStore({...initialState(),newProject:{page:'run',busy:false,result:{analysisId:'owned'},restartAnalysisId:'new-plan',run:{status:'running'}}});
const restartCalls=[];
const restartActions=createActions(restartStore,{
  resetProjectRun:async({id})=>{restartCalls.push(['stop',id]);return {ok:true};},
  startProjectRun:async({id})=>{restartCalls.push(['start',id]);return {ok:true,run:{id,cwd:'/repo',status:'running'}};},
  projectRunState:async({id})=>({ok:true,run:{id,cwd:'/repo',status:'running'}})
});
await restartActions.restartProjectRun();
assert.deepEqual(restartCalls,[['stop','owned'],['start','new-plan']]);
const blockerStore=createStore({...initialState(),newProject:{page:'run',busy:false,result:{analysisId:'github'},run:{status:'needs_input',blockingRuns:[{id:'desktop'}]}}});
const blockerCalls=[];
const blockerActions=createActions(blockerStore,{
  resetProjectRun:async({id})=>{blockerCalls.push(['stop',id]);return {ok:true};},
  startProjectRun:async({id})=>{blockerCalls.push(['start',id]);return {ok:true,run:{id,cwd:'/github',status:'running'}};},
  projectRunState:async({id})=>({ok:true,run:{id,cwd:'/github',status:'running'}})
});
await blockerActions.stopBlockingProject('unrelated');assert.deepEqual(blockerCalls,[]);
await blockerActions.stopBlockingProject('desktop');
assert.deepEqual(blockerCalls,[['stop','desktop'],['start','github']]);
const readableTrace={name:'Run Order Agent',calls:[
 {status:'done',durationSeconds:5,prompt:'policy',response:'Reading base.py.\n{"status":"read_more","files":["backend/base.py"]}'},
 {status:'done',durationSeconds:7,prompt:'policy\nEvidence JSON:\n{"requestedFiles":[{"path":"backend/base.py","text":"route"}]}',response:'```json\n{"status":"plan"}\n```'}
]};
componentStore.set({newProject:{page:'order',path:'/repo',busy:false,order:{status:'done',agentTrace:readableTrace}}});
const readable=text(renderNewProject(componentStore.get(),componentActions));
assert.match(readable,/Run Order Agent · Assessment complete/);
assert.match(readable,/2 model calls · 12s model time/);
assert.match(readable,/Call 1 — Initial assessment/);
assert.match(readable,/Call 2 — Assessment with requested files/);
assert.match(readable,/Engelbart supplied requested file excerpts: backend\/base.py/);
assert.match(readable,/Host validated the launch plan/);
assert.match(readable,/View prompts and responses/);
assert.doesNotMatch(readable,/Request 2 ·/);

const otherTask=await autoFixture({'/repo/backend':[{name:'DEPLOY_TOKEN',status:'uncertain',group:'other',blocksContinuation:false}]});
assert.equal(otherTask.starts(),1,'Other-task settings must not pause the selected launch');
const requiredApp=await autoFixture({'/repo/backend':[{name:'APP_TOKEN',status:'missing',group:'required',blocksContinuation:true}]});
assert.equal(requiredApp.starts(),0,'Missing required app settings must pause');
console.log('Grouped environment continuation passed');

// Embedded previews use confirmed services, default to the entry, and reject remote URLs.
const {renderProjectPreview,previewServices}=await import('./project-preview.js');
const previewState={run:{id:'run-1',status:'running',healthy:true,previewServices:[
  {id:'remote',url:'http://127.0.0.1:5174/',healthy:true},
  {id:'host',url:'http://127.0.0.1:5173/',healthy:true,isEntry:true},
  {id:'bad',url:'https://example.com/',healthy:true}
]}};
const previewActions={selectPreviewService(id){previewState.previewService=id;},refreshProjectPreview(){previewState.previewRevision=1;}};
let previewTree=renderProjectPreview(previewState,previewActions);
assert.equal(previewServices(previewState.run).length,2);
assert.equal(find(previewTree,n=>n.tag==='iframe').attrs.src,'http://127.0.0.1:5173/');
find(previewTree,n=>n.attrs?.role==='tab'&&text(n)==='remote').onclick();
previewTree=renderProjectPreview(previewState,previewActions);
const originalKey=find(previewTree,n=>n.tag==='iframe').attrs['data-key'];
assert.equal(find(previewTree,n=>n.tag==='iframe').attrs.src,'http://127.0.0.1:5174/');
find(previewTree,n=>n.tag==='button'&&text(n)==='Refresh preview').onclick();
assert.notEqual(find(renderProjectPreview(previewState,previewActions),n=>n.tag==='iframe').attrs['data-key'],originalKey);
previewState.run.previewServices[0].embeddable=false;
assert.ok(!find(renderProjectPreview(previewState,previewActions),n=>n.tag==='iframe'));
assert.match(text(renderProjectPreview(previewState,previewActions)),/blocks embedded previews/);
assert.deepEqual(previewServices({...previewState.run,status:'failed'}),[]);
console.log('Service preview selection, refresh, URL filtering and blocked embedding passed');
// Concurrent project controllers retain their own async state across selection.
const batchStore=createStore({...initialState()});
const deferred=new Map(),batchStarted=[];
const batchActions=createActions(batchStore,{
  discoverProjectComponents:({path})=>new Promise(resolve=>deferred.set(path,resolve)),
  analyzeProject:async({path})=>({ok:true,path,analysisId:path,info:{}}),
  inspectProjectEnvironment:async({path})=>({ok:true,variables:path.endsWith('second')?[{name:'TOKEN',status:'missing'}]:[],warnings:[]}),
  projectRunState:async({id})=>({ok:true,run:{id,cwd:id,status:batchStarted.includes(id)?'running':'ready'}}),
  startProjectRun:async({id})=>{batchStarted.push(id);return {ok:true,run:{id,cwd:id,status:'running'}};}
});
batchActions.openNewProject();
batchActions.editProjectUrls('https://github.com/demo/first\nhttps://github.com/demo/second');
await batchActions.addGithubProjects();
assert.equal(deferred.size,0,'adding only queues projects');
const batchPending=batchActions.runAllProjects();
assert.equal(batchStore.get().projectWorkspaceAdding,true,'Run all keeps the overview open');
await batchActions.runAllProjects();
assert.equal(deferred.size,2,'both checkouts start without waiting for the other');
const batchInstances=batchStore.get().projectInstances;
const firstInstance=batchInstances.find(p=>p.sourceUrl?.endsWith('/first'));
const secondInstance=batchInstances.find(p=>p.sourceUrl?.endsWith('/second'));
batchActions.selectProjectInstance(firstInstance.id);
deferred.get('https://github.com/demo/second')({ok:true,root:'/repos/second',components:[]});
deferred.get('https://github.com/demo/first')({ok:true,root:'/repos/first',components:[]});
await batchPending;
assert.deepEqual(batchStarted,['/repos/first']);
batchActions.showProjectEntry();
const statusOverview=renderNewProject(batchStore.get(),batchActions);
assert.ok(find(statusOverview,n=>n.attrs?.['aria-label']==='Project status overview'));
assert.match(text(statusOverview),/Configuration/);
assert.match(text(statusOverview),/View details/);
batchActions.selectProjectInstance(firstInstance.id);
assert.equal(batchStore.get().newProject.run.cwd,'/repos/first');
batchActions.selectProjectInstance(secondInstance.id);
assert.equal(batchStore.get().newProject.page,'environment');
assert.equal(batchStore.get().newProject.environment.variables[0].name,'TOKEN');
batchActions.editEnvironmentValue('TOKEN','private-draft');
batchActions.selectProjectInstance(firstInstance.id);
assert.equal(batchStore.get().newProject.envValues.TOKEN,undefined);
batchActions.selectProjectInstance(secondInstance.id);
assert.equal(batchStore.get().newProject.envValues.TOKEN,'private-draft');
batchActions.editProjectUrls('https://github.com/demo/first.git');
await batchActions.addGithubProjects();
assert.equal(batchStore.get().projectInstances.length,batchInstances.length,'duplicate selects existing project');
batchActions.editProjectUrls('https://example.com/not-github');
await batchActions.addGithubProjects();
assert.match(batchStore.get().projectBatchError,/GitHub HTTPS/);
console.log('Concurrent setup, independent configuration, switching and deduplication passed');
// Resume references persist, but draft configuration values never do.
const persisted=new Map();
globalThis.localStorage={getItem:k=>persisted.get(k)||null,setItem:(k,v)=>persisted.set(k,v),removeItem:k=>persisted.delete(k)};
batchActions.editEnvironmentValue('PRIVATE_TOKEN','do-not-persist-this');
assert.ok(![...persisted.values()].join('').includes('do-not-persist-this'));
const resumeStore=createStore(initialState());
const resumed=[];
const resumeActions=createActions(resumeStore,{projectRunState:async({id})=>{resumed.push(id);return {ok:true,run:{id,cwd:id,status:'running'}};}});
resumeActions.openNewProject();
await new Promise(resolve=>setTimeout(resolve,0));
assert.ok(resumed.includes('/repos/first'));
assert.equal(resumeStore.get().projectInstances.length,batchStore.get().projectInstances.length);
console.log('Workspace restores run references without persisting draft secrets');

const startsBeforeRunAll=batchStarted.length;
await batchActions.runAllProjects();
assert.equal(batchStarted.length,startsBeforeRunAll,'Run all does not retry waiting or started projects');
const workspaceResetStore=createStore(initialState());
let stops=0;
const workspaceResetActions=createActions(workspaceResetStore,{resetProjectRun:async()=>{stops++;return {ok:true};}});
// Use a fresh browser store to check queued reset without starting anything.
globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
workspaceResetActions.openNewProject();
workspaceResetActions.editProjectUrls('https://github.com/demo/queued');
await workspaceResetActions.addGithubProjects();
assert.equal(workspaceResetStore.get().projectInstances.filter(p=>p.sourceUrl).length,1);
await workspaceResetActions.resetProjectWorkspace();
assert.equal(workspaceResetStore.get().projectInstances.filter(p=>p.sourceUrl).length,0);
assert.equal(stops,0);
assert.equal(workspaceResetStore.get().projectWorkspaceAdding,true);
console.log('Run all skips started projects and manual reset clears queued projects');
// Required-value skips are scoped to the reviewed component and forwarded on launch.
const skipState=createStore({...initialState(),newProject:{path:'/skip',page:'environment',autoContinue:true,result:{ok:true,path:'/skip',analysisId:'skip'},environmentPath:'/skip',environmentChecks:{'/skip':{ok:true,variables:[{name:'API_URL',requirement:'required',status:'missing',blocksContinuation:true}]}},environment:{ok:true,variables:[{name:'API_URL',requirement:'required',status:'missing',blocksContinuation:true}]}}});
let skipLaunch;
const skipActions=createActions(skipState,{
 projectRunState:async()=>({ok:true,run:{id:'skip',cwd:'/skip',status:skipLaunch?'running':'ready',healthy:!!skipLaunch,url:'http://127.0.0.1:59999/',stages:[]}}),
 startProjectRun:async args=>{skipLaunch=args;return {ok:true,run:{id:'skip',cwd:'/skip',status:'running',healthy:true}};}
});
await skipActions.skipEnvironmentValue('API_URL',true);
assert.deepEqual(skipLaunch.environmentSkips,{'/skip':['API_URL']});
assert.equal(skipState.get().newProject.viewStep,'preview');
assert.match(text(renderNewProject(skipState.get(),skipActions)),/Done/);
skipActions.openNewProject();
assert.equal(skipState.get().projectWorkspaceAdding,true);
assert.match(text(renderNewProject(skipState.get(),skipActions)),/GitHub URLs/);
console.log('Explicit component skip launches, preview reports Done, Projects reopens entry');
// Bulk skip preserves entered/saved values and other components' choices.
const bulkRows=[
 {name:'MISSING',requirement:'required',status:'missing'},
 {name:'ENTERED',requirement:'required',status:'missing'},
 {name:'SAVED',requirement:'required',status:'found'},
 {name:'OPTIONAL',requirement:'optional',status:'optional'},
 {name:'OTHER',requirement:'required',status:'missing',editable:false}
].map(row=>({...row,evidence:[]}));
const bulkStore=createStore({...initialState(),newProject:{page:'environment',result:{path:'/bulk'},environment:{ok:true,variables:bulkRows,warnings:[]},envValues:{ENTERED:'draft'},environmentSkips:{'/other':['KEEP']}}});
const bulkActions=createActions(bulkStore,{});
assert.ok(!find(renderNewProject(bulkStore.get(),bulkActions),n=>n.tag==='button'&&text(n)==='Skip all').disabled);
await bulkActions.skipAllEnvironmentValues();
assert.deepEqual(bulkStore.get().newProject.environmentSkips,{'/other':['KEEP'],'/bulk':['MISSING']});
assert.equal(bulkStore.get().newProject.envValues.ENTERED,'draft');
assert.ok(find(renderNewProject(bulkStore.get(),bulkActions),n=>n.tag==='button'&&text(n)==='Skip all').disabled);
// Continue saves filled values before advancing, and save errors keep drafts.
const enteredState=createStore({...initialState(),newProject:{page:'environment',path:'/entered',result:{path:'/entered',analysisId:'entered'},environmentPath:'/entered',environment:{ok:true,variables:[{name:'API_URL',group:'required',blocksContinuation:true,status:'missing',requirement:'required',evidence:[]}],warnings:[]},envValues:{API_URL:'fixture-value'}}});
const continueCalls=[];let saveFails=true;
const enteredActions=createActions(enteredState,{
 saveProjectEnvironment:async args=>{continueCalls.push('save');assert.equal(args.values.API_URL,'fixture-value');return saveFails?{ok:false,error:'Fixture save failed'}:{ok:true,variables:[{name:'API_URL',status:'found',group:'required',blocksContinuation:false}],warnings:[]};},
 projectRunState:async()=>{continueCalls.push('run');return {ok:true,run:{status:'ready',cwd:'/entered',stages:[]}};}
});
assert.ok(!find(renderNewProject(enteredState.get(),enteredActions),n=>n.tag==='button'&&text(n)==='Save and continue').disabled);
await enteredActions.continueProjectEnvironment();
assert.deepEqual(continueCalls,['save']);
assert.equal(enteredState.get().newProject.envValues.API_URL,'fixture-value');
saveFails=false;
await enteredActions.continueProjectEnvironment();
assert.deepEqual(continueCalls,['save','save','run']);
assert.equal(enteredState.get().newProject.page,'run');
console.log('Filled required values save before Continue; failed saves preserve drafts and block advancement');
// Opening the run step directly honors Automatic, without a caller follow-up.
for(const enabled of [true,false]){
 const readyStore=createStore({...initialState(),newProject:{page:'environment',autoContinue:enabled,result:{analysisId:'ready-direct',path:'/ready-direct'}}});
 let launches=0;
 const readyActions=createActions(readyStore,{
  projectRunState:async()=>({ok:true,run:{id:'ready-direct',cwd:'/ready-direct',status:launches?'running':'ready',healthy:!!launches,url:'http://127.0.0.1:59999/',stages:[]}}),
  startProjectRun:async()=>{launches++;return {ok:true,run:{id:'ready-direct',cwd:'/ready-direct',status:'running',healthy:true}};}
 });
 await readyActions.openProjectRun();
 assert.equal(launches,enabled?1:0);
 await readyActions.autoAdvanceProject();
 assert.equal(launches,enabled?1:0,'ready hook and callers must not double launch');
}
console.log('Direct run-step entry launches once with Automatic on, and waits when off');
const visibleValueState={newProject:{page:'environment',result:{path:'/public'},environment:{ok:true,variables:[{name:'NEXT_PUBLIC_SUPABASE_URL',public:true,publicValue:'https://saved.example',status:'found',group:'required',evidence:[]}],warnings:[]}}};
let publicEdit;
const publicTree=renderNewProject(visibleValueState,{editEnvironmentValue:(name,value)=>publicEdit={name,value}});
const publicInput=find(publicTree,n=>n.attrs?.['aria-label']==='NEXT_PUBLIC_SUPABASE_URL');
assert.equal(publicInput.attrs.type,'text');
assert.equal(publicInput.value,'https://saved.example');
publicInput.oninput({target:{value:'https://replacement.example'}});
assert.deepEqual(publicEdit,{name:'NEXT_PUBLIC_SUPABASE_URL',value:'https://replacement.example'});
console.log('Saved public value is visible and editable');

// Local Supabase is explicit, resumes configuration, and discards stale hosted drafts.
const sbRows=[{name:'NEXT_PUBLIC_SUPABASE_URL',status:'missing',requirement:'required',evidence:[]}];
const sbStore=createStore({...initialState(),newProject:{page:'environment',path:'/sb',result:{path:'/sb'},environment:{ok:true,variables:sbRows,warnings:[],localSupabase:{available:true}},envValues:{NEXT_PUBLIC_SUPABASE_URL:'https://prod.example',OTHER:'keep'},environmentSkips:{'/sb':['NEXT_PUBLIC_SUPABASE_URL'],'/other':['KEEP']}}});
let sbCalls=0;
const sbActions=createActions(sbStore,{
 projectLocalSupabase:async args=>{sbCalls++;assert.equal(args.path,'/sb');return {ok:true,available:true,status:'ready',selected:true,variableNames:['NEXT_PUBLIC_SUPABASE_URL']};},
 inspectProjectEnvironment:async()=>({ok:true,variables:[],warnings:[],localSupabase:{available:true,status:'ready',selected:true}})
});
assert.equal(sbCalls,0);
find(renderNewProject(sbStore.get(),sbActions),n=>n.tag==='button'&&text(n)==='Install Docker and set up local Supabase').onclick();
await new Promise(r=>setTimeout(r,0));
assert.equal(sbCalls,1);
assert.deepEqual(sbStore.get().newProject.envValues,{OTHER:'keep'});
assert.deepEqual(sbStore.get().newProject.environmentSkips,{'/sb':[],'/other':['KEEP']});
assert.match(text(renderNewProject(sbStore.get(),sbActions)),/Stop local services/);
console.log('Explicit local Supabase action and scoped configuration resume passed');
