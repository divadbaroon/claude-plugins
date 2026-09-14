# Reusing project runs across previews

A repeat launch of the same resolved local project directory returns its existing active run. The new plan is not executed until the user chooses Stop and restart. Different local checkouts remain different projects, even if their GitHub origin matches.

Each UI supervisor issues a random ownership capability. Retained run records store that capability and the supervisor's loopback address outside the repository, in private run files; the capability is never returned in run state to the browser. Other previews contact that supervisor to read state, stop the run, or resume explicit approvals. Requests verify both the capability and in-memory ownership. No PID-based adoption or port-based process termination occurs. The client disallows redirects and non-loopback endpoints.

A per-directory launch lock serializes launch requests across processes. If another launch is in progress, the caller receives a retry message. Process crashes can leave launch locks requiring inspection. An unavailable owner blocks replacement with an actionable error; it does not authorize killing a recorded PID. Runs created by older supervisors require stopping through the old preview before migration.

The UI retains the originally requested analysis when joining an existing run. Stop and restart first waits for the owner to stop the existing run, then launches the requested analysis. Auto-continue does not trigger this destructive replacement automatically.

Validation includes two separate real UI supervisors sharing isolated local storage, an actual static HTTP service, reuse with unchanged PID, authenticated rejection, remote stopping, and replacement startup. UI action tests cover stop-before-start ordering and selection of the requested plan. Cross-repository release gates have not been run; no merge is performed.
