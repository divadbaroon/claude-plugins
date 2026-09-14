import {h,svg} from './dom.js';
import {createBenchmarkActions,benchmarkServices,renderBenchmark} from './project-benchmark.js';

const canRun=p=>p?.path&&!p.launchRequested&&!p.busy&&!p.error&&(!p.run||p.run.status==='ready')&&(!p.order||p.order.status==='done');

export function createProjectWorkspace(store,services,createSession){
  const sessions=new Map();
  let sequence=0;
  const remember=()=>{try{globalThis.localStorage?.setItem('engelbart-project-workspace',JSON.stringify([...sessions.values()].map(p=>({id:p.id,benchmark:p.state?.benchmark,path:p.state?.path||'',sourceUrl:p.state?.sourceUrl,launchRequested:p.state?.launchRequested||!!p.state?.run,runId:p.state?.page==='run'||p.state?.benchmark&&p.state?.run?p.state.result?.analysisId:undefined,orderId:p.state?.page==='order'?p.state.order?.id:undefined}))));}catch{}};
  const publish=(patch={})=>store.set(patch);
  function add(state,savedId,inheritAutomatic=true){
    if(inheritAutomatic)state={...state,autoContinue:state.autoContinue??(store.get().projectWorkspaceAutomatic!==false)};
    const id=savedId||'project-'+Date.now()+'-'+(++sequence);
    const scoped={get:()=>({...store.get(),newProject:sessions.get(id)?.state}),set:patch=>{
      if(!sessions.has(id)||!Object.hasOwn(patch,'newProject'))return;
      const session=sessions.get(id);session.state=session.benchmark&&patch.newProject?{...patch.newProject,benchmark:session.benchmark}:patch.newProject;remember();
      publish({projectInstances:[...sessions.values()].map(p=>({id:p.id,...p.state})),
        ...(store.get().activeProjectInstance===id&&store.get().newProject?{newProject:session.state}: {})});
    }};
    const session={id,state,benchmark:state.benchmark};sessions.set(id,session);
    // Each controller captures its own store and resume keys. Async completions
    // never resolve against whichever project happens to be selected now.
    const storage={getItem:key=>globalThis.localStorage?.getItem(key+':'+id),setItem:(key,value)=>globalThis.localStorage?.setItem(key+':'+id,value),removeItem:key=>globalThis.localStorage?.removeItem(key+':'+id)};
    session.actions=createSession(scoped,state.benchmark?benchmarkServices(services,state.benchmark,error=>publish({projectBenchmark:{...store.get().projectBenchmark,error}})):services,storage);
    remember();
    publish({projectInstances:[...sessions.values()].map(p=>({id:p.id,...p.state}))});
    return session;
  }
  function select(id){const p=sessions.get(id);if(p)publish({activeProjectInstance:id,newProject:p.state,projectWorkspaceAdding:false});}
  function restoreBatch(batch){
    for(const c of batch.cases){
      if([...sessions.values()].some(p=>p.benchmark?.batchId===batch.id&&p.benchmark.caseId===c.id))continue;
      const p=add({path:c.repoUrl,sourceUrl:c.repoUrl,busy:false,error:'',launchRequested:true,autoContinue:false,
        benchmark:{batchId:batch.id,caseId:c.id},result:c.runId?{analysisId:c.runId}:null});
      if(c.runId)p.actions.openProjectRun();
      else if(c.orderId)p.actions.resumeProjectOrder(c.orderId);
    }
  }
  const benchmark=createBenchmarkActions(store,services,{add,select,restoreBatch});
  const controls={
    ...benchmark,
    openNewProject(){
      void benchmark.loadProjectBenchmark();
      if(sessions.size){select(store.get().activeProjectInstance||sessions.keys().next().value);publish({projectWorkspaceAdding:true});return;}
      let saved=[];
      try{saved=JSON.parse(globalThis.localStorage?.getItem('engelbart-project-workspace')||'[]');}catch{}
      if(Array.isArray(saved)&&saved.length){
        const restored=saved.filter(p=>typeof p.id==='string'&&typeof p.path==='string').slice(0,50).map(p=>({saved:p,session:add({path:p.path,sourceUrl:p.sourceUrl,benchmark:p.benchmark,launchRequested:p.launchRequested||!!p.runId||!!p.orderId,busy:false,result:null,error:''},p.id)}));
        if(restored.length){
          select(restored[0].session.id);
          for(const {saved:p,session} of restored){
            if(p.runId){session.state.result={analysisId:p.runId,path:p.path};session.actions.openProjectRun();}
            else if(p.orderId)session.actions.resumeProjectOrder(p.orderId);
          }
          publish({projectWorkspaceAdding:true});return;
        }
      }
      const p=add({path:'',busy:false,result:null,error:''});select(p.id);publish({projectWorkspaceAdding:true});
      // Migrate the previous single-project resume record once.
      try{
        const runId=globalThis.localStorage?.getItem('engelbart-project-run');
        const orderId=globalThis.localStorage?.getItem('engelbart-project-order');
        if(orderId)p.actions.resumeProjectOrder(orderId);
        else if(runId){p.state.result={analysisId:runId};p.actions.openProjectRun();}
      }catch{}
    },
    closeNewProject(){if(store.get().newProject?.busy)return;publish({newProject:null});},
    selectProjectInstance:select,
    addProjectInstance(){if(store.get().projectWorkspaceResetting)return;const p=add({path:'',busy:false,result:null,error:''});select(p.id);},
    async toggleWorkspaceAutomatic(){
      const enabled=store.get().projectWorkspaceAutomatic===false;
      publish({projectWorkspaceAutomatic:enabled});
      await Promise.allSettled([...sessions.values()].map(p=>p.actions.setProjectAutoContinue(enabled)));
    },
    showProjectEntry(){publish({projectWorkspaceAdding:true});},
    selectProjectSource(tab){if(['github','local','paper','benchmark'].includes(tab)){publish({projectSourceTab:tab});if(tab==='benchmark')void benchmark.loadProjectBenchmark();}},
    async addProjectPapers(files){
      const added=[];
      const papers=[...(store.get().projectPapers||[])];
      for(const file of Array.from(files||[])){
        if(!papers.some(p=>p.file.name===file.name&&p.file.size===file.size&&p.file.lastModified===file.lastModified)){
          const paper={id:'paper-'+(++sequence),file,status:'reading',links:[]};papers.push(paper);added.push(paper);
        }
      }
      publish({projectPapers:papers});
      await Promise.allSettled(added.map(async paper=>{
        let result;
        try{result=await services.extractProjectPaperLinks({file:paper.file});}
        catch{result={ok:false,error:'Could not extract links from this file.'};}
        publish({projectPapers:(store.get().projectPapers||[]).map(p=>p.id===paper.id?{...p,status:result.ok?'done':'error',links:result.links||[],error:result.error}:p)});
      }));
    },
    removeProjectPaper(id){publish({projectPapers:(store.get().projectPapers||[]).filter(p=>p.id!==id)});},
    editProjectUrls(value){publish({projectUrls:value,projectBatchError:''});},
    async addPaperProject(url){
      const repository=paperRepository(url);
      if(repository)await controls.addGithubProjects([repository]);
    },
    async addGithubProjects(paperUrls){
      if(store.get().projectWorkspaceResetting)return;
      const urls=paperUrls||(store.get().projectUrls||'').split(/[\s,]+/).filter(Boolean);
      if(!urls.length)return;
      if(urls.length>10){publish({projectBatchError:'Add up to 10 repositories at a time.'});return;}
      for(const value of urls){
        try{const u=new URL(value);if(u.protocol!=='https:'||u.hostname!=='github.com'||u.username||u.password||u.pathname.split('/').filter(Boolean).length<2)throw Error();}
        catch{publish({projectBatchError:'Enter GitHub HTTPS repository URLs, one per line.'});return;}
      }
      const canonical=value=>{const u=new URL(value);u.search='';u.hash='';const parts=u.pathname.split('/').filter(Boolean);parts[0]=parts[0].toLowerCase();parts[1]=parts[1].replace(/\.git$/,'').toLowerCase();u.pathname='/'+parts.join('/');return u.href;};

      for(const url of urls){
        const existing=[...sessions.values()].find(p=>p.state.sourceUrl&&canonical(p.state.sourceUrl)===canonical(url));
        if(existing){select(existing.id);continue;}
        const p=add({path:url,sourceUrl:url,busy:false,result:null,error:'',autoContinue:store.get().projectWorkspaceAutomatic!==false});
        select(p.id);
      }
      publish({...(!paperUrls?{projectUrls:''}:{}),projectBatchError:'',projectWorkspaceAdding:true});
    },
    async runAllProjects(){
      if(store.get().projectWorkspaceResetting)return;
      const pending=[...sessions.values()].filter(p=>canRun(p.state));
      // Mark every selected project before yielding, so repeated clicks cannot
      // launch a second analysis or retry a project that is waiting for input.
      for(const p of pending){p.state={...p.state,launchRequested:true,autoContinue:store.get().projectWorkspaceAutomatic!==false};}
      remember();publish({projectInstances:[...sessions.values()].map(p=>({id:p.id,...p.state}))});
      await Promise.allSettled(pending.map(p=>p.state.result?.ok?p.actions.autoAdvanceProject():p.actions.analyzeNewProject()));
    },
    async resetProjectWorkspace(){
      if([...sessions.values()].some(p=>p.state?.busy)||store.get().projectWorkspaceResetting)return;
      publish({projectWorkspaceResetting:true,projectBatchError:''});
      try{
        const ids=[...new Set([...sessions.values()].map(p=>p.state.result?.analysisId).filter(Boolean))];
        const answers=await Promise.all(ids.map(id=>services.resetProjectRun({id})));
        if(answers.some(a=>!a.ok))throw Error('Some projects could not be stopped. The project list has been kept; retry reset.');
        for(const p of sessions.values())for(const key of ['engelbart-project-run','engelbart-project-order']){try{globalThis.localStorage?.removeItem(key+':'+p.id);}catch{}}
        sessions.clear();remember();
        for(const key of ['engelbart-project-run','engelbart-project-order']){try{globalThis.localStorage?.removeItem(key);}catch{}}
        const p=add({path:'',busy:false,result:null,error:''});select(p.id);
        publish({projectWorkspaceAdding:true,projectUrls:''});
      }catch(e){publish({projectBatchError:e.message});}
      finally{publish({projectWorkspaceResetting:false});}
    }
  };
  const forward={};
  for(const key of Object.keys(createSession(store,services))){
    if(key in controls)continue;
    forward[key]=(...args)=>{
      if(['chooseNewProjectFolder','editNewProjectPath','analyzeNewProject'].includes(key))publish({projectWorkspaceAdding:false});
      let p=sessions.get(store.get().activeProjectInstance);
      if(!p&&store.get().newProject){p=add(store.get().newProject,undefined,false);select(p.id);}
      if(p&&store.get().newProject)p.state=store.get().newProject;
      return p?.actions[key]?.apply(p.actions,args);
    };
  }
  return {...forward,...controls};
}

