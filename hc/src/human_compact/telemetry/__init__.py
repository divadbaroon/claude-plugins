"""Bart telemetry for the plugin: the goal page's own record of what it
did, in the data contract the site's debugger reads.

    from human_compact import telemetry as TELEMETRY

    with TELEMETRY.operation("goals.save", "storage", writes=["goals"]) as op:
        ...

See ``core`` for the API, ``contract`` for the vocabulary, ``sinks`` for
where records go.
"""
from .contract import (CONTRACT_VERSION, EVENT_TYPES, LEVELS, LINEAGE_ATTRIBUTES,
                       MODES, RUN_ID, RUN_ID_PREFIX, SNAPSHOT_KINDS, STATUS, TYPES,
                       bundle, derive_run, level_of, tree)
from .core import (activate, DEFAULT, Telemetry, context, current, current_purpose,
                   instance, lifecycle_event, operation, purpose, read_settings,
                   run, run_defaults, start_operation, under_test_runner,
                   untraced, user_hash, with_run)
from .redaction import REDACTED, host_of, redact, safe_url, sanitize_error, sha256
from .sinks import (ConsoleSink, FileSink, ForwardSink, MemorySink, Rejected,
                    Unauthorized, Unavailable)
from .snapshots import MAX_SNAPSHOT_BYTES, now_iso

__all__ = [
    "activate", "CONTRACT_VERSION", "EVENT_TYPES", "LEVELS", "LINEAGE_ATTRIBUTES", "MODES",
    "RUN_ID", "RUN_ID_PREFIX", "SNAPSHOT_KINDS", "STATUS", "TYPES", "bundle",
    "derive_run", "level_of", "tree", "DEFAULT", "Telemetry", "context", "current",
    "current_purpose", "instance", "lifecycle_event", "operation", "purpose",
    "read_settings", "run", "run_defaults", "start_operation", "under_test_runner",
    "untraced", "user_hash", "with_run", "REDACTED", "host_of", "redact", "safe_url",
    "sanitize_error", "sha256", "ConsoleSink", "FileSink", "ForwardSink", "MemorySink",
    "Rejected", "Unauthorized", "Unavailable", "MAX_SNAPSHOT_BYTES", "now_iso",
]
