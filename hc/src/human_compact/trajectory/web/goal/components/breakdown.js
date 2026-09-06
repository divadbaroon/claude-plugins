/* The rail, labelled Plan: the goal's subgoals, one selected, and the
   row that adds one. */

import { h } from "../dom.js";

export function renderBreakdown(state, actions) {
  return h("aside", { class: "rail", "aria-label": "Plan" },
    h("div", { class: "rail-label" }, "Plan"),
    state.subgoals.map((subgoal) =>
      renderSubgoal(subgoal, subgoal.id === state.activeId, actions)),
    state.status === "ready" && state.goal && (state.addingSubgoal
      ? renderAddInput(state, actions)
      : renderAddButton(actions)));
}

function renderSubgoal(subgoal, active, actions) {
  return h("button", {
    key: subgoal.id,
    type: "button",
    class: active ? "sub is-active" : "sub",
    "aria-current": active ? "true" : null,
    onclick: () => actions.selectSubgoal(subgoal.id),
  },
  h("span", { class: "sub-mark", "aria-hidden": "true" }),
  h("span", { class: "sub-title" }, subgoal.title));
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