export function renderProjectWorkspace(state,actions,content){
  if(!state.projectInstances?.length)return content;
  const projects=state.projectInstances.filter(p=>p.path||p.sourceUrl||p.result);
  const adding=state.projectWorkspaceAdding??!projects.length;
  const pending=projects.filter(canRun);
  const inner=content?.firstElementChild;
  if(inner){inner.removeAttribute('role');inner.removeAttribute('aria-modal');inner.onkeydown=e=>{if(e.key==='Escape')actions.closeNewProject();};}
  return h('div',{class:'project-analysis-scrim',key:'project-workspace'},
    h('section',{class:'project-workspace',role:'dialog','aria-modal':'true','aria-label':'Project workspace'},
      h('aside',{class:'project-instance-panel'},h('h2',{},h('button',{class:'ghost-btn',onclick:actions.showProjectEntry},'Projects')),
        !projects.length&&h('p',{},'Added projects will appear here.'),
        h('nav',{'aria-label':'Project instances'},projects.map(p=>h('button',{
          class:'project-instance',key:p.id,'aria-current':!adding&&p.id===state.activeProjectInstance?'true':null,
          onclick:()=>actions.selectProjectInstance(p.id)},
          h('strong',{},(p.sourceUrl||p.path||'Project').split('/').filter(Boolean).pop()),
          h('span',{},projectStatus(p).label))))),
      h('div',{class:'project-instance-content'},
        state.projectBatchError&&h('p',{role:'alert',class:'project-workspace-message'},state.projectBatchError),
        adding?h('section',{class:'project-entry',key:'project-entry'},h('h2',{},'Projects'),
          renderProjectOverview(projects,actions),
          renderProjectSources(state,actions),

        h('div',{class:'project-entry-controls'},
          h('button',{class:'ghost-btn','aria-pressed':String(state.projectWorkspaceAutomatic!==false),onclick:actions.toggleWorkspaceAutomatic},'Automatic: '+(state.projectWorkspaceAutomatic===false?'off':'on')),
          h('button',{class:'ghost-btn',disabled:!pending.length||state.projectWorkspaceResetting,onclick:actions.runAllProjects},'Run all'+(pending.length?' ('+pending.length+')':'')),
          h('button',{class:'ghost-btn',disabled:state.projectInstances.some(p=>p.busy)||state.projectWorkspaceResetting,onclick:actions.resetProjectWorkspace},state.projectWorkspaceResetting?'Resetting…':'Reset all'),
          h('button',{class:'ghost-btn',disabled:state.newProject?.busy,onclick:actions.closeNewProject},'Close workspace'))):
          h('div',{class:'project-selected-content',key:state.activeProjectInstance},content,renderStepNavigation(state.newProject,actions)))));
}

