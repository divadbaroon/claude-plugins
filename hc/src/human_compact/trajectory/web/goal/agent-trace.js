import {h} from './dom.js';
// Legacy traces retain JSON responses and evidence; derive labels only from those fields.
function object(text){try{return JSON.parse(text.slice(text.indexOf('{'),text.lastIndexOf('}')+1));}catch{return {};}}
function facts(call){
  const response=object(call.response||'');
  const evidence=object((call.prompt||'').split('Evidence JSON:\n')[1]||'');
  return {outcome:call.outcome||response.status,followupReason:call.followupReason||evidence.followupReason,
    requested:call.requestedFiles||response.files||[],
    supplied:call.suppliedFiles||(evidence.requestedFiles||[]).map(f=>f.path)};
}
export function renderAgentTrace(s,trace,actions,prefix){
  if(!trace)return null;
  const calls=trace.calls||[],last=calls.at(-1), info=calls.map(facts);
  const elapsed=c=>Math.max(0,Math.floor(c.durationSeconds??((Date.now()/1000)-(c.startedAt||Date.now()/1000))));
  const detail=(key,label,body)=>h('details',{open:s.agentDetails?.[key]||null,ontoggle:e=>actions.toggleAgentDetails(key,e.target.open)},h('summary',{},label),body);
  const outcome=f=>({read_more:'Requested additional evidence',plan:'Returned a launch plan',needs_input:'Needs your input',unsupported:'No supported plan returned'}[f.outcome]||'Response received');
  const parent=prefix==='order'?s.order:null;
  let phase=!last?'Preparing evidence':last.status==='running'?'Waiting for model response':last.status==='timed_out'?'Model call timed out':last.status==='error'?'Model call failed':outcome(info.at(-1));
  if(parent?.status==='assessing'&&last?.status==='done'&&info.at(-1)?.outcome==='read_more')phase='Reading requested files';
  if(parent?.progress==='Validating returned launch plan'&&parent.status==='assessing')phase='Validating plan';
  if(parent?.status==='done')phase='Assessment complete';
  if(parent?.status==='error')phase='Assessment failed';
  if(prefix.startsWith('repair-')&&s.run?.attempts?.find(a=>'repair-'+a.number===prefix)?.status==='rejected')phase='Proposal rejected by validator';
  const names=files=>files.map(f=>typeof f==='string'?f:f.path).filter(Boolean).join(', ');
  return h('div',{class:'agent-trace'},h('h3',{},trace.name+' · '+phase),
    h('p',{class:'agent-trace-meta'},calls.length+' model '+(calls.length===1?'call':'calls')+' · '+calls.reduce((sum,c)=>sum+elapsed(c),0)+'s model time'),
    h('p',{class:'agent-trace-meta'},'One '+(prefix==='order'?'assessment':'repair attempt')+' can include multiple model calls when more evidence is needed.'),
    !calls.length&&h('p',{},'Preparing evidence. No model request has started.'),
    h('ol',{class:'agent-timeline'},calls.map((c,i)=>h('li',{},
      info[i].followupReason&&h('p',{},'Host initiated follow-up: '+info[i].followupReason),
      info[i].supplied.length>0&&h('p',{},(info[i].followupReason?'✓ Engelbart supplied additional file excerpts: ':'✓ Engelbart supplied requested file excerpts: ')+names(info[i].supplied)),
      h('p',{role:c.status==='running'?'status':undefined},'Call '+(i+1)+' — '+(info[i].followupReason?'Follow-up evidence review':info[i].supplied.length?'Assessment with requested files':i===0?'Initial assessment':'Follow-up assessment')+' · '+(c.status==='running'?'Waiting for model response':c.status==='done'?outcome(info[i]):c.status==='timed_out'?'Timed out':'Failed')),
      info[i].outcome==='read_more'&&h('p',{},'Requested: '+names(info[i].requested)),
      c.error&&h('p',{role:'alert'},c.error)))),
    parent?.status==='done'&&h('p',{},'✓ Host validated the launch plan.'),
    detail(prefix+'-calls','View prompts and responses',h('div',{},calls.map((c,i)=>h('div',{class:'agent-trace-call'},
      h('h4',{},'Call '+(i+1)+' — '+(info[i].followupReason?'Follow-up evidence review':info[i].supplied.length?'Assessment with requested files':i===0?'Initial assessment':'Follow-up assessment')),
      h('p',{class:'agent-trace-meta'},c.provider+' · '+c.model+' · '+(c.promptChars||0).toLocaleString()+' prompt characters · '+c.effort+' effort · tools '+c.tools+' · '+elapsed(c)+'s / '+c.timeoutSeconds+'s limit'),
      c.status==='running'&&h('p',{class:'agent-trace-meta'},'The request is in progress; internal model reasoning is not streamed.'),
      detail(prefix+'-prompt-'+i,'Prompt · call '+(i+1),h('pre',{},c.prompt)),
      c.response!==undefined&&detail(prefix+'-response-'+i,'Response · call '+(i+1),h('div',{},h('pre',{},c.response),c.responseTruncated&&h('p',{},'Response display capped at 24,000 characters.'))))))),
    calls.length>0&&h('p',{class:'agent-trace-meta'},'Prompts and responses are saved locally with known values redacted.'));
}
