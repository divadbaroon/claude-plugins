/* The header: the brand, the goal, and the account at the right. */

import { h } from "../dom.js";

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
      "aria-haspopup": "dialog",
      "aria-expanded": state.accountOpen ? "true" : "false",
      title: label,
      onclick: actions.toggleAccount,
    }, h("span", { class: "account-dot", "aria-hidden": "true" })),
    state.accountOpen && h("div", { class: "account-pop", role: "dialog", "aria-label": "Account" },
      renderAccountDetail(state.account)));
}

function renderAccountDetail(account) {
  if (!account) return h("div", { class: "account-note" }, "Checking the account…");
  if (account.error) {
    return [
      h("div", { class: "account-title" }, "Could not read the account"),
      h("div", { class: "account-note" }, account.error),
    ];
  }
  if (!account.connected) {
    return [
      h("div", { class: "account-title" }, "Not connected"),
      h("div", { class: "account-note" }, "Connect this machine to your Engelbart account:"),
      h("code", { class: "account-cmd" }, "engelbart auth"),
    ];
  }
  return [
    h("div", { class: "account-title" }, account.email || "Connected"),
    h("div", { class: "account-note" }, "Connected on this machine"),
  ];
}
