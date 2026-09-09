/* Optimistic row editing. Pending writes survive the goal change feed; DOM
   identities never change when a row first reaches disk. */
import {sliceOf, todoHeld, todoPhase, TODO_LABELS} from "./store.js";

export function createTodoEditor({get, set, changeSlice, services, refresh, invalidateReads}) {
  const dirty = new Map(), timers = new Map(), queues = new Map(), failures = new Map();
  let version = 0;
  const keyOf = (id, row) => `${id}/${row}`;
  const mark = (id, row) => {const v = ++version; dirty.set(keyOf(id,row), v); return v;};
  const rowId = () => `t${crypto.randomUUID().replaceAll("-", "").slice(0,20)}`;

  function merge(id, incoming, held) {
    if (!held) return incoming;
    const local = new Map(held.todos.map(r=>[r.id,r]));
    const rows = incoming.filter(r=>!dirty.has(keyOf(id,r.id)) || local.has(r.id))
      .map(r=>dirty.has(keyOf(id,r.id)) && local.has(r.id)
        ? {...r, text:local.get(r.id).text, depth:local.get(r.id).depth, done:local.get(r.id).done} : r);
    held.todos.forEach((r,index)=>{
      if (!dirty.has(keyOf(id,r.id)) || rows.some(x=>x.id===r.id)) return;
      const before = held.todos.slice(0,index).reverse().find(x=>rows.some(y=>y.id===x.id));
      rows.splice(before ? rows.findIndex(x=>x.id===before.id)+1 : 0,0,r);
    });
    return rows;
  }

  function write(id, row, v, operation) {
    const key = keyOf(id,row);
    const task={id,row,v,operation};
    const run = (queues.get(id) || Promise.resolve()).then(async()=>{
      // A failed insertion must be retried before edits depending on its ID.
      // Keep the whole ordered suffix, including later edits and removals.
      if(failures.has(id)){failures.get(id).push(task);return;}
      try {
        await operation();
        invalidateReads();
        if (dirty.get(key) === v) dirty.delete(key);
      } catch(error) {
        failures.set(id,[task]);
        changeSlice(id,{saveError:error.message || "Could not save. Your edits are still here."});
      }
    });
    queues.set(id, run);
    run.then(()=>{if (queues.get(id)===run) {queues.delete(id); refresh();}});
    return run;
  }

  function editTodo(todoId, text) {
    const id = get().activeId, todo = sliceOf(get(),id).todos.find(r=>r.id===todoId);
    if (!todo || todoHeld(todo,get())) return;
    changeSlice(id,c=>({todos:c.todos.map(r=>r.id===todoId ? {...r,text} : r)}));
    const key = keyOf(id,todoId), v = mark(id,todoId);
    clearTimeout(timers.get(key)?.timer);
    const save = () => {
      timers.delete(key);
      const row = sliceOf(get(),id).todos.find(r=>r.id===todoId);
      if (row) write(id,todoId,v,()=>services.updateTodo({subgoalId:id,todoId,patch:{text:row.text}}));
    };
    timers.set(key,{timer:setTimeout(save,400),save});
  }

  async function flush(id=get().activeId) {
    for (const [key, pending] of [...timers]) if (key.startsWith(`${id}/`)) {clearTimeout(pending.timer);pending.save();}
    await queues.get(id);
    if (failures.has(id)) throw new Error("Save the pending edits before building. Use Retry save.");
  }

  function insert(id, afterId, text="", depth=0) {
    const todo={id:rowId(),text,depth,done:false,status:"",question:""};
    const v=mark(id,todo.id);
    changeSlice(id,c=>{
      const rows=[...c.todos], at=afterId ? rows.findIndex(r=>r.id===afterId)+1 : 0;
      rows.splice(at,0,todo);return {todos:rows,todosShown:true};
    });
    write(id,todo.id,v,()=>services.insertTodo({subgoalId:id,todo,afterId}));
    return todo;
  }

  function focus(todoId, caret=0) {
    const target=document.querySelector(`[data-todo-input="${todoId}"]`);
    if (target) {target.focus();target.setSelectionRange(caret,caret);}
  }

  function splitTodo(todoId, start, end) {
    const id=get().activeId, todo=sliceOf(get(),id).todos.find(r=>r.id===todoId);
    if (!todo || todoHeld(todo,get())) return;
    const tail=todo.text.slice(end);
    editTodo(todoId,todo.text.slice(0,start));
    const made=insert(id,todoId,tail,todo.depth || 0);
    focus(made.id,0);
  }

  function indentTodo(todoId, direction) {
    const id=get().activeId, rows=sliceOf(get(),id).todos, index=rows.findIndex(r=>r.id===todoId), todo=rows[index];
    if (!todo || todoHeld(todo,get())) return;
    const depth=Math.max(0,Math.min(8,(todo.depth||0)+direction));
    if (depth===(todo.depth||0) || direction>0 && (!index || depth>(rows[index-1].depth||0)+1)) return;
    const family=[todo];
    for(let i=index+1;i<rows.length && (rows[i].depth||0)>(todo.depth||0);i++) family.push(rows[i]);
    if(family.some(r=>todoHeld(r,get()) || (r.depth||0)+direction>8)) return;
    changeSlice(id,c=>({todos:c.todos.map(r=>family.includes(r)?{...r,depth:(r.depth||0)+direction}:r)}));
    family.forEach(r=>{
      // Flush text before depth so a depth acknowledgement cannot release
      // the overlay protecting a still-unsaved edit on this same row.
      const key=keyOf(id,r.id), pending=timers.get(key);
      if(pending){clearTimeout(pending.timer);pending.save();}
      write(id,r.id,mark(id,r.id),()=>services.updateTodo({subgoalId:id,todoId:r.id,patch:{depth:(r.depth||0)+direction}}));
    });
  }

  function removeTodo(todoId) {
    const id=get().activeId, todo=sliceOf(get(),id).todos.find(r=>r.id===todoId);
    if(!todo || todoHeld(todo,get())) return;
    const key=keyOf(id,todoId);clearTimeout(timers.get(key)?.timer);timers.delete(key);
    changeSlice(id,c=>({todos:c.todos.filter(r=>r.id!==todoId)}));
    write(id,todoId,mark(id,todoId),()=>services.removeTodo({subgoalId:id,todoId}));
  }

  function toggleTodo(todoId) {
    const id=get().activeId, todo=sliceOf(get(),id).todos.find(r=>r.id===todoId);
    if(!todo || todoHeld(todo,get())) return;
    const pending=timers.get(keyOf(id,todoId));if(pending){clearTimeout(pending.timer);pending.save();}
    const done=!todo.done;
    changeSlice(id,c=>({todos:c.todos.map(r=>r.id===todoId?{...r,done,status:done?"done":""}:r)}));
    write(id,todoId,mark(id,todoId),()=>services.updateTodo({subgoalId:id,todoId,patch:{done}}));
  }

  function toggleSelection(todoId) {
    const id=get().activeId, slice=sliceOf(get(),id);
    const ids=todoId?[todoId]:slice.todos.filter(r=>r.text.trim()&&!r.done&&!todoHeld(r,get())).map(r=>r.id);
    const selected=new Set(slice.selectedTodos || []), all=ids.every(r=>selected.has(r));
    ids.forEach(r=>all?selected.delete(r):selected.add(r));
    changeSlice(id,{selectedTodos:[...selected]});
  }

  function todoKey(event,todoId) {
    if(event.isComposing)return;
    const mod=event.metaKey||event.ctrlKey, input=event.target;
    if(mod&&event.key.toLowerCase()==="a") {event.preventDefault();toggleSelection();return;}
    if(mod&&event.key==="/") {event.preventDefault();toggleSelection(todoId);return;}
    if(event.key==="Tab") {event.preventDefault();indentTodo(todoId,event.shiftKey?-1:1);return;}
    if(event.key==="Enter"&&!mod&&!event.shiftKey) {event.preventDefault();splitTodo(todoId,input.selectionStart,input.selectionEnd);return;}
    const at=input.selectionStart;
    if((event.key==="ArrowUp"||event.key==="ArrowDown") && at===input.selectionEnd && caretAtEdge(input,event.key==="ArrowUp")) {
      const inputs=[...input.closest('.todo-list').querySelectorAll('textarea.todo-text,input.todo-new-input')];
      const next=inputs[inputs.indexOf(input)+(event.key==="ArrowUp"?-1:1)];
      if(next){event.preventDefault();next.focus();const caret=Math.min(at,next.value.length);next.setSelectionRange(caret,caret);}
    }
    if(event.key==="Backspace"&&at===0&&input.selectionEnd===0&&!input.value){event.preventDefault();const rows=sliceOf(get(),get().activeId).todos,index=rows.findIndex(r=>r.id===todoId),prev=rows[index-1];removeTodo(todoId);if(prev)focus(prev.id,prev.text.length);}
  }

  function commitNewTodo() {
    const id=get().activeId, slice=sliceOf(get(),id), text=slice.newTodo;
    if(!id||!text.trim())return;
    changeSlice(id,{newTodo:""});
    insert(id,slice.todos.at(-1)?.id || "",text,0);
  }

  async function retrySave() {
    const id=get().activeId;
    await queues.get(id);
    const pending=failures.get(id)||[];
    failures.delete(id);
    changeSlice(id,{saveError:""});
    for(const f of pending) write(f.id,f.row,f.v,f.operation);
    await flush(id).catch(()=>{});
  }

  return {merge,flush,editTodo,removeTodo,toggleTodo,commitNewTodo,todoKey,toggleSelection,retrySave};
}

