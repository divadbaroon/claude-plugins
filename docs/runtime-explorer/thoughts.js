// Persisted questions for this reading layer. Included inside the explorer IIFE.
const THOUGHT_GROUP='Your added questions';
const THOUGHT_POLL_MS=2200;
const thoughtStatusLabels={
 waiting:'Waiting to send',sending:'Sending to Codex',queued:'Queued in this Codex conversation',
 answering:'Codex is answering',answered:'Answered',failed:'Delivery failed',
 uncertain:'Delivery uncertain'
};
let thoughtToken='',thoughtConnected=false,thoughtConnectionError='',thoughtPollTimer=0;
let thoughtPollBusy=false,thoughtInitialized=false,thoughtSelection=null,thoughtLastSignature='';
let thoughtDeferredArticleRefresh='';
const thoughtNodes=new Map(),thoughtTransientErrors=new Map();

function thoughtLimit(value,max){return String(value||'').slice(0,max)}
function thoughtStatus(node){return thoughtStatusLabels[node?.status]||'Saved'}
function thoughtStatusClass(status){return ['answered','failed','uncertain'].includes(status)?status:'pending'}
function thoughtUuid(){
 const raw=globalThis.crypto?.randomUUID?.()||([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g,c=>(c^crypto.getRandomValues(new Uint8Array(1))[0]&15>>c/4).toString(16));
 return 'q_'+raw;
}
function thoughtNodeToQa(node){
 let item=qaById[node.id];
 if(!item){
  item={id:node.id,group:THOUGHT_GROUP,title:node.question,answer:[node.answer||'',node.error||''],sources:[],related:[],isThought:true,thought:node};
  qa.push(item);qaById[node.id]=item;
 }else{
  item.title=node.question;item.answer=[node.answer||'',node.error||''];item.thought=node;
 }
 thoughtNodes.set(node.id,node);
 return item;
}
function thoughtSignature(nodes){
 return JSON.stringify(nodes.map(n=>[n.id,n.parent_id,n.question,n.status,n.answer,n.error,n.updated_at]));
}
function thoughtHasDraft(){return Boolean(document.querySelector('.thought-composer'))}
function thoughtHasSelection(){const s=document.getSelection();return Boolean(s&&!s.isCollapsed&&s.toString())}
function thoughtCanRefreshArticle(){return !thoughtHasDraft()&&!thoughtHasSelection()}
function thoughtRefresh(changedIds=[]){
 if(!changedIds.length||guideMode!=='questions'||!$('qa-results'))return;
 const focusedId=document.activeElement?.dataset?.qa;
 renderQuestionList();
 if(focusedId)[...document.querySelectorAll('[data-qa]')].find(el=>el.dataset.qa===focusedId)?.focus({preventScroll:true});
 if(changedIds.includes(selectedQuestion)&&qaById[selectedQuestion]?.isThought){
  if(thoughtCanRefreshArticle()){thoughtDeferredArticleRefresh='';renderQuestionArticle()}
  else{thoughtDeferredArticleRefresh=selectedQuestion;updateThoughtStatusInPlace()}
 }else updateThoughtStatusInPlace();
}
function updateThoughtStatusInPlace(){
 const node=thoughtNodes.get(selectedQuestion),el=document.querySelector('[data-thought-current-status]');
 if(!node||!el)return;
 el.className='thought-status '+thoughtStatusClass(node.status);
 el.textContent=thoughtStatus(node);
}
function syncThoughtNodes(nodes){
 const safe=Array.isArray(nodes)?nodes.filter(n=>n&&typeof n.id==='string'&&n.id.startsWith('q_')):[];
 const signature=thoughtSignature(safe);
 if(signature===thoughtLastSignature)return [];
 const changed=[];
 safe.forEach(node=>{
  const was=thoughtNodes.get(node.id);
  if(!was||thoughtSignature([was])!==thoughtSignature([node]))changed.push(node.id);
  thoughtNodeToQa(node);
 });
 thoughtLastSignature=signature;
 if(!groups.includes(THOUGHT_GROUP))groups.push(THOUGHT_GROUP);
 return changed;
}
async function fetchThoughts(initial=false){
 if(thoughtPollBusy)return;
 thoughtPollBusy=true;
 try{
  const response=await fetch('/api/thoughts',{headers:{Accept:'application/json'},credentials:'same-origin',cache:'no-store'});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.error||'Could not load added questions.');
  thoughtToken=typeof data.token==='string'?data.token:thoughtToken;
  thoughtConnected=data.connected===true;
  thoughtConnectionError=thoughtConnected?'':String(data.error||'Question service is not connected to this conversation.');
  const changed=syncThoughtNodes(data.questions);
  updateThoughtConnectionInPlace();
  if(initial){
   const requested=new URLSearchParams(location.hash.slice(1)).get('question');
   if(requested&&qaById[requested]){
    selectedQuestion=requested;selectMode('questions');renderQuestions();
   }else thoughtRefresh(changed);
  }else thoughtRefresh(changed);
 }catch(error){
  thoughtConnected=false;thoughtConnectionError=error.message||'Could not reach the question service.';
  updateThoughtConnectionInPlace();
 }finally{thoughtPollBusy=false}
}
function updateThoughtConnectionInPlace(){
 const el=document.querySelector('[data-thought-connection]');
 if(!el)return;
 el.textContent=thoughtConnected?'Connected to this Codex conversation':thoughtConnectionError||'Question service unavailable';
 el.classList.toggle('disconnected',!thoughtConnected);
}
function initThoughts(){
 if(thoughtInitialized)return;
 thoughtInitialized=true;
 installThoughtControls();
 fetchThoughts(true);
 thoughtPollTimer=window.setInterval(()=>fetchThoughts(false),THOUGHT_POLL_MS);
}
window.initThoughts=initThoughts;

