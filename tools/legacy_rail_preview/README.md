# Legacy TODO rail reference

An editable cutout of Engelbart's actual legacy right rail, for bringing its
interaction behavior into the new interface. It runs the checked-in legacy
bundle and bridge. The rail editor has not been reimplemented.

## Run

From this checkout:

```sh
python3 tools/legacy_rail_preview/serve.py --port 8766
```

Open http://127.0.0.1:8766/. Generated HTML and browser assets are exported to
`~/Desktop/Codex Readings/engelbart-legacy-rail/`. Edits persist in that folder's
`demo-state.json`; the server uses no real Engelbart project, credentials, model
API, or shell. Builds simulate a two-second run on the selected rows. Questions
and notes save, but Understanding does not call an AI. This is a UI reference,
not an implementation of the production agent service.

## Preserve when integrating

- A continuous editor with no separator between each TODO.
- Enter splits a row and leaves the caret in the new row.
- Tab / Shift+Tab indent and outdent. Arrow keys move within the editor.
- Cmd/Ctrl+A selects buildable rows; Cmd/Ctrl+/ selects the current row.
- Cmd/Ctrl+Enter builds the selection, or the whole list if nothing is selected.
- Row state changes preserve unsaved text and the caret.
- Copy all exports the list. Notes and Understanding remain distinct tabs.

`cutout.css` removes the surrounding workspace and trims Quick, token estimates,
and build diagnostics. The underlying editor and owner menu remain the legacy
ones. The small demo labels should not be copied into the product.

## Integration boundaries

The source of truth is `hc/src/human_compact/trajectory/web/bridge.js`:
`renderTodoRail`, `todoKey`, `todoBeforeInput`, `todoSaveNow`,
`todoReconcile`, `todoBuild`, and the `understand*` functions. The exact function
names can be located with `rg`; they are deliberately not duplicated here.

The editor uses GET `/api/state`, POST `/api/import` (goals plus base revision),
and POST `/api/op` (including `build_todos` and `set_understanding`). This demo's
`DemoStore` is only an isolated adapter. Production should retain its existing
revision, authorization, active-build, and server-owned row-status handling.

The generated `bridge.js` is a snapshot for review. Re-run the command after
changing the source; do not maintain that generated copy as another editor.
