# Highlight questions in the runtime explorer

User request: highlight text, ask this Codex, and append the answer as a
question/subquestion on this same page. Push the implementation branch.

## Contract

- Preserve the existing 36 questions, scenarios, and source disclosures.
- Select text in reading content to reveal Ask Codex; also permit a general
  question without selection. Empty/cancelled questions create no annotation.
- Store exact quote, prefix/suffix, source title/text/hash URL, parent ID, and
  question before dispatch. New answers can themselves be highlighted.
- Continue this specific Codex thread through the installed `codex queue`
  interface. Do not substitute another model/session or run a second builder.
- Serialize page questions so one completed response has one pending parent.
- Capture only tagged page-question answers from this thread's local rollout;
  do not import unrelated conversation history. Persist questions/answers in
  SQLite outside the generated HTML. Polling updates the UI without reloading.
- Bind the service to loopback. Mutations require same-origin JSON requests
  and an unpredictable session token. Never interpolate text into shell code.
- A delivery failure retains the question and exposes retry; a timeout is
  uncertain delivery and must not be automatically resent.
- Keep the existing page's typography and layout. Nest follow-ups in the
  question index, show their source quote and parent, retain navigation/search.
- Include keyboard access, small-screen layout, and an export of added Q&A.
- Verify storage/idempotency, answer correlation/restart, queue failure,
  selection/ask/nesting, escaping, and one real queue round trip. Push branch,
  update its draft PR, and leave the local service running on port 8768.

## Implementation

- [x] Backend: `thought_store.py` (SQLite and tagged rollout reader),
  `serve.py` (loopback API/static server + serialized CLI delivery).
- [x] Frontend: `thoughts.js`, `thoughts.css`, targeted `guide.js` hooks.
  Added questions share the existing `qa` index and use `#question=<id>`.
- [x] Integration: render/validate scripts embed assets, README documents
  service startup and the exact response-correlation boundary.
- [x] Verification: temporary-state Python tests and real browser checks;
  read-only review before commit/push. No Engelbart runtime modifications.

## Frontend/backend interface

`GET /api/thoughts` returns `{questions:[...],token,connected:true}`. A node is
`{id,question,parent_id,anchor:{quote,prefix,suffix},source:{id,title,text,url},
status,answer,error,created_at,updated_at}`. Status is waiting/sending/queued/
answering/answered/failed/uncertain. Answer is Markdown text, escaped on render.

`POST /api/questions` receives the node's `id` (client UUID prefixed `q_`),
question, parent_id, anchor, source; returns `{question:node}`. Send header
`X-Thought-Token`. `POST /api/questions/<id>/retry` retries failed/uncertain
delivery only. `GET /api/export` downloads just the added questions/answers.

The app-server target thread and transcript path are server configuration,
never browser-provided input. The queue prompt carries a unique
`[THOUGHT_QUESTION:q_<uuid>]` line. Completed answer correlation uses that
user-message marker and/or an explicit `[THOUGHT_ANSWER:q_<uuid>]` marker;
unrelated turns are never silently attached.

## Live completion check

Completed: selecting an authored passage created a linked question, the
installed desktop queue delivered it to this same conversation, and the
service captured its real completed answer under the correct parent.
The answer marker was removed. Browser inspection confirmed the answer,
source quote, parent link, and source reference after regeneration and reload.
The real response exposed flattened numbered steps; the renderer now preserves
ordered lists. No queued follow-up tests remain. The local service stays open.
