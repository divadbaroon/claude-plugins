Dataset collections
===================

Production Dataset accepts a file or folder. Choose file retains the existing file input; Choose folder uses `webkitdirectory`. Drops recursively enumerate `webkitGetAsEntry` directories, reading every batch from each directory reader. Only File handles and relative paths are held in the browser; no directory Blob, ZIP, or client-side data parser is created. Unsupported directory enumeration reports an error and offers the folder picker. Empty directories are not represented by the file-picker API. `.DS_Store` and `Thumbs.db` are ignored.

`POST /api/project-dataset/import` accepts begin/status/finish/cancel JSON commands. Begin validates a manifest and creates an opaque persisted import session. Files stream sequentially through the existing `/api/project-dataset/upload` octet-stream boundary using `X-HC-Import` and the encoded relative path in `X-HC-Name`. The existing local Host/Origin/shared-project checks apply. No collection bytes go through JSON. A partial file is removed; status lists completed files so a caller can resume. The current browser cancels failed transfers and invites reselection; it does not silently activate or resume an abandoned import on reload. Unreferenced staging files expire after 24 hours and are cleaned on the next import.

Policies are local and configurable:
- HC_DATASET_MAX_BYTES: 8 GiB total.
- HC_DATASET_MAX_FILE_BYTES: 1 GiB per file.
- HC_DATASET_MAX_FILES: 5,000 files.
- At least 256 MiB free disk space must remain beyond the declared collection size.

Paper's separate project-PDF upload cap remains 20 MiB. Legacy dataset file uploads now use the dataset file policy. Raw file reads/writes and hashing use 64 KiB chunks. Paths are NFC-normalized; traversal, absolute/Windows-device paths, duplicate casefolded paths, file/directory collisions, and symlink/special-file storage entries are rejected. Stored files are non-executable. Browser APIs do not expose POSIX link metadata; the server never traverses supplied local filesystem links.

All files remain under `.engelbart-resources/import-…/files/` with their hierarchy. The complete `manifest.json` is stored alongside them. It contains version/root, file/folder counts, byte total, directories, paths, sizes, formats, content digests, and bounded inspection metadata. The normal project resource contains only a summary (at most 64 file entries), a manifestPath, and at most two bounded table previews. Up to 24 readable tables are inspected incrementally; other files remain accessible and can be inspected later. Invalid/unsupported files do not invalidate a collection with readable data. A collection with no readable supported table fails without changing the active dataset.

CSV/TSV inspection is bounded by rows, physical line size, and a 4 MiB input budget. JSON arrays over 2 MiB use a bounded first-ten-record parser. JSONL, Parquet, and XLSX reuse safe inspection. XLSX remains read-only/data-only, with external links disabled, no formula evaluation, bounded ZIP metadata and macro refusal. Uploaded archives are retained as artifacts, never automatically expanded by folder import; legacy remote ZIP extraction retains its existing bounds.

The activeDatasetId changes only after successful preparation, under the shared project resource lock. Previous resources remain available. A manifest/content digest deduplicates repeated completed imports, and provenance records which selected/unresolved resources a local folder satisfies. Prepared resources and their manifests survive reload.

GitHub and Anonymous GitHub handoffs use the same collection shape. Local acquisition fetches only manifest-listed paths under the selected root. GitHub uses a pinned commit and verifies blob SHA when provided. Incomplete/unknown-size manifests require a local folder; no whole unrelated repository is cloned. A failed acquisition preserves the prior active dataset.

Build/context reads the local full manifest within an 8 MiB metadata budget, chooses at most four likely relevant table references using current project terms, and includes a reason. This is an explicit heuristic, not a claim of scientific relevance. The full directory and manifest remain available to the builder to refine the subset. Raw dataset contents and the complete manifest are never dumped into normal model context.

Release requires the updated installed wheel (0.20.0). Publish the installed client before deploying hosted collection handoffs. No database migration is required.

Hosted Paper-step attachments (0.20.1)
------------------------------------
The hosted uploader carries private uploaded collections through the same resources payload. Supabase file URLs are signed only at authenticated claim time. The importer downloads each listed file into the existing staging session and makes the completed dataset active automatically. Signed URLs are removed before writing project metadata. A failed or expired download preserves the current dataset and asks for retry/local supply. Hosted copies remain private in the separate dataset bucket; the Paper bucket and 20 MiB PDF policy are unchanged.

## Linked local folders (0.20.2)

The hosted Paper step can carry a `local_path` provider with a user-entered absolute or `~/` folder path. The installed runtime resolves it on this computer and inspects it in place, without copying files or uploading bytes. Metadata is stored in project resource storage; the data remains in the original folder. It uses the same bounded collection manifest, Dataset pane, active dataset and Build subset context. Keep the folder at its original path. Missing paths become needs_user; invalid paths, symlinks and special files fail without replacing the previous active dataset. Normal local ingestion size/count policies still apply.

## Native folder picker (0.20.3)

Dataset's **Choose local folder** opens the existing operating-system directory dialog through the authenticated local UI and prepares its result using the in-place collection importer. The browser does not send an arbitrary selected path in this operation. The existing Choose folder/upload controls still copy files into local workspace storage. Canceling the native dialog preserves the active dataset.

Hosted onboarding can queue a `local_picker` source without typing a path. The dialog opens when the installed runtime first prepares that project (not while viewing the hosted Paper page, which precedes installation). Successful preparation saves a `local_path` source and is cached; cancellation/unavailable dialogs produce needs_user with a retry button in Dataset. No native dialog implementation is replaced: macOS AppleScript, Windows FolderBrowserDialog, and Linux Zenity/KDialog remain the existing adapters. No new localhost/CORS bridge is exposed.
