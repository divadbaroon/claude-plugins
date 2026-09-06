/* The page: header, the breakdown rail, and the main column with its tabs
   and whichever pane the current tab shows. */

import { h } from "../dom.js";
import { renderHeader } from "./header.js";
import { renderBreakdown } from "./breakdown.js";
import { renderTabs } from "./tabs.js";
import { renderPlan } from "./plan.js";
import { renderPreview } from "./preview.js";
import { renderTerminal } from "./terminal.js";

export function renderPage(state, actions) {
  return h("div", { class: "app" },
    renderHeader(state),
    h("div", { class: "body" },
      renderBreakdown(state, actions),
      h("main", { class: "main" },
        state.status === "failed"
          ? h("p", { class: "notice" }, "The goal could not be loaded.")
          : [renderTabs(state, actions), renderPane(state, actions)])));
}

function renderPane(state, actions) {
  if (state.tab === "preview") return renderPreview(state);
  if (state.tab === "terminal") return renderTerminal(state);
  return renderPlan(state, actions);
}
