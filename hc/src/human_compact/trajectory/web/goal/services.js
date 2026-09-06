/* The boundary between the goal page and everything behind it.

   Every function here is the shape a real service will take -- the goal
   store, the Bart runtime, the builder, the preview server, the terminal.
   loadAccount is real: it asks the server who this machine is connected
   as. The rest are mocked: their answers are the example content of the
   design, held in memory for the life of the page. Replace the bodies and
   keep the signatures; nothing above this file knows the difference.

   Each function takes one object of named arguments and returns a promise,
   so the swap to a fetch is a change inside the function alone. */

const REPLY_DELAY_MS = 900;
const BUILD_DELAY_MS = 700;

const GOAL = { id: "g-1", title: "Create an interface to import the dataset" };

const SUBGOALS = [
  { id: "s-1", title: "Create a blank interface with an import button" },
  { id: "s-2", title: "Save the dataset locally to my project folder" },
  { id: "s-3", title: "Allow me to inspect the dataset in a CSV viewer" },
];

const SLICES = {
  "s-1": {
    todos: [
      { id: "t-1", text: "Create a blank interface", done: false },
      { id: "t-2", text: "Add an import button", done: false },
    ],
  },
};

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

// Ids minted here start past every id the seeds above use.
let seq = 100;
const nextId = (prefix) => `${prefix}-${(seq += 1)}`;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const copy = (value) => JSON.parse(JSON.stringify(value));

export const services = {
  /** Who this machine is connected as. The server reads the account the
      installer wrote (auth.json under ~/.human-compact) and answers; the
      page never holds a token of its own. */
  async loadAccount() {
    const response = await fetch("/api/supabase", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`account status answered ${response.status}`);
    const status = await response.json();
    return {
      connected: Boolean(status.connected),
      signedIn: Boolean(status.signed_in),
      email: String(status.email || ""),
      name: String(status.display_name || ""),
    };
  },

  /** The goal this page is about, its subgoals, and what each already holds. */
  async loadGoal() {
    return copy({ goal: GOAL, subgoals: SUBGOALS, slices: SLICES });
  },

  /** A new subgoal under the goal; answers with the record as stored. */
  async addSubgoal({ goalId, title }) {
    return { id: nextId("s"), title, goalId };
  },

  async saveNotes({ subgoalId, text }) {
    return { subgoalId, length: text.length };
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
      the proposing message). Answers with the row as stored. */
  async addTodo({ subgoalId, text, source }) {
    return { id: nextId("t"), text, done: false };
  },

  async updateTodo({ subgoalId, todoId, patch }) {
    return { subgoalId, todoId, patch };
  },

  async removeTodo({ subgoalId, todoId }) {
    return { subgoalId, todoId };
  },

  /** Build every open todo of a subgoal. Answers with the ones it built. */
  async startBuild({ goalId, subgoalId, todos }) {
    await wait(BUILD_DELAY_MS);
    return { status: "built", todoIds: todos.filter((todo) => !todo.done).map((todo) => todo.id) };
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
