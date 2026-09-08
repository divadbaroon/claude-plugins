/* The rail, labelled Plan: the goal's subgoals, one selected, and the
   row that adds one. */

import { h } from "../dom.js";
import { completionHeld } from "../store.js";

export function renderBreakdown(state, actions) {
  return h("aside", { class: "rail", "aria-label": "Plan" },
    h("div", { class: "rail-label" }, "Plan"),
    state.subgoals.map((subgoal) =>
      renderSubgoal(subgoal, subgoal.id === state.activeId, actions, state)),
    state.status === "ready" && state.goal && (state.addingSubgoal
      ? renderAddInput(state, actions)
      : renderAddButton(actions)));
}

function renderSubgoal(subgoal, active, actions, state) {
  const done=subgoal.status==="completed";
  const edit = state.renamingSubgoal?.id === subgoal.id ? state.renamingSubgoal : null;
  return h("div", {key:subgoal.id, class:active?"sub is-active":"sub", "aria-current":active?"true":null},
    h("button",{type:"button",class:"sub-mark", "aria-label":`${done?"Reopen":"Complete"} subgoal: ${subgoal.title}`,
      "aria-pressed":String(done),disabled:completionHeld(state,subgoal.id)||null,
      onclick:()=>actions.toggleGoalCompletion(subgoal.id)},done?"✓":""),
    edit ? h("div", {class:"sub-rename"},
      h("input", {key:"sub-rename-input",class:"sub-input",type:"text",maxlength:120,
        "aria-label":"Rename subgoal",value:edit.title,readonly:edit.saving || null,
        oninput:event=>actions.editSubgoalTitle(event.target.value),
        onblur:actions.commitRenameSubgoal,
        onkeydown:event=>{
          if(event.key === "Enter") { event.preventDefault();actions.commitRenameSubgoal(); }
          if(event.key === "Escape") { event.preventDefault();actions.cancelRenameSubgoal(); }
        }}),
      edit.error && h("span", {role:"alert",class:"menu-note"},edit.error))
    : h("button",{type:"button",class:"sub-title", "aria-current":active?"true":null,
      title:"Double-click to rename",
      onclick:()=>actions.selectSubgoal(subgoal.id),
      ondblclick:()=>actions.beginRenameSubgoal(subgoal.id),
      onkeydown:event=>{ if(event.key === "F2") {event.preventDefault();actions.beginRenameSubgoal(subgoal.id);} }},subgoal.title));
}

function renderAddButton(actions) {
  return h("button", {
    key: "sub-add", type: "button", class: "sub-add", onclick: actions.beginAddSubgoal,
  }, "+ Add subgoal");
}

function renderAddInput(state, actions) {
  return h("div", { key: "sub-add", class: "sub is-editing" },
    h("span", { class: "sub-mark", "aria-hidden": "true" }),
    h("input", {
      key: "sub-add-input",
      class: "sub-input",
      type: "text",
      placeholder: "describe the subgoal…",
      spellcheck: "false",
      "aria-label": "New subgoal",
      value: state.subgoalDraft,
      oninput: (event) => actions.editSubgoalDraft(event.target.value),
      onkeydown: (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          actions.commitAddSubgoal();
        } else if (event.key === "Escape") {
          event.preventDefault();
          actions.cancelAddSubgoal();
        }
      },
      // Leaving the row keeps what was typed and drops an empty one.
      onblur: () => actions.commitAddSubgoal(),
    }));
}
