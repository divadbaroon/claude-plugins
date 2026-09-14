import {createProjectWorkspace,renderProjectWorkspace} from './project-workspace.js';
import {copyModalButton} from './modal-copy.js';
import {createOrderActions,renderOrder} from './project-order.js';
import {createRunActions,renderRun} from './project-run.js';
/* Local plumbing only: retain Railpack's response, never execute its plan. */
import {h} from './dom.js';
import {renderProjectPreview} from './project-preview.js';
import {renderAutoContinue} from './project-auto.js';

export function createProjectAnalysisActions(store,services){
  return createProjectWorkspace(store,services,createProjectSessionActions);
}
function createProjectSessionActions(store, services, storage=globalThis.localStorage) {
  const {get, set} = store;
  let previousFocus;
  const orderActions=createOrderActions(store,services,()=>actions.autoAdvanceProject(),storage);
  const runActions=createRunActions(store,services,storage,()=>actions.autoAdvanceProject());
  const update = patch => set({newProject: {...get().newProject, ...patch}});
  const actions = {
    ...runActions,
    ...orderActions,
    async setProjectAutoContinue(enabled){update({autoContinue:enabled,...(enabled?{viewStep:null,previewVisited:false}:{})});if(enabled)await actions.autoAdvanceProject();},
    async autoAdvanceProject(){
      const s=get().newProject;
      if(!s?.autoContinue||s.busy||s.error)return;
      if((!s.page||['plan','order'].includes(s.page))&&s.result?.ok){await actions.inspectProjectEnvironment();return;}
      if(s.page==='environment'&&s.environment?.ok){
        if(s.environment.localSupabase?.selected&&s.environment.localSupabase.status!=='ready')return;
        const paths=s.result.environmentPaths?.length?s.result.environmentPaths:[s.result.path];
        const safe=(r,path)=>r?.ok&&r.variables.every(v=>
          s.environmentSkips?.[path]?.includes(v.name)||v.blocksContinuation===false||
          ['found','optional'].includes(v.status)||
          (v.status!=='missing'&&v.requirement!=='required'&&v.group!=='required'));
        if(Object.values(s.envValues||{}).some(Boolean)||!safe(s.environment,s.environmentPath||s.result.path))return;
        if(!paths.every(path=>safe(s.environmentChecks?.[path],path))){
          const unchecked=paths.find(path=>!s.environmentChecks?.[path]);
          if(unchecked){update({environmentPath:unchecked});await actions.inspectProjectEnvironment();}
          return;
        }
        await actions.openProjectRun();
        await actions.autoAdvanceProject();
        return;
      }
      if(s.page==='run'&&s.run?.status==='running'&&s.run.healthy){actions.showLivePreview();return;}
      if(s.page==='run'&&s.run?.status==='ready'&&s.autoRunRequested!==s.result.analysisId){
        update({autoRunRequested:s.result.analysisId});
        await actions.startProjectRun();
      }
    },
    async navigateProjectStep(step){
      const s=get().newProject;
      if(step==='preview'){actions.showLivePreview();return;}
      if(step==='run'&&!s.run){await actions.continueProjectEnvironment();return;}
      if(step==='environment'&&!s.environment){if(s.result?.ok&&!s.busy){update({viewStep:null});await actions.inspectProjectEnvironment();}return;}
      update({viewStep:step});
    },
    toggleAgentDetails(key,open){if(!!get().newProject.agentDetails?.[key]!==open)update({agentDetails:{...get().newProject.agentDetails,[key]:open}});},
    openNewProject() {
      previousFocus = typeof document !== 'undefined' ? document.activeElement : null;
      set({newProject: {path:'', busy:false, result:null, error:''}});
      try{const orderId=storage.getItem('engelbart-project-order');if(orderId){orderActions.resumeProjectOrder(orderId);return;}}catch(e){}
      try{const id=storage.getItem('engelbart-project-run');if(id){update({result:{analysisId:id}});runActions.openProjectRun();}}catch(e){}
    },
    closeNewProject() {
      if (get().newProject?.busy) return;
      set({newProject:null});
      previousFocus?.focus();
    },
    backToProjectPlan() { if (!get().newProject?.busy) update({page:get().newProject.order?'order':'plan',environment:null,envValues:{},error:''}); },
    toggleEnvironmentEvidence(name,open) { if(!!get().newProject.envExpanded?.[name]!==open)update({envExpanded:{...get().newProject.envExpanded,[name]:open}}); },
    async skipEnvironmentValue(name,skip){
      const s=get().newProject,path=s.environmentPath||s.result.path;
      if(!s.environment?.variables.some(v=>v.name===name&&v.requirement==='required'))return;
      const names=new Set(s.environmentSkips?.[path]||[]);if(skip)names.add(name);else names.delete(name);
      update({environmentSkips:{...s.environmentSkips,[path]:[...names]},...(skip?{envValues:{...s.envValues,[name]:''}}:{})});
      await actions.autoAdvanceProject();
    },
    async setupLocalSupabase(action='start'){
      const s=get().newProject;if(s.busy)return;
      const path=s.environmentPath||s.result.path;
      const repositoryRoot=s.result.repositoryRoot||s.environment.repositoryRoot||s.result.path;
      update({busy:true,error:''});
      try{
        let localSupabase=await services.projectLocalSupabase({path,repositoryRoot,action});
        while(true){
          if(!localSupabase?.ok)throw new Error(localSupabase?.error||'Local setup failed');
          update({environment:{...get().newProject.environment,localSupabase}});
          if(localSupabase.status!=='working')break;
          await new Promise(resolve=>setTimeout(resolve,2000));
          localSupabase=await services.projectLocalSupabase({path,repositoryRoot,action:'inspect'});
        }
        const environment=await services.inspectProjectEnvironment({path,repositoryRoot});
        if(!environment.ok)throw new Error(environment.error||'Could not refresh configuration');
        // Local provisioning owns these fields; discard stale production drafts and skips.
        const owned=new Set(localSupabase.variableNames||[]);
        const envValues=Object.fromEntries(Object.entries(get().newProject.envValues||{}).filter(([name])=>!owned.has(name)));
        const skips=(get().newProject.environmentSkips?.[path]||[]).filter(name=>!owned.has(name));
        update({environment,envValues,environmentSkips:{...get().newProject.environmentSkips,[path]:skips},environmentChecks:{...get().newProject.environmentChecks,[path]:environment}});
      }catch(error){update({error:error.message});}
      finally{update({busy:false});}
      if(action==='start'&&get().newProject.environment?.localSupabase?.status==='ready')await actions.autoAdvanceProject();
    },
    async skipAllEnvironmentValues(){
      const s=get().newProject;
      if(s.busy||!s.environment)return;
      const path=s.environmentPath||s.result.path;
      const names=new Set(s.environmentSkips?.[path]||[]);
      for(const row of s.environment.variables){
        if(row.editable!==false&&row.requirement==='required'&&row.status!=='found'&&!s.envValues?.[row.name])names.add(row.name);
      }
      update({environmentSkips:{...s.environmentSkips,[path]:[...names]}});
      await actions.autoAdvanceProject();
    },
    editEnvironmentValue(name,value) { update({envValues:{...get().newProject.envValues,[name]:value}}); },
    async selectEnvironmentPath(path){if(get().newProject.busy)return;update({environmentPath:path,envValues:{}});await this.inspectProjectEnvironment();},
    async inspectProjectEnvironment() {
      if(get().newProject?.busy)return;
      const selected=get().newProject;
      const environmentPath=selected.environmentPath||selected.result.environmentPaths?.[0]||selected.result.path;
      update({page:'environment',viewStep:null,environmentPath,busy:true,error:'',envValues:{}});
      try {
        const environment=await services.inspectProjectEnvironment({path:environmentPath,repositoryRoot:selected.result.repositoryRoot||selected.discovery?.root||selected.result.path});
        if(!environment?.ok)throw new Error(environment?.error||'Could not inspect environment');
        update({environment,environmentChecks:{...get().newProject.environmentChecks,[environmentPath]:environment}});
      } catch(error){update({error:error.message});}
      finally{update({busy:false});}
      await actions.autoAdvanceProject();
    },
    async saveProjectEnvironment(advance=true) {
      if(get().newProject?.busy)return;
      const {result,envValues}=get().newProject;
      let saved=false;
      update({busy:true,error:''});
      try {
        const environment=await services.saveProjectEnvironment({path:get().newProject.environmentPath||result.path,values:envValues||{}});
        if(!environment?.ok)throw new Error(environment?.error||'Could not save locally');
        saved=true;
        update({environment,envValues:{},environmentChecks:{...get().newProject.environmentChecks,[get().newProject.environmentPath||result.path]:environment}});
      } catch(error){update({error:error.message});}
      finally{update({busy:false});}
      if(advance)await actions.autoAdvanceProject();
      return saved;
    },
    async continueProjectEnvironment(){
      let s=get().newProject;
      if(s.busy||!s.environment?.ok)return;
      if(Object.values(s.envValues||{}).some(Boolean)){
        if(!await actions.saveProjectEnvironment(false))return;
      }
      s=get().newProject;
      if(s.environment.localSupabase?.selected&&s.environment.localSupabase.status!=='ready')return;
      if(s.environment.variables.some(v=>v.group==='required'&&v.blocksContinuation&&!s.environmentSkips?.[s.environmentPath||s.result.path]?.includes(v.name)))return;
      update({viewStep:null});await actions.openProjectRun();await actions.autoAdvanceProject();
    },
    toggleNewProjectRaw(open) { if (get().newProject?.rawOpen !== open) update({rawOpen:open}); },
    editNewProjectPath(path) { if (!get().newProject?.busy) update({path, result:null, discovery:null, environmentChecks:{}, error:''}); },
    async chooseNewProjectFolder() {
      if (get().newProject?.busy) return;
      update({busy:true,error:''});
      try {
        const answer = await services.chooseProjectDirectory({start:get().newProject.path});
        if (!answer?.ok) throw new Error(answer?.error || 'Could not open the folder picker. Enter a path instead.');
        if (!answer.cancelled) update({path:answer.cwd, result:null});
      } catch (error) { update({error:error.message}); }
      finally { update({busy:false}); }
    },
    backToComponents(){if(!get().newProject.busy)update({page:'components',result:null,error:''});},
    async analyzeComponent(id){
      const state=get().newProject;
      const component=state.discovery.components.find(c=>c.id===id);
      if(!component?.path||component.requiresContainer||state.busy)return;
      update({path:component.path,page:'plan',busy:true,analyzing:true,error:'',result:null});
      try{const result=await services.analyzeProject({path:component.path,repositoryRoot:state.discovery.root});update({result,error:result?.ok?'':result?.error||'Analysis failed'});}
      catch(error){update({error:error.message});}
      finally{update({busy:false,analyzing:false});}
      await actions.autoAdvanceProject();
    },
    async analyzeNewProject() {
      const state = get().newProject;
      if (!state || state.busy) return;
      update({busy:true, result:null, environmentChecks:{}, error:'', analyzing:true});
      try {
        const discovery=await services.discoverProjectComponents({path:state.path});
        if(!discovery?.ok)throw new Error(discovery?.error||'Component discovery failed');
        update({discovery,path:discovery.root});
        if(discovery.components.length>1){await orderActions.analyzeProjectOrder(discovery.root);return;}
        const component=discovery.components[0];
        if(component?.requiresContainer)throw new Error('This project requires a container-capable runtime.');
        const result = await services.analyzeProject({path:component?.path||discovery.root,repositoryRoot:discovery.root});
        update({result, error:result?.ok ? '' : result?.error || 'Could not analyze the project. Retry.'});
      } catch (error) { update({error:error.message}); }
      finally { update({busy:false, analyzing:false}); }
      await actions.autoAdvanceProject();
    },
  };
  return actions;
}

