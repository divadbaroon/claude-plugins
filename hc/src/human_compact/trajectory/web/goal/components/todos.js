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
  const canBuild = hasOpenTodos(slice) && !state.building && !workInFlight(state);
  return h("div", { key: "todos", class: "todos" },
    h("div", { class: "section-head" },
      h("span", { class: "section-label" }, "Todos"),
      h("button", {
        type: "button", class: "ghost-btn", onclick: actions.toggleTodosPane,
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
            if (event.key !== "Enter") return;
            event.preventDefault();
            actions.commitNewTodo();
          },
        })),
      h("div", { key: "todo-actions", class: "todos-actions" },
        state.buildNote && h("span", {
          class: state.buildNote.error ? "build-note is-error" : "build-note", role: "status",
        }, state.buildNote.text),
        h("button", {
          type: "button",
          class: "build-btn is-secondary",
          disabled: !canBuild,
          onclick: actions.buildAll,
        }, building ? "Building…" : "Build all",
        h("span", { class: "build-caret", "aria-hidden": "true" }, "›")))));
}

function renderTodo(todo, actions, state) {
  const held = todoHeld(todo, state);
  const status = todoPhase(todo, state);
  const done = status === "done";
  const label = TODO_LABELS[status];
  const classes = ["todo", done && "is-done", held && "is-held",
    status === "failed" && "is-failed"].filter(Boolean).join(" ");
  return h("div", { key: todo.id, class: "todo-entry" }, h("div", { class: classes },
    h("button", {
      type: "button",
      class: "todo-mark",
      "aria-label": done ? "Mark as not done" : "Mark as done",
      "aria-pressed": done ? "true" : "false",
      disabled: held || null,
      onclick: () => actions.toggleTodo(todo.id),
    }, done ? "✓" : "–"),
    h("textarea", {
      class: "todo-text", rows: "1",
      spellcheck: "false",
      "aria-label": "Todo",
      readonly: held || null,
      value: todo.text,
      oninput: (event) => actions.editTodo(todo.id, event.target.value.replace(/[\r\n]+/g," ")),
      onkeydown: event => { if (event.key === "Enter") { event.preventDefault(); event.target.blur(); } },
    }),
    label && h("span", { class: `todo-status is-${status}` }, label),
    !held && !done && h("button", {type:"button", class:"todo-build", disabled: state.building || workInFlight(state) || null,
      "aria-label":`Build todo: ${todo.text}`, onclick:()=>actions.buildTodo(todo.id)}, "Build ›"),
    !held && h("button", {
      type: "button",
      class: "todo-remove",
      "aria-label": "Remove todo",
      onclick: () => actions.removeTodo(todo.id),
    }, "×")));
}
