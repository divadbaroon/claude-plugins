import {h} from "../dom.js";
import {activeSlice,activeSubgoal} from "../store.js";

export function renderNotes(state,actions) {
  const slice=activeSlice(state),sub=activeSubgoal(state);
  return h("section",{key:`notes:${state.activeId}`,class:"pane notes-pane",role:"tabpanel","aria-label":"Notes"},
    h("div",{class:"notes-heading"},h("h2",{},sub?.title || "Notes"),
      h("span",{role:"status"},slice.noteStatus || ""),
      slice.noteError && h("button",{type:"button",class:"ghost-btn",onclick:actions.retryNotes},"Retry save")),
    h("textarea",{class:"notes-editor","aria-label":"Subgoal notes",placeholder:"Write notes for this subgoal…",value:slice.notes || "",
      oninput:event=>actions.editNotes(event.target.value)}));
}
