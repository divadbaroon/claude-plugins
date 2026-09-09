/* The todos of the selected subgoal: each one toggled, edited in place or
   removed; a row to add one; and Build all. A row the builder holds says
   so and is left alone until it comes back. */

import { h } from "../dom.js";
import { activeSlice, hasOpenTodos, todoHeld, todoPhase, TODO_LABELS, workInFlight } from "../store.js";

export function renderTodos(state, actions) {
  const slice = activeSlice(state);
  const open = slice.todos.filter(todo => todoPhase(todo, state) !== "done");
  const busy = todo => ["building", "checking", "fixing"].includes(todoPhase(todo, state));
  const building = open.some(busy) && (state.buildAllFor === state.activeId || open.every(busy));
  const canBuild = (hasOpenTodos(slice) || slice.newTodo?.trim()) && !state.building && !workInFlight(state);
  const selected=slice.selectedTodos?.length || 0;
  return h("div", { key: "todos", class: "todos", onkeydown:event=>{
    if((event.metaKey||event.ctrlKey)&&event.key==="Enter") {event.preventDefault();actions.buildAll();}
  } },
    h("div", { class: "section-head" },
      h("span", { class: "section-label" }, "Todos"),
      h("button", {
        type: "button", class: "ghost-btn hide-todos-btn", onclick: actions.toggleTodosPane,
      }, "Hide todos")),
    h("div", { class: "todo-list" },
      slice.todos.map((todo) => renderTodo(todo, actions, state)),
      h("div", { key: "todo-new", class: "todo todo-new" },
        h("span", { class: "todo-mark is-faint", "aria-hidden": "true" }, "–"),
        h("input", {
          key: `new-todo:${state.activeId}`,
          class: "todo-text todo-new-input",
          type: "text",
          placeholder: "add a todo…",
          spellcheck: "false",
          "aria-label": "New todo",
          value: slice.newTodo,
          oninput: (event) => actions.editNewTodo(event.target.value),
          onkeydown: (event) => {
            if(event.isComposing)return;
            if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==="a"){event.preventDefault();actions.toggleTodoSelection();return;}
            if(event.metaKey||event.ctrlKey)return;
            if(event.key==="ArrowUp" && event.target.selectionStart===0){const inputs=event.target.closest('.todo-list').querySelectorAll('textarea.todo-text');const last=inputs[inputs.length-1];if(last){event.preventDefault();last.focus();last.setSelectionRange(last.value.length,last.value.length);}return;}
            if (event.key !== "Enter") return;
            event.preventDefault();
            actions.commitNewTodo();
          },
        })),
      h("div", { key: "todo-actions", class: "todos-actions" },
        h("button", {type:"button",class:"ghost-btn copy-todos",onclick:actions.copyTodos},slice.copyStatus || "Copy TODOs"),
        slice.saveError && h("div",{class:"save-error",role:"alert"},slice.saveError,
          h("button",{type:"button",class:"ghost-btn",onclick:actions.retryTodoSave},"Retry save")),
        state.buildNote && h("span", {
          class: state.buildNote.error ? "build-note is-error" : "build-note", role: "status",
        }, state.buildNote.text),
        h("button", {
          type: "button",
          class: "build-btn",
          disabled: !canBuild,
          onclick: actions.buildAll,
        }, building ? "Building…" : selected ? `Build ${selected}` : "Build all"))));
}

function renderTodo(todo, actions, state) {
  const held = todoHeld(todo, state);
  const status = todoPhase(todo, state);
  const done = status === "done";
  const label = TODO_LABELS[status];
  const timing = buildTiming(state, status);
  const classes = ["todo", done && "is-done", held && "is-held",
    status === "failed" && "is-failed"].filter(Boolean).join(" ");
  const picked=activeSlice(state).selectedTodos?.includes(todo.id);
  const question=todo.question || (status==="needs_user" ? state.phases?.[state.activeId]?.reason : "");
  return h("div", { key: todo.id, class: `todo-entry${picked?" is-picked":""}`, "data-todo-id":todo.id, style:`margin-left:${(todo.depth||0)*24}px` }, h("div", { class: classes },
    h("button", {
      type: "button",
      class: "todo-mark",
      "aria-label": done ? "Mark as not done" : "Mark as done",
      "aria-pressed": done ? "true" : "false",
      disabled: held || null,
      onclick: event => event.metaKey||event.ctrlKey ? actions.toggleTodoSelection(todo.id) : actions.toggleTodo(todo.id),
    }, done ? "✓" : "–"),
    h("textarea", {
      class: "todo-text", rows: "1",
      spellcheck: "false",
      "aria-label": "Todo",
      "data-todo-input":todo.id,
      readonly: held || null,
      value: todo.text,
      oninput: (event) => actions.editTodo(todo.id, event.target.value.replace(/[\r\n]+/g," ")),
      onkeydown: event => actions.todoKey(event,todo.id),
    }),
    label && h("span", { class: `todo-status is-${status}` }, label),
    !held && !done && h("button", {type:"button", class:"todo-build", disabled: state.building || workInFlight(state) || null,
      "aria-label":`Build todo: ${todo.text}`, onclick:()=>actions.buildTodo(todo.id)}, "Build"),
    !held && h("button", {
      type: "button",
      class: "todo-remove",
      "aria-label": "Remove todo",
      onclick: () => actions.removeTodo(todo.id),
    }, "×")), timing && h("div", {class:"todo-timing", title:timing.title}, timing.text),
    (question || status==="needs_user") && h("div",{class:"todo-question"},
      h("p",{},question || "This task needs your input. Reply here to continue."),
      h("div",{class:"todo-answer-row"},
        h("textarea",{rows:1,"aria-label":`Answer about ${todo.text}`,placeholder:"Reply…",value:activeSlice(state).answers?.[todo.id] || "",
          oninput:e=>actions.editAnswer(todo.id,e.target.value),
          onkeydown:e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.metaKey&&!e.ctrlKey&&!e.isComposing){e.preventDefault();e.stopPropagation();actions.answerTodo(todo.id);}}}),
        h("button",{type:"button",class:"ghost-btn",disabled:activeSlice(state).answerBusy || null,onclick:()=>actions.answerTodo(todo.id)},"Send")),
      activeSlice(state).answerError && h("p",{role:"alert"},activeSlice(state).answerError)));
}

// Reuse the server's clock/estimate. It covers this run, not verification or
// future repairs; never count down to a promised completion or invent a total.
function buildTiming(state, status) {
  if (state.panesFor !== state.activeId || !["building", "fixing"].includes(status)) return null;
  const run = state.panes?.build?.run;
  if (!run?.running || run.status === "checking") return null;
  const eta = run.eta_s;
  if (typeof eta === "number" && Number.isFinite(eta) && eta > 0) {
    const remaining = eta < 60 ? "under a minute" : `~${Math.ceil(eta / 60)} min`;
    return {text: `${remaining} left · then checks`,
      title: "Approximate time for the current build, shared across its selected todos. Checks and any further fixes can take longer."};
  }
  const elapsed = run.elapsed_s;
  if (typeof elapsed !== "number" || !Number.isFinite(elapsed) || elapsed < 5) return null;
  const duration = elapsed < 60 ? "under a minute" : `${Math.floor(elapsed / 60)} min`;
  return {text: `${duration} elapsed`, title: eta === 0
    ? "This build is taking longer than estimated. Checks still follow."
    : "Time spent on this build so far. A remaining-time estimate is not available yet."};
}
