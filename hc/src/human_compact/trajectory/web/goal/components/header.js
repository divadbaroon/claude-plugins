/* The header: the brand, the project and the goal as a path -- each step
   of it a way to that view: every project, this project's goals, the goal
   -- and the account at the right. */

import { h, svg } from "../dom.js";

const ICONS = {
  person: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.2 3.6-7 8-7s8 2.8 8 7"/></svg>`,
  signOut: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
    <path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>`,
  signIn: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>
    <path d="M10 17l5-5-5-5"/><path d="M15 12H3"/></svg>`,
};

export function renderHeader(state, actions) {
  const project = state.project;
  const here = state.view;
  return h("header", { class: "header" },
    h("nav", { class: "crumbs", "aria-label": "Where you are" },
      h("button", {
        type: "button", class: "brand crumb-btn", title: "All projects",
        "aria-current": here === "projects" ? "page" : null, onclick: actions.showProjects,
      }, "Engelbart"),
      project && project.name && h("span", { class: "crumb", "aria-hidden": "true" }, "/"),
      project && project.name && h("button", {
        type: "button", class: "project-name crumb-btn", title: "This project's goals",
        "aria-current": here === "goals" ? "page" : null, onclick: actions.showGoals,
      }, project.name),
      state.goal && h("span", { class: "crumb", "aria-hidden": "true" }, "/"),
      state.goal && h("h1", { class: "goal-title" },
        h("button", {
          type: "button", class: "crumb-btn", title: "The goal",
          "aria-current": here === "goal" ? "page" : null, onclick: actions.showGoal,
        }, state.goal.title))),
    renderAccount(state, actions));
}

function accountLabel(account) {
  if (!account) return "Account";
  if (account.error) return "Account: could not be read";
  if (!account.connected) return "Not connected";
  return `Connected as ${account.email || "your Engelbart account"}`;
}

function renderAccount(state, actions) {
  const label = accountLabel(state.account);
  return h("div", { class: "account", "data-account": "" },
    h("button", {
      type: "button",
      class: "account-btn",
      "aria-label": label,
      "aria-haspopup": "menu",
      "aria-expanded": state.accountOpen ? "true" : "false",
      title: label,
      onclick: actions.toggleAccount,
    }, h("span", { class: "account-dot", "aria-hidden": "true" })),
    state.accountOpen && h("div", { class: "account-menu", role: "menu", "aria-label": "Account" },
      renderAccountRows(state, actions)));
}

function icon(name) {
  const node = svg(ICONS[name]);
  node.classList.add("menu-icon");
  return node;
}

function renderAccountRows(state, actions) {
  const { account } = state;
  if (!account) {
    return h("div", { class: "menu-row" }, icon("person"),
      h("span", { class: "menu-sub" }, "Checking the account…"));
  }
  if (account.error) {
    return h("div", { class: "menu-row" }, icon("person"),
      h("span", { class: "menu-label" }, "Could not read the account"));
  }
  if (!account.connected) {
    return [
      h("div", { class: "menu-row" }, icon("person"),
        h("span", { class: "menu-label" }, "Not connected")),
      renderNote(state.accountNote),
      renderSignIn(state, actions),
    ];
  }
  return [
    h("div", { class: "menu-row" }, icon("person"),
      h("span", { class: "menu-label" }, "Account"),
      h("span", { class: "menu-sub" }, account.email)),
    renderNote(state.accountNote),
    h("button", {
      type: "button", role: "menuitem", class: "menu-row is-action",
      disabled: state.accountBusy || null, onclick: actions.signOut,
    }, icon("signOut"), h("span", { class: "menu-label" },
      state.accountBusy ? "Signing out…" : "Sign out")),
  ];
}

function renderNote(note) {
  if (!note) return null;
  return h("div", { class: `menu-note${note.error ? " is-error" : ""}`, role: "status" }, note.text);
}

/* The way in: `engelbart auth`, run by the server. While it waits, the
   code it printed and the page that approves it are shown here; the CLI
   has already opened that page in a tab of its own. */
function renderSignIn(state, actions) {
  const signIn = state.signIn;
  if (signIn && signIn.status === "starting") {
    return h("div", { class: "menu-row is-muted" }, icon("signIn"),
      h("span", { class: "menu-label" }, "Starting sign-in…"));
  }
  if (signIn && signIn.status === "waiting") {
    return h("div", { class: "menu-signin", "data-key": "signin" },
      h("div", { class: "menu-hint" }, signIn.code
        ? "Approve this code in the browser tab that just opened."
        : "Asking Engelbart for a code…"),
      signIn.code && h("div", { class: "menu-code", "aria-label": "sign-in code" }, signIn.code),
      signIn.url && h("a", { class: "menu-link", href: signIn.url, target: "_blank", rel: "noopener" },
        "Open the approval page"),
      h("div", { class: "menu-wait" },
        h("span", null, "Waiting for the approval…"),
        h("button", { type: "button", class: "menu-cancel", onclick: actions.cancelSignIn }, "Cancel")));
  }
  return [
    signIn && signIn.status === "failed" && h("div", { class: "menu-note is-error", role: "alert" },
      `Could not connect: ${signIn.error || "the sign-in did not finish"}`),
    h("button", {
      type: "button", role: "menuitem", class: "menu-row is-action", onclick: actions.startSignIn,
    }, icon("signIn"), h("span", { class: "menu-label" }, "Sign in")),
  ];
}
