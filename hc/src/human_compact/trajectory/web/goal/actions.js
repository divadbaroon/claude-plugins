/* What the reader can do on the goal page, each one a change to the store
   and, where something is worth keeping, a call across the service
   boundary. Components call these and never touch the store themselves. */

import { EMPTY_SLICE, sliceOf, withSlice, todosShown, hasOpenTodos } from "./store.js";

const NOTES_SAVE_DELAY_MS = 400;

export function createActions(store, services) {
  const { get, set } = store;
  let seq = 0;
  const nextId = (prefix) => `${prefix}-${(seq += 1)}`;
  const notesTimers = new Map();

  // A write the page does not wait on: the store already holds the change.
  function persist(promise) {
    promise.catch((error) => console.error("engelbart: a write failed", error));
  }

  function changeSlice(id, change) {
    set((state) => withSlice(state, id, change));
  }

  async function loadAccount() {
    try {
      set({ account: await services.loadAccount() });
    } catch (error) {
      console.error("engelbart: the account could not be read", error);
      set({ account: { connected: false, signedIn: false, email: "", name: "",
                       error: String((error && error.message) || error) } });
    }
  }

  async function boot() {
    loadAccount();   // beside the goal, never ahead of it
    try {
      const loaded = await services.loadGoal();
      const slices = {};
      for (const [id, slice] of Object.entries(loaded.slices || {})) {
        slices[id] = { ...EMPTY_SLICE, ...slice };
      }
      set({
        status: "ready",
        goal: loaded.goal,
        subgoals: loaded.subgoals,
        activeId: loaded.subgoals.length ? loaded.subgoals[0].id : null,
        slices,
      });
      const [preview, terminal] = await Promise.all([
        services.getPreview({ goalId: loaded.goal.id }),
        services.getTerminal({ goalId: loaded.goal.id }),
      ]);
      set({ preview, terminal });
    } catch (error) {
      console.error("engelbart: the goal did not load", error);
      set({ status: "failed" });
    }
  }

  function toggleAccount() {
    set((state) => ({ ...state, accountOpen: !state.accountOpen }));
  }

  function closeAccount() {
    if (get().accountOpen) set({ accountOpen: false });
  }

  function selectSubgoal(id) {
    set({ activeId: id, tab: "plan" });
  }

  function showTab(tab) {
    set({ tab });
  }

  function beginAddSubgoal() {
    set({ addingSubgoal: true, subgoalDraft: "" });
  }

  function editSubgoalDraft(text) {
    set({ subgoalDraft: text });
  }

  function cancelAddSubgoal() {
    set({ addingSubgoal: false, subgoalDraft: "" });
  }

  async function commitAddSubgoal() {
    const state = get();
    if (!state.addingSubgoal) return;
    const title = state.subgoalDraft.trim();
    set({ addingSubgoal: false, subgoalDraft: "" });
    if (!title || !state.goal) return;
    const subgoal = await services.addSubgoal({ goalId: state.goal.id, title });
    set((current) => ({
      ...current,
      subgoals: [...current.subgoals, { id: subgoal.id, title: subgoal.title }],
      activeId: subgoal.id,
      tab: "plan",
    }));
  }

  function editNotes(text) {
    const id = get().activeId;
    if (!id) return;
    changeSlice(id, { notes: text });
    clearTimeout(notesTimers.get(id));
    notesTimers.set(id, setTimeout(() => {
      notesTimers.delete(id);
      persist(services.saveNotes({ subgoalId: id, text: sliceOf(get(), id).notes }));
    }, NOTES_SAVE_DELAY_MS));
  }

  function editDraft(text) {
    const id = get().activeId;
    if (id) changeSlice(id, { draft: text });
  }

  async function sendMessage() {
    const state = get();
    const id = state.activeId;
    const slice = sliceOf(state, id);
    const text = slice.draft.trim();
    if (!id || !text) return;
    const mine = { id: nextId("m"), who: "you", kind: "text", text };
    changeSlice(id, (current) => ({ draft: "", chat: [...current.chat, mine] }));
    const reply = await services.sendBartMessage({
      goalId: state.goal.id,
      subgoalId: id,
      text,
      history: slice.chat,
      todos: slice.todos,
    });
    const answer = { id: nextId("m"), who: "bart", kind: reply.kind, text: reply.text };
    if (reply.kind === "proposal") answer.added = false;
    changeSlice(id, (current) => ({ chat: [...current.chat, answer] }));
  }

  async function acceptProposal(messageId) {
    const state = get();
    const id = state.activeId;
    const message = sliceOf(state, id).chat.find((m) => m.id === messageId);
    if (!message || message.kind !== "proposal" || message.added) return;
    const todo = await services.addTodo({
      subgoalId: id, text: message.text, source: { messageId },
    });
    changeSlice(id, (current) => ({
      todos: [...current.todos, todo],
      todosShown: true,
      chat: current.chat.map((m) => (m.id === messageId ? { ...m, added: true, todoId: todo.id } : m)),
    }));
  }

  function toggleTodosPane() {
    const state = get();
    const id = state.activeId;
    if (id) changeSlice(id, (current) => ({ todosShown: !todosShown(current) }));
  }

  function toggleTodo(todoId) {
    const id = get().activeId;
    const todo = sliceOf(get(), id).todos.find((t) => t.id === todoId);
    if (!todo) return;
    const done = !todo.done;
    changeSlice(id, (current) => ({
      todos: current.todos.map((t) => (t.id === todoId ? { ...t, done } : t)),
    }));
    persist(services.updateTodo({ subgoalId: id, todoId, patch: { done } }));
  }

  function editTodo(todoId, text) {
    const id = get().activeId;
    changeSlice(id, (current) => ({
      todos: current.todos.map((t) => (t.id === todoId ? { ...t, text } : t)),
    }));
    persist(services.updateTodo({ subgoalId: id, todoId, patch: { text } }));
  }

  function removeTodo(todoId) {
    const id = get().activeId;
    changeSlice(id, (current) => ({ todos: current.todos.filter((t) => t.id !== todoId) }));
    persist(services.removeTodo({ subgoalId: id, todoId }));
  }

  function editNewTodo(text) {
    const id = get().activeId;
    if (id) changeSlice(id, { newTodo: text });
  }

  async function commitNewTodo() {
    const state = get();
    const id = state.activeId;
    const text = sliceOf(state, id).newTodo.trim();
    if (!id || !text) return;
    changeSlice(id, { newTodo: "" });
    const todo = await services.addTodo({ subgoalId: id, text, source: null });
    changeSlice(id, (current) => ({ todos: [...current.todos, todo] }));
  }

  async function buildAll() {
    const state = get();
    const id = state.activeId;
    const slice = sliceOf(state, id);
    if (!id || state.building || !hasOpenTodos(slice)) return;
    set({ building: id });
    let result;
    try {
      result = await services.startBuild({ goalId: state.goal.id, subgoalId: id, todos: slice.todos });
    } catch (error) {
      console.error("engelbart: the build did not start", error);
      set({ building: null });
      return;
    }
    const built = new Set(result.todoIds);
    set((current) => {
      const index = current.subgoals.findIndex((s) => s.id === id);
      const next = current.subgoals[Math.min(index + 1, current.subgoals.length - 1)];
      return {
        ...withSlice(current, id, (s) => ({
          todos: s.todos.map((t) => (built.has(t.id) ? { ...t, done: true } : t)),
        })),
        building: null,
        activeId: next ? next.id : current.activeId,
        tab: "plan",
      };
    });
  }

  return {
    boot, toggleAccount, closeAccount, selectSubgoal, showTab,
    beginAddSubgoal, editSubgoalDraft, commitAddSubgoal, cancelAddSubgoal,
    editNotes, editDraft, sendMessage, acceptProposal,
    toggleTodosPane, toggleTodo, editTodo, removeTodo, editNewTodo, commitNewTodo,
    buildAll,
  };
}
