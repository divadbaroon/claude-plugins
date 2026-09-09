/* The header: the brand, the project and the goal as a path -- each step
   of it a way to that view: every project, this project's goals, the goal
   -- and the account at the right, with the reader's level under a rule
   in its menu. */

import { h, svg } from "../dom.js";
import { renderExpertise } from "./expertise.js";

const ICONS = {
  models: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/></svg>`,
  api: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <circle cx="8" cy="9" r="4"/><path d="m11 12 9 9m-3-3 3-3m-6 0 3-3"/></svg>`,
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
      renderSettingsContent(state, actions)));
}

export function renderSettingsContent(state, actions) {
  return [
    h("label", {class:"model-choice"}, h("span", {}, "Interface"),
      h("select", {"aria-label":"Interface", value:state.interfaceMode || "goal",
        disabled:state.interfaceBusy || null,
        onchange:event => actions.switchInterface(event.target.value)},
        h("option", {value:"goal", selected:state.interfaceMode !== "legacy" || null}, "New workspace"),
        h("option", {value:"legacy", selected:state.interfaceMode === "legacy" || null}, "Legacy workspace"))),
    state.interfaceError && h("p", {role:"alert"}, state.interfaceError),
    h("p", {class:"menu-hint"}, "Remembered when you open Bart, across projects and restarts."),
    h("hr", {class:"menu-rule"}),
    renderAccountRows(state, actions), renderApi(state, actions), renderModels(state, actions),
    h("hr", {class:"menu-rule"}), renderExpertise(state, actions),
  ];
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

function renderApi(state, actions) {
  const credits = state.apiCredits;
  const known = typeof credits?.budget_usd === "number" && typeof credits?.spend_usd === "number";
  const money = n => new Intl.NumberFormat("en-US", {style:"currency",currency:"USD"}).format(n);
  const blocked = state.apiBusy || state.apiLoading || !credits?.ok || credits?.foreign_helper;
  return h("div", {class:"api-control", "data-api":""},
    h("button", {type:"button",role:"menuitem",class:"menu-row is-action api-toggle", "aria-controls":"api-credits", "aria-expanded":state.apiOpen ? "true":"false", onclick:actions.toggleApi}, icon("api"), h("span", {class:"menu-label"}, "API")),
    state.apiOpen && h("div", {id:"api-credits",class:"api-menu",role:"group","aria-label":"API and credits"},
      h("div", {class:"api-heading"}, h("span",{class:"section-label"},"API"),
        h("button",{type:"button",class:"menu-cancel",disabled:state.apiLoading || state.apiBusy || null,onclick:actions.loadApiCredits},"Refresh")),
      state.apiLoading && h("p",{role:"status"},"Checking credits…"),
      h("button", {type:"button",class:"api-choice", "aria-pressed":credits?.using === "engelbart" ? "true":"false",
        disabled:blocked || !credits?.available || credits?.credit_status === "exhausted" || null,
        onclick:()=>actions.switchApiCredits("engelbart")},
        h("span",{},"Engelbart"), credits?.using === "engelbart" && h("span",{class:"menu-sub"},"Active")),
      h("p",{class:"api-balance"},known ? `${money(Math.max(0,credits.budget_usd-credits.spend_usd))} left of ${money(credits.budget_usd)}`
        : credits?.available ? "Balance unavailable" : credits ? "Connect your Engelbart account to use its credit." : ""),
      known && h("p",{class:"menu-hint"},"Account balance · shared across sessions"),
      h("button", {type:"button",class:"api-choice", "aria-pressed":credits?.using === "own" ? "true":"false",
        disabled:blocked || null, onclick:()=>actions.switchApiCredits("own")},
        h("span",{},"My Claude"), credits?.using === "own" && h("span",{class:"menu-sub"},"Active")),
      h("p",{class:"menu-hint"},"Uses your Claude login. Balance is managed by Anthropic."),
      h("p",{class:"menu-hint"},"Applies to new work on this machine. Running Claude sessions keep their current credit source."),
      credits?.env_pinned && h("p",{class:"menu-hint"},"Reopen this workspace to use your Claude login."),
      credits?.foreign_helper && h("p",{role:"status"},"Another credential helper is configured; switching is unavailable."),
      state.apiBusy && h("p",{role:"status"},"Switching…"),
      state.apiError && h("p",{role:"alert"},state.apiError)));
}

function renderModels(state, actions) {
  const settings = state.modelOptions?.settings || {};
  const options = [...new Set(["opus","sonnet","haiku", ...(state.modelOptions?.aliases || []),
    ...(state.modelOptions?.models || []), settings.interface_model, settings.model].filter(Boolean))];
  const row = (role, label, value) => h("label", {class:"model-choice"},
    h("span", {}, label), h("select", {"aria-label":label + " model",value,
      disabled:state.modelsBusy || !state.modelOptions || null,
      onchange:event=>actions.chooseModel(role,event.target.value)},
      options.map(model=>h("option",{value:model,selected:model === value || null},
        ["opus","sonnet","haiku"].includes(model) ? model[0].toUpperCase()+model.slice(1) : model))));
  return h("div", {},
    h("button", {type:"button",role:"menuitem",class:"menu-row is-action", "aria-expanded":String(Boolean(state.modelsOpen)),onclick:actions.toggleModels},
      icon("models"),h("span",{class:"menu-label"},"Models")),
    state.modelsOpen && h("div",{class:"api-menu",role:"group","aria-label":"Model settings"},
      row("interface","Engelbart",settings.interface_model || "opus"),
      row("preview","Live Preview",settings.model || "sonnet"),
      state.modelsBusy && h("p",{role:"status"},"Saving/loading models…"),
      state.modelsError && h("p",{role:"alert"},state.modelsError)));
}
