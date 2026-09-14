import {h} from './dom.js';

// Only supervisor-confirmed loopback services may be embedded. Never embed
// Engelbart itself: sandbox allow-same-origin must not grant host UI access.
export function previewServices(run){
  if(run?.status!=='running'||!run.healthy)return [];
  // Older supervisors report verified addresses on completed service stages.
  const verified=(run.stages||[]).filter(step=>step.status==='done'&&step.healthUrl);
  const services=run.previewServices??(verified.length?verified.map(step=>({id:step.stage,url:step.healthUrl,healthy:true,isEntry:step.healthUrl===run.url})):[{id:'app',url:run.url,healthy:true,isEntry:true}]);
  return services.filter(service=>{
    try{
      const url=new URL(service.url);
      return ['http:','https:'].includes(url.protocol)&&['127.0.0.1','localhost','[::1]'].includes(url.hostname)&&
        !url.username&&!url.password&&url.origin!==globalThis.location?.origin;
    }catch{return false;}
  });
}
export function renderProjectPreview(s,actions){
  const services=previewServices(s.run);
  if(!services.length)return null;
  const selected=services.find(p=>p.id===s.previewService)||services.find(p=>p.isEntry)||services[0];
  const index=services.indexOf(selected);
  const select=(i)=>{actions.selectPreviewService(services[i].id);document.getElementById('project-preview-tab-'+i)?.focus();};
  return h('section',{class:'project-preview',key:'project-preview','aria-label':'Live service previews'},
    h('h3',{},'Live preview'),
    h('div',{role:'tablist','aria-label':'Running services',class:'project-preview-tabs'},services.map((service,i)=>
      h('button',{id:'project-preview-tab-'+i,role:'tab','aria-selected':String(i===index),'aria-controls':'project-preview-panel',tabindex:i===index?'0':'-1',
        onclick:()=>actions.selectPreviewService(service.id),onkeydown:e=>{
          const next=e.key==='ArrowRight'?(i+1)%services.length:e.key==='ArrowLeft'?(i+services.length-1)%services.length:e.key==='Home'?0:e.key==='End'?services.length-1:null;
          if(next!==null){e.preventDefault();select(next);}
        }},service.id+(service.isEntry?' · Main app':'')+(service.healthy?'':' · Unavailable')))),
    h('div',{id:'project-preview-panel',role:'tabpanel','aria-labelledby':'project-preview-tab-'+index},
      h('div',{class:'project-preview-toolbar'},h('code',{},selected.url),
        h('button',{class:'ghost-btn',disabled:!selected.healthy,onclick:actions.refreshProjectPreview},'Refresh preview'),
        h('a',{href:selected.url,target:'_blank',rel:'noopener noreferrer'},'Open in new tab')),
      !selected.healthy?h('p',{role:'status'},'This service is unavailable. Return to Run to check the logs.'):
        selected.embeddable===false?h('p',{},'This service blocks embedded previews. Open it in a new tab.'):
        h('iframe',{key:[s.run.id,selected.id,selected.url,s.previewRevision||0].join(':'),src:selected.url,title:'Preview: '+selected.id,
          sandbox:'allow-scripts allow-same-origin allow-forms allow-downloads allow-popups',referrerpolicy:'no-referrer',class:'project-preview-frame'}),
      h('p',{class:'project-preview-note'},'Some services expose APIs or modules without a standalone page. If the preview is blank or blocked, open it in a new tab.')));
}