function thoughtHeaderActions(){
 return '<div class="thought-header-actions"><div class="thought-header-buttons"><button class="primary" data-thought-root>Add question</button><a class="thought-export" href="/api/export" download>Export added Q&amp;A</a></div><span class="thought-connection'+(thoughtConnected?'':' disconnected')+'" data-thought-connection>'+esc(thoughtConnected?'Connected to this Codex conversation':thoughtConnectionError||'Connecting to this Codex conversation…')+'</span></div>';
}
function thoughtChildren(parentId){
 return [...thoughtNodes.values()].filter(n=>n.parent_id===parentId).sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at)));
}
function thoughtRow(q,depth=0){
 const node=q.thought;
 return '<button class="qa-row'+(depth?' thought-child':'')+'" style="--thought-depth:'+Math.min(depth,6)+'" data-qa="'+esc(q.id)+'" aria-current="'+(q.id===selectedQuestion)+'">'+
  '<span class="thought-row-title">'+esc(q.title)+'</span>'+
  (node?'<span class="thought-row-status '+thoughtStatusClass(node.status)+'"><span class="sr-only">Status: </span>'+esc(thoughtStatus(node))+'</span>':'')+'</button>';
}
function renderThoughtQuestionList(found){
 if(!thoughtInitialized&&!thoughtNodes.size)return false;
 const count=$('qa-count'),results=$('qa-results');if(!count||!results)return true;
 const foundIds=new Set(found.map(q=>q.id)),visible=new Set(foundIds);
 // Keep the path to a matching follow-up visible so indentation has meaning.
 for(const id of [...foundIds]){
  let parent=qaById[id]?.thought?.parent_id,guard=0;
  while(parent&&qaById[parent]&&guard++<30){visible.add(parent);parent=qaById[parent]?.thought?.parent_id}
 }
 const renderChildren=(parentId,depth)=>thoughtChildren(parentId).filter(n=>visible.has(n.id)).map(n=>thoughtRow(qaById[n.id],depth)+renderChildren(n.id,depth+1)).join('');
 let html='';
 groups.filter(g=>g!==THOUGHT_GROUP).forEach(group=>{
  const rows=qa.filter(q=>!q.isThought&&q.group===group&&visible.has(q.id));
  const body=rows.map(q=>thoughtRow(q)+renderChildren(q.id,1)).join('');
  if(body)html+='<h3 class="qa-group">'+esc(group)+'</h3>'+body;
 });
 const roots=[...thoughtNodes.values()].filter(n=>!n.parent_id||!qaById[n.parent_id]).filter(n=>visible.has(n.id)).sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at)));
 const added=roots.map(n=>thoughtRow(qaById[n.id])+renderChildren(n.id,1)).join('');
 if(added)html+='<h3 class="qa-group">'+THOUGHT_GROUP+'</h3>'+added;
 count.textContent=found.length+' of '+qa.length+' questions';
 results.innerHTML=html||'<p class="small">No matching question. Try “notes,” “preview,” or “Bart.”</p>';
 return true;
}

