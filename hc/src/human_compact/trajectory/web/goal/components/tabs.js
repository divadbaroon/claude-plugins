import { h } from "../dom.js";
import { TABS } from "../store.js";

const LABELS = { plan: "Plan", preview: "Live preview", terminal: "Terminal" };

export function renderTabs(state, actions) {
  return h("nav", { class: "tabs", role: "tablist", "aria-label": "Views" },
    TABS.map((tab) => h("button", {
      key: `tab-${tab}`,
      type: "button",
      role: "tab",
      class: state.tab === tab ? "tab is-active" : "tab",
      "aria-selected": state.tab === tab ? "true" : "false",
      onclick: () => actions.showTab(tab),
    }, LABELS[tab])),
    // Where the app is running: shown beside the panes that look at it.
    state.tab !== "plan" && state.preview
      && h("span", { class: "host" }, state.preview.host));
}
