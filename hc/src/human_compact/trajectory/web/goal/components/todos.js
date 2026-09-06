/* The todos of the selected subgoal: each one toggled, edited in place or
   removed; a row to add one; and Build all. */

import { h } from "../dom.js";
import { activeSlice, hasOpenTodos } from "../store.js";

export function renderTodos(state, actions) {
  const slice = activeSlice(state);
  const building = state.building === state.activeId;
  const canBuild = hasOpenTodos(slice) && !state.building;
  return h("div", { key: "todos", class: "todos" },
    h("div", { class: "section-head" },
      h("span", { class: "section-label" }, "Todos"),
      h("button", {
        type: "button", class: "ghost-btn", onclick: actions.toggleTodosPane,
      }, "Hide todos")),
    h("div", { class: "todo-list" },
      slice.todos.map((todo) => renderTodo(todo, actions)),
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
        h("button", {
          type: "button",
          class: "build-btn",
          disabled: !canBuild,
          onclick: actions.buildAll,
        }, building ? "Building…" : "Build all",
        h("span", { class: "build-caret", "aria-hidden": "true" }, "›")))));
}

function renderTodo(todo, actions) {
  return h("div", { key: todo.id, class: todo.done ? "todo is-done" : "todo" },
    h("button", {
      type: "button",
      class: "todo-mark",
      "aria-label": todo.done ? "Mark as not done" : "Mark as done",
      "aria-pressed": todo.done ? "true" : "false",
      onclick: () => actions.toggleTodo(todo.id),
    }, "–"),
    h("input", {
      class: "todo-text",
      type: "text",
      spellcheck: "false",
      "aria-label": "Todo",
      value: todo.text,
      oninput: (event) => actions.editTodo(todo.id, event.target.value),
    }),
    h("button", {
      type: "button",
      class: "todo-remove",
      "aria-label": "Remove todo",
      onclick: () => actions.removeTodo(todo.id),
    }, "×"));
}
