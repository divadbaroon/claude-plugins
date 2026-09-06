/* One in-memory state tree for the goal page, and the readers every
   component uses on it. Nothing here touches the DOM or a service.

   Everything that belongs to one subgoal alone -- its notes, its Bart
   conversation, its todos, and the drafts being typed into them -- lives in
   that subgoal's slice, so switching subgoals is a change of activeId and
   nothing else. */

export const TABS = ["plan", "preview", "terminal"];

export const EMPTY_SLICE = Object.freeze({
  notes: "",
  chat: [],          // [{ id, who: "you" | "bart", kind: "text" | "proposal", text, added? }]
  draft: "",         // the message being typed to Bart
  newTodo: "",       // the todo being typed
  todos: [],         // [{ id, text, done }]
  todosShown: null,  // null until the reader chooses: shown iff there are todos
});

export function initialState() {
  return {
    status: "loading",      // "loading" | "ready" | "failed"
    goal: null,             // { id, title }
    subgoals: [],           // [{ id, title }]
    activeId: null,
    tab: "plan",            // one of TABS
    slices: {},             // subgoal id -> slice
    addingSubgoal: false,
    subgoalDraft: "",
    building: null,         // the subgoal id whose Build all is out, else null
    preview: null,          // what getPreview answered
    terminal: null,         // what getTerminal answered
  };
}

export function createStore(state) {
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      state = typeof patch === "function" ? patch(state) : { ...state, ...patch };
      for (const listener of listeners) listener(state);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export function sliceOf(state, id) {
  return (id && state.slices[id]) || EMPTY_SLICE;
}

export function activeSlice(state) {
  return sliceOf(state, state.activeId);
}

export function activeSubgoal(state) {
  return state.subgoals.find((subgoal) => subgoal.id === state.activeId) || null;
}

export function withSlice(state, id, change) {
  const before = sliceOf(state, id);
  const patch = typeof change === "function" ? change(before) : change;
  return { ...state, slices: { ...state.slices, [id]: { ...before, ...patch } } };
}

export function todosShown(slice) {
  return slice.todosShown === null ? slice.todos.length > 0 : slice.todosShown;
}

export function hasOpenTodos(slice) {
  return slice.todos.some((todo) => !todo.done);
}
