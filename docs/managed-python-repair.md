# Managed Python repair and approval

The Run Order and Setup Repair agents may add `pythonRuntimes` to a complete
launch plan: `[{"cwd":"system/backend","version":"3.11"}]`. Runtime paths are
chosen by hc, not supplied by the model. hc discovers installed interpreters
using uv with downloads disabled, or versioned Python executables. Versions
3.10–3.14 (including explicit patch versions) are supported.

Direct Python launch plans with a venv creation or requirements installation command
use managed storage on their first run. Before creating environments or installing,
hc reads component/ancestor declarations and bounded dependency manifests. It
honors `.python-version` and standard `[project].requires-python` constraints.

For direct exact pins, a read-only assessment fetches public PyPI release metadata
(up to 32 releases, four concurrent requests, five seconds per request, 1 MB per
response). It checks release Requires-Python and matches wheel tags against the
host platform and candidate CPython versions. It ranks candidates by wheel gaps,
matching wheels, installed availability, the initial preference, then newest
version. An inferred default may change; an agent's explicit runtime request is
checked against declarations. Missing runtimes still require download approval.
No package code is executed during assessment. Repository files, global Python
settings, and existing environments are unchanged.

Missing wheels do not establish incompatibility. Source-only packages, unpinned
requirements, unsupported dependency syntax, dynamic metadata, and network errors
remain explicitly unverified. This is not a full transitive resolver or proof of
application compatibility. Lockfiles are fingerprinted for repair comparison;
this milestone does not interpret every lockfile format or Poetry constraints.
Opaque npm wrappers and Node runtime selection retain their existing behavior.
Compatibility results and their limits are visible in the Run dialog.

Environments are fresh directories beneath
`HUMAN_COMPACT_HOME/project-environments/<project-component-hash>/`.
Known Python/venv commands are bound to that environment. Existing repository
virtual environments and system defaults are preserved. Failed managed
environments remain available for diagnosis; automatic cleanup is not yet added.

A missing interpreter creates a persisted `awaiting_approval` run with the exact
relative-path proposal, requested versions, changes, and a unique approval ID.
`project_run_approval` accepts `{id, approvalId, approve}` through the existing
local operation boundary. Approval is claimed under the run lock. Duplicate
clicks do not launch twice; decline/reset revokes execution. Pending approvals
survive server/browser reload; active processes are never reclaimed by saved PID.
Changed download requirements require a new approval. Approval resumes the
same repair attempt without another model call.

Approved downloads use the installed uv binary:
`uv python install <version> --no-bin --no-registry`
with isolated Python/cache directories and no inherited uv configuration or
application credentials. No global default or PATH setting is changed. Missing
uv is reported as an unsupported prerequisite; approval does not install uv.
Creation, dependency installs, and services use the existing process supervisor,
bounded logs/timeouts, cancellation, and HTTP health checks. Project environment
values reach application commands, not runtime provisioning commands.

Each failed stage retains its normalized command, component, runtime, manifest
fingerprint, Python entrypoint fingerprint, and compatibility assessment. Repair
validation ignores step labels and explanations, rejects unchanged failed inputs
and removal of the failed component, and ignores health URL changes when a process
actually exited with an error. Changes to an unrelated sibling component do not
qualify as repairs. For recognized native Python dependency build failures, a
runtime-only repair must demonstrate improved wheel coverage; an arbitrary version
switch is insufficient. Changed dependency files can be reassessed.

Retry retains the saved failure and its five-attempt budget; this change does not
reset an exhausted run. Legacy records without configuration fingerprints cannot
prove a semantic match; new executions capture them. Repairs still have a
five-attempt budget. Arbitrary source edits, destructive operations, and
unrestricted shell proposals are not newly supported by this milestone.
