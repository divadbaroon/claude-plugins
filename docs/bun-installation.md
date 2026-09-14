# Install Bun and continue

A missing Bun runtime (or a different installed version from the exact repository
pin) offers a host-owned installation action in the Run step. The action installs
the exact declared release, or resolves and displays the npm stable release when
no version is declared. Accepting resumes the same attempt; declining pauses it.

The installer uses Bun's official npm package in a versioned Engelbart directory,
with separate empty npm user/global configuration and without project environment
values. It verifies `bun --version` before activating the managed launch PATH.
Repository install commands still use Bun; npm is only the bootstrap installer.
No global system installation or project source edit is performed. Installation
has a three-minute timeout and observes cancellation. Logs stay in the runtime
directory. Missing npm remains a specific blocker.

Validation: package-manager, setup approval/resume, and UI tests pass. A real
Bun 1.4.2 install and version verification passed in isolated local storage on
macOS arm64. Windows/Linux and the installed cross-repository gates remain to be
run before merge. This does not establish that Manifund itself launches.
