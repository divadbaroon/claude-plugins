/* The goal page. State lives in the store, the reader's actions change it,
   and every change redraws the page from it. */

import { createStore, initialState, activeSlice } from "./store.js";
import { services } from "./services.js";
import { createActions } from "./actions.js";
import { mount } from "./dom.js";
import { renderPage } from "./components/page.js";

const host = document.getElementById("app");
const store = createStore(initialState());
const actions = createActions(store, services);

let feedMark = "";

function draw(state) {
  mount(host, renderPage(state, actions));
  document.title = state.goal ? `Engelbart · ${state.goal.title}` : "Engelbart";
  // The feed follows its newest message, and opens on it; a reader who has
  // scrolled up to read is left where they are until one arrives.
  const feed = host.querySelector("[data-feed]");
  const mark = feed ? `${state.activeId}:${activeSlice(state).chat.length}` : "";
  if (feed && mark !== feedMark) feed.scrollTop = feed.scrollHeight;
  feedMark = mark;
  if (state.addingSubgoal) {
    const input = host.querySelector('[data-key="sub-add-input"]');
    if (input && document.activeElement !== input) input.focus();
  }
}

store.subscribe(draw);
draw(store.get());
actions.boot();

document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-account]")) actions.closeAccount();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") actions.closeAccount();
});

// For the console and the tests; nothing on the page reads it.
window.engelbart = { store, actions, services };