function thoughtSafeUrl(raw){
 try{const url=new URL(raw,location.href);return ['http:','https:'].includes(url.protocol)?url.href:''}catch{return ''}
}
function thoughtInlineMarkdown(raw){
 let out='',last=0;const pattern=/\[([^\]\n]+)\]\(([^)\s]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*/g;let match;
 while((match=pattern.exec(raw))){
  out+=esc(raw.slice(last,match.index));
  if(match[1]!==undefined){const url=thoughtSafeUrl(match[2]);out+=url?'<a href="'+esc(url)+'" target="_blank" rel="noreferrer">'+esc(match[1])+'</a>':esc(match[0])}
  else if(match[3]!==undefined)out+='<code>'+esc(match[3])+'</code>';
  else out+='<strong>'+esc(match[4])+'</strong>';
  last=pattern.lastIndex;
 }
 return out+esc(raw.slice(last));
}
function thoughtMarkdown(raw){
 const lines=String(raw||'').replace(/\r\n?/g,'\n').split('\n');let html='',paragraph=[],list=[],listKind='ul',listStart=1,code=[],inCode=false;
 const flushParagraph=()=>{if(paragraph.length){html+='<p>'+thoughtInlineMarkdown(paragraph.join(' '))+'</p>';paragraph=[]}};
 const flushList=()=>{if(list.length){html+='<'+listKind+(listKind==='ol'?' start="'+listStart+'"':'')+'>'+list.map(x=>'<li>'+thoughtInlineMarkdown(x)+'</li>').join('')+'</'+listKind+'>';list=[]}};
 const flushCode=()=>{html+='<pre><code>'+esc(code.join('\n'))+'</code></pre>';code=[]};
 lines.forEach(line=>{
  if(/^\s*```/.test(line)){flushParagraph();flushList();if(inCode)flushCode();inCode=!inCode;return}
  if(inCode){code.push(line);return}
  const bullet=line.match(/^\s*[-*]\s+(.+)$/),ordered=line.match(/^\s*(\d{1,9})[.)]\s+(.+)$/);
  if(bullet||ordered){
   flushParagraph();const kind=ordered?'ol':'ul';if(list.length&&listKind!==kind)flushList();
   if(!list.length){listKind=kind;listStart=ordered?Number(ordered[1]):1}
   list.push(ordered?ordered[2]:bullet[1]);return;
  }
  if(!line.trim()){flushParagraph();flushList();return}
  flushList();paragraph.push(line.trim());
 });
 if(inCode)flushCode();flushParagraph();flushList();return html;
}
function renderThoughtQuestionArticle(q,i){
 if(!q.isThought)return false;
 const node=q.thought,parent=node.parent_id&&qaById[node.parent_id],status=thoughtStatus(node);
 const retry=['failed','uncertain'].includes(node.status);
 const waiting=node.status!=='answered';
 const quote=node.anchor?.quote?'<blockquote class="thought-source-quote"><span>Source passage</span>“'+esc(node.anchor.quote)+'”</blockquote>':'';
 const parentLink=parent?'<p class="thought-parent">Follow-up to <button data-qa="'+esc(parent.id)+'">'+esc(parent.title)+'</button></p>':'';
 const transient=thoughtTransientErrors.get(node.id);
 $('qa-article').innerHTML='<p class="guide-kicker">'+THOUGHT_GROUP+'</p><h2>'+esc(node.question)+'</h2>'+parentLink+quote+
  '<div class="thought-delivery"><span class="thought-status '+thoughtStatusClass(node.status)+'" data-thought-current-status>'+esc(status)+'</span>'+
  (retry?'<button data-thought-retry="'+esc(node.id)+'">Retry delivery</button>':'')+'</div>'+
  (node.answer?'<div class="qa-answer thought-markdown" data-thought-source="answer">'+thoughtMarkdown(node.answer)+'</div>':'')+
  (waiting?'<div class="thought-waiting" role="status"><p>'+esc(node.error||status+'. This question is saved and will remain here.')+'</p>'+(node.status==='uncertain'?'<p class="small">The queue may have received it. It will not be resent unless you choose Retry delivery.</p>':'')+'</div>':'')+
  (transient?'<p class="thought-request-error" role="alert">'+esc(transient)+'</p>':'')+
  '<div class="guide-links"><button class="primary" data-thought-add>Add follow-up</button></div><div id="thought-anchor-note" class="thought-anchor-note" role="status"></div>'+
  '<div class="question-neighbors"><button data-qa="'+esc(qa[i-1]?.id||q.id)+'" '+(i===0?'disabled':'')+'>← Previous question</button><button data-qa="'+esc(qa[i+1]?.id||q.id)+'" '+(i===qa.length-1?'disabled':'')+'>Next question →</button></div>';
 afterThoughtArticleRender(q);
 return true;
}
function afterThoughtArticleRender(q){
 const article=$('qa-article');
 if(article&&thoughtChildren(q.id).some(n=>n.anchor?.quote)&&!$('thought-anchor-note')){
  const note=document.createElement('div');note.id='thought-anchor-note';note.className='thought-anchor-note';note.setAttribute('role','status');
  article.querySelector('.question-neighbors')?.before(note);
 }
 restoreThoughtHighlights(q);
}

function thoughtSourceRootFromRange(range){
 const common=range.commonAncestorContainer.nodeType===Node.ELEMENT_NODE?range.commonAncestorContainer:range.commonAncestorContainer.parentElement;
 if(!common)return null;
 if(common.closest('.qa-sidebar,.topnav,.thought-composer,.thought-ask-selection,button,input,textarea,select'))return null;
 return common.closest('.qa-answer,.qa-detail,.incident-article,.qa-article,.guide,.workspace');
}
function thoughtSourceId(root){
 if(root.closest('.qa-article'))return 'question:'+thoughtLimit(selectedQuestion,72)+':'+(root.classList.contains('qa-answer')?'answer':'article');
 if(root.closest('.incident-article'))return 'incident:'+thoughtLimit(incidentId,80);
 if(root.closest('#scenario-panel'))return 'trace:'+thoughtLimit(typeof scenario==='string'?scenario:'runtime',80);
 return 'guide:'+thoughtLimit(guideMode,80);
}
function thoughtSourceTitle(root){
 const local=root.closest('.qa-article,.incident-article,.guide,.workspace')?.querySelector('h2,h3');
 return thoughtLimit(local?.textContent?.trim()||document.title,600);
}
function thoughtSourceUrl(){
 if(guideMode==='questions'&&selectedQuestion)return thoughtLimit(location.origin+location.pathname+location.search+'#question='+encodeURIComponent(selectedQuestion),1000);
 return thoughtLimit(location.href,1000);
}
function thoughtSourceText(root,start=0,quoteLength=0){
 const full=String(root?.textContent||'');if(full.length<=24000)return full;
 const before=Math.max(0,start-Math.max(1000,Math.floor((24000-quoteLength)/2)));
 return full.slice(before,before+24000);
}
function captureThoughtSelection(){
 if(thoughtHasDraft())return thoughtSelection;
 const selection=document.getSelection();
 if(!selection||selection.rangeCount!==1||selection.isCollapsed){
  hideThoughtAsk();
  if(!thoughtHasDraft()&&thoughtDeferredArticleRefresh===selectedQuestion&&qaById[selectedQuestion]?.isThought){thoughtDeferredArticleRefresh='';renderQuestionArticle()}
  return null;
 }
 const range=selection.getRangeAt(0),root=thoughtSourceRootFromRange(range),quote=range.toString();
 if(!root||!quote.trim()){hideThoughtAsk();return null}
 if(quote.length>8000){thoughtSelection=null;showThoughtAskError('Select 8,000 characters or fewer.');return null}
 const before=document.createRange();before.selectNodeContents(root);before.setEnd(range.startContainer,range.startOffset);
 const start=before.toString().length,full=String(root.textContent||'');
 thoughtSelection={
  anchor:{quote:thoughtLimit(quote,8000),prefix:thoughtLimit(full.slice(Math.max(0,start-200),start),200),suffix:thoughtLimit(full.slice(start+quote.length,start+quote.length+200),200)},
  source:{id:thoughtLimit(thoughtSourceId(root),100),title:thoughtSourceTitle(root),text:thoughtSourceText(root,start,quote.length),url:thoughtSourceUrl()}
 };
 positionThoughtAsk(range.getBoundingClientRect());return thoughtSelection;
}
function currentThoughtSource(){
 const root=$('qa-article')||$(guideMode==='trace'?'scenario-panel':'guide-panel')||document.body;
 return {id:thoughtLimit(thoughtSourceId(root),100),title:thoughtSourceTitle(root),text:thoughtLimit(root.textContent,24000),url:thoughtSourceUrl()};
}
function thoughtAskButton(){
 let button=$('thought-ask-selection');
 if(!button){button=document.createElement('button');button.id='thought-ask-selection';button.className='thought-ask-selection';button.type='button';button.dataset.thoughtAskSelection='';button.textContent='Ask Codex';button.title='Ask Codex about this selection (Alt+Shift+A)';button.hidden=true;document.body.appendChild(button)}
 return button;
}
function positionThoughtAsk(rect){
 const button=thoughtAskButton();button.disabled=false;button.classList.remove('error');button.textContent='Ask Codex';button.hidden=false;
 const left=Math.max(10,Math.min(window.innerWidth-button.offsetWidth-10,rect.left+rect.width/2-button.offsetWidth/2));
 const below=rect.bottom+10+button.offsetHeight<window.innerHeight;
 button.style.left=left+'px';button.style.top=Math.max(10,below?rect.bottom+8:rect.top-button.offsetHeight-8)+'px';
}
function showThoughtAskError(message){const button=thoughtAskButton();button.disabled=true;button.textContent=message;button.classList.add('error');button.hidden=false;button.style.left='12px';button.style.top='72px'}
function hideThoughtAsk(){const button=$('thought-ask-selection');if(button)button.hidden=true}
function openThoughtComposer(captured=null,forceRoot=false){
 closeThoughtComposer(false);
 const parentId=!forceRoot&&guideMode==='questions'&&qaById[selectedQuestion]?selectedQuestion:null;
 const context=captured||{anchor:{quote:'',prefix:'',suffix:''},source:currentThoughtSource()};
 thoughtSelection=context;hideThoughtAsk();
 const panel=document.createElement('section');panel.className='thought-composer';panel.setAttribute('role','dialog');panel.setAttribute('aria-labelledby','thought-composer-title');
 panel.innerHTML='<form id="thought-form"><div class="thought-composer-head"><h2 id="thought-composer-title">'+(context.anchor.quote?'Ask about this passage':parentId?'Add a follow-up':'Add a question')+'</h2><button type="button" class="thought-close" data-thought-cancel aria-label="Cancel question">×</button></div>'+
  (context.anchor.quote?'<blockquote>“'+esc(context.anchor.quote)+'”</blockquote>':'')+
  (parentId?'<p class="small">This will appear under “'+esc(qaById[parentId].title)+'”.</p>':'')+
  '<label for="thought-question">Question for this Codex conversation</label><textarea id="thought-question" maxlength="4000" rows="4" required></textarea><p class="thought-form-error" id="thought-form-error" role="alert"></p><div class="thought-form-actions"><button type="button" data-thought-cancel>Cancel</button><button class="primary" type="submit">Save and ask Codex</button></div></form>';
 document.body.appendChild(panel);panel.dataset.parentId=parentId||'';panel.querySelector('textarea').focus();
}
function closeThoughtComposer(refresh=true){
 const panel=document.querySelector('.thought-composer');if(panel)panel.remove();
 thoughtSelection=null;hideThoughtAsk();
 if(refresh&&guideMode==='questions'&&qaById[selectedQuestion]?.isThought){thoughtDeferredArticleRefresh='';renderQuestionArticle()}
}
async function submitThoughtQuestion(form){
 const textarea=form.querySelector('textarea'),question=textarea.value.trim(),error=$('thought-form-error'),submit=form.querySelector('[type=submit]');
 if(!question)return;
 if(!thoughtToken){error.textContent=thoughtConnectionError||'The question service is still connecting. Your draft has been kept.';return}
 const panel=form.closest('.thought-composer'),context=thoughtSelection||{anchor:{quote:'',prefix:'',suffix:''},source:currentThoughtSource()};
 const request={question:thoughtLimit(question,4000),parent_id:panel.dataset.parentId||null,
  anchor:{quote:thoughtLimit(context.anchor?.quote,8000),prefix:thoughtLimit(context.anchor?.prefix,200),suffix:thoughtLimit(context.anchor?.suffix,200)},
  source:{id:thoughtLimit(context.source?.id,100),title:thoughtLimit(context.source?.title,600),text:thoughtLimit(context.source?.text,24000),url:thoughtLimit(context.source?.url,1000)}};
 const fingerprint=JSON.stringify(request);
 if(panel._thoughtFingerprint!==fingerprint){panel._thoughtFingerprint=fingerprint;panel._thoughtRequest={id:thoughtUuid(),...request}}
 const payload=panel._thoughtRequest;
 submit.disabled=true;submit.textContent='Saving…';error.textContent='';
 try{
  const response=await fetch('/api/questions',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Thought-Token':thoughtToken,Accept:'application/json'},body:JSON.stringify(payload)});
  const data=await response.json().catch(()=>({}));if(!response.ok||!data.question)throw new Error(data.error||'The question could not be saved.');
  thoughtNodeToQa(data.question);thoughtLastSignature='';closeThoughtComposer(false);selectedQuestion=data.question.id;selectMode('questions');renderQuestions();history.pushState(null,'','#question='+encodeURIComponent(data.question.id));scrollReading('qa-article');
 }catch(requestError){error.textContent=(requestError.message||'The question could not be saved.')+' Your draft has been kept.';submit.disabled=false;submit.textContent='Save and ask Codex'}
}
async function retryThought(id,button){
 const node=thoughtNodes.get(id);if(!node||!['failed','uncertain'].includes(node.status))return;
 thoughtTransientErrors.delete(id);button.disabled=true;button.textContent='Retrying…';
 try{
  const response=await fetch('/api/questions/'+encodeURIComponent(id)+'/retry',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Thought-Token':thoughtToken,Accept:'application/json'},body:'{}'});
  const data=await response.json().catch(()=>({}));if(!response.ok||!data.question)throw new Error(data.error||'Retry failed.');
  thoughtNodeToQa(data.question);thoughtLastSignature='';renderQuestionList();renderQuestionArticle();
 }catch(error){thoughtTransientErrors.set(id,(error.message||'Retry failed.')+' The saved question was not removed.');renderQuestionArticle()}
}

function findThoughtAnchor(root,anchor){
 const text=String(root.textContent||''),quote=String(anchor.quote||'');if(!quote)return null;
 const candidates=[];let at=text.indexOf(quote);
 while(at!==-1){
  const prefix=String(anchor.prefix||''),suffix=String(anchor.suffix||'');
  if((!prefix||text.slice(Math.max(0,at-prefix.length),at)===prefix)&&(!suffix||text.slice(at+quote.length,at+quote.length+suffix.length)===suffix))candidates.push(at);
  at=text.indexOf(quote,at+1);
 }
 return candidates.length===1?{start:candidates[0],end:candidates[0]+quote.length}:null;
}
function markThoughtRange(root,span,id){
 const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:n=>n.parentElement?.closest('script,style')?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
 const entries=[];let node,offset=0;
 while((node=walker.nextNode())){entries.push({node,start:offset,end:offset+node.data.length});offset+=node.data.length}
 entries.filter(e=>e.end>span.start&&e.start<span.end).reverse().forEach(e=>{
  const from=Math.max(0,span.start-e.start),to=Math.min(e.node.data.length,span.end-e.start);let target=e.node;
  if(to<target.data.length)target.splitText(to);if(from>0)target=target.splitText(from);
  const mark=document.createElement('mark');mark.className='thought-linked-highlight';mark.dataset.thoughtHighlight=id;mark.tabIndex=0;mark.title='Open linked question';
  target.parentNode.replaceChild(mark,target);mark.appendChild(target);
 });
}
function restoreThoughtHighlights(q){
 const article=$('qa-article');if(!article||!q)return;
 let unresolved=0;
 thoughtChildren(q.id).filter(n=>n.anchor?.quote).forEach(child=>{
  const suffix=String(child.source?.id||'').split(':').pop();
  const root=suffix==='answer'?article.querySelector('.qa-answer'):article;
  const span=root&&findThoughtAnchor(root,child.anchor);
  if(span)markThoughtRange(root,span,child.id);else unresolved++;
 });
 const note=$('thought-anchor-note');
 if(note&&unresolved)note.textContent=unresolved+' linked passage'+(unresolved===1?' could':'s could')+' not be placed unambiguously in the current text.';
}
function installThoughtControls(){
 thoughtAskButton();
 document.addEventListener('selectionchange',()=>window.setTimeout(captureThoughtSelection,0));
 document.addEventListener('keyup',event=>{if(event.key==='Escape'){if(thoughtHasDraft())closeThoughtComposer();else hideThoughtAsk()}else if(!thoughtHasDraft()&&event.altKey&&event.shiftKey&&event.key.toLowerCase()==='a'){const captured=captureThoughtSelection();if(captured)openThoughtComposer(captured)}});
 document.addEventListener('pointerdown',event=>{if(event.target.closest('[data-thought-ask-selection]'))event.preventDefault()});
 document.addEventListener('click',event=>{
  const ask=event.target.closest('[data-thought-ask-selection]');if(ask){if(thoughtSelection)openThoughtComposer(thoughtSelection);return}
  const root=event.target.closest('[data-thought-root]');if(root){openThoughtComposer(null,true);return}
  const add=event.target.closest('[data-thought-add]');if(add){openThoughtComposer();return}
  const cancel=event.target.closest('[data-thought-cancel]');if(cancel){closeThoughtComposer();return}
  const retry=event.target.closest('[data-thought-retry]');if(retry){retryThought(retry.dataset.thoughtRetry,retry);return}
  const mark=event.target.closest('[data-thought-highlight]');if(mark&&!thoughtHasSelection())openQuestion(mark.dataset.thoughtHighlight);
 });
 document.addEventListener('keydown',event=>{const mark=event.target.closest?.('[data-thought-highlight]');if(mark&&(event.key==='Enter'||event.key===' ')){event.preventDefault();openQuestion(mark.dataset.thoughtHighlight)}});
 document.addEventListener('submit',event=>{if(event.target.id==='thought-form'){event.preventDefault();submitThoughtQuestion(event.target)}});
 window.addEventListener('beforeunload',()=>{if(thoughtPollTimer)clearInterval(thoughtPollTimer)});
}
