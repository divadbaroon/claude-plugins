/* The Bart conversation of the selected subgoal: the feed, its proposals,
   and the composer. */

import { h } from "../dom.js";
import { activeSlice } from "../store.js";

export function renderBrainstorm(state, actions, withTodos) {
  const slice = activeSlice(state);
  const ready = slice.draft.trim().length > 0;
  return h("div", { class: "brainstorm" },
    h("div", { class: "section-head" },
      h("span", { class: "section-label" }, "Brainstorm"),
      !withTodos && h("button", {
        type: "button", class: "ghost-btn", onclick: actions.toggleTodosPane,
      }, "Show todos")),
    h("div", { class: "feed", "data-feed": "", role: "log" },
      h("div", { key: `feed:${state.activeId}`, class: "feed-inner" },
        slice.chat.map((message) => renderMessage(message, actions)))),
    h("div", { class: "composer" },
      h("div", { class: "composer-box" },
        h("input", {
          key: `draft:${state.activeId}`,
          class: "composer-input",
          type: "text",
          placeholder: "message Bart…",
          spellcheck: "false",
          "aria-label": "Message Bart",
          value: slice.draft,
          oninput: (event) => actions.editDraft(event.target.value),
          onkeydown: (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            actions.sendMessage();
          },
        }),
        h("button", {
          type: "button",
          class: ready ? "send is-ready" : "send",
          "aria-label": "Send",
          "aria-disabled": ready ? "false" : "true",
          onclick: (event) => {
            const box = event.currentTarget.closest(".composer-box");
            actions.sendMessage();
            const input = box && box.querySelector("input");
            if (input) input.focus();
          },
        }, "↑"))));
}

function renderMessage(message, actions) {
  return h("div", {
    key: message.id,
    class: message.who === "you" ? "msg from-you" : "msg from-bart",
  },
  h("span", { class: "msg-who" }, message.who),
  message.kind === "proposal"
    ? renderProposal(message, actions)
    : h("div", { class: "bubble" }, message.text));
}

function renderProposal(message, actions) {
  return h("div", { class: "proposal" },
    h("div", { class: "proposal-title" }, "Proposed todo"),
    h("div", { class: "proposal-row" },
      h("span", { class: "todo-mark", "aria-hidden": "true" }, "–"),
      h("span", { class: "proposal-text" }, message.text)),
    message.added
      ? h("div", { class: "proposal-note" }, "added to todos")
      : h("div", { class: "proposal-actions" },
        h("button", {
          type: "button", class: "primary-btn",
          onclick: () => actions.acceptProposal(message.id),
        }, "Add")));
}
