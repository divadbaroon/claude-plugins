/* The Terminal pane: what the builder's terminal holds, and a prompt. */

import { h } from "../dom.js";

export function renderTerminal(state) {
  const lines = state.terminal ? state.terminal.lines : [];
  return h("section", { key: "pane-terminal", class: "pane terminal", role: "tabpanel" },
    lines.map((line) => h("div", {
      class: line.kind === "cmd" ? "term-line term-cmd" : "term-line",
    }, line.kind === "cmd" ? `$ ${line.text}` : line.text)),
    h("div", { class: "term-prompt" },
      h("span", { class: "term-cmd" }, "$"),
      h("span", { class: "cursor", "aria-hidden": "true" })));
}
