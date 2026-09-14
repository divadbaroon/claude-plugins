import {h} from './dom.js';
const normalize=value=>String(value||'').trim().toLowerCase().replace(/[\s-]+/g,'_');
export function visibleCases(dataset,filters={}){
  const query=String(filters.search||'').trim().toLowerCase();
  return (dataset?.cases||[]).filter(c=>
    (!filters.dependency||c.dependencies.some(t=>normalize(t)===normalize(filters.dependency)))&&
    (!filters.type||c.artifactTypes.some(t=>normalize(t)===normalize(filters.type)))&&
    (!query||JSON.stringify(c).toLowerCase().includes(query)));
}
function download(text,name,type){
  const url=URL.createObjectURL(new Blob([text],{type}));
  const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
const observed=new Set(['startProjectOrder','projectOrderState','discoverProjectComponents','approveProjectRepair','resetProjectRun','projectRunState','startProjectRun','inspectProjectEnvironment','saveProjectEnvironment','analyzeProject']);
export function benchmarkServices(services,context,onError){
  return new Proxy(services,{get(target,key){
    const fn=target[key];if(!observed.has(key)||typeof fn!=='function')return fn;
    return async args=>{
      try{
        const answer=await fn({...args,benchmark:context});
        if(answer?.benchmarkEvidenceError)onError(answer.benchmarkEvidenceError);
        return answer;
      }catch(error){
        // Never ship exception text: browser/provider errors can echo submitted values.
        try{await services.projectBenchmark({action:'exception',context,error:'Browser request failed during '+key+'. Controller evidence may be pending.'});}
        catch{onError('Could not persist the request failure. Download the report to inspect its pending boundary.');}
        throw error;
      }
    };
  }});
}
export function createBenchmarkActions(store,services,{add,select,restoreBatch}){
  const get=()=>store.get().projectBenchmark||{selectedIds:[],filters:{}};
  const update=patch=>store.set({projectBenchmark:{...get(),...patch}});
  const request=async body=>{const answer=await services.projectBenchmark(body);if(!answer?.ok)throw Error(answer?.error||'Benchmark request failed.');return answer;};
  let loading=null,launching=false;
  const actions={
    async loadProjectBenchmark(){
      if(loading)return loading;
      if(!services.projectBenchmark||get().dataset)return;
      loading=(async()=>{
        try{
          const answer=await request({action:'latest'});
          update({dataset:answer.dataset,batch:answer.batch,selectedIds:answer.batch?.dataset.id===answer.dataset?.id?answer.batch.selection.ids:[],filters:answer.batch?.selection.filters||{},error:''});
          if(answer.batch)restoreBatch(answer.batch);
        }catch(e){update({error:e.message});}finally{loading=null;}
      })();
      return loading;
    },
    async importProjectBenchmark(file){
      if(!file||launching)return;
      if(loading)await loading;
      update({busy:true,error:''});
      try{
        if(file.size>1024*1024)throw Error('Choose a CSV under 1 MB.');
        const answer=await request({action:'import',csv:await file.text(),name:file.name});
        update({dataset:answer.dataset,selectedIds:[],filters:{},batch:null,lastSelection:null});
      }catch(e){update({error:e.message});}finally{update({busy:false});}
    },
    filterProjectBenchmark(key,value){update({filters:{...get().filters,[key]:value}});},
    toggleBenchmarkCase(id,checked){
      if(!get().dataset?.cases.some(c=>c.id===id)||launching)return;
      const ids=new Set(get().selectedIds);if(checked)ids.add(id);else ids.delete(id);update({selectedIds:[...ids]});
    },
    selectVisibleBenchmark(){if(!launching)update({selectedIds:[...new Set([...get().selectedIds,...visibleCases(get().dataset,get().filters).map(c=>c.id)])]});},
    clearBenchmarkSelection(){if(!launching)update({selectedIds:[]});},
    newBenchmarkCohort(){if(!launching)update({batch:null,lastSelection:null,error:''});},
    async runSelectedBenchmark(){
      const state=get(),ids=[...(state.selectedIds||[])];
      const fingerprint=JSON.stringify([state.dataset?.id,[...ids].sort()]);
      if(launching||state.busy||!ids.length||!state.dataset||state.lastSelection===fingerprint||state.batch?.dataset.id===state.dataset.id&&JSON.stringify([...state.batch.selection.ids].sort())===JSON.stringify([...ids].sort()))return;
      launching=true;update({launching:true,error:''});
      try{
        const answer=await request({action:'create',datasetId:state.dataset.id,selectedIds:ids,filters:state.filters});
        update({batch:answer.batch,lastSelection:fingerprint});
        const sessions=answer.batch.cases.map(c=>add({path:c.repoUrl,sourceUrl:c.repoUrl,busy:false,result:null,error:'',launchRequested:true,autoContinue:store.get().projectWorkspaceAutomatic!==false,benchmark:{batchId:answer.batch.id,caseId:c.id}}));
        // Dedicated sessions preserve row identity even when two rows share a repository.
        await Promise.allSettled(sessions.map(async p=>{
          try{await p.actions.analyzeNewProject();}
          catch{await request({action:'exception',context:p.state.benchmark,error:'Project controller stopped before completing analysis.'});}
        }));
      }catch(e){update({error:e.message});}finally{launching=false;update({launching:false});}
    },
    async refreshProjectBenchmark(){
      if(!get().batch)return;
      try{const answer=await request({action:'export',id:get().batch.id});update({batch:answer.batch,error:''});return answer.batch;}
      catch(e){update({error:e.message});}
    },
    async exportProjectBenchmark(){const batch=await actions.refreshProjectBenchmark();if(batch)download(JSON.stringify(batch,null,2),'benchmark-'+batch.id+'.json','application/json');},
    async downloadBenchmarkSample(){
      try{const answer=await request({action:'sample'});download(answer.csv,answer.name,'text/csv');}catch(e){update({error:e.message});}
    },
    openBenchmarkCase(id){const p=store.get().projectInstances?.find(p=>p.benchmark?.batchId===get().batch?.id&&p.benchmark.caseId===id);if(p)select(p.id);}
  };
  return actions;
}
export function renderBenchmark(state,actions){
  const b=state.projectBenchmark||{},d=b.dataset,filters=b.filters||{},selected=new Set(b.selectedIds||[]),visible=visibleCases(d,filters);
  const hidden=[...selected].filter(id=>!visible.some(c=>c.id===id)).length;
  const selectFilter=(key,label,options)=>h('label',{},label,h('select',{value:filters[key]||'',onchange:e=>actions.filterProjectBenchmark(key,e.target.value)},
    h('option',{value:''},'All'),[...new Set(options)].sort().map(value=>h('option',{value,selected:filters[key]===value},value))));
  const sameBatch=b.batch?.dataset.id===d?.id&&b.batch&&JSON.stringify([...b.batch.selection.ids].sort())===JSON.stringify([...selected].sort());
  return h('section',{class:'project-benchmark','aria-label':'Paper repository benchmark'},
    h('p',{},'Import a CSV, filter cases, then run the exact selection through Projects. Importing does not clone or run repositories.'),
    h('div',{class:'project-benchmark-controls'},
      h('label',{},'Upload benchmark CSV',h('input',{type:'file',accept:'.csv,text/csv',disabled:b.busy||b.launching,onchange:e=>{actions.importProjectBenchmark(e.target.files?.[0]);e.target.value='';}})),
      h('button',{class:'ghost-btn',onclick:actions.downloadBenchmarkSample},'Download CSV template')),
    b.error&&h('p',{role:'alert'},b.error),
    d&&h('p',{},d.name+' · '+d.cases.length+' cases · SHA-256 '+d.id.slice(0,12)),
    d&&h('div',{class:'project-benchmark-filters'},
      h('label',{},'Search',h('input',{type:'search',value:filters.search||'',oninput:e=>actions.filterProjectBenchmark('search',e.target.value)})),
      selectFilter('dependency','Dependency',d.cases.flatMap(c=>c.dependencies)),
      selectFilter('type','Artifact type',d.cases.flatMap(c=>c.artifactTypes))),
    d&&h('p',{'aria-live':'polite'},visible.length+' visible · '+selected.size+' selected · '+hidden+' selected hidden by filters'),
    d&&h('div',{class:'project-benchmark-controls'},
      h('button',{class:'ghost-btn',disabled:b.launching,onclick:actions.selectVisibleBenchmark},'Select visible'),
      h('button',{class:'ghost-btn',disabled:b.launching,onclick:actions.clearBenchmarkSelection},'Clear selection'),
      h('button',{class:'ghost-btn',disabled:b.busy||b.launching||!selected.size||sameBatch,onclick:actions.runSelectedBenchmark},b.launching?'Launching…':'Run selected ('+selected.size+')'),
      b.batch&&h('button',{class:'ghost-btn',disabled:b.launching,onclick:actions.newBenchmarkCohort},'New cohort')),
    b.batch&&h('div',{class:'project-benchmark-report'},
      h('p',{},'Batch '+b.batch.id+' · fixed denominator '+b.batch.selection.ids.length),
      h('p',{},b.batch.summary?Object.entries(b.batch.summary.counts).map(([k,v])=>v+' '+k.replaceAll('_',' ')).join(' · '):'Download or refresh evidence to update outcomes.'),
      h('p',{},'Healthy startup measures an application responding to its health check. Scientific correctness is not assessed. Dataset and CLI cases are not startup successes.'),
      h('button',{class:'ghost-btn',onclick:actions.refreshProjectBenchmark},'Refresh outcomes'),
      h('button',{class:'ghost-btn',onclick:actions.exportProjectBenchmark},'Download diagnostic JSON')),
    d&&h('div',{class:'project-benchmark-table'},h('table',{},
      h('thead',{},h('tr',{},['Select','Repository / paper','Artifact type','Dependencies','Outcome'].map(s=>h('th',{scope:'col'},s)))),
      h('tbody',{},visible.map(c=>{
        const result=b.batch?.cases.find(item=>item.id===c.id);
        return h('tr',{key:c.id},
          h('td',{},h('input',{type:'checkbox',checked:selected.has(c.id),disabled:b.launching,'aria-label':'Select '+c.repoUrl,onchange:e=>actions.toggleBenchmarkCase(c.id,e.target.checked)})),
          h('td',{},h('strong',{},c.repoUrl),h('small',{},c.paperDoi||'No DOI supplied'),h('small',{},'CSV row '+c.row)),
          h('td',{},c.artifactTypes.join('; ')),h('td',{},c.dependencies.join('; ')),
          h('td',{},result?h('button',{class:'ghost-btn',onclick:()=>actions.openBenchmarkCase(c.id)},(result.outcome||'pending').replaceAll('_',' ')):'Not selected in batch'));
      })))));
}
