/* The boundary between the goal page and everything behind it.

   Every function here takes one object of named arguments and returns a
   promise, so nothing above this file knows where an answer comes from.

   The goal store is real. loadGoal reads GET /api/goal-page; the writes go
   through POST /api/goal-page/op -- the same operations the workspace at
   /legacy applies, to the same goals.json -- and watchGoal opens the
   server's event stream, which carries the goals' revision whenever the
   files change on disk: the chat writing goals while the reader talks, a
   build marking a row, a sync. Every write answers with the revision it
   made, so the page can tell its own changes from everyone else's.

   The account is real: loadAccount asks the server who this machine is
   connected as, and signOut / startSignIn run `engelbart logout` and
   `engelbart auth` through it.

   Bart is real: sendBartMessage sends the subgoal's whole conversation to
   POST /api/goal-page/bart, where the server asks the model -- the same
   brainstorm the workspace at /legacy talks to, on the reader's own
   account, told which piece the conversation is about -- and answers with
   what to draw: prose as text, each row the model proposed as a proposal
   the reader can add. The conversation itself is kept beside the goals:
   saveChat writes it whole after every change, and loadGoal brings it
   back in each subgoal's slice.

   The panes are real. getPanes reads GET /api/goal-page/panes: the Live
   preview is the middle pane's engine from /legacy -- what the project
   can run, the process the server started for it, the address it answers
   on -- and the Terminal is the build log of the open subgoal. previewOp
   sends the preview's own operations (find how to run it, show its page,
   run, stop) to POST /api/goal-page/preview. Nothing runs on a page load;
   every start is a click. */

import { WITH_BUILDER } from "./store.js";

async function get(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return response.json();
}

// A write to the server. JSON in, JSON out; the media type is what lets
// the server tell the page apart from any other site's form.
async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return response.json();
}

// A message of the page as a turn of the conversation the server reads:
// the reader's as theirs, Bart's as its own -- a proposal as the row it
// put forward, and whether the reader took it.
function asTurn(message) {
  if (message.kind === "proposal") {
    const taken = message.added ? " (added to the list)" : "";
    return { role: "bart", text: `Proposed TODO row: ${message.text}${taken}` };
  }
  return { role: message.who === "you" ? "you" : "bart", text: message.text };
}

// One operation on the goals. The server refuses with a reason when it
// cannot be applied: a row the builder holds, a goal that is gone.
async function op(operation) {
  const answer = await post("/api/goal-page/op", operation);
  if (!answer.ok) throw new Error(answer.error || `${operation.op} was refused`);
  return answer;
}

