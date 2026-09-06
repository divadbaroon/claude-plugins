/* What the reader can do on the goal page, each one a change to the store
   and, where something is worth keeping, a call across the service
   boundary. Components call these and never touch the store themselves.

   The store is the page's copy of the goal; the server's files are the
   truth. Every write lands in the store at once and goes to the server
   behind it; every change the server hears of -- from this page or any
   other writer -- comes back through the change feed as a revision, and
   the page reads the goal again unless the revision is one it made. */

import {
  EMPTY_SLICE, sliceOf, withSlice, todosShown, hasOpenTodos, isWithBuilder,
} from "./store.js";

const TODO_SAVE_DELAY_MS = 400;
const SIGN_IN_POLL_MS = 2000;
const REVISIONS_KEPT = 8;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createActions(store, services) {
  const { get, set } = store;
  let seq = 0;
  // Stamped per page load: a conversation read back from the server
  // carries the ids it was saved with, and a new message must not take one.
  const stamp = Date.now().toString(36);
  const nextId = (prefix) => `${prefix}-${stamp}-${(seq += 1)}`;
  const todoTimers = new Map();    // "subgoal/todo" -> the save waiting on that row's text
  let signInRun = 0;               // the sign-in attempt that is current
  let wanted = "";                 // the goal the address names, if any
  let loadRun = 0;                 // the load whose answer is current
  let watcher = null;              // the open change feed
  const seen = [];                 // the last revisions this page loaded or made

  // A revision this page has already seen -- loaded, or made by one of its
  // own writes -- is not news when the change feed carries it.
  function saw(revision) {
    if (!revision) return;
    seen.push(revision);
    while (seen.length > REVISIONS_KEPT) seen.shift();
  }

  // A write the page does not wait on: the store already holds the change.
  function persist(promise) {
    promise
      .then((answer) => saw(answer && answer.revision))
      .catch((error) => console.error("engelbart: a write failed", error));
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

  // The store with what the server now holds laid under what the reader is
  // in the middle of: the subgoal they are on, the message they are typing,
  // the todo text a save is still waiting on, and the conversation,
  // which this page writes and so keeps its own copy of once it has one.
  function merge(state, loaded) {
    const slices = {};
    for (const [id, incoming] of Object.entries(loaded.slices || {})) {
      const slice = { ...EMPTY_SLICE, ...incoming };
      const held = state.slices[id];
      if (held) {
        slice.chat = held.chat;
        slice.draft = held.draft;
        slice.newTodo = held.newTodo;
        slice.todosShown = held.todosShown;
        slice.todos = slice.todos.map((todo) => {
          if (!todoTimers.has(`${id}/${todo.id}`)) return todo;
          const mine = held.todos.find((t) => t.id === todo.id);
          return mine ? { ...todo, text: mine.text } : todo;
        });
      }
      slices[id] = slice;
    }
    const subgoals = loaded.subgoals || [];
    const kept = subgoals.some((subgoal) => subgoal.id === state.activeId);
    return {
      ...state,
      status: "ready",
      goal: loaded.goal,
      project: loaded.project || null,
      empty: !loaded.goal,
      subgoals,
      activeId: kept ? state.activeId : (subgoals.length ? subgoals[0].id : null),
      slices,
      revision: loaded.revision,
    };
  }

  // Read the goal again and lay it under the page. Only the newest load
  // draws; an answer that arrives after a later one asked is dropped.
  async function refresh() {
    const run = (loadRun += 1);
    let loaded;
    try {
      loaded = await services.loadGoal({ goalId: wanted });
    } catch (error) {
      console.error("engelbart: the goal did not load", error);
      if (get().status === "loading") set({ status: "failed" });
      return;
    }
    if (run !== loadRun) return;
    saw(loaded.revision);
    set((state) => merge(state, loaded));
    if (loaded.goal && !get().preview) loadPanes(loaded.goal.id);
  }

  async function loadPanes(goalId) {
    try {
      const [preview, terminal] = await Promise.all([
        services.getPreview({ goalId }),
        services.getTerminal({ goalId }),
      ]);
      set({ preview, terminal });
    } catch (error) {
      console.error("engelbart: the panes did not load", error);
    }
  }

  async function boot() {
    loadAccount();   // beside the goal, never ahead of it
    wanted = new URLSearchParams(window.location.search).get("goal") || "";
    await refresh();
    if (!watcher && services.watchGoal) {
      watcher = services.watchGoal({
        onChange: (revision) => {
          if (!seen.includes(revision)) refresh();
        },
      });
    }
  }

  function toggleAccount() {
    set((state) => ({ ...state, accountOpen: !state.accountOpen }));
  }

  function closeAccount() {
    if (get().accountOpen) set({ accountOpen: false });
  }

  // The menu stays open through both: what the CLI answered is shown there.
  async function signOut() {
    if (get().accountBusy) return;
    set({ accountBusy: true, accountNote: null, signIn: null });
    try {
      const message = await services.signOut();
      set({ accountNote: { text: message, error: false } });
    } catch (error) {
      console.error("engelbart: sign out failed", error);
      set({ accountNote: { text: `Could not sign out: ${error.message}`, error: true } });
    }
    await loadAccount();
    set({ accountBusy: false });
  }

  // Sign-in is a command that waits on a person, so the page asks after it
  // until it has finished. A cancel or a newer attempt retires the loop.
  async function startSignIn() {
    const state = get();
    if (state.accountBusy || (state.signIn && state.signIn.status === "waiting")) return;
    const run = (signInRun += 1);
    set({ accountNote: null, signIn: { status: "starting", code: "", url: "", error: "" } });
    try {
      let answer = await services.startSignIn();
      while (run === signInRun && answer.status === "waiting") {
        set({ signIn: answer });
        await sleep(SIGN_IN_POLL_MS);
        answer = await services.signInStatus();
      }
      if (run !== signInRun) return;
      if (answer.status === "ready") {
        await loadAccount();
        set({ signIn: null });
      } else if (answer.status === "cancelled") {
        set({ signIn: null });
      } else {
        set({ signIn: answer });
      }
    } catch (error) {
      console.error("engelbart: sign in failed", error);
      if (run === signInRun) {
        set({ signIn: { status: "failed", code: "", url: "", error: String(error.message || error) } });
      }
    }
  }

  async function cancelSignIn() {
    signInRun += 1;
    set({ signIn: null });
    try {
      await services.cancelSignIn();
    } catch (error) {
      console.error("engelbart: the sign-in could not be cancelled", error);
    }
  }

  // A workspace with no goal yet: the line typed becomes the goal at the
  // top of the tree, and the page is about it from then on.
  function editGoalDraft(text) {
    set({ goalDraft: text });
  }

  async function commitCreateGoal() {
    const title = get().goalDraft.trim();
    if (!title) return;
    set({ goalDraft: "" });
    try {
      const made = await services.createGoal({ title });
      saw(made.revision);
    } catch (error) {
      console.error("engelbart: the goal could not be made", error);
      set({ goalDraft: title });
      return;
    }
    await refresh();
    if (get().goal) set({ addingSubgoal: true, subgoalDraft: "" });
  }

  function selectSubgoal(id) {
    set({ activeId: id, tab: "bart", buildNote: null });
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
    let subgoal;
    try {
      subgoal = await services.addSubgoal({ goalId: state.goal.id, title });
    } catch (error) {
      console.error("engelbart: the subgoal could not be added", error);
      return;
    }
    saw(subgoal.revision);
    set((current) => ({
      ...current,
      subgoals: current.subgoals.some((s) => s.id === subgoal.id)
        ? current.subgoals
        : [...current.subgoals, { id: subgoal.id, title: subgoal.title, status: "active" }],
      activeId: subgoal.id,
      tab: "bart",
    }));
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
    if (!id || !text || slice.thinking) return;
    const mine = { id: nextId("m"), who: "you", kind: "text", text };
    changeSlice(id, (current) => ({ draft: "", thinking: true, chat: [...current.chat, mine] }));
    keepChat(id);
    let reply;
    try {
      reply = await services.sendBartMessage({
        goalId: state.goal.id,
        subgoalId: id,
        text,
        history: slice.chat,
        todos: slice.todos,
      });
    } catch (error) {
      // Said in the conversation, where the reader is looking: a model
      // that could not be reached is an answer, not a defect of the page.
      const failed = { id: nextId("m"), who: "bart", kind: "error", text: error.message || "Bart could not answer" };
      changeSlice(id, (current) => ({ thinking: false, chat: [...current.chat, failed] }));
      keepChat(id);
      return;
    }
    const answers = reply.replies.map((r) => ({
      id: nextId("m"), who: "bart", kind: r.kind, text: r.text,
      ...(r.kind === "proposal" ? { added: false } : {}),
    }));
    changeSlice(id, (current) => ({ thinking: false, chat: [...current.chat, ...answers] }));
    keepChat(id);
  }

  // The conversation, written down whole after each change to it. The
  // page is its only writer, so the copy on screen is the truth and the
  // server's is a record of it.
  function keepChat(id) {
    persist(services.saveChat({ subgoalId: id, messages: sliceOf(get(), id).chat }));
  }

  // A row laid on the list once: a refresh that arrived first may have
  // brought it already, and a line the subgoal had comes back as that row.
  function withRow(todos, todo) {
    if (todos.some((t) => t.id === todo.id)) return todos;
    return [...todos, { id: todo.id, text: todo.text, done: Boolean(todo.done), status: todo.status || "" }];
  }

  async function acceptProposal(messageId) {
    const state = get();
    const id = state.activeId;
    const message = sliceOf(state, id).chat.find((m) => m.id === messageId);
    if (!message || message.kind !== "proposal" || message.added) return;
    let todo;
    try {
      todo = await services.addTodo({ subgoalId: id, text: message.text, source: { messageId } });
    } catch (error) {
      console.error("engelbart: the todo could not be added", error);
      return;
    }
    saw(todo.revision);
    changeSlice(id, (current) => ({
      todos: withRow(current.todos, todo),
      todosShown: true,
      chat: current.chat.map((m) => (m.id === messageId ? { ...m, added: true, todoId: todo.id } : m)),
    }));
    keepChat(id);
  }

  function toggleTodosPane() {
    const state = get();
    const id = state.activeId;
    if (id) changeSlice(id, (current) => ({ todosShown: !todosShown(current) }));
  }

  function toggleTodo(todoId) {
    const id = get().activeId;
    const todo = sliceOf(get(), id).todos.find((t) => t.id === todoId);
    if (!todo || isWithBuilder(todo)) return;
    const done = !todo.done;
    changeSlice(id, (current) => ({
      todos: current.todos.map((t) => (t.id === todoId ? { ...t, done, status: done ? "done" : "" } : t)),
    }));
    persist(services.updateTodo({ subgoalId: id, todoId, patch: { done } }));
  }

  // The text lands in the store on every keystroke and goes to the server
  // once the reader pauses.
  function editTodo(todoId, text) {
    const id = get().activeId;
    const todo = sliceOf(get(), id).todos.find((t) => t.id === todoId);
    if (!todo || isWithBuilder(todo)) return;
    changeSlice(id, (current) => ({
      todos: current.todos.map((t) => (t.id === todoId ? { ...t, text } : t)),
    }));
    const key = `${id}/${todoId}`;
    clearTimeout(todoTimers.get(key));
    todoTimers.set(key, setTimeout(() => {
      todoTimers.delete(key);
      const row = sliceOf(get(), id).todos.find((t) => t.id === todoId);
      if (row) persist(services.updateTodo({ subgoalId: id, todoId, patch: { text: row.text } }));
    }, TODO_SAVE_DELAY_MS));
  }

  function removeTodo(todoId) {
    const id = get().activeId;
    const todo = sliceOf(get(), id).todos.find((t) => t.id === todoId);
    if (!todo || isWithBuilder(todo)) return;
    const key = `${id}/${todoId}`;
    clearTimeout(todoTimers.get(key));
    todoTimers.delete(key);
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
    let todo;
    try {
      todo = await services.addTodo({ subgoalId: id, text, source: null });
    } catch (error) {
      console.error("engelbart: the todo could not be added", error);
      changeSlice(id, { newTodo: text });
      return;
    }
    saw(todo.revision);
    changeSlice(id, (current) => ({ todos: withRow(current.todos, todo) }));
  }

  // Build all hands the subgoal's open rows to the builder. What happens to
  // them from there is the server's to say: it marks them as it takes them,
  // and the page reads the goal again to show it. A build that cannot start
  // says why, under the button.
  async function buildAll() {
    const state = get();
    const id = state.activeId;
    const slice = sliceOf(state, id);
    if (!id || state.building || !hasOpenTodos(slice)) return;
    set({ building: id, buildNote: null });
    try {
      const answer = await services.startBuild({ goalId: state.goal.id, subgoalId: id, todos: slice.todos });
      saw(answer.revision);
    } catch (error) {
      set({ building: null, buildNote: { text: String((error && error.message) || error), error: true } });
      return;
    }
    await refresh();
    set({ building: null });
  }

  return {
    boot, refresh, toggleAccount, closeAccount, signOut, startSignIn, cancelSignIn,
    editGoalDraft, commitCreateGoal,
    selectSubgoal, showTab,
    beginAddSubgoal, editSubgoalDraft, commitAddSubgoal, cancelAddSubgoal,
    editDraft, sendMessage, acceptProposal,
    toggleTodosPane, toggleTodo, editTodo, removeTodo, editNewTodo, commitNewTodo,
    buildAll,
  };
}
