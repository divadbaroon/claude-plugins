/* The header: the brand, the goal, and the account at the right. */

import { h, svg } from "../dom.js";

const ICONS = {
  person: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.2 3.6-7 8-7s8 2.8 8 7"/></svg>`,
  signOut: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
    <path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>`,
};

export function renderHeader(state, actions) {
  return h("header", { class: "header" },
    h("span", { class: "brand" }, "Engelbart"),
    state.goal && h("span", { class: "crumb", "aria-hidden": "true" }, "/"),
    state.goal && h("h1", { class: "goal-title" }, state.goal.title),
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
      renderAccountRows(state.account, actions)));
}

function icon(name) {
  const node = svg(ICONS[name]);
  node.classList.add("menu-icon");
  return node;
}

function renderAccountRows(account, actions) {
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
      h("div", { class: "menu-note" }, "Connect this machine with ",
        h("code", { class: "menu-cmd" }, "engelbart auth")),
    ];
  }
  return [
    h("div", { class: "menu-row" }, icon("person"),
      h("span", { class: "menu-label" }, "Account"),
      h("span", { class: "menu-sub" }, account.email)),
    h("button", { type: "button", role: "menuitem", class: "menu-row is-action", onclick: actions.signOut },
      icon("signOut"), h("span", { class: "menu-label" }, "Sign out")),
  ];
}
