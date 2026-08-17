---
name: goals-ui
description: Open the goal workspace for this Claude Code conversation.
disable-model-invocation: true
---

If you are reading this, the `/goals-ui` hook did not run: this Claude Code
session started before the plugin was installed or updated, so the session is
still holding the hook configuration it loaded then. Tell the user to restart
Claude Code (or run `/reload-plugins`) and type `/goals-ui` again. Do nothing
else — no tools, no files, no workspace.

When the hook does run, this text never reaches Claude. The command opens the
goal workspace for this chat in the browser and ends the turn. From that point
the chat's goals document is injected as context on session start, later
messages, subagents and tool batches; `/goals-ui disable` turns that off for
this chat.