function renderStepNavigation(s,actions){
  const current=s.viewStep||s.page||'plan';
  const steps=[{id:s.order?'order':'plan',label:'Assessment',available:true},
    {id:'environment',label:'Environment',available:!!s.environment||!!s.result?.ok},
    {id:'run',label:'Run',available:!!s.run||!!s.environment?.ok&&!s.busy&&!s.environment.variables.some(v=>v.group==='required'&&v.blocksContinuation&&!s.envValues?.[v.name]&&!s.environmentSkips?.[s.environmentPath||s.result.path]?.includes(v.name))},
    {id:'preview',label:'Live preview',available:!!s.run?.healthy&&s.run?.status==='running'}];
  const index=Math.max(0,steps.findIndex(step=>step.id===current));
  const move=step=>step&&actions.navigateProjectStep(step.id);
  return h('nav',{class:'project-step-navigation','aria-label':'Project steps'},
    h('button',{class:'ghost-btn',disabled:index===0,onclick:()=>move(steps[index-1])},'Back'),
    steps.map(step=>h('button',{class:'ghost-btn',disabled:!step.available,'aria-current':step.id===current?'step':null,onclick:()=>move(step)},step.label)),
    h('button',{class:'ghost-btn',disabled:index===steps.length-1||!steps[index+1]?.available,onclick:()=>move(steps[index+1])},'Next'));
}