export const services = {
  /** Who this machine is connected as. The server reads the account the
      installer wrote (auth.json under ~/.human-compact) and answers; the
      page never holds a token of its own. */
  async loadAccount() {
    const status = await get("/api/supabase");
    return {
      connected: Boolean(status.connected),
      signedIn: Boolean(status.signed_in),
      email: String(status.email || ""),
      name: String(status.display_name || ""),
    };
  },

  /** Disconnect this machine. The server runs `engelbart logout`: the
      machine token is revoked at the backend, the Claude Code helper is
      unwired and auth.json is removed. Resolves to what the CLI said. */
  /** The reader's profile, as every prompt reads it: the four answers
      and, for the menu, the level's name. Account-scoped. */
  async loadReader() {
    const answer = await get("/api/reader");
    if (!answer.ok) throw new Error(answer.error || "the profile could not be read");
    return { profile: answer.profile || {}, levelLabel: answer.level_label || "" };
  },

  /** One of the four levels, kept on the profile. */
  async saveLevel({ level }) {
    const answer = await post("/api/goal-page/reader", { level });
    if (!answer.ok) throw new Error(answer.error || "the level could not be saved");
    return { profile: answer.profile || {}, levelLabel: answer.level_label || "" };
  },

  async signOut() {
    const answer = await post("/api/account/sign-out");
    if (!answer.ok) throw new Error(answer.error || "sign out failed");
    return answer.message || "Disconnected.";
  },

  /** Connect this machine. The server runs `engelbart auth`, which prints
      a code, opens the page that approves it, and waits for the approval.
      Each answer is { status, code, url, error }, status one of waiting,
      ready, failed, cancelled; ask signInStatus until it is not waiting. */
  async startSignIn() {
    return post("/api/account/sign-in");
  },

  async signInStatus() {
    return get("/api/account/sign-in");
  },

  async cancelSignIn() {
    return post("/api/account/sign-in/cancel");
  },

  /** Every project this vault knows, newest worked-in first, and which
      one this workspace is in. */
  async listProjects() {
    const answer = await get("/api/projects");
    if (!answer.ok) throw new Error(answer.error || "the projects could not be read");
    return { projects: answer.projects || [], active: answer.active || "" };
  },

  /** Another project's workspace, opened beside this one: the server
      answers with its address. The same door /legacy uses. */
  async openProject({ cwd }) {
    const answer = await post("/api/op", { op: "open_project", cwd });
    if (!answer.ok) throw new Error(answer.error || "that project could not be opened");
    return { url: answer.url || "" };
  },

  /** The goal this page is about, its subgoals, and what each already
      holds; and the project the workspace is in, when it is in one. goalId
      is the goal the address names, or empty for whichever this workspace
      is most recently about. A workspace with no goal answers goal null and
      empty true. */
  async loadGoal({ goalId } = {}) {
    const query = goalId ? `?goal=${encodeURIComponent(goalId)}` : "";
    const answer = await get(`/api/goal-page${query}`);
    if (!answer.ok) throw new Error(answer.error || "the goal could not be read");
    return {
      goal: answer.goal,
      project: answer.project || null,
      subgoals: answer.subgoals || [],
      slices: answer.slices || {},
      goals: answer.goals || [],
      empty: Boolean(answer.empty),
      revision: answer.revision,
    };
  },

  /** A goal at the top of the tree, for a workspace that has none yet. */
  async createGoal({ title }) {
    const answer = await op({ op: "add_goal", title });
    return { id: answer.id, title, revision: answer.revision };
  },

  /** A new subgoal under the goal; answers with the record as stored. */
  async addSubgoal({ goalId, title }) {
    const answer = await op({ op: "add_goal", title, parent_goal_id: goalId });
    return { id: answer.id, title, goalId, revision: answer.revision };
  },

  /** Bart's reply to one message, in the conversation of one subgoal.

      The whole conversation goes out each time: the server holds the
      tree, not the argument. Answers with the messages to draw, in order:
      { kind: "text", text } for prose, a question or a choice, and
      { kind: "proposal", text } for each row the model put forward. Throws
      with the reason when the model could not be reached. */
  async sendBartMessage({ goalId, subgoalId, text, history, todos }) {
    const transcript = [
      ...history.filter((m) => m.kind !== "error").map(asTurn),
      { role: "you", text },
    ];
    const answer = await post("/api/goal-page/bart", {
      goal_id: goalId, subgoal_id: subgoalId, transcript,
    });
    if (!answer.ok) throw new Error(answer.error || "Bart could not answer");
    return { replies: answer.replies || [] };
  },

  /** One subgoal's conversation, written down whole -- after a message
      sent, a reply landed, a proposal taken -- so a reload draws what was
      on screen. Answers with the messages as kept. */
  async saveChat({ subgoalId, messages }) {
    const answer = await post("/api/goal-page/chat", { subgoal_id: subgoalId, messages });
    if (!answer.ok) throw new Error(answer.error || "the conversation could not be saved");
    return { subgoalId, messages: answer.messages || [] };
  },

  /** A todo on a subgoal, typed or accepted from a proposal (source names
      the proposing message). Answers with the row as stored; a line the
      subgoal already has comes back as that row, marked existing. */
  async addTodo({ subgoalId, text, source }) {
    const answer = await op({ op: "add_todo_row", goal_id: subgoalId, text });
    return { ...answer.row, existing: Boolean(answer.existing), revision: answer.revision };
  },

  /** One change to a row: { text } or { done }. Refused while the builder
      holds the row. */
  async updateTodo({ subgoalId, todoId, patch }) {
    let answer = null;
    if ("text" in patch) {
      answer = await op({ op: "set_todo_text", goal_id: subgoalId, id: todoId, text: patch.text });
    }
    if ("done" in patch) {
      answer = await op({ op: "set_todo_done", goal_id: subgoalId, id: todoId, done: Boolean(patch.done) });
    }
    return { subgoalId, todoId, row: answer && answer.row, revision: answer && answer.revision };
  },

  async removeTodo({ subgoalId, todoId }) {
    const answer = await op({ op: "remove_todo_row", goal_id: subgoalId, id: todoId });
    return { subgoalId, todoId, revision: answer.revision };
  },

  /** Hand every open todo of a subgoal to the builder. The server marks
      the rows as it takes them, so the goal's files change and the page
      hears it. Answers with the rows handed over; refused with the
      builder's reason when the build cannot start. */
  async startBuild({ goalId, subgoalId, todos }) {
    const ids = todos
      .filter((todo) => !todo.done && !WITH_BUILDER.has(todo.status))
      .map((todo) => todo.id);
    const answer = await op({ op: "build_todos", goal_id: subgoalId, ids });
    return { started: true, todoIds: answer.rows || ids, revision: answer.revision };
  },

  /** The server's change feed: onChange(revision) each time the goals'
      revision changes, whoever changed them. The browser reconnects a
      dropped stream on its own. Returns { close }. */
  watchGoal({ onChange }) {
    const source = new EventSource("/api/goal-page/events");
    source.addEventListener("change", (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data && data.revision) onChange(data.revision);
      } catch (error) {
        console.error("engelbart: a change event could not be read", error);
      }
    });
    return { close: () => source.close() };
  },

  /** The two side panes for one subgoal, in one read: the project's run
      as the preview engine sees it (status, surface, profiles, blockers,
      the running process and its url), and the subgoal's build log
      ({ lines: [{ at, kind, text }], run }). */
  async getPanes({ subgoalId }) {
    const answer = await get(`/api/goal-page/panes?goal=${encodeURIComponent(subgoalId || "")}`);
    if (!answer.ok) throw new Error(answer.error || "the panes could not be read");
    return { preview: answer.preview, build: answer.build };
  },

  /** One of the preview's operations -- preview_configure, preview_show_ui,
      preview_start, preview_stop, preview_forget -- with its arguments.
      Answers as the engine does: { ok } with a reason when it would not. */
  async previewOp(op) {
    return post("/api/goal-page/preview", op);
  },
};
