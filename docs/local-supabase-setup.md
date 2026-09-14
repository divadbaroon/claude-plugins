# Local Supabase setup

The Environment check offers **Install Docker and set up local Supabase** when a
component or its repository ancestors contain `supabase/config.toml`. Nothing is
installed by inspection or automatic continuation. The explicit action authorizes
Docker installation/startup, the verified Supabase CLI download, image downloads,
and local database creation. OS elevation and Docker Desktop agreements remain
native user interactions. Resume continues the same persistent local workspace.

The host adapter owns every command. It never runs repository setup shell scripts,
`link`, `db push`, remote migration commands, or production data imports. Docker
contexts must use a local Unix socket or Windows named pipe; TCP/SSH contexts are
rejected, including on stop. Supabase runs with an isolated tool home and no
inherited Supabase access token. Each component path has a stable hashed identity,
private workspace and separate ports. Process and file leases serialize duplicate
operations and tool installations. Multiple UI servers cannot provision the same
workspace concurrently.

Only schema/seed files are copied; `.temp`, `.env`, remote links and functions are
excluded. Symlinks, escaped seed paths, network operations in SQL, unresolved
external configuration, and absent migrations require review. Missing seed paths
are reported and omitted, allowing an empty database. External auth providers,
auth hooks, edge functions, analytics and experimental configuration are disabled
for this database/auth/storage preview; full hosted-feature parity is not claimed.
A changed schema/config pauses rather than resetting an existing database.

The CLI release is downloaded from the official Supabase GitHub release, checked
against its SHA-256 manifest, and installed outside the repository. `start` performs
the CLI's health checks and applies migrations. `status --output json` supplies the
local URL and credentials. Conventional `SUPABASE_*`, `NEXT_PUBLIC_SUPABASE_*`,
`VITE_SUPABASE_*`, `PUBLIC_SUPABASE_*` and `REACT_APP_SUPABASE_*` names, including
DEV/LOCAL suffixes, are mapped only if detected in the component. Custom mappings
need review. Values are stored in private files and are never returned in service
status or repair evidence. Local launch values override repository/saved Supabase
values; selected but incomplete/stopped local setup prevents fallback to hosted
configuration. Manifund's selector is pinned to its default branch, whose values
are replaced with the local credentials. Unrelated integrations are not modified.
Applications that hardcode remote clients or deliberately override process
configuration still need application-specific review; this is not a network sandbox.

**Stop local services** uses only the owned workspace and does not discard data.
No delete/reset database action is provided. After server interruption, inspect
reports Resume rather than leaving a permanently busy UI. Diagnostic command
output is retained only in private local error logs, not copied into model prompts.

## Platform support

- macOS: official Docker DMG, Gatekeeper assessment, native installer with an OS
  administrator dialog, then Docker Desktop first launch. Existing local runtimes
  are reused. No automatic license acceptance.
- Windows: exact `Docker.DockerDesktop` package through winget's interactive
  installer, then local engine verification. App Installer/WSL or reboot requirements
  can require user input.
- Debian/Ubuntu desktop: distribution `docker.io` package through apt and pkexec.
  Engine socket access remains subject to existing account permissions; setup does
  not silently grant root-equivalent docker-group membership. Headless machines or
  other distributions need an existing compatible local runtime.

This is a reusable adapter for one app component, not an automatic shared-database
assignment across an arbitrary monorepo. Separate components have separate backend
identities. Repositories requiring a shared cross-component backend need review.

## Validation

`test_project_supabase` covers credential override, local endpoint enforcement,
remote context rejection, leases, interrupted resume, stop/data retention, schema
changes, symlink rejection, missing seeds, mapping, and independent identities.
The existing environment UI harness covers explicit provisioning and removal of
stale hosted drafts. Native CLI installation/checksum/version was exercised on
macOS ARM64. Docker system installation and a complete real Supabase startup have
not been exercised on this machine. Cross-platform Docker tests are still needed.

Contracts: https://supabase.com/docs/reference/cli/supabase-start,
https://supabase.com/docs/reference/cli/supabase-status,
https://docs.docker.com/desktop/setup/install/mac-install/,
https://docs.docker.com/desktop/setup/install/linux/ubuntu/.

Local validation results: 57 focused tests passed; Firefox and WebKit compatibility
passed (2 tests). The installed native round trip passed its Claude lifecycle test
but failed the pre-existing installer stdout contract: Berkeley expects the BART
banner, while this feature branch prints installation progress. This fails before
Supabase UI execution. No merge is permitted while that gate or the macOS/Ubuntu/
Windows CI gates remain red or unverified.