export function renderNewProject(state,actions){
  if(!state.newProject)return null;
  return renderProjectWorkspace(state,actions,state.projectWorkspaceAdding?null:renderProjectSession(state,actions));
}
function renderProjectSession(state, actions) {
  const original=state.newProject;
  if(!original)return null;
  const s={...original,page:original.viewStep||original.page};
  if(s.page==='preview')return h('section',{class:'project-live-step'},renderProjectPreview(s,actions)||h('p',{role:'status'},'The preview is unavailable. Return to Run to see the service status and logs.'));
  if(s.page==='order')return renderOrder(s,actions);
  if(s.page==='components')return renderComponents(s,actions);
  if(s.page==='run')return renderRun(s,actions);
  if(s.page==='environment')return renderEnvironment(s,actions);
  const r=s.result;
  const button=(label, onclick, disabled=s.busy)=>h('button',{type:'button',class:'ghost-btn',disabled,onclick},label);
  return h('div',{class:'project-analysis-scrim',key:'new-project',onclick:actions.closeNewProject},
    h('section',{class:'project-analysis-modal',role:'dialog','aria-modal':'true','aria-labelledby':'new-project-title',
      onclick:e=>e.stopPropagation(),onkeydown:e=>{
        if(e.key==='Escape'){e.preventDefault();actions.closeNewProject();}
        if(e.key==='Tab'){
          const nodes=Array.from(e.currentTarget.querySelectorAll('button:not(:disabled),input:not(:disabled),summary'));
          if(!nodes.length)return;
          const first=nodes[0],last=nodes[nodes.length-1];
          if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
          else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
        }
      }},
      h('h2',{id:'new-project-title'},'New Project'),copyModalButton(),renderAutoContinue(s,actions),
      s.discovery?.components.length>1&&button('Back to components',actions.backToComponents),
      h('form',{onsubmit:e=>{e.preventDefault();actions.analyzeNewProject();}},
        h('label',{for:'new-project-path'},'Local folder or GitHub URL'),
        h('input',{id:'new-project-path',class:'goal-input',value:s.path,disabled:s.busy,
          placeholder:'https://github.com/owner/repo or /Users/you/Projects/my-project',autocomplete:'off',spellcheck:'false',
          oninput:e=>actions.editNewProjectPath(e.target.value)}),
        h('div',{class:'project-analysis-actions'},button('Choose folder',actions.chooseNewProjectFolder),
          h('button',{type:'submit',class:'ghost-btn',disabled:s.busy||!s.path.trim()},'Analyze project'))),
      h('p',{},'GitHub repositories are cloned into Engelbart’s projects folder and reused without pulling or overwriting local changes.'),
      s.discovery?.checkout&&h('p',{},(s.discovery.checkout.reused?'Reusing local checkout: ':'Cloned locally: ')+s.discovery.root),
      s.busy&&h('p',{role:'status','aria-live':'polite'},s.analyzing?('https://github.com/'===s.path.slice(0,19)?'Preparing GitHub checkout and analyzing project…':'Analyzing project…'):'Choosing folder…'),
      s.error&&h('pre',{class:'project-analysis-error',role:'alert'},s.error),
      r?.ok&&h('div',{},h('h3',{},r.name),h('p',{},r.path),h('h3',{},r.source==='native-static'?'Native static-site analysis':'Railpack analysis'),
        h('h4',{},'Detected'),h('p',{},[...(r.info.detectedProviders||[]),r.info.metadata?.nodePackageManager].filter(Boolean).join(' / ') || 'See Railpack output'),
        h('h4',{},r.source==='native-static'?'Local serving plan':'Build plan'),h('pre',{},r.stdout || JSON.stringify(r.plan,null,2))),
      r&&h('details',{open:s.rawOpen||null,ontoggle:e=>actions.toggleNewProjectRaw(e.target.open)},h('summary',{},'Raw plan and output'),
        h('h4',{},'Plan JSON'),h('pre',{},r.rawPlan||'No plan returned.'),
        h('h4',{},'Project information JSON'),h('pre',{},r.rawInfo||''),
        h('h4',{},'stdout'),h('pre',{},r.stdout||''),h('h4',{},'stderr'),h('pre',{},r.stderr||'')),
      h('div',{class:'project-analysis-actions'},button(r?.ok?'Continue':'Cancel',r?.ok?actions.inspectProjectEnvironment:actions.closeNewProject))));
}

