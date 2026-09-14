import {createProjectAnalysisActions} from './new-project.js';
import assert from 'node:assert/strict';
import {createStore,initialState} from './store.js';
import {createProjectWorkspace} from './project-workspace.js';
import {visibleCases,renderBenchmark} from './project-benchmark.js';
const storage=new Map();globalThis.localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
const cases=[1,2,3].map(i=>({id:'case-'+i,repoUrl:'https://github.com/demo/repo'+i,artifactTypes:['web_application'],dependencies:[i===3?'node':'python'],keywords:['learning'],metadata:{}}));
const dataset={id:'dataset',cases,name:'fixture.csv'};
assert.deepEqual(visibleCases(dataset,{dependency:'PYTHON',type:'Web application'}).map(c=>c.id),['case-1','case-2']);
const launches=[];let creations=0;let held;let batch;
const services={projectBenchmark:async body=>{
  if(body.action==='import')return {ok:true,dataset};
  if(body.action==='latest')return {ok:true,dataset,batch};
  if(body.action==='create'){
    creations++;await new Promise(r=>held=r);
    batch={id:'batch',dataset:{id:'dataset'},selection:{ids:body.selectedIds,filters:body.filters},cases:cases.filter(c=>body.selectedIds.includes(c.id))};return {ok:true,batch};
  }
  return {ok:true,batch};
},analyzeProject:async args=>{launches.push(args);return {ok:false,error:'fixture stops here'};}};
const session=(store,svc)=>({analyzeNewProject:async()=>{const s=store.get().newProject;await svc.analyzeProject({path:s.path});},openProjectRun(){},resumeProjectOrder(){}});
const store=createStore(initialState());const actions=createProjectWorkspace(store,services,session);
actions.openNewProject();actions.editProjectUrls('https://github.com/demo/manual');await actions.addGithubProjects();
await actions.importProjectBenchmark({name:'fixture.csv',text:async()=>''});
await actions.runSelectedBenchmark();assert.equal(creations,0);
actions.filterProjectBenchmark('dependency','python');actions.selectVisibleBenchmark();
actions.filterProjectBenchmark('dependency','node');
assert.equal(store.get().projectBenchmark.selectedIds.length,2,'hidden selection preserved');
const pending=actions.runSelectedBenchmark();await actions.runSelectedBenchmark();assert.equal(creations,1);
held();await pending;
assert.equal(launches.length,2);assert.deepEqual(launches.map(c=>c.path),[cases[0].repoUrl,cases[1].repoUrl]);
assert.deepEqual(launches.map(c=>c.benchmark.caseId),['case-1','case-2']);
assert.ok(!launches.some(c=>c.path.includes('manual')));
const restoredStore=createStore(initialState());const restored=createProjectWorkspace(restoredStore,services,session);
restored.openNewProject();await restored.loadProjectBenchmark();
assert.equal(restoredStore.get().projectBenchmark.batch.id,'batch');
assert.equal(restoredStore.get().projectInstances.filter(c=>c.benchmark?.batchId==='batch').length,2);
console.log('Benchmark filters, exact selection, duplicate lock, manual isolation and restore passed');
// Server metadata also restores cases if browser storage was cleared or evicted.
storage.clear();const serverRestoredStore=createStore(initialState());
const serverRestored=createProjectWorkspace(serverRestoredStore,services,session);
serverRestored.openNewProject();await serverRestored.loadProjectBenchmark();
assert.equal(serverRestoredStore.get().projectInstances.filter(c=>c.benchmark?.batchId==='batch').length,2);
assert.equal(launches.length,2,'restoration does not execute cases');

class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};}
  setAttribute(key,value){this.attrs[key]=value;}
  append(child){this.children.push(child);}
}
globalThis.Node=Element;
globalThis.document={createElement:tag=>new Element(tag),createTextNode:text=>({text})};
const filters=renderBenchmark(store.get(),actions);
const descendants=node=>[node,...(node.children||[]).flatMap(descendants)];
assert.deepEqual(descendants(filters).filter(node=>node.tag==='select').map(node=>node.attrs['aria-label']),['Dependency','Artifact type']);

// The real session catches container blockers internally; the benchmark must observe
// the resolved error state, rather than waiting only for rejected promises.
storage.clear();const containerStore=createStore(initialState());const clientReports=[];
const containerDataset={...dataset,cases:[cases[0]]};
const containerActions=createProjectAnalysisActions(containerStore,{
  projectBenchmark:async request=>{
    clientReports.push(request);
    if(request.action==='latest')return {ok:true,dataset:null,batch:null};
    if(request.action==='import')return {ok:true,dataset:containerDataset};
    if(request.action==='create')return {ok:true,batch:{id:'container-batch',dataset:{id:'dataset'},selection:{ids:['case-1']},cases:containerDataset.cases}};
    return {ok:true};
  },
  discoverProjectComponents:async()=>({ok:true,root:'/container-fixture',components:[{path:'/container-fixture',requiresContainer:true}]}),
  analyzeProject:async()=>{throw Error('Container blocker must stop before assessment');}
});
containerActions.openNewProject();await containerActions.importProjectBenchmark({name:'container.csv',text:async()=>''});
containerActions.selectVisibleBenchmark();await containerActions.runSelectedBenchmark();
assert.ok(clientReports.some(request=>request.action==='client_outcome'&&request.code==='container_required'));
assert.ok(containerStore.get().projectInstances.some(p=>p.error?.includes('container-capable')));

// A partial browser snapshot must be hydrated from the newer server record.
storage.clear();batch.cases[0].runId='retained-run';
storage.set('engelbart-project-workspace',JSON.stringify([{id:'partial',path:cases[0].repoUrl,sourceUrl:cases[0].repoUrl,launchRequested:true,benchmark:{batchId:'batch',caseId:'case-1'}}]));
const hydrated=[];const partialStore=createStore(initialState());
const partialSession=(scoped,svc)=>({...session(scoped,svc),openProjectRun(){hydrated.push(scoped.get().newProject.result?.analysisId);assert.equal(scoped.get().newProject.autoContinue,false);}});
const partialActions=createProjectWorkspace(partialStore,services,partialSession);
partialActions.openNewProject();await partialActions.loadProjectBenchmark();
assert.deepEqual(hydrated,['retained-run']);
assert.equal(partialStore.get().projectInstances.find(p=>p.id==='partial').result.analysisId,'retained-run');
