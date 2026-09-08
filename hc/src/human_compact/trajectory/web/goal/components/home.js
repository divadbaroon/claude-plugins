/* The two lists the header's path opens: every project this vault knows,
   and this project's goals. Cards, plainly: a name, a line about it, a few
   facts. A project card opens that project's workspace; a goal card opens
   the goal here. A goal whose every piece is finished says so. */

import { h } from "../dom.js";

export function renderProjects(state, actions) {
  const rows = state.projects;
  const here = state.projectsHere;
  const count = rows ? `${rows.length} ${rows.length === 1 ? "project" : "projects"}` : "";
  return h("section", { key: "view-projects", class: "home", "aria-label": "Projects" },
    h("div", { class: "home-head" },
      h("h2", { class: "home-title" }, "Projects"),
      h("span", { class: "home-count" }, count)),
    h("p", { class: "home-sub" },
      "Every project this vault knows. Goals belong to a project, and each project has a workspace of its own."),
    !rows
      ? h("p", { class: "home-empty" }, "looking…")
      : !rows.length
        ? h("p", { class: "home-empty" }, "No project yet.")
        : h("div", { class: "home-grid" },
          ordered(rows, here).map((row) => renderProjectCard(row, row.cwd === here, state, actions))),
    state.projectsNote && h("p", { class: "home-note", role: "alert" }, state.projectsNote.text));
}

// The one this page is in comes first, whatever the server's order.
function ordered(rows, here) {
  return [...rows.filter((r) => r.cwd === here), ...rows.filter((r) => r.cwd !== here)];
}

function renderProjectCard(row, here, state, actions) {
  const why = String(row.objective || row.description || "").replace(/\s+/g, " ").trim();
  const goals = Number(row.goals || 0);
  const chats = Number(row.chats || 0);
  return h("button", {
    key: `project-${row.cwd}`,
    type: "button",
    class: here ? "card is-here" : "card",
    title: row.cwd,
    disabled: state.projectsBusy || null,
    onclick: () => actions.openProject(row.cwd),
  },
  h("span", { class: "card-name" }, row.name || row.cwd),
  h("span", { class: why ? "card-text" : "card-text is-empty" }, why || "no purpose written yet"),
  h("span", { class: "card-where" }, row.cwd),
  h("span", { class: "card-facts" },
    h("span", null, `${goals} ${goals === 1 ? "goal" : "goals"}`),
    h("span", null, `${chats} ${chats === 1 ? "chat" : "chats"}`),
    here && h("span", { class: "card-here" }, "this workspace")));
}

export function renderGoals(state, actions) {
  const rows = state.goals || [];
  const name = state.project && state.project.name;
  const plan = state.project && state.project.plan;
  const count = `${rows.length} ${rows.length === 1 ? "goal" : "goals"}`;
  return h("section", { key: "view-goals", class: "home goals-overview", "aria-label": "Goals" },
    h("div", { class: "home-head" },
      h("h2", { class: "home-title" }, name ? `Goals of ${name}` : "Goals"),
      h("span", { class: "home-count" }, count)),
    plan && h("p", { class: "home-sub" }, String(state.project.objective || plan).split(/\n\s*\n/)[0]),
    !rows.length
      ? h("p", { class: "home-empty" }, "No goal yet.")
      : h("div", { class: "home-grid" },
        rows.map((row) => renderGoalCard(row, state.goal && row.id === state.goal.id, actions))),
    plan && h("details", {class:"project-brief", open:state.projectDetailsOpen || null,
      ontoggle:event=>actions.setProjectDetailsOpen(event.currentTarget.open)},
      h("summary", {}, "Project details"),
      h("div", {class:"project-brief-text"}, plan)));
}

function renderGoalCard(row, open, actions) {
  const pieces = Number(row.subgoals || 0);
  const finished = Number(row.completed || 0);
  const progress = !pieces ? "nothing under it yet"
    : `${finished} of ${pieces} ${pieces === 1 ? "subgoal" : "subgoals"} done`;
  return h("button", {
    key: `goal-${row.id}`,
    type: "button",
    class: `card${row.done ? " is-done" : ""}${open ? " is-here" : ""}`,
    "aria-label": row.done ? `${row.title}, done` : row.title,
    onclick: () => actions.openGoal(row.id),
  },
  h("span", { class: "card-name" },
    row.done && h("span", { class: "card-check", "aria-hidden": "true" }, "✓"),
    row.title),
  h("span", { class: row.why ? "card-text" : "card-text is-empty" }, row.why || "no reason written"),
  h("span", { class: "card-facts" },
    h("span", { class: row.done ? "card-done" : null }, row.done ? "Done" : progress),
    open && h("span", { class: "card-here" }, "open")));
}
