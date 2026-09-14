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
