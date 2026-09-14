# Repair workflow review

Repair diagnosis compares the failed command with repository documentation,
package-manager/lockfile evidence, runtime requirements, working directory,
scripts, and Railpack before suggesting workarounds. Conclusions must distinguish
an application failure from an unsupported runner operation or missing local tool.

An initial `unsupported` response receives one host-controlled review of those
alternatives. This shares the existing two-call evidence budget; it does not
increase the five-repair limit or permit rejected commands. Missing tools and
configuration should return `needs_input` with a concrete explanation.

`originalFailure` now advances when a new execution fails and survives validation
rejections and persisted recovery. Thus a dependency install failure after a port
repair remains the failure to fix, rather than an empty initial plan record.
Package-manager evidence is resolved from the failed component, not accidentally
from the repository root after walking ancestor documentation.

Regression coverage uses a simulated npm peer conflict with a nested Bun project,
checks the bounded terminal review and verifies latest execution failure tracking.
This tests evidence delivery and orchestration, not live model accuracy or a
successful Manifund installation. Cross-repository installed gates remain required
before merge.
