import { h } from "../dom.js";
import { TABS } from "../store.js";

const LABELS = { bart: "Bart", paper: "Paper", preview: "Live preview", terminal: "Terminal", dataset: "Dataset" };

export function renderTabs(state, actions) {
  const paper = state.project?.resources?.find(r => r.kind === "paper");
  const dataset = state.project?.resources?.find(r => r.id === state.project.activeDatasetId) || state.project?.resources?.find(r => r.kind === "dataset");
  const tabs = [...TABS, ...(paper ? ["paper"] : []), ...(state.project ? ["dataset"] : [])];
  return h("nav", { class: "tabs", role: "tablist", "aria-label": "Views" },
    tabs.map((tab) => h("button", {
      key: `tab-${tab}`,
      type: "button",
      role: "tab",
      class: state.tab === tab ? "tab is-active" : "tab",
      "aria-selected": state.tab === tab ? "true" : "false",
      onclick: () => tab === "paper" ? actions.openResource(paper.id) : tab === "dataset" && dataset ? actions.openResource(dataset.id) : actions.showTab(tab),
    }, LABELS[tab])),
    // Where the app is running, once it is: shown beside the panes that look at it.
    state.tab !== "bart" && hostOf(state)
      && h("span", { class: "host" }, hostOf(state)));
}

function hostOf(state) {
  const preview = state.panes && state.panes.preview;
  if (!preview || !preview.url) return "";
  try { return new URL(preview.url).host; } catch (error) { return preview.url; }
}
