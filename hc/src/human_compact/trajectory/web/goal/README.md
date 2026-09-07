# The goal page

What a chat workspace opens on: `hc chat-ui` serves `index.html` at `/` and
the rest of this directory at `/goal/<name>`. Plain ES modules, no build
step; the browser loads them as they are.

    index.html      the shell: fonts, styles.css, app.js
    styles.css      the design's tokens and one class per element
    app.js          creates the store, the actions and the first draw
    store.js        the state tree and the readers on it (one slice per subgoal)
    actions.js      what the reader can do; the only writer of the store
    services.js     the boundary to everything behind the page (the goals,
                    the account, Bart and the panes are all real)
    dom.js          h() to build a tree, mount() to morph the page toward it
    components/     one render function per region, pure in state and actions

Every change to the store redraws the whole page from state; `mount()`
morphs the tree on screen toward the new one, so inputs the reader is
typing in keep their focus and caret. Rows that can reorder carry a `key`.

`services.js` is the boundary to everything behind the page. Each function
there takes one object of named arguments and returns a promise; replace
the bodies, keep the signatures.

The goals are real. `loadGoal` reads `GET /api/goal-page`: the goal the
address names (`/?goal=<id>`), or when it names none the top-level goal
touched most recently, among those in progress or with something under
them; its subgoals; for each the todo rows, straight from the
chat's `goals.json` through the goals model; and the project the chat is
in, whose name the header shows. This is how a project finished
in the web onboarding arrives: `/bart` claims it, writes its tree and binds
the chat, and the page opens on the direction the reader chose (the ones
they were offered and did not take stay in the tree, with nothing under
them) with its pieces as subgoals (each
piece's notes are seeded from the setup's description and its why, kept
in the tree for `/legacy` and the hooks; this page does not draw notes). The
header's path is the way around: the brand opens every project the vault
knows (`GET /api/projects`, a card each, opening one goes to its workspace
through `open_project`), the project's name opens this project's goals as
cards -- the direction's why, how many pieces are done, a check when all of
them are -- and a goal card opens that goal here, named on the address. The
account menu has the reader's level under a rule, on the bar slider the web
setup asks it with (`GET /api/reader`, `POST /api/goal-page/reader` with one
of `reader.LEVELS`; the rest of the profile is kept as it was). A workspace
with no goal answers empty, and the page asks for one in a line; a goal
with nothing under it yet asks for its first subgoal, since the
conversation and the todos each belong to one. Every
write is one operation on `POST /api/goal-page/op` -- `add_goal`,
`add_todo_row`, `set_todo_text`, `set_todo_done`,
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

Bart is the brainstorm behind `/legacy`, told which subgoal the
conversation is about. `POST /api/goal-page/bart` takes the subgoal's whole
conversation, reads and digests the tree under the state lock, asks the
model outside it on the reader's own account (`claude` in safe mode, as
setup and the brainstorm do), and answers with what to draw: prose as
text, each row it proposed as a proposal the reader adds with one click
(through `add_todo_row`, so nothing is written until they do). A question
or a choice is said as text with its options, and answered by typing. The
conversation is kept beside the goals, per subgoal, in the page's own
shape (`chat_state.save_bart_chat`, `bart.json` in the tree's session):
`POST /api/goal-page/chat` writes it whole after every change, and the
payload's slices carry it back, so a reload draws what was on screen.

The preview and the terminal are still the design's example content, held
in memory for the life of the page.

The workspace this page replaced still answers at `/legacy`.
