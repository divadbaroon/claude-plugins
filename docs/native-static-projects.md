# Native static projects

Discovery recognizes a plain `index.html` directory even without a language
manifest. A nested static entrypoint becomes a component, so selecting a data or
research repository can find its visualization directory. Child HTML routes under
that static root are not separate components. Existing build, container, Staticfile,
or Caddy configuration prevents automatic classification as a plain static site.

For these plain sites, analysis creates a native service proposal instead of
executing a Railpack Caddy plan. The proposal uses `kind: "static"` and the internal
`argv: ["hc-static"]` capability marker. The marker is validated, never passed to a
shell. The supervisor substitutes its own installed Python interpreter and packaged
static-server entrypoint, preserving the same ownership, health, logging, and Reset
behavior as application services. No Caddy installation, generated Caddyfile,
dependency installation, or project source edits are required.

The server binds only to 127.0.0.1 on the selected nonprivileged port. It serves
existing static assets and denies hidden paths, path traversal, symlinks and
directory listing. It does not emulate Caddy configuration, proxy APIs, or build
source applications. External browser resources still require network access.

Both the Run Order and Setup Repair policy describe this capability. A repair brief
also lists eligible static directories as available services, so a previously
retained Caddy failure has evidence for a supported alternative. Static plan details
are labeled as native analysis in the UI, rather than attributed to Railpack.

Validation includes a disposable real supervisor round trip with HTML/JS requests,
health, denied paths, and Reset; bounded-discovery and command validation tests;
and a local Variety Box Office launch with unchanged visualization files. The
actual page and data.js returned HTTP 200. Chart interaction was not automated.

## Port conflicts

A standalone built-in static service tries its requested loopback port. If that
bind fails because the address is in use, it asks the OS for an available port
while creating the actual listening server. There is no separate choose/release/
rebind sequence for the fallback. The child reports its bound port through a
private, atomic supervisor-owned status file. The supervisor updates the health
probe, stored service URL, and UI notice to that actual address. Reset only stops
the owned process; the unrelated listener remains running.

Automatic remapping is limited to a single static service. Plans containing other
services may embed the original address in API URLs, proxy targets, CORS settings,
or build-time browser configuration. Without a verified connection contract, hc
does not guess those edits. It checks declared ports before preparation and returns
needs_input with the conflicting services and dependents. The user can free the
port or update connected configuration and analyze again. Fixed service ports are
also checked immediately before launch. Existing owned project runs are reused
separately; unrelated servers are never adopted just because they respond.
