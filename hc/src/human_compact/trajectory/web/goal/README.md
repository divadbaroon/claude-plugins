# The goal page

What a chat workspace opens on: `hc chat-ui` serves `index.html` at `/` and
the rest of this directory at `/goal/<name>`. Plain ES modules, no build
step; the browser loads them as they are.

    index.html      the shell: fonts, styles.css, app.js
    styles.css      the design's tokens and one class per element
    app.js          creates the store, the actions and the first draw
    store.js        the state tree and the readers on it (one slice per subgoal)
    actions.js      what the reader can do; the only writer of the store
    services.js     the boundary to everything behind the page (the goals
                    and the account are real, Bart and the panes mocked)
    dom.js          h() to build a tree, mount() to morph the page toward it
    components/     one render function per region, pure in state and actions

Every change to the store redraws the whole page from state; `mount()`
morphs the tree on screen toward the new one, so inputs the reader is
typing in keep their focus and caret. Rows that can reorder carry a `key`.

`services.js` is the boundary to everything behind the page. Each function
there takes one object of named arguments and returns a promise; replace
the bodies, keep the signatures.

The goals are real. `loadGoal` reads `GET /api/goal-page`: the goal the
address names (`/?goal=<id>`), or the top-level goal touched most recently
when it names none; its subgoals; and for each the notes and the todo rows,
straight from the chat's `goals.json` through the goals model. A workspace
with no goal answers empty, and the page asks for one in a line; a goal
with nothing under it yet asks for its first subgoal, since the notes,
the conversation and the todos each belong to one. Every
write is one operation on `POST /api/goal-page/op` -- `add_goal`,
`set_notes`, `add_todo_row`, `set_todo_text`, `set_todo_done`,
`remove_todo_row`, `build_todos` -- the same operations the workspace at
`/legacy` applies, so the two pages and the chat's hooks write one file.
Each answer carries the goals' revision after the write.

The page hears about everyone else's writes through `GET
/api/goal-page/events`, a stream of server-sent events. The server stats
the goal files every half second and, when one has moved, sends the new
revision; the page reads the goal again unless the revision is one it made
itself, and lays the answer under whatever the reader is in the middle of.
A row the builder holds (`queued`, `building`, `asking`) says so and is
left alone until it comes back. Local files are the truth; the chat's
autosync sends them to the account four seconds after the last edit.

The account shows the same pattern. `loadAccount` asks `/api/supabase` who
this machine is connected as (the account the installer wrote to
`auth.json`). `signOut` and `startSignIn` post to `/api/account/sign-out`
and `/api/account/sign-in`, where the server runs the Engelbart CLI itself:
`engelbart logout` revokes the machine token, unwires the Claude Code
helper and removes `auth.json`; `engelbart auth` prints a code, opens the
page that approves it, and waits, while the page asks `GET
/api/account/sign-in` after it until it has finished. The CLI is the one
the installer put at `~/.local/bin/engelbart`, or whatever `ENGELBART_CLI`
names.

Bart's replies, the preview and the terminal are still the design's
example content, held in memory for the life of the page.

The workspace this page replaced still answers at `/legacy`.
