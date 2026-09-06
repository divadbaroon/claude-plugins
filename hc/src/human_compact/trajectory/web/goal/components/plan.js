/* The Plan pane: notes across the top, the Bart conversation below, and the
   todos beside it whenever they are shown. */

import { h } from "../dom.js";
import { activeSlice, todosShown } from "../store.js";
import { renderNotes } from "./notes.js";
import { renderBrainstorm } from "./brainstorm.js";
import { renderTodos } from "./todos.js";

export function renderPlan(state, actions) {
  const withTodos = todosShown(activeSlice(state));
  return h("section", { key: "pane-plan", class: "pane plan", role: "tabpanel" },
    h("div", { class: "panel" },
      renderNotes(state, actions),
      h("div", { class: withTodos ? "columns has-todos" : "columns" },
        renderBrainstorm(state, actions, withTodos),
        withTodos && h("div", { key: "divider", class: "divider" }),
        withTodos && renderTodos(state, actions))));
}
