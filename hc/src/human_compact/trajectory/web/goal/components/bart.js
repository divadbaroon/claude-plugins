/* The Bart pane: the conversation, and the todos beside it whenever they
   are shown. */

import { h } from "../dom.js";
import { activeSlice, todosShown } from "../store.js";
import { renderBrainstorm } from "./brainstorm.js";
import { renderTodos } from "./todos.js";

export function renderBart(state, actions) {
  const withTodos = todosShown(activeSlice(state));
  return h("section", { key: "pane-bart", class: "pane bart", role: "tabpanel" },
    h("div", { class: "panel" },
      h("div", { class: withTodos ? "columns has-todos" : "columns" },
        renderBrainstorm(state, actions, withTodos),
        withTodos && h("div", { key: "divider", class: "divider" }),
        withTodos && renderTodos(state, actions))));
}
