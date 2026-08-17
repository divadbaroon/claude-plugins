---
name: goals-ui
description: Open the goal workspace for this Claude Code conversation.
disable-model-invocation: true
---

The goals-ui expansion hook has opened the local goal workspace for Claude
session `${CLAUDE_SESSION_ID}` and supplied its exact URL as hook context.

Report that URL and say the workspace belongs to this chat, then stop. Do not
run a second server or modify the goal state yourself.
