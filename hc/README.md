# human-compact

Papert Lab tools for Claude Code conversation persistence.

    pip install human-compact
    hc-backup

`hc-backup` onboards Vault: installs the Claude Code plugin and the
`claude --vault` shim, optionally imports your existing conversation
history, and lets you choose always-on or selective (per-flag) capture.
Local-first: no network calls, no telemetry, everything stays on your machine.
