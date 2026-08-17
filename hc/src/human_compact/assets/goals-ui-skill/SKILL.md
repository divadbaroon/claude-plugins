---
name: goals-ui
description: Open the goal workspace for this Claude Code conversation.
disable-model-invocation: true
---

When the hook runs, this text never reaches Claude: the command opens the goal
workspace for this chat and ends the turn without a reply. You are reading it
because the hook did not run — this session loaded its hook configuration
before the plugin was installed or updated, and resuming a session keeps that
configuration. So open the workspace yourself, the way the hook would:

    "$HOME/.human-compact/bin/hc" chat-ui --session ${CLAUDE_SESSION_ID} --cwd "$(pwd)"

If that path does not exist, use `hc chat-ui` with the same arguments. The
command prints one line — a `http://127.0.0.1:PORT` URL — and opens it in the
browser. Reply with that URL and one sentence: the workspace is open, and a
new Claude Code session will open it without a reply. Nothing else: no other
commands, no files, no summary of the goals.

From this point the chat's goals document is injected as context on session
start, later messages, subagents and tool batches. `/goals-ui disable` turns
that off for this chat.
