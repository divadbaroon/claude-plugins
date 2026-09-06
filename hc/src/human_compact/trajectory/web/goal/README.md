# The goal page

What a chat workspace opens on: `hc chat-ui` serves `index.html` at `/` and
the rest of this directory at `/goal/<name>`. Plain ES modules, no build
step; the browser loads them as they are.

    index.html      the shell: fonts, styles.css, app.js
    styles.css      the design's tokens and one class per element
    app.js          creates the store, the actions and the first draw
    store.js        the state tree and the readers on it (one slice per subgoal)
    actions.js      what the reader can do; the only writer of the store
    services.js     the boundary to everything behind the page (loadAccount is
                    real, the rest mocked)
    dom.js          h() to build a tree, mount() to morph the page toward it
    components/     one render function per region, pure in state and actions

Every change to the store redraws the whole page from state; `mount()`
morphs the tree on screen toward the new one, so inputs the reader is
typing in keep their focus and caret. Rows that can reorder carry a `key`.

`services.js` is where the real goal store, the Bart runtime, the builder,
the preview server and the terminal will attach. `loadAccount` shows the
pattern: it asks the server's existing `/api/supabase` route who this
machine is connected as (the account the installer wrote to `auth.json`),
and the header draws the answer. `signOut` is still a mock: the real one
is `engelbart logout`, which revokes the machine token and removes the
file, and needs a server route to run it. Each function there takes
one object of named arguments and returns a promise; replace the bodies,
keep the signatures.

The workspace this page replaced still answers at `/legacy`.