function projectStatus(p){
  const status=p.run?.status||p.order?.status;
  if(p.error||['failed','error','unsupported','needs_input'].includes(status))return {label:'Needs attention',kind:'attention',detail:p.error||p.run?.reason||p.order?.reason||p.order?.error||'Open this project to review the issue.'};
  if(status==='awaiting_approval')return {label:'Awaiting approval',kind:'attention',detail:'Review the proposed setup change.'};
  if(p.run?.status==='running'&&p.run.healthy)return {label:p.previewVisited?'Done':'Running',kind:'done',detail:'Application is healthy. Open to view the live preview.'};
  if(p.busy||['installing','building','starting','setup_planning','setup_running','analyzing','assessing'].includes(status))return {label:status==='setup_planning'?'Repairing':p.run?'Setting up':'Analyzing',kind:'active',detail:p.run?.stage||p.order?.progress||'Preparing checkout and launch plan…'};
  if(p.page==='environment')return {label:'Configuration',kind:'attention',detail:'Review environment values to continue.'};
  if(p.launchRequested||p.result?.ok||p.run)return {label:'Ready to continue',kind:'attention',detail:'Open this project to continue setup.'};
  return {label:'Queued',kind:'queued',detail:'Starts when you choose Run all.'};
}
function renderProjectOverview(projects,actions){
  if(!projects.length)return null;
  const statuses=projects.map(projectStatus);
  return h('section',{class:'project-overview','aria-label':'Project status overview'},
    h('div',{class:'project-overview-summary',role:'status','aria-live':'polite'},
      [['queued','queued'],['active','in progress'],['attention','need attention'],['done','healthy']].map(([kind,label])=>h('span',{},statuses.filter(s=>s.kind===kind).length+' '+label))),
    h('div',{class:'project-overview-grid'},projects.map((p,i)=>{
      const status=statuses[i],stages=p.run?.stages||[];
      return h('button',{class:'project-status-card',key:p.id,onclick:()=>actions.selectProjectInstance(p.id)},
        h('div',{class:'project-status-heading'},h('strong',{},(p.sourceUrl||p.path||'Project').split('/').filter(Boolean).pop()),h('span',{class:'project-status-badge '+status.kind},status.label)),
        h('span',{class:'project-status-path'},p.sourceUrl||p.path),
        h('span',{class:'project-status-detail'},String(status.detail).slice(0,200)),
        stages.length>0&&h('span',{class:'project-status-progress'},stages.filter(step=>step.status==='done').length+' of '+stages.length+' recorded steps complete'),
        h('span',{class:'project-status-open'},status.kind==='done'?'View project →':'View details →'));
    })));
}

