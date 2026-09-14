import {copyModalButton} from './modal-copy.js';
import {renderAgentTrace} from './agent-trace.js';
import {h} from './dom.js';
import {renderAutoContinue} from './project-auto.js';
export function createRunActions(store,services,storage=globalThis.localStorage,onReady=async()=>{}){
  const update=patch=>store.set({newProject:{...store.get().newProject,...patch}});
  async function poll(id){
    while(store.get().newProject?.page==='run'&&store.get().newProject?.result?.analysisId===id){
      try{
        const answer=await services.projectRunState({id});
        if(!answer.ok)throw new Error(answer.error);
        if(store.get().newProject?.page!=='run'||store.get().newProject?.resetting||store.get().newProject?.result?.analysisId!==id)return;
        update({run:answer.run,environmentSkips:store.get().newProject.environmentSkips||answer.run.environmentSkips||{},result:{...store.get().newProject.result,path:answer.run.cwd,environmentPaths:answer.run.environmentPaths||[]},error:'',busy:false});
        if(answer.run.status==='running'){
          const state=store.get().newProject;
          if(state.autoContinue&&!state.previewVisited&&answer.run.healthy)update({viewStep:'preview',previewVisited:true});
          const timer=setTimeout(()=>poll(id),3000);timer?.unref?.();return;
        }
        if(answer.run.status==='ready'){await onReady();return;}
        if(['failed','ready','needs_input','unsupported','awaiting_approval'].includes(answer.run.status))return;
      }catch(error){if(store.get().newProject)update({error:error.message,busy:false});return;}
      await new Promise(resolve=>setTimeout(resolve,store.get().newProject?.run?.status==='running'?3000:700));
    }
  }
  return {
    async openProjectRun(){
      if(store.get().newProject.busy)return;
      const id=store.get().newProject.result.analysisId;
      update({page:'run',viewStep:null,busy:true,error:'',run:null,joinedExistingRun:false});
      try{storage.removeItem('engelbart-project-order');storage.setItem('engelbart-project-run',id);}catch(e){}
      await poll(id);
    },
    async startProjectRun(){
      const s=store.get().newProject;if(s.busy)return;
      update({busy:true,error:'',viewStep:null,previewVisited:false});
      try{
        const answer=await services.startProjectRun({id:s.result.analysisId,environmentSkips:s.environmentSkips||{},retry:['failed','needs_input','unsupported'].includes(s.run?.status)});
        if(!answer.ok)throw new Error(answer.error);
        const actualId=answer.run.id||s.result.analysisId;
        update({run:answer.run,busy:false,joinedExistingRun:!!answer.run.joinedExistingRun,restartAnalysisId:s.restartAnalysisId||s.result.analysisId,
          result:{...s.result,analysisId:actualId,path:answer.run.cwd||s.result.path}});
        try{storage.setItem('engelbart-project-run',actualId);}catch(e){}
        await poll(actualId);
      }catch(error){update({busy:false,error:error.message});}
    },
    async stopBlockingProject(id){
      const s=store.get().newProject;
      if(s.busy||!s.run?.blockingRuns?.some(run=>run.id===id))return;
      update({busy:true,error:''});
      try{
        const answer=await services.resetProjectRun({id});
        if(!answer.ok)throw new Error(answer.error||'Could not stop the conflicting run');
        update({busy:false});
        await this.startProjectRun();
      }catch(error){update({busy:false,error:error.message});}
    },
    async restartProjectRun(){
      const s=store.get().newProject;if(s.busy)return;
      update({busy:true,error:''});
      try{
        const stopped=await services.resetProjectRun({id:s.result.analysisId});
        if(!stopped.ok)throw new Error(stopped.error||'Could not stop the owned run');
        update({busy:false,joinedExistingRun:false,result:{...s.result,analysisId:s.restartAnalysisId||s.result.analysisId},run:{status:'failed'}});
        await this.startProjectRun();
      }catch(error){update({busy:false,error:error.message});}
    },
    async decideProjectRepair(approve){
      const s=store.get().newProject;if(s.busy)return;
      update({busy:true,error:''});
      try {
        const answer=await services.approveProjectRepair({id:s.result.analysisId,approvalId:s.run.approval.id,approve});
        if(!answer.ok)throw new Error(answer.error);
        update({run:answer.run,busy:false});await poll(s.result.analysisId);
      }catch(error){update({busy:false,error:error.message});}
    },
    async chooseAnotherProject(){
      const s=store.get().newProject;if(s.busy)return;
      update({busy:true,resetting:true,error:''});
      try{
        if(s.result?.analysisId){const answer=await services.resetProjectRun({id:s.result.analysisId});if(!answer.ok)throw new Error(answer.error||'Could not reset run');}
        try{storage.removeItem('engelbart-project-run');storage.removeItem('engelbart-project-order');}catch(e){}
        store.set({newProject:{page:'plan',path:'',result:null,run:null,error:'',busy:false}});
      }catch(error){update({busy:false,resetting:false,error:error.message});}
    },
    showLivePreview(){if(store.get().newProject?.run?.healthy)update({viewStep:'preview',previewVisited:true});},
    selectPreviewService(id){update({previewService:id});},
    refreshProjectPreview(){update({previewRevision:(store.get().newProject.previewRevision||0)+1});},
    toggleRunLogs(open){if(store.get().newProject.logsOpen!==open)update({logsOpen:open});},
  };
}
export function renderRun(s,actions){
  const run=s.run,active=run&&['installing','building','starting','setup_planning','setup_running'].includes(run.status);
  return h('div',{class:'project-analysis-scrim',onclick:actions.closeNewProject},
    h('section',{class:'project-analysis-modal project-run-modal',role:'dialog','aria-modal':'true','aria-label':'Run project',onclick:e=>e.stopPropagation(),onkeydown:e=>{if(e.key==='Escape')actions.closeNewProject();}},
      h('h2',{},run?.status==='running'?'Project is running':active?'Setting up project':'Run project'),copyModalButton(),
      renderAutoContinue(s,actions),h('p',{},run?.cwd||s.result.path),
      h('p',{},(run?.source==='run_order'?'Run the assessed launch plan using installed local tools.':'Run Railpack’s application commands using installed local tools.')+' This installs dependencies and creates build output. If execution fails, Setup reviews bounded failure evidence and attempts a validated local setup, up to five repair attempts.'),
      (run?.stages||[]).map(step=>h('div',{class:'environment-row'},
        h('h3',{},(step.status==='done'?'✓ ':step.status==='running'?'● ':step.status==='failed'?'× ':'○ ')+step.stage),
        h('pre',{},step.command))),
      (run?.healthAddressChanges||[]).map(change=>h('p',{role:'status'},change.service+': verified the service’s own listener at '+change.actualUrl+' (planned health address: '+change.requestedUrl+').')),
      (run?.portChanges||[]).map(change=>h('p',{role:'status'},change.service+': '+change.requestedUrl+' was occupied. Serving at '+change.actualUrl+' instead.')),
      s.joinedExistingRun&&h('p',{role:'status'},'Showing the existing run for this project. The new plan has not been executed.'),
      s.busy&&h('p',{role:'status'},'Loading run…'),
      active&&h('p',{role:'status'},run.status==='setup_planning'?'Reviewing the setup failure…':'Executing '+run.stage+'…'),
      run?.reason&&run.status==='ready'&&h('p',{},run.reason+' Setup will review this when you run the project.'),
      ['needs_input','unsupported'].includes(run?.status)&&h('div',{role:'alert'},h('h3',{},run.status==='needs_input'?'Setup needs your input':'Setup needs manual review'),h('p',{},run.reason)),
      (run?.blockingRuns||[]).map(other=>h('div',{class:'environment-row'},
        h('h3',{},'Another checkout is running'),h('p',{},other.cwd),
        h('p',{},'Uses ports '+other.ports.join(', ')+'. Stopping it will interrupt that project before retrying this checkout.'),
        other.url&&h('a',{href:other.url,target:'_blank',rel:'noopener noreferrer'},'Open existing app'),
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:()=>actions.stopBlockingProject(other.id)},'Stop this run and retry current checkout'))),
      (run?.compatibility||[]).map(report=>h('details',{},
        h('summary',{},'Python compatibility: '+report.cwd),
        h('p',{},report.recommendedVersion?'Preferred candidate: Python '+report.recommendedVersion:'No candidate satisfies the declared constraints.'),
        h('p',{},report.scope),
        report.candidates.map(candidate=>h('div',{class:'environment-row'},
          h('strong',{},'Python '+candidate.version+' — '+candidate.status),
          h('p',{},candidate.wheelMatches+' matching wheels; '+candidate.wheelGaps.length+' wheel gaps.'),
          [...candidate.reasons,...candidate.wheelGaps,...candidate.unknown].map(reason=>h('p',{},reason)))))),
      (run?.attempts||[]).map(a=>h('div',{class:'environment-row'},h('h3',{},a.role==='run_order'?'Assessed run order':'Setup attempt '+a.number),h('p',{},a.summary||a.reason||'Reviewing evidence…'),renderAgentTrace(s,a.agentTrace,actions,'repair-'+a.number))),
      run?.status==='awaiting_approval'&&h('div',{class:'environment-row'},
        h('h3',{},'Approve repair'),h('p',{},run.approval.summary),h('p',{},run.reason),
        run.approval.changes.map(change=>h('p',{},change)),
        h('p',{},run.approval.kind==='bun'?'Bun will be installed in Engelbart local storage and used for this project.':'Python will be stored locally by Engelbart. System defaults and the existing project environment stay unchanged.'),
        h('details',{},h('summary',{},'Exact repair plan'),h('pre',{},JSON.stringify(run.approval.proposal,null,2))),
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:()=>actions.decideProjectRepair(true)},run.approval.kind==='bun'?'Install Bun and continue':'Approve and retry'),
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:()=>actions.decideProjectRepair(false)},'Decline')),
      s.error&&h('p',{role:'alert'},s.error),
      run?.status==='failed'&&h('div',{role:'alert'},h('h3',{},'Failed: '+run.stage),
        h('p',{},run.reason),h('p',{},run.exitCode!=null?'Exit code: '+run.exitCode:''),
        h('pre',{},(run.stderr||run.stdout||'').slice(-2000))),

      run&&h('details',{open:s.logsOpen||null,ontoggle:e=>actions.toggleRunLogs(e.target.open)},h('summary',{},'Show logs'),
        (run.failures||[]).map(f=>h('div',{},h('h4',{},'Failure: '+f.stage),h('pre',{},[f.command,f.reason,f.stdout,f.stderr].filter(Boolean).join('\n')))),
        run.stages.map(step=>h('div',{},h('h4',{},step.stage),h('pre',{},step.stdout||''),h('pre',{},step.stderr||'')))),
      h('div',{class:'project-analysis-actions'},
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.chooseAnotherProject},active||run?.status==='running'?'Stop and reset':'Reset'),
        run?.status==='running'&&h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.restartProjectRun},'Stop and restart'),
        run&&['ready','failed','needs_input','unsupported'].includes(run.status)&&h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.startProjectRun},run?.status==='ready'?'Run project':'Retry'),
        !active&&run?.status!=='running'&&h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.inspectProjectEnvironment},'Environment check'),
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:run?.status==='running'?actions.showLivePreview:actions.closeNewProject},run?.status==='running'?'Live preview':'Close'))));
}