// Respect wrapped lines inside a row. Only its first/last visual line hands
// an arrow to the adjacent row; ordinary arrows remain native text editing.
function caretAtEdge(input,up) {
  if(up&&input.selectionStart===0 || !up&&input.selectionStart===input.value.length)return true;
  const style=getComputedStyle(input),mirror=document.createElement("div"),marker=document.createElement("span");
  Object.assign(mirror.style,{position:"fixed",visibility:"hidden",pointerEvents:"none",left:"-10000px",top:"0",height:"auto",
    boxSizing:"border-box",width:`${input.clientWidth}px`,font:style.font,lineHeight:style.lineHeight,letterSpacing:style.letterSpacing,
    padding:style.padding,whiteSpace:"pre-wrap",overflowWrap:style.overflowWrap,wordBreak:style.wordBreak});
  marker.textContent="\u200b";
  mirror.append(input.value.slice(0,input.selectionStart),marker,input.value.slice(input.selectionStart)||"\u200b");
  document.body.append(mirror);
  const line=parseFloat(style.lineHeight)||20,pad=parseFloat(style.paddingTop)||0;
  const index=Math.round((marker.offsetTop-pad)/line),last=Math.max(0,Math.round((mirror.scrollHeight-pad-(parseFloat(style.paddingBottom)||0))/line)-1);
  mirror.remove();return up?index<=0:index>=last;
}

export function copyTodoText(state) {
  const sub=state.subgoals.find(g=>g.id===state.activeId),slice=sliceOf(state,state.activeId);
  const lines=[`# ${state.goal?.title || "Goal"}`,`Status: ${state.goal?.status || "active"}`,"",
    `## ${sub?.title || "Todos"}`,`Status: ${sub?.status || "active"}`,""];
  for(const todo of slice.todos) if(todo.text.trim()) {
    const status=todoPhase(todo,state), label=TODO_LABELS[status] || "Todo";
    lines.push(`${"  ".repeat(todo.depth||0)}- [${status==="done"?"x":" "}] ${todo.text} — ${label.replace(/…$/,"")}`);
    if(todo.question)lines.push(`${"  ".repeat((todo.depth||0)+1)}Question: ${todo.question}`);
  }
  if(slice.notes?.trim()) lines.push("","## Notes","",slice.notes.trim());
  return lines.join("\n")+"\n";
}
