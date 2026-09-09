/* What the reader can do on the goal page, each one a change to the store
   and, where something is worth keeping, a call across the service
   boundary. Components call these and never touch the store themselves.

   The store is the page's copy of the goal; the server's files are the
   truth. Every write lands in the store at once and goes to the server
   behind it; every change the server hears of -- from this page or any
   other writer -- comes back through the change feed as a revision, and
   the page reads the goal again unless it already draws that revision. */

import {
  EMPTY_SLICE, sliceOf, withSlice, todosShown, hasOpenTodos, isWithBuilder, workInFlight, todoHeld, completionHeld,
} from "./store.js";

const PANES_POLL_MS = 2000;
const TODO_SAVE_DELAY_MS = 400;
const SIGN_IN_POLL_MS = 2000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createActions(store, services) {
  const { get, set } = store;
  async function switchInterface(value) {
    if (get().interfaceBusy) return;
    set({interfaceBusy: true, interfaceError: ""});
    try {
      const answer = await services.saveInterface(value);
      if (!answer.ok) throw new Error(answer.error || "Could not save the interface choice");
      // The settings page may be embedded by the legacy workspace.
      window.top.location.assign(answer.url);
    } catch (error) {
      set({interfaceBusy: false, interfaceError: error.message});
    }
  }
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
    return [...messages.values()].sort((a,b)=>(Date.parse(a.createdAt)||0)-(Date.parse(b.createdAt)||0));
  }

  // Stamped per page load: a conversation read back from the server
  // carries the ids it was saved with, and a new message must not take one.
  const nextId = (prefix) => `${prefix}-${crypto.randomUUID()}`;
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

  async function toggleModels() {
    const open = !get().modelsOpen;
    set({modelsOpen:open});
    if (!open) return;
    set({modelsBusy:true, modelsError:""});
    try {
      const result = await services.loadModels();
      if (!result.ok) throw new Error();
      set({modelOptions:result});
    } catch (_) { set({modelsError:"Could not load models. Try again."}); }
    finally { set({modelsBusy:false}); }
  }
  async function chooseModel(role, model) {
    if (get().modelsBusy) return;
    set({modelsBusy:true, modelsError:""});
    try {
      const result = await services.saveModels(role === "interface" ? {interface_model:model} : {model,quick_model:model});
      if (!result.ok) throw new Error();
      set({modelOptions:{...get().modelOptions,settings:result.settings}});
    } catch (_) { set({modelsError:"Could not save the model. Try again."}); }
    finally { set({modelsBusy:false}); }
  }

  let apiCreditRun = 0;
  async function loadApiCredits() {
    const run = ++apiCreditRun;
    set({apiLoading:true, apiError:""});
    try {
      const credits = await services.loadApiCredits();
      if (run === apiCreditRun) set({apiCredits:credits, apiError:credits.ok ? "" : credits.error || "Could not read credits."});
    } catch (error) {
      if (run === apiCreditRun) set({apiError:"Could not refresh credits. Try again."});
    } finally { if (run === apiCreditRun) set({apiLoading:false}); }
  }
  function toggleApi() {
    const open = !get().apiOpen;
    set({apiOpen:open});
    if (open) loadApiCredits();
  }
  async function switchApiCredits(use) {
    if (get().apiBusy) return;
    ++apiCreditRun;
    set({apiBusy:true, apiLoading:false, apiError:""});
    try {
      const result = await services.switchApiCredits(use);
      if (!result.ok) set({apiError:result.error || "The credit source could not be changed."});
      else set({apiCredits:result});
    } catch (error) { set({apiError:"The credit source could not be changed. Try again."}); }
    finally { set({apiBusy:false}); }
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
        slice.chat = held.clearing ? held.chat : mergeMessages(held.chat, slice.chat || []);
        slice.clearing = held.clearing;
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
      phases: loaded.phases || {},
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
  const previewAutoAttempts = new Set();
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
    set({ panes, panesFor: id, phases: panes.phases || get().phases });
    changeSlice(id, (current) => ({ chat: current.clearing ? current.chat : mergeMessages(current.chat, panes.chat || []) }));
    const preview = panes.preview;
    if (preview?.cwd && preview.autostart !== false && ["unconfigured", "ready", "stale"].includes(preview.status)
        && !get().previewBusy && !workInFlight(get()) && !get().building) {
      const key = JSON.stringify([preview.cwd, preview.detected_at, preview.stale, panes.build?.phase?.startedAt]);
      if (!previewAutoAttempts.has(key)) {
        previewAutoAttempts.add(key);
        previewOp({op:"preview_show_ui", auto:true});
      }
    }
    if (panes.preview?.status === "failed" && !panes.preview.recovery && !get().previewBusy && !workInFlight(get())) {
      previewOp({ op: "preview_explain" });
    }
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
    if (get().accountOpen) set({ accountOpen: false, apiOpen:false, modelsOpen:false });
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
      answer = await services.previewOp({ ...op, goal_id: get().activeId });
    } catch (error) {
      answer = { ok: false, error: String((error && error.message) || error) };
    }
    const said = answer && !answer.ok ? (answer.reason || answer.error) : "";
    set({ previewBusy: false, previewNote: said && !op.auto ? { text: said } : null });
    await loadPanes();
  }
  const previewConfigure = () => previewOp({ op: "preview_configure" });
  const previewShowUi = () => previewOp({ op: "preview_show_ui" });
  const previewRun = (profileId) => previewOp({ op: "preview_start", profile_id: profileId || "" });
  const previewStop = () => previewOp({ op: "preview_stop" });
  const previewForget = () => previewOp({ op: "preview_forget" });

  function beginRenameSubgoal(id) {
    const subgoal = get().subgoals.find(sub => sub.id === id);
    if (subgoal) set({renamingSubgoal:{id, title:subgoal.title, original:subgoal.title}});
  }
  async function commitRenameSubgoal() {
    const edit = get().renamingSubgoal;
    if (!edit || edit.saving) return;
    const title = edit.title.trim();
    if (!title || title === edit.original) { set({renamingSubgoal:null}); return; }
    set({renamingSubgoal:{...edit,saving:true,error:""}});
    try {
      await services.renameSubgoal(edit.id, title);
      set(state=>({...state,renamingSubgoal:null,subgoals:state.subgoals.map(sub=>sub.id === edit.id ? {...sub,title} : sub)}));
      await refresh();
    } catch (error) {
      set({renamingSubgoal:{...edit,error:"Could not rename this subgoal. Try again."}});
    }
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
    if (!id || !text || slice.thinking || slice.clearing) return;
    const mine = { id: nextId("m"), who: "you", kind: "text", text, channel:"conversation", createdAt:new Date().toISOString() };
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
      id: nextId("m"), who: "bart", kind: r.kind, text: r.text, channel:"conversation", turnId:mine.id, createdAt:new Date().toISOString(),
      ...(r.kind === "proposal" ? { added: false } : {}),
    }));
    changeSlice(id, (current) => ({ thinking: false, chat: [...current.chat, ...answers] }));
    keepChat(id);
    await refresh();
  }

  // Save through the shared merge boundary: other open pages may also write.
  const chatSaves = new Map();
  function keepChat(id) {
    interaction("chat.saved", { subgoalId: id });
    const pending = services.saveChat({ subgoalId: id, messages: sliceOf(get(), id).chat });
    chatSaves.set(id, pending);
    persist(pending);
  }

  async function clearChat() {
    const id = get().activeId;
    const slice = sliceOf(get(), id);
    if (!id || slice.thinking || slice.clearing || !slice.chat.length) return;
    changeSlice(id, {clearing:true});
    try {
      await chatSaves.get(id)?.catch(() => {});
      const answer = await services.saveChat({subgoalId:id, messages:slice.chat, clear:true});
      loadRun += 1; panesRun += 1;
      changeSlice(id, {chat:answer.messages || [], clearing:false});
      interaction("chat.saved", {subgoalId:id, cleared:true});
    } catch (error) {
      changeSlice(id, {clearing:false});
      console.error("engelbart: the conversation could not be cleared", error);
    }
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
    if (!todo || todoHeld(todo, get())) return;
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
    if (!todo || todoHeld(todo, get())) return;
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
    if (!todo || todoHeld(todo, get())) return;
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
  async function buildAll(todoId = null) {
    const state = get();
    const id = state.activeId;
    const slice = sliceOf(state, id);
    if (!id || state.building || workInFlight(state) || !hasOpenTodos(slice)) return;
    const rows = todoId ? slice.todos.filter(t => t.id === todoId) : slice.todos;
    set({ building: id, buildAllFor: todoId ? null : id, buildingIds: rows.filter(t=>!t.done).map(t=>t.id), buildNote: null });
    try {
      await services.startBuild({ goalId: state.goal.id, subgoalId: id, todos: rows });
    } catch (error) {
      set({ building: null, buildNote: { text: String((error && error.message) || error), error: true } });
      return;
    }
    await refresh();
    await loadPanes(); // Receive the persisted preview gate before removing the click guard.
    set({ building: null });
  }

  return {
    switchInterface, loadAccount,
    setProjectDetailsOpen: open => { if (get().projectDetailsOpen !== open) set({projectDetailsOpen:open}); },
    toggleModels, chooseModel,
    toggleApi, closeApi: () => set({apiOpen:false}), loadApiCredits, switchApiCredits,
    interaction, boot, refresh, toggleAccount, closeAccount, signOut, startSignIn, cancelSignIn,
    showGoal, showGoals, showProjects, openGoal, openProject,
    loadReader, setLevel,
    editGoalDraft, commitCreateGoal,
    async uploadPaper(file) {
      if (!file || get().paperUpload?.busy) return;
      if (!/\.pdf$/i.test(file.name) || !file.size || file.size > 20 * 1024 * 1024) {
        set({paperUpload:{error:true,text:"Choose a PDF up to 20 MB."}}); return;
      }
      set({paperUpload:{busy:true,text:"Uploading…"}});
      try {
        const answer = await services.uploadPaper(file, () => set({paperUpload:{busy:true,text:"Reading PDF…"}}));
        await refresh();
        if (answer.ok) {
          set({resourceId:answer.resource.id,resourceUrl:services.projectPaperUrl(answer.resource.id, get().paperView)});
          showTab("paper"); interaction("artifact.opened", {resourceId:answer.resource.id});
        }
        set({paperUpload:answer.ok ? null : {error:true,text:answer.error || "Could not read this PDF."}});
      } catch (error) { set({paperUpload:{error:true,text:"Could not upload this PDF. Try again."}}); }
    },
    datasetUploadError(error) { set({datasetUpload:{error:true,text:error.message || "Could not read the folder. Use Choose folder."}}); },
    async chooseLocalDataset() {
      if (get().datasetUpload?.busy) return;
      set({datasetUpload:{busy:true,text:"Choose a local dataset folder in the dialog…"}});
      try {
        const answer=await services.chooseLocalDataset();
        if (answer.cancelled) {set({datasetUpload:null});return;}
        await refresh();
        if (answer.ok && answer.resource) {set({resourceId:answer.resource.id,resourceUrl:""});showTab("dataset");}
        set({datasetUpload:answer.ok ? null : {error:true,text:answer.error || "Could not prepare the selected folder."}});
      } catch(error) {set({datasetUpload:{error:true,text:error.message}});}
    },
    async uploadDataset(file) {
      if (!file || get().datasetUpload?.busy) return;
      const entries=Array.isArray(file) ? file : [{file,path:file.name}];
      if(!entries.length) return;
      set({datasetUpload:{busy:true,text:"Uploading…"}});
      try {
        const answer = await services.uploadCollection(entries, text => set({datasetUpload:{busy:true,text}}));
        await refresh();
        // A change-feed refresh can supersede the awaited refresh before it
        // has drawn. Keep the committed selection for that pending response.
        if (answer.ok && answer.resource) {
          set({resourceId:answer.resource.id,resourceUrl:""});showTab("dataset");
          interaction("artifact.opened",{resourceId:answer.resource.id});
        }
        set({datasetUpload:answer.ok ? null : {error:true,text:answer.error || "No readable tabular data was found."}});
      } catch (error) { set({datasetUpload:{error:true,text:error.message}}); }
    },
    setPaperView(view) {
      if (!["pdf", "lines"].includes(view)) return;
      const id = get().resourceId;
      if (!id) return;
      set({paperView: view, resourceUrl: services.projectPaperUrl(id, view)});
    },
    async openResource(id) {
      const resource = get().project?.resources?.find(r => r.id === id);
      if (!resource) return;
      interaction("artifact.opened", { resourceId: id });
      set({ resourceId: id, resourceUrl: resource.kind === "paper" ? services.projectPaperUrl(id, get().paperView) : "" });
      showTab(resource.kind === "paper" ? "paper" : resource.kind === "dataset" ? "dataset" : "resource");
      if (resource.kind === "dataset" && resource.status === "ready" && !resource.metadata?.files?.[0]?.sample) {
        try {
          const answer = await services.projectDataset(id);
          if (answer.ok) set(current => ({ ...current, project: { ...current.project,
            resources: current.project.resources.map(r => r.id === id ? { ...r, metadata: { ...r.metadata,
              files: [answer.file, ...(r.metadata?.files || []).slice(1)] } } : r) } }));
        } catch (_) { /* Existing persisted metadata stays visible when the file is unavailable. */ }
      }
    },
    clearChat, selectSubgoal, showTab, loadPanes,
    previewConfigure, previewShowUi, previewRun, previewStop, previewForget,
    beginRenameSubgoal, commitRenameSubgoal,
    editSubgoalTitle: title => set({renamingSubgoal:{...get().renamingSubgoal,title}}),
    cancelRenameSubgoal: () => set({renamingSubgoal:null}),
    beginAddSubgoal, editSubgoalDraft, commitAddSubgoal, cancelAddSubgoal,
    editDraft, sendMessage, acceptProposal, rejectProposal,
    toggleTodosPane, toggleTodo, editTodo, removeTodo, editNewTodo, commitNewTodo,
    buildAll: () => buildAll(), buildTodo: id => buildAll(id),
    async toggleGoalCompletion(id) {
      const state=get(), goal=id===state.goal?.id ? state.goal : state.subgoals.find(s=>s.id===id);
      if (!goal || completionHeld(state,id)) return;
      try { await services.setGoalStatus(id, goal.status==="completed"?"active":"completed"); await refresh(); }
      catch (error) { set({buildNote:{error:true,text:error.message}}); }
    },
  };
}
