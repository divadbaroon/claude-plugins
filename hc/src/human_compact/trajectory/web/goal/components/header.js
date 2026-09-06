import { h } from "../dom.js";

export function renderHeader(state) {
  return h("header", { class: "header" },
    h("span", { class: "brand" }, "Engelbart"),
    state.goal && h("span", { class: "crumb", "aria-hidden": "true" }, "/"),
    state.goal && h("h1", { class: "goal-title" }, state.goal.title));
}
