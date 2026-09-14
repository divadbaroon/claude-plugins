# Preserve the repository package manager

Local setup now supports bounded install/build/script commands for npm, Bun,
pnpm, and Yarn. Both assessment and repair receive packageManager and lockfile
evidence. Repair also retains Railpack application commands. The validator rejects
substituting a different manager for a declared manager or a single detected
lockfile manager; repair checks the retained Railpack install manager as well.
Workspace components inherit ancestor declarations within the repository boundary.
Conflicting lockfiles without a declaration require review.

Allowed installs: npm install/ci, bun or pnpm install with optional
--frozen-lockfile, and yarn install with optional --frozen-lockfile or --immutable.
Existing build and service scripts use <manager> run <script>. Validated Next/Vite
port and loopback hostname options use npm's -- separator; Bun, pnpm, and Yarn
receive those options directly after the script name. Arbitrary install flags,
package additions, exec/download commands, and shell expressions remain excluded.

Before execution, the supervisor checks the manager on the launch PATH and probes
its version. A packageManager version must match exactly. Missing or mismatched
runtimes pause with a specific message; the supervisor neither substitutes npm nor
automatically downloads a package manager. Corepack network access is disabled for
the version probe. Package-manager range declarations are not automatically resolved.

Manifund's bun.lock selects Bun; bun install --frozen-lockfile validates, while
npm install is rejected with an explanation. This enables the documented setup
workflow; it does not establish runtime compatibility or supply missing app values.
The OpenScience container-selection issue is unchanged.
