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

   Still mocked: Bart's replies, the preview and the terminal. Their answers
   are the example content of the design, held in memory for the life of
   the page. Replace the bodies and keep the signatures. */

import { WITH_BUILDER } from "./store.js";

const REPLY_DELAY_MS = 900;

const PREVIEW = {
  host: "localhost:5173",
  app: "dataset-importer",
  url: null,   // a real preview answers with the address to frame
  placeholder: { action: "Import dataset", hint: "CSV only · up to 100mb" },
};

const TERMINAL = {
  lines: [
    { kind: "cmd", text: 'bart build "Create a blank interface with an import button"' },
    { kind: "out", text: "[1/2] built · create a blank interface" },
    { kind: "out", text: "[2/2] built · add an import button" },
    { kind: "cmd", text: "npm run dev" },
    { kind: "out", text: "ready · http://localhost:5173" },
  ],
};

const QUESTION = "Imagine this subgoal is done — what is the first thing you would see or click?";
const LEADING_INTENT = /^(i want to|i need to|i should|let me|allow me to)\s+/i;

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const copy = (value) => JSON.parse(JSON.stringify(value));

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

  async saveNotes({ subgoalId, text }) {
    const answer = await op({ op: "set_notes", goal_id: subgoalId, notes: text });
    return { subgoalId, revision: answer.revision };
  },

  /** Bart's reply to one message, in the conversation of one subgoal.

      The mock keeps the design's two answers: a subgoal with no todos yet
      gets the message back as a proposed todo, one that has some gets the
      question that draws the next one out. */
  async sendBartMessage({ goalId, subgoalId, text, history, todos }) {
    await wait(REPLY_DELAY_MS);
    if (todos.length) return { kind: "text", text: QUESTION };
    return { kind: "proposal", text: text.replace(LEADING_INTENT, "") };
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

  /** Where the goal's app is running, or the placeholder to draw instead. */
  async getPreview({ goalId }) {
    return copy(PREVIEW);
  },

  /** The terminal the builder works in, as lines. */
  async getTerminal({ goalId }) {
    return copy(TERMINAL);
  },
};
