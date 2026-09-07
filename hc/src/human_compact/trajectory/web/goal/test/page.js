/* Alternate rendering only. Every control uses the shared workspace actions. */
import { h } from "../dom.js";
import { activeSlice, activeSubgoal, hasOpenTodos, todoPhase, todoHeld, lifecycleOf, TODO_LABELS } from "../store.js";
import { renderPage } from "../components/page.js";
import { renderBrainstorm } from "../components/brainstorm.js";

const LABELS = { "": "Not started", cancelled: "Not started", ...TODO_LABELS };
const BUSY = new Set(["building", "checking", "fixing", "queued"]);
const phaseOf = lifecycleOf;

export function renderTestPage(state, actions) {
  const page = renderPage(state, actions);
  page.classList.add("test-workspace");
  const rail = page.querySelector(".rail");
  if (rail && state.activeId) {
    for (const sub of rail.querySelectorAll(".sub[data-key]")) {
      const rows = state.slices[sub.dataset.key]?.todos || [];
      if (rows.length && rows.every(t => todoPhase(t, state, sub.dataset.key) === "done")) sub.classList.add("is-complete");
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
      title: LABELS[phase.status] }, LABELS[phase.status]));
  }
  const resources = state.project?.resources || [];
  if (rail && resources.length) {
    rail.append(h("section", { class: "resource-list", "aria-label": "Resources" },
      h("div", { class: "rail-todos-label" }, "Resources"),
      resources.map(r => h("button", { class: "resource-link", onclick: () => actions.openResource(r.id) },
        `${r.kind === "paper" ? "▤" : "▣"} ${r.name} · ${({ready:"Ready", acquiring:"Acquiring…", needs_user:"Needs you", failed:"Failed", selected:"Selected"})[r.status] || r.status}`))));
  }
  const paper = resources.find(r => r.kind === "paper" && r.status === "ready");
  if (tabs && paper) {
    tabs.children[0].after(h("button", { role: "tab", class: state.tab === "paper" ? "tab is-active" : "tab",
      "aria-selected": String(state.tab === "paper"), onclick: () => actions.openResource(paper.id) }, "Paper"));
  }
  if (state.tab === "paper" || state.tab === "resource") {
    const resource = resources.find(r => r.id === state.resourceId);
    const main = page.querySelector(".main");
    if (resource && main) {
      while (main.children.length > 1) main.lastChild.remove();
      const content = state.tab === "paper" && resource.status === "ready"
        ? h("iframe", { class: "paper-frame", title: resource.name, src: state.resourceUrl })
        : h("section", { class: "resource-detail", "aria-label": "Resource details" },
            h("h3", {}, resource.name), h("p", {}, `${resource.kind} · ${resource.status}`),
            resource.error && h("p", {}, resource.error),
            h("p", {}, "Local: " + (resource.access?.localPath || resource.access?.pdf || "Not acquired")),
            (resource.metadata?.files || []).map(f => h("div", {},
              h("p", {}, f.path), h("p", {}, `${f.rowCount ?? "Unknown"} rows · ${f.columns?.length || 0} inspected columns`),
              h("p", {}, (f.columns || []).map(c => `${c.name} (${c.type})`).join(", ")))),
            h("p", {}, "Source: " + (resource.source?.url || resource.source?.objectPath || "")));
      main.append(content);
    }
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
      const held = todoHeld(todo, state);
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
