/* One in-memory state tree for the goal page, and the readers every
   component uses on it. Nothing here touches the DOM or a service.

   Everything that belongs to one subgoal alone -- its Bart conversation,
   its todos, and the drafts being typed into them -- lives in
   that subgoal's slice, so switching subgoals is a change of activeId and
   nothing else. */

export const TABS = ["bart", "preview", "terminal"];

// The builder holds a row from the moment it is picked until it comes
// back; a row that failed is the reader's again, to reword or to clear.
export const WITH_BUILDER = new Set(["queued", "building", "asking"]);

export const EMPTY_SLICE = Object.freeze({
  chat: [],          // [{ id, who: "you" | "bart", kind: "text" | "proposal" | "error", text, added? }]
  thinking: false,   // a reply from Bart on its way
  draft: "",         // the message being typed to Bart
  newTodo: "",       // the todo being typed
  todos: [],         // [{ id, text, done, status }] -- status as the server keeps it
  todosShown: null,  // null until the reader chooses: shown iff there are todos
});

export function initialState() {
  return {
    status: "loading",      // "loading" | "ready" | "failed"
    goal: null,             // { id, title, status }
    project: null,          // the project the workspace is in: { name, objective, plan }, or null
    view: "goal",           // "goal" | "goals" (this project's goals) | "projects" (every project)
    goals: [],              // the project's top-level goals: [{ id, title, status, why, subgoals, completed, done }]
    projects: null,         // what listProjects answered, once asked: [{ cwd, name, objective, goals, chats }]
    projectsHere: "",       // the cwd of the project this workspace is in
    projectsBusy: false,    // a project being opened
    projectsNote: null,     // what the last open said when it would not: { text }
    empty: false,           // ready, and the workspace has no goal yet
    goalDraft: "",          // the goal being typed into an empty workspace
    revision: null,         // the goals' revision the page last drew
    subgoals: [],           // [{ id, title, status }]
    activeId: null,
    tab: "bart",            // one of TABS
    slices: {},             // subgoal id -> slice
    addingSubgoal: false,
    subgoalDraft: "",
    building: null,         // the subgoal id whose Build all is out, else null
    buildNote: null,        // what the builder answered when it would not start: { text, error }
    panes: null,            // what getPanes answered for panesFor: { preview, build }
    panesFor: null,         // the subgoal id the panes were read for
    previewBusy: false,     // a preview operation on its way
    previewNote: null,      // what the last preview operation said when it would not: { text }
    account: null,          // what loadAccount answered: { connected, email, ... }
    accountOpen: false,     // the account popover in the header
    accountBusy: false,     // a sign-out on its way
    accountNote: null,      // what the last sign-out said: { text, error }
    signIn: null,           // an `engelbart auth` in flight: { status, code, url, error }
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

export function isWithBuilder(todo) {
  return WITH_BUILDER.has(todo.status);
}

/* The rows the reader can still hand over: not done, not already out. */
export function openTodos(slice) {
  return slice.todos.filter((todo) => !todo.done && !isWithBuilder(todo));
}

export function hasOpenTodos(slice) {
  return openTodos(slice).length > 0;
}

export function anyWithBuilder(slice) {
  return slice.todos.some(isWithBuilder);
}
