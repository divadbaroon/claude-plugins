# GitHub project checkouts

The New Project modal accepts an absolute local folder or an HTTPS GitHub repository URL. Component discovery first resolves remote sources to a local checkout; analysis, setup, and running then use the existing local pipeline.

Checkouts live under `$HUMAN_COMPACT_HOME/projects/github.com/owner/repo` (default `~/.human-compact`). A registry beside the checkouts records the canonical GitHub source. Reopening the same source reuses the checkout, including uncommitted edits, without fetching, pulling, resetting, or switching branches. Origin and checkout root are checked before reuse. Unregistered directories are left untouched.

`/tree/branch` URLs select a branch or tag in a separate checkout, with a stable suffix derived from the source. Slash-containing branch names must be URL-encoded (`feature%2Fcharts`). Subdirectory URLs are rejected instead of guessing branch/path boundaries. The selected local repository remains the discovery root, so nested components retain access to sibling data and scripts.

Cloning uses installed Git and its existing credential support, with terminal prompts disabled. Credentials must not be embedded in the URL. Failures return bounded, generic messages without echoing Git authentication output. Clones have a three-minute timeout, disable hooks and recursive submodules, and are published only after success. Failed temporary clones are removed. A per-source filesystem lock prevents duplicate simultaneous clones; another request receives a retry message. A process crash may leave a lock requiring manual inspection/removal.

Validation uses real Git fixture repositories to test clone/reuse, preservation of edits and sibling data, branch isolation, and failed clone cleanup. UI tests verify remote input is replaced by the discovered local root before analysis. Release and cross-repository end-to-end gates remain separate from these local checks.
