/* The page: header, then the view the header's path names -- every
   project, this project's goals, or the goal: the plan rail and the main
   column with its tabs and whichever pane the current tab shows, or, for
   a workspace with no goal yet, the one line that asks for it. */

import { h } from "../dom.js";
import { renderHeader } from "./header.js";
import { renderBreakdown } from "./breakdown.js";
import { renderResourcePane } from "./resources.js";
import { renderTabs } from "./tabs.js";
import { renderBart } from "./bart.js";
import { renderPreview } from "./preview.js";
import { renderTerminal } from "./terminal.js";
import { renderProjects, renderGoals } from "./home.js";

export function renderPage(state, actions) {
  const empty = state.status === "ready" && state.empty;
  return h("div", { class: "app" },
    renderHeader(state, actions),
    state.view === "projects" ? renderProjects(state, actions)
    : state.view === "goals" ? renderGoals(state, actions)
    : h("div", { class: "body" },
      !empty && renderBreakdown(state, actions),
      h("main", { class: empty ? "main is-empty" : "main" },
        state.status === "failed"
          ? h("p", { class: "notice" }, "The goal could not be loaded.")
          : empty
            ? renderEmpty(state, actions)
            : [renderTabs(state, actions), renderPane(state, actions)])));
}

function renderPane(state, actions) {
  if (state.tab === "paper" || state.tab === "dataset" || state.tab === "resource") return renderResourcePane(state, actions);
  if (state.tab === "preview") return renderPreview(state, actions);
  if (state.tab === "terminal") return renderTerminal(state);
  if (state.status === "ready" && !state.subgoals.length) return renderFirstSubgoal(state, actions);
  return renderBart(state, actions);
}

/* A goal with nothing under it yet: the conversation and the todos both
   belong to a subgoal, so the first thing to do is name one. */
function renderFirstSubgoal(state, actions) {
  return h("section", { key: "pane-first", class: "pane is-blank", role: "tabpanel" },
    h("div", { class: "empty" },
      h("p", { class: "empty-label" }, "Break it into subgoals. Each one gets its own conversation and todos."),
      !state.addingSubgoal && h("button", {
        type: "button", class: "ghost-btn first-subgoal-btn", onclick: actions.beginAddSubgoal,
      }, "+ Add the first subgoal")));
}

/* No goal yet: one line, and the page is about it. */
function renderEmpty(state, actions) {
  return h("section", { key: "empty", class: "empty" },
    h("label", { class: "empty-label", for: "goal-input" }, "What is the goal?"),
    h("input", {
      key: "goal-input",
      id: "goal-input",
      class: "goal-input",
      type: "text",
      placeholder: "describe it in a line…",
      spellcheck: "false",
      autocomplete: "off",
      value: state.goalDraft,
      oninput: (event) => actions.editGoalDraft(event.target.value),
      onkeydown: (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        actions.commitCreateGoal();
      },
    }));
}
