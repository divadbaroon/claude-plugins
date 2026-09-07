/* Alternate rendering only. Every control uses the shared workspace actions. */
import { h } from "../dom.js";
import { activeSlice, activeSubgoal, isWithBuilder, hasOpenTodos } from "../store.js";
import { renderPage } from "../components/page.js";
import { renderBrainstorm } from "../components/brainstorm.js";

const LABELS = { "": "Not started", queued: "Queued", building: "Building",
  checking: "Checking", fixing: "Fixing", asking: "Needs user", needs_user: "Needs user",
  failed: "Failed", done: "Done", cancelled: "Not started" };
const BUSY = new Set(["building", "checking", "fixing", "queued"]);

function phaseOf(state) {
  return state.panesFor === state.activeId ? state.panes?.build?.phase : null;
}

export function todoPhase(todo, state) {
  const phase = phaseOf(state);
  // Run-level checking can follow a row being marked done. Do not display
  // completion until that run's recorded check has actually passed.
  if (phase?.todoIds.includes(todo.id) &&
      ["building", "checking", "fixing", "needs_user", "failed"].includes(phase.status)) return phase.status;
  return todo.done ? "done" : todo.status || "";
}

export function renderTestPage(state, actions) {
  const page = renderPage(state, actions);
  page.classList.add("test-workspace");
  const rail = page.querySelector(".rail");
  if (rail && state.activeId) {
    for (const sub of rail.querySelectorAll(".sub[data-key]")) {
      const rows = state.slices[sub.dataset.key]?.todos || [];
      if (rows.length && rows.every(t => t.done && (sub.dataset.key !== state.activeId || todoPhase(t, state) === "done"))) sub.classList.add("is-complete");
    }
    rail.append(renderRailTodos(state, actions));
  }
  const bart = page.querySelector(".bart");
  if (bart) {
    const conversation = renderBrainstorm(state, actions, true);
    conversation.querySelector(".section-head").remove();
    bart.replaceChildren(conversation);
    bart.classList.add("panel");
    bart.setAttribute("aria-label", "Bart");
  }
  const tabs = page.querySelector(".tabs");
  const phase = phaseOf(state);
  if (tabs && phase && LABELS[phase.status]) {
    tabs.querySelector(".host")?.remove();
    tabs.append(h("span", { class: `execution-status is-${phase.status}`, role: "status",
      title: phase.reason || LABELS[phase.status] }, LABELS[phase.status]));
  }
  return page;
}

function renderRailTodos(state, actions) {
  const slice = activeSlice(state);
  const busy = slice.todos.some(t => BUSY.has(todoPhase(t, state)));
  return h("section", { key: `rail-todos:${state.activeId}`, class: "rail-todos", "aria-label": "Todos" },
    h("div", { class: "rail-todos-label", title: activeSubgoal(state)?.title }, "Todos · this subgoal"),
    h("div", { class: "todo-list" }, slice.todos.map(todo => {
      const status = todoPhase(todo, state);
      const held = isWithBuilder(todo) || BUSY.has(status);
      return h("div", { key: todo.id, class: `todo is-${status || "new"}${held ? " is-held" : ""}` },
        h("button", { type: "button", class: "todo-mark", disabled: held,
          "aria-label": todo.done ? "Mark as not done" : "Mark as done",
          "aria-pressed": String(Boolean(todo.done)), onclick: () => actions.toggleTodo(todo.id) },
          status === "done" ? "✓" : BUSY.has(status) ? "◌" : status === "failed" ? "×" : "○"),
        h("div", { class: "todo-content" },
          h("textarea", { class: "todo-text", rows: Math.max(1, Math.ceil(todo.text.length / 26)),
            "aria-label": "Todo", spellcheck: "false", readonly: held,
            value: todo.text, oninput: e => actions.editTodo(todo.id, e.target.value) }),
          h("span", { class: `todo-status is-${status || "new"}` }, LABELS[status] || status)),
        !held && h("button", { type: "button", class: "todo-remove", "aria-label": "Remove todo",
          onclick: () => actions.removeTodo(todo.id) }, "×"));
    }),
    h("div", { key: "todo-new", class: "todo todo-new" },
      h("span", { class: "todo-mark is-faint", "aria-hidden": "true" }, "–"),
      h("input", { class: "todo-text todo-new-input", "aria-label": "New todo",
        placeholder: "add a todo…", value: slice.newTodo, spellcheck: "false",
        oninput: e => actions.editNewTodo(e.target.value),
        onkeydown: e => { if (e.key === "Enter") { e.preventDefault(); actions.commitNewTodo(); } } }))),
    h("div", { class: "todos-actions" },
      state.buildNote && h("span", { class: `build-note${state.buildNote.error ? " is-error" : ""}`, role: "status" }, state.buildNote.text),
      h("button", { class: "build-btn", type: "button", disabled: !hasOpenTodos(slice) || Boolean(state.building) || busy,
        onclick: actions.buildAll }, "Build all", h("span", { "aria-hidden": "true" }, "›"))));
}
