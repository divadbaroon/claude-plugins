/* The Terminal pane: what the open subgoal's build has been doing, line by
   line, then the output of the project's own run when there is one. The
   build log is the builder's account of itself -- each tool call and the
   first line of what it said between them -- so this is the shape of the
   work, not the work. */

import { h } from "../dom.js";

export function renderTerminal(state) {
  const panes = state.panes || {};
  const build = panes.build || { lines: [], run: null };
  const preview = panes.preview;
  const lines = build.lines || [];
  const run = preview && preview.ok && preview.run;
  const live = Boolean((build.run && build.run.running)
    || (preview && (preview.status === "running" || preview.status === "starting")));
  return h("section", { key: "pane-terminal", class: "pane terminal", role: "tabpanel" },
    h("div", { class: "term-line term-head" }, headline(build)),
    lines.map((line, i) => h("div", {
      key: `${line.at}-${i}`,
      class: line.kind === "say" ? "term-line term-say" : "term-line",
    }, `${stamp(line.at)}  ${line.text}`)),
    run && run.command && [
      h("div", { key: "run-cmd", class: "term-line term-cmd" }, `$ ${run.command}`),
      (run.lines || []).map((text, i) => h("div", { key: `run-${i}`, class: "term-line" }, text)),
    ],
    live && h("div", { class: "term-prompt" },
      h("span", { class: "term-cmd" }, "$"),
      h("span", { class: "cursor", "aria-hidden": "true" })));
}

const stamp = (at) => (typeof at === "string" && at.length >= 19 ? at.slice(11, 19) : "        ");

function headline(build) {
  const run = build.run;
  if (!run) return "# no build has run on this subgoal yet";
  if (run.running) return `# building · ${run.rows || 1} row${run.rows === 1 ? "" : "s"}`;
  return `# last build ${run.status || "ended"}${run.updated_at ? " · " + stamp(run.updated_at) : ""}`;
}
