# Berkeley Research Directory — UI

A static front end over the Berkeley directory in Supabase. No build step, no
dependencies: open `index.html` and it runs.

## Connecting it

It reads two published surfaces:

| what | used for |
| --- | --- |
| `berkeley_people` view | everything — the whole set is loaded once and filtered in the browser |
| `berkeley_search` RPC | ranked full-text search when a query is typed |
| `berkeley_projects` view | projects on the detail drawer, if it is exposed |

Only `berkeley_people` is required. If the RPC is missing the search falls back
to substring matching in the browser; if the projects view is missing the
drawer simply omits that section.

Put the project URL and the **anon (public)** key in `config.js`:

```js
window.BERKELEY_CONFIG = {
  url: 'https://yourproject.supabase.co',
  anonKey: 'eyJhbGciOi...',
};
```

Or leave it empty and use the **Connection** button in the header — the answer
is kept in `localStorage`. The service-role key never belongs here.

## Running it

```sh
python3 -m http.server 8000 --directory berkeley-ui
```

then open <http://localhost:8000>. Opening `index.html` from the filesystem
works too, as long as the Supabase project allows the `null` origin.

## The two views

**Search** — one box, ranked results, plus filters for role (professor / PhD
student), department, and a free-text research interest. An empty box lists
everyone the filters allow rather than waiting for a query.

**Browse** — departments on the left with counts, the people in the selected
department on the right, split into professors and PhD students.

Either one opens the same detail drawer: bio, research interests, lab (linked),
majors, advisor, projects, and the source page the record was scraped from. An
advisor is clickable, and a professor's drawer lists the students they advise.

## Column names

The published view's exact column names are not pinned down, so `FIELDS` and
`PROJECT_FIELDS` at the top of `app.js` map each logical field to a list of
candidate columns and take the first one present. A renamed column shows up as
a missing field rather than a broken page — add the new name to the list.
