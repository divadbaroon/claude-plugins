/* The goal page. State lives in the store, the reader's actions change it,
   and every change redraws the page from it. */

import { createStore, initialState, activeSlice } from "./store.js";
import { services } from "./services.js";
import { createActions } from "./actions.js";
import { mount } from "./dom.js";

export function startWorkspace(renderPage) {
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
  // A workspace with no goal opens on the line that asks for one.
  if (state.status === "ready" && state.empty) {
    const input = host.querySelector("#goal-input");
    if (input && document.activeElement !== input) input.focus();
  }
}

store.subscribe(draw);
draw(store.get());
actions.boot();

// On the capture phase: by the time a click has bubbled up, the row it
// landed on may have been redrawn out of the tree and no longer counts as
// inside the menu.
document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-account]")) actions.closeAccount();
}, true);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") actions.closeAccount();
});

// For the console and the tests; nothing on the page reads it.
window.engelbart = { store, actions, services };

// Cross-origin preview content stays opaque; entering the frame is still a
// meaningful local interaction and never causes a model call.
window.addEventListener("blur", () => {
  setTimeout(() => {
    if (document.activeElement && document.activeElement.matches("iframe.preview-frame")) {
      actions.interaction("preview.interacted", { action: "focused" });
    }
  }, 0);
});
document.addEventListener("click", (event) => {
  const link = event.target.closest("a[data-artifact]");
  if (link) actions.interaction("artifact.opened", { artifact: link.getAttribute("data-artifact") });
}, true);

}
