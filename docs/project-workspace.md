# Concurrent project workspace

The setup modal accepts up to ten GitHub HTTPS URLs per submission. URLs are queued from the central entry panel. Adding a URL does not start checkout, assessment, or execution. Run all starts checkout and assessment concurrently for projects that have not started yet. The left sidebar selects a project's existing setup, configuration, repair, logs, and service previews. After Run all, projects automatically continue when their own configuration and approvals permit it. Subsequent Run all clicks skip started projects, including those waiting for input or needing an explicit retry.

Each project has a controller bound to an independent store view. Async requests, model polling, run polling, configuration drafts, and automatic continuation remain associated with that controller even when another project is selected. A project's required input or failure does not block the others. Per-project reset/restart applies to the selected project. The manual Reset all action stops tracked runs before clearing the workspace; if stopping fails, the list is retained for review. The sidebar only selects projects; add, run, and reset controls are in the main panel.

Duplicate repository URLs select the existing instance. Repository owner/name case, .git suffixes, and tracking query parameters are normalized for this comparison; branch path case is preserved. The managed checkout and process supervisor retain final authority over checkout reuse and run ownership.

Browser storage retains project paths and run/order references for reopening. Draft environment values are not persisted by the workspace. A refreshed page restores existing runs and assessments; unfinished configuration can be checked again. Existing single-project resume records are migrated into the workspace. References are local to the browser origin.

Concurrency does not allocate arbitrary replacement ports or create containers. The supervisor's existing port conflict, process ownership, environment, and approval rules still apply independently to each project. Projects that require the same fixed ports may pause for resolution.

Validation covers concurrent request completion in reverse order, a running project alongside one waiting for configuration, independent drafts, duplicate handling, invalid URLs, and run-reference restoration. A Chromium UI check verifies batch entry and switching through the actual rendered components with fixture service responses. This is not a claim that arbitrary GitHub repositories can all launch without configuration.


## Preview and step navigation

Live preview is a separate view, containing service tabs, preview controls and the embedded application. Run retains installation output and logs. Bottom navigation provides Assessment, Environment, Run and Live preview, plus Back and Next. Unavailable steps are disabled; revisiting a completed step does not restart the application.

The top Automatic toggle controls automatic progression for workspace projects. It preserves required configuration and approval pauses. Once a project is healthy, automatic progression opens Live preview once; returning to Run to review logs does not bounce back on the next status poll. Run all keeps the Projects overview open. Live status cards show queued, active, configuration/approval, error and healthy states; selecting a card opens that project. Adding projects still only queues them.


Required configuration rows support an explicit “Skip for this run” checkbox. Skips are keyed by component directory and forwarded to the supervisor. Only those names bypass the missing-value gate; no placeholder value is injected. The application can still fail without configuration. Run state retains the skip names across repair attempts. Returning to Projects opens the central entry page without resetting projects. The sidebar displays Done for a healthy project that has reached Live Preview; the underlying running/health status remains unchanged.


Railpack analysis is tracked per canonical project directory. Different directories analyze concurrently. A newer request for the same directory cancels the prior analysis, waits for its process to be reaped, and rejects late results from the replaced request. This affects analysis only, not running application services.

Port recovery supports bounded `--port` and loopback `--hostname` overrides for
npm scripts directly invoking `next dev` or `next start`, alongside the existing
Vite options. The repair assessment must consider dependent origin/proxy settings
and update the health URL with the launch port. Occupied ports in single-service plans feed back into the
repair budget with an available-port candidate (not a reservation). The original
execution failure remains available after validator errors. The supervisor keeps
its existing owner-verified same-checkout reuse; it never kills an unknown listener.
Successful npm preparation can be reused during a port-only repair in the same
launch, with unchanged command, directory, and no environment overrides. This
cache is not carried across launches or configuration edits. A prelaunch failure
has empty process logs rather than inheriting the preceding build's output.

The entry page offers GitHub, Local codebase, and Paper tabs. Paper files remain
in browser memory until refresh; a bounded local upload endpoint extracts GitHub,
Hugging Face, and OSF links from PDF text/annotations, DOCX, or text. Extracted
GitHub repositories can be added to the same queue without starting them. Links
are not followed during extraction. The PDF supplied during manual verification
returned both Verina resource URLs. Managed launches set `BROWSER=none` to avoid
CRA opening a separate browser tab.

Multi-service port conflicts pause for review of dependent configuration before
preparation or a repair model call.