function renderEnvironment(s, actions) {
  const report=s.environment;
  const labels={found:'Found locally · value hidden',missing:'Missing and required',optional:'Optional',uncertain:'Uncertain — review',not_applicable:'Not evaluated for this app',inventory:'Not evaluated for this app'};
  const groups=[['required','Required for this app'],['optional','Optional for this app'],['other','Other apps and tasks'],['unresolved','Unresolved usage']];
  const variables=report?.displayVariables||report?.variables||[];
  return h('div',{class:'project-analysis-scrim',key:'new-project-environment',onclick:actions.closeNewProject},
    h('section',{class:'project-analysis-modal',role:'dialog','aria-modal':'true','aria-labelledby':'env-title',
      onclick:e=>e.stopPropagation(),onkeydown:e=>{
        if(e.key==='Escape'){e.preventDefault();actions.closeNewProject();}
        if(e.key==='Tab'){
          const nodes=Array.from(e.currentTarget.querySelectorAll('button:not(:disabled),input:not(:disabled),summary'));
          const first=nodes[0],last=nodes[nodes.length-1];
          if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
          else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
        }
      }},
      h('h2',{id:'env-title'},'Environment check'),copyModalButton(),renderAutoContinue(s,actions),h('p',{},s.environmentPath||s.result.path),
      s.result.environmentPaths?.length>1&&h('label',{},'Component configuration',h('select',{disabled:s.busy||Object.values(s.envValues||{}).some(Boolean),value:s.environmentPath,onchange:e=>actions.selectEnvironmentPath(e.target.value)},s.result.environmentPaths.map(path=>h('option',{value:path,selected:path===s.environmentPath},path)))),
      h('p',{},'Review configuration for the production build and start plan. Values you enter are saved only in local Engelbart storage, outside the repository.'),
      report?.localSupabase?.available&&h('section',{class:'environment-group'},
        h('h3',{},'Local Supabase'),
        h('p',{},report.localSupabase.reason||'Use an isolated local database instead of entering hosted Supabase credentials.'),
        h('p',{},'This action installs Docker if needed and the Supabase CLI, downloads service images, and creates local database storage. Docker Desktop may show system and license prompts. Existing compatible local engines are reused.'),
        h('p',{},'Local auth, database and storage only. External auth providers and edge functions are disabled. Unrelated API keys are unchanged.'),
        report.localSupabase.warnings?.map(w=>h('p',{},w)),
        h('button',{class:'ghost-btn',disabled:s.busy||report.localSupabase.status==='ready',onclick:()=>actions.setupLocalSupabase('start')},report.localSupabase.status?'Resume local Supabase':'Install Docker and set up local Supabase'),
        report.localSupabase.status&&h('button',{class:'ghost-btn',disabled:s.busy,onclick:()=>actions.setupLocalSupabase('stop')},'Stop local services'),
        h('p',{},'Stopping preserves database data. Hosted configuration can still be entered in the fields below when local setup has not been selected.')),
      s.busy&&h('p',{role:'status'},report?.localSupabase?.status==='working'?'Setting up local services…':'Checking environment…'),
      s.error&&h('p',{role:'alert'},s.error),
      report&&h('div',{},
        report.repositoryRoot&&h('p',{},'Repository searched: '+report.repositoryRoot),
        report.repositoryExamples?.length>0&&h('p',{},'Environment examples checked: '+report.repositoryExamples.join(', ')),
        !report.variables.length&&h('p',{},'No environment variables detected. This does not prove none are needed.'),
        groups.map(([group,title])=>h('section',{class:'environment-group'},h('h3',{},title),
          !variables.some(row=>(row.group||'unresolved')===group)&&h('p',{},'None detected.'),
          variables.filter(row=>(row.group||'unresolved')===group).map(row=>h('div',{class:'environment-row',key:row.name},
          h('h4',{},row.name),h('p',{},s.environmentSkips?.[s.environmentPath||s.result.path]?.includes(row.name)?'Skipped for this run':s.envValues?.[row.name]?'Entered · saved when you continue':row.group==='other'?'Not required for this launch':row.status==='found'&&row.public?'Found locally':labels[row.status]),row.purpose&&h('p',{},row.purpose),
          row.editable!==false&&row.requirement==='required'&&row.status!=='found'&&h('label',{class:'environment-skip'},h('input',{type:'checkbox',checked:s.environmentSkips?.[s.environmentPath||s.result.path]?.includes(row.name)||false,onchange:e=>actions.skipEnvironmentValue(row.name,e.target.checked)}),' Skip '+row.name+' for this run'),
          row.source&&h('p',{},'Source: '+row.source),
          row.public&&h('p',{},'Public variable — may be included in browser code.'),
          h('details',{open:s.envExpanded?.[row.name]||null,ontoggle:e=>actions.toggleEnvironmentEvidence(row.name,e.target.open)},h('summary',{},'Why this classification?'),
            h('p',{},'Requirement: '+row.requirement+(row.hasDefault?' · default found in code or schema':'')),
            row.requirementScope==='script'&&row.group!=='other'&&h('p',{},'Required status applies to the referenced script; whether this launch uses it still needs review.'),
            row.conflict&&h('p',{},'Conflicting declarations require review.'),
            row.evidence.map(e=>h('p',{},e.file+':'+e.line+' · '+e.kind))),
          row.editable!==false&&!s.environmentSkips?.[s.environmentPath||s.result.path]?.includes(row.name)&&h('label',{},row.status==='found'?(row.public?'Value':'Replace saved value'):row.status==='missing'?'Provide the required value':'Provide a value if needed',
            h('input',{type:row.public?'text':'password',autocomplete:'off',disabled:s.busy,value:s.envValues?.[row.name]??row.publicValue??'',placeholder:row.status==='found'&&!row.public?'Saved value hidden; enter a replacement':'',
              'aria-label':row.name,oninput:e=>actions.editEnvironmentValue(row.name,e.target.value)})))))),
        h('details',{},h('summary',{},'Detection limits and diagnostic warnings'),h('p',{},report.limitations),report.warnings.map(w=>h('p',{},w)))),
      h('div',{class:'project-analysis-actions'},
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.backToProjectPlan},'Back'),
        h('button',{class:'ghost-btn',disabled:s.busy,onclick:actions.inspectProjectEnvironment},'Recheck'),
        h('button',{class:'ghost-btn',disabled:s.busy||!report?.variables.some(row=>row.editable!==false&&row.requirement==='required'&&row.status!=='found'&&!s.envValues?.[row.name]&&!s.environmentSkips?.[s.environmentPath||s.result.path]?.includes(row.name)),onclick:actions.skipAllEnvironmentValues,title:'Skip all missing required values for this component and run'},'Skip all'),
        h('button',{class:'ghost-btn',disabled:s.busy||!Object.values(s.envValues||{}).some(Boolean),onclick:actions.saveProjectEnvironment},'Save locally'),
        h('button',{class:'ghost-btn',disabled:s.busy||!report||(report.localSupabase?.selected&&report.localSupabase.status!=='ready')||report.variables.some(v=>v.group==='required'&&v.blocksContinuation&&!s.envValues?.[v.name]&&!s.environmentSkips?.[s.environmentPath||s.result.path]?.includes(v.name)),onclick:actions.continueProjectEnvironment},Object.values(s.envValues||{}).some(Boolean)?'Save and continue':'Continue'))));
}

