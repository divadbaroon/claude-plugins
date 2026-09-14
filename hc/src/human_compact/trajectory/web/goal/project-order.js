import {copyModalButton} from './modal-copy.js';
import {renderAgentTrace} from './agent-trace.js';
import {h} from './dom.js';
import {renderAutoContinue} from './project-auto.js';
export function createOrderActions(store,services,onReady=async()=>{},storage=globalThis.localStorage){
  const update=patch=>store.set({newProject:{...store.get().newProject,...patch}});
  async function resumeProjectOrder(id){
    update({page:'order',busy:true,error:''});
    try{
      while(store.get().newProject?.page==='order'){
        const answer=await services.projectOrderState({id});
        if(!answer.ok)throw new Error(answer.error);
        const order=answer.order;
        update({order});
        if(!['analyzing','assessing'].includes(order.status)){
          update({busy:false,result:order.status==='done'?{ok:true,path:order.path,name:order.path.split('/').pop(),analysisId:id,info:{},orderPlan:order.plan,environmentPaths:order.environmentPaths||[]}:null});
          if(order.status==='done')await onReady();
          return;
        }
        await new Promise(resolve=>setTimeout(resolve,700));
      }
    }catch(error){update({busy:false,error:error.message});}
  }
  return {
    resumeProjectOrder,
    async analyzeProjectOrder(path){
      update({page:'order',busy:true,error:'',order:{status:'analyzing',progress:'Analyzing project components'}});
      try{
        const answer=await services.startProjectOrder({path});
        if(!answer.ok)throw new Error(answer.error);
        try{storage.setItem('engelbart-project-order',answer.order.id);}catch(e){}
        await resumeProjectOrder(answer.order.id);
      }catch(error){update({busy:false,error:error.message});}
    },
    resetProjectOrder(){
      if(store.get().newProject.busy)return;
      try{storage.removeItem('engelbart-project-order');}catch(e){}
      store.set({newProject:{page:'plan',path:'',busy:false,result:null,error:''}});
    }
  };
}
export function renderOrder(s,actions){
  const o=s.order,p=o?.plan;
  return h('div',{class:'project-analysis-scrim',onclick:actions.closeNewProject},
    h('section',{class:'project-analysis-modal',role:'dialog','aria-modal':'true','aria-label':'Project run order',onclick:e=>e.stopPropagation(),onkeydown:e=>{if(e.key==='Escape')actions.closeNewProject();}},
      h('h2',{},'Project run order'),copyModalButton(),renderAutoContinue(s,actions),h('p',{},o?.path||s.path),
      s.busy&&h('p',{role:'status'},o?.progress||'Assessing run order…'),
      renderAgentTrace(s,o?.agentTrace,actions,'order'),
      o?.status==='error'&&!o.agentTrace&&h('p',{},'Prompt details were not recorded for this older attempt. Retry assessment to capture them.'),
      s.error&&h('p',{role:'alert'},s.error),
      ['error','needs_input','unsupported'].includes(o?.status)&&h('div',{role:'alert'},h('h3',{},o.status==='needs_input'?'Choose the intended application':'Run order needs review'),h('p',{},o.error||o.reason)),
      p&&h('div',{},h('p',{},o.summary),h('p',{},o.orderingRationale),o.metadataNote&&h('p',{},o.metadataNote),
        ['preparation','services'].map(kind=>h('div',{},h('h3',{},kind==='services'?'Persistent services':'Preparation'),
          p[kind].map(step=>h('div',{class:'environment-row'},h('h4',{},step.id),h('p',{},'Run from: '+step.cwd),h('pre',{},step.argv.join(' ')),
            step.environmentCwd&&h('p',{},'Configuration from: '+step.environmentCwd),
            step.dependsOn?.length>0&&h('p',{},'After healthy: '+step.dependsOn.join(', ')),step.healthUrl&&h('p',{},'HTTP health: '+step.healthUrl))))),
        h('h3',{},'Evidence'),p.evidence.map(e=>h('p',{},e)),
        o.evidenceNotes?.length>0&&h('details',{},h('summary',{},'Unvalidated agent reference notes'),o.evidenceNotes.map(e=>h('p',{},e))),
        (o.excludedComponents||[]).map(c=>h('p',{},c.id+' · '+c.reason))),
      (o?.components||[]).length>0&&h('div',{class:'agent-trace'},h('h3',{},'Component analysis'),o.components.map(c=>h('p',{},c.component+' · '+(c.status==='running'?'Railpack running · '+Math.floor(Date.now()/1000-c.startedAt)+'s':c.ok?'Railpack analyzed'+(c.durationSeconds!=null?' · '+c.durationSeconds+'s':''):c.error)))),
      p&&h('details',{},h('summary',{},'Raw launch plan'),h('pre',{},JSON.stringify(p,null,2))),
      h('div',{class:'project-analysis-actions'},
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.resetProjectOrder},'Choose another project'),
        !s.busy&&o?.status!=='done'&&h('button',{class:'ghost-btn',onclick:()=>actions.analyzeProjectOrder(o?.path||s.path)},'Retry assessment'),
        p&&h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.inspectProjectEnvironment},'Continue'))));
}