function renderProjectSources(state,actions){
  const tabs=[['github','GitHub'],['local','Local codebase'],['paper','Paper'],['benchmark','Benchmark CSV']];
  const selected=state.projectSourceTab||'github';
  return h('section',{class:'project-sources'},
    h('div',{role:'tablist','aria-label':'Add project or paper',class:'project-source-tabs'},
      tabs.map(([id,label],index)=>h('button',{id:'project-source-'+id,type:'button',role:'tab',
        class:'ghost-btn','aria-selected':String(id===selected),'aria-controls':'project-source-panel-'+id,
        tabindex:id===selected?'0':'-1',onclick:()=>actions.selectProjectSource(id),
        onkeydown:e=>{
          let next;
          if(e.key==='ArrowRight')next=(index+1)%tabs.length;
          if(e.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;
          if(e.key==='Home')next=0;
          if(e.key==='End')next=tabs.length-1;
          if(next!==undefined){e.preventDefault();actions.selectProjectSource(tabs[next][0]);document.getElementById('project-source-'+tabs[next][0])?.focus();}
        }},label))),
    h('div',{role:'tabpanel',id:'project-source-panel-'+selected,'aria-labelledby':'project-source-'+selected,key:selected},
      selected==='benchmark'?renderBenchmark(state,actions):
      selected==='github'?h('div',{},
          h('form',{onsubmit:e=>{e.preventDefault();actions.addGithubProjects();}},
            h('label',{for:'project-urls'},'GitHub URLs · one per line'),
            h('textarea',{id:'project-urls',rows:'4',value:state.projectUrls||'',placeholder:'https://github.com/owner/repo',oninput:e=>actions.editProjectUrls(e.target.value)}),
            h('button',{class:'ghost-btn',type:'submit',disabled:!state.projectUrls?.trim()||state.projectWorkspaceResetting},'Add to projects'))):
      selected==='local'?h('div',{},h('p',{},'Choose a codebase from your computer.'),
        h('button',{class:'ghost-btn',onclick:actions.addProjectInstance},'Choose a local project')):
      h('section',{class:'project-paper-upload','aria-labelledby':'project-papers-heading'},
            h('label',{class:'paper-upload-zone',for:'project-paper-files'},
              h('span',{class:'paper-upload-icon','aria-hidden':'true'},svg('<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M12 18v-7m-3 3 3-3 3 3"/></svg>')),
              h('strong',{id:'project-papers-heading'},'Upload your papers'),
              h('span',{class:'paper-upload-formats'},'PDF, Word, or text files'),
              h('span',{class:'paper-upload-browse'},'Choose files'),
              h('input',{id:'project-paper-files',class:'paper-upload-input',type:'file',multiple:true,accept:'.pdf,.doc,.docx,.txt,.md',
                onchange:e=>{actions.addProjectPapers(e.target.files);e.target.value='';}})),
            h('p',{class:'paper-upload-note'},'Kept here until page refresh.'),
            (state.projectPapers||[]).length>0&&h('ul',{},state.projectPapers.map(p=>h('li',{key:p.id},
              h('span',{},p.file.name+' · '+(p.file.size<1048576?Math.max(1,Math.ceil(p.file.size/1024))+' KB':(p.file.size/1048576).toFixed(1)+' MB')),
              h('button',{class:'ghost-btn',type:'button','aria-label':'Remove '+p.file.name,onclick:()=>actions.removeProjectPaper(p.id)},'Remove'),
              h('div',{class:'paper-resource-links','aria-live':'polite'},
                p.status==='reading'?h('span',{},'Finding resource links…'):
                p.error?h('span',{role:'alert'},p.error):
                p.links?.length?p.links.map(link=>{
                  const repository=paperRepository(link.url);
                  const added=repository&&(state.projectInstances||[]).some(project=>paperRepository(project.sourceUrl||project.path)===repository);
                  return h('div',{class:'paper-resource-row'},
                    h('a',{href:link.url,target:'_blank',rel:'noopener noreferrer'},link.platform+' · '+link.url),
                    repository&&h('button',{class:'ghost-btn',type:'button',disabled:!!added||state.projectWorkspaceResetting,
                      'aria-label':(added?'Added project ':'Add project ')+repository,onclick:()=>actions.addPaperProject(link.url)},added?'Added':'Add to projects'));
                }):
                h('span',{},'No GitHub, Hugging Face, or OSF links found.'))))))));
}

function paperRepository(value){
  try{
    const url=new URL(value);
    const parts=url.pathname.split('/').filter(Boolean);
    if(url.protocol!=='https:'||url.hostname!=='github.com'||url.username||url.password||parts.length<2)return null;
    if(!parts.slice(0,2).every(part=>/^[a-zA-Z0-9_.-]+$/.test(part)))return null;
    return 'https://github.com/'+parts[0].toLowerCase()+'/'+parts[1].replace(/\.git$/,'').toLowerCase();
  }catch{return null;}
}