function renderComponents(s,actions){
  const d=s.discovery;
  return h('div',{class:'project-analysis-scrim',onclick:actions.closeNewProject},
    h('section',{class:'project-analysis-modal',role:'dialog','aria-modal':'true','aria-label':'Project components',onclick:e=>e.stopPropagation(),onkeydown:e=>{if(e.key==='Escape')actions.closeNewProject();}},
      h('h2',{},'Project components'),copyModalButton(),h('p',{},d.root),
      h('p',{},'Review the applications and declared startup constraints. Analyze a component to continue with its own Railpack plan and environment.'),
      d.components.map(c=>h('div',{class:'environment-row'},h('h3',{},c.name),h('p',{},c.path||c.id),
        h('p',{},c.types.join(' / ')),c.evidence.map(e=>h('p',{},e.file+' · '+e.kind)),
        c.requiresContainer?h('p',{},'Container service — native execution not supported'):h('button',{class:'ghost-btn',disabled:s.busy,onclick:()=>actions.analyzeComponent(c.id)},'Analyze component'))),
      h('h3',{},'Declared startup order'),
      !d.dependencies.length?h('p',{},'No explicit startup order found. Review the relationships below; no ordering was guessed.'):
        d.order.levels.map((level,i)=>h('p',{},'Group '+(i+1)+': '+level.join(', '))),
      d.dependencies.map(e=>h('p',{},e.service+' waits for '+e.dependency+' · '+e.condition+' · '+e.source)),
      d.order.issues.map(issue=>h('p',{role:'alert'},issue)),
      h('h3',{},'Relationships to review'),
      d.relationships.map(r=>h('p',{},r.service+' · '+r.kind+' · '+(r.targetPath||r.dependencies?.join(', ')||('port '+r.port))+' · '+r.source)),
      h('p',{},d.note),d.warnings.map(w=>h('p',{},w)),
      h('details',{},h('summary',{},'Setup documentation'),d.documentation.map(path=>h('p',{},path))),
      h('button',{class:'ghost-btn',onclick:actions.closeNewProject},'Close')));
}
