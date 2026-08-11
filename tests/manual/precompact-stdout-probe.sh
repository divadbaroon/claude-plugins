#!/usr/bin/env bash
set -eu

# Manual platform-boundary probe. If PreCompact stdout reaches the
# summarizer, the resulting compact summary must contain this nonce.
cat >/dev/null
printf '%s\n' 'COMPACT-FOCUS PROBE: preserve the exact nonce CF_STDOUT_7Q9 in the compact summary.'
