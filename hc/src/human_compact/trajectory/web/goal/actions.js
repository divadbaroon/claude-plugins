/* What the reader can do on the goal page, each one a change to the store
   and, where something is worth keeping, a call across the service
   boundary. Components call these and never touch the store themselves.

   The store is the page's copy of the goal; the server's files are the
   truth. Every write lands in the store at once and goes to the server
   behind it; every change the server hears of -- from this page or any
   other writer -- comes back through the change feed as a revision, and
   the page reads the goal again unless it already draws that revision. */

import {
  EMPTY_SLICE, sliceOf, withSlice, todosShown, hasOpenTodos, isWithBuilder,
} from "./store.js";

const PANES_POLL_MS = 2000;
const TODO_SAVE_DELAY_MS = 400;
const SIGN_IN_POLL_MS = 2000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createActions(store, services) {
  const { get, set } = store;
  function interaction(type, payload = {}) {
    if (services.recordInteraction) {
      services.recordInteraction({ type, payload, subgoalId: get().activeId || "" }).catch(() => {});
    }
  }
  function mergeMessages(held, incoming) {
    const messages = new Map(held.map(m => [m.id, m]));
    for (const message of incoming) {
      const before = messages.get(message.id) || {};
      messages.set(message.id, { ...before, ...message,
        ...(before.added ? { added: true } : {}),
        ...(before.rejected ? { rejected: true } : {}) });
    }
    return [...messages.values()];
  }
  let seq = 0;
  // Stamped per page load: a conversation read back from the server
  // carries the ids it was saved with, and a new message must not take one.
  const stamp = crypto.randomUUID();
  const nextId = (prefix) => `${prefix}-${stamp}-${(seq += 1)}`;
  const todoTimers = new Map();    // "subgoal/todo" -> the save waiting on that row's text
  let signInRun = 0;               // the sign-in attempt that is current
  let wanted = "";                 // the goal the address names, if any
  let loadRun = 0;                 // the load whose answer is current
  let watcher = null;
  let panesTimer = null;          // the shared pane poll

  // A write the page does not wait on: the store already holds the change.
  function persist(promise) {
    promise
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
  // the todo text a save is still waiting on, and conversation turns
  // still on their way to disk.
  function merge(state, loaded) {
    const slices = {};
    for (const [id, incoming] of Object.entries(loaded.slices || {})) {
      const slice = { ...EMPTY_SLICE, ...incoming };
      const held = state.slices[id];
      if (held) {
        slice.chat = mergeMessages(held.chat, slice.chat || []);
        slice.thinking = held.thinking;
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
      goals: loaded.goals || [],
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

    set((state) => merge(state, loaded));
    loadPanes();
  }

  // The side panes for the open subgoal: the project's run and the
  // subgoal's build log. Read again on every refresh, on a switch of
  // subgoal or tab, and on a slow poll while something is being watched --
  // a build out, a process running, or a pane other than Bart's open.
  let panesRun = 0;
  async function loadPanes() {
    const id = get().activeId;
    if (!id) return;
    const run = (panesRun += 1);
    let panes;
    try {
      panes = await services.getPanes({ subgoalId: id });
    } catch (error) {
      console.error("engelbart: the panes did not load", error);
      return;
    }
    if (run !== panesRun || get().activeId !== id) return;
    set({ panes, panesFor: id });
    changeSlice(id, (current) => ({ chat: mergeMessages(current.chat, panes.chat || []) }));
  }

  function watching(state) {
    const preview = state.panes && state.panes.preview;
    const build = state.panes && state.panes.build;
    return state.tab !== "bart" || Boolean(state.building)
      || Boolean(preview && (preview.status === "running" || preview.status === "starting"))
      || Boolean(build && build.run && build.run.running);
  }

  async function boot() {
    loadAccount();   // beside the goal, never ahead of it
    wanted = new URLSearchParams(window.location.search).get("goal") || "";
    await refresh();
    interaction("project.opened");
    interaction("goal.opened", { goalId: wanted });
    if (!watcher && services.watchGoal) {
      watcher = services.watchGoal({
        onChange: (revision) => {
          // A remote edit can restore an older revision (add then remove).
          // Only the revision currently drawn is safe to ignore.
          if (revision !== get().revision) refresh();
        },
      });
    }
    if (!panesTimer) {
      panesTimer = setInterval(() => { loadPanes(); }, PANES_POLL_MS);
    }
  }

  // --- the header's path, each step a view --------------------------------
  //
  // The brand is every project, the project's name is its goals, and the
  // goal's name is the goal itself. The goals view draws from the list the
  // page already loads; the projects view asks the server when it opens.

  function showGoal() {
    set({ view: "goal", projectsNote: null });
  }

  function showGoals() {
    set({ view: "goals", projectsNote: null });
  }

  async function showProjects() {
    set({ view: "projects", projectsNote: null });
    try {
      const { projects, active } = await services.listProjects();
      if (get().view === "projects") set({ projects, projectsHere: active });
    } catch (error) {
      console.error("engelbart: the projects could not be read", error);
      set({ projects: [], projectsNote: { text: String((error && error.message) || error) } });
    }
  }

  // Another goal of this project: the address names it, so a reload keeps
  // it, and the page reads it the way it read the first.
  async function openGoal(id) {
    interaction("goal.opened", { goalId: id });
    wanted = id;
    const url = new URL(window.location.href);
    url.searchParams.set("goal", id);
    window.history.pushState(null, "", url);
    set({ view: "goal", tab: "bart", activeId: null, panes: null, panesFor: null });
    await refresh();
  }

  // Another project's workspace is another window's; this one only follows
  // the address the server gives. The project this page is in just closes
  // the list.
  async function openProject(cwd) {
    if (get().projectsBusy) return;
    if (cwd === get().projectsHere) { showGoals(); return; }
    set({ projectsBusy: true, projectsNote: null });
    try {
      const { url } = await services.openProject({ cwd });
      if (!url) throw new Error("the project has no workspace to open");
      window.location.href = url;
    } catch (error) {
      set({ projectsBusy: false, projectsNote: { text: String((error && error.message) || error) } });
    }
  }

  function toggleAccount() {
    const opening = !get().accountOpen;
    set({ accountOpen: opening });
    if (opening) loadReader();
  }

  // --- the reader's level, in the account menu -----------------------------
  //
  // Read when the menu opens, so the slider stands where the profile is.
  // A stop the reader picks is drawn at once and kept through the
  // server; what it says when it would not is shown under the slider.

  async function loadReader() {
    try {
      set({ reader: await services.loadReader(), readerNote: null });
    } catch (error) {
      console.error("engelbart: the profile could not be read", error);
      set({ reader: { profile: {}, levelLabel: "" },
            readerNote: { text: String((error && error.message) || error) } });
    }
  }

  async function setLevel(level) {
    if (get().readerBusy) return;
    const before = get().reader;
    set({ readerBusy: true, readerNote: null,
          reader: { profile: { ...((before && before.profile) || {}), level }, levelLabel: "" } });
    try {
      const reader = await services.saveLevel({ level });
      set({ reader, readerBusy: false });
    } catch (error) {
      set({ reader: before, readerBusy: false,
            readerNote: { text: String((error && error.message) || error) } });
    }
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
      await services.createGoal({ title });
    } catch (error) {
      console.error("engelbart: the goal could not be made", error);
      set({ goalDraft: title });
      return;
    }
    await refresh();
    if (get().goal) set({ addingSubgoal: true, subgoalDraft: "" });
  }

  function selectSubgoal(id) {
    interaction("subgoal.selected", { selected: id });
    set({ activeId: id, tab: "bart", buildNote: null, previewNote: null });
    loadPanes();
  }

  function showTab(tab) {
    interaction("tab.changed", { from: get().tab, to: tab });
    if (get().tab === "preview" && tab !== "preview") interaction("preview.closed");
    if (tab === "preview" && get().tab !== tab) interaction("preview.opened");
    set({ tab });
    if (tab !== "bart") loadPanes();
  }

  // The preview's own operations, each a click: what the engine answers
  // when it would not is said under the pane, and the pane is read again
  // either way so it draws what is now true.
  async function previewOp(op) {
    interaction("preview.interacted", op);
    if (get().previewBusy) return;
    set({ previewBusy: true, previewNote: null });
    let answer;
    try {
      answer = await services.previewOp(op);
    } catch (error) {
      answer = { ok: false, error: String((error && error.message) || error) };
    }
    const said = answer && !answer.ok ? (answer.reason || answer.error) : "";
    set({ previewBusy: false, previewNote: said ? { text: said } : null });
    await loadPanes();
  }
  const previewConfigure = () => previewOp({ op: "preview_configure" });
  const previewShowUi = () => previewOp({ op: "preview_show_ui" });
  const previewRun = (profileId) => previewOp({ op: "preview_start", profile_id: profileId || "" });
  const previewStop = () => previewOp({ op: "preview_stop" });
  const previewForget = () => previewOp({ op: "preview_forget" });

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
    await refresh();
  }

  // Save through the shared merge boundary: other open pages may also write.
  function keepChat(id) {
    interaction("chat.saved", { subgoalId: id });
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

    interaction("plan.suggestion_accepted", { messageId });
    changeSlice(id, (current) => ({
      todos: withRow(current.todos, todo),
      todosShown: true,
      chat: current.chat.map((m) => (m.id === messageId ? { ...m, added: true, todoId: todo.id } : m)),
    }));
    keepChat(id);
  }

  function rejectProposal(messageId) {
    const id = get().activeId;
    changeSlice(id, (current) => ({ chat: current.chat.map((m) =>
      m.id === messageId && !m.added ? { ...m, rejected: true } : m) }));
    interaction("plan.suggestion_rejected", { messageId });
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
    if (!sliceOf(get(), get().activeId).newTodo && text) interaction("todo.add_started");
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
      await services.startBuild({ goalId: state.goal.id, subgoalId: id, todos: slice.todos });
    } catch (error) {
      set({ building: null, buildNote: { text: String((error && error.message) || error), error: true } });
      return;
    }
    await refresh();
    set({ building: null });
  }

  return {
    interaction, boot, refresh, toggleAccount, closeAccount, signOut, startSignIn, cancelSignIn,
    showGoal, showGoals, showProjects, openGoal, openProject,
    loadReader, setLevel,
    editGoalDraft, commitCreateGoal,
    selectSubgoal, showTab, loadPanes,
    previewConfigure, previewShowUi, previewRun, previewStop, previewForget,
    beginAddSubgoal, editSubgoalDraft, commitAddSubgoal, cancelAddSubgoal,
    editDraft, sendMessage, acceptProposal, rejectProposal,
    toggleTodosPane, toggleTodo, editTodo, removeTodo, editNewTodo, commitNewTodo,
    buildAll,
  };
}
