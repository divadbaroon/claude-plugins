import { h } from "../dom.js";
import { activeSlice } from "../store.js";

export function renderNotes(state, actions) {
  const slice = activeSlice(state);
  return h("div", { class: "notes" },
    h("label", { class: "section-label notes-label", for: "notes-input" }, "Notes"),
    h("textarea", {
      // Keyed by subgoal: switching subgoals is a different document, not
      // an edit to this one.
      key: `notes:${state.activeId}`,
      id: "notes-input",
      class: "notes-input",
      placeholder: "take notes on how this should work…",
      spellcheck: "false",
      value: slice.notes,
      oninput: (event) => actions.editNotes(event.target.value),
    }));
}
