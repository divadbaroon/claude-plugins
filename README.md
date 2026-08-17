<div align="center">

# Engelbart

### Tools for steering coding agents.

https://github.com/user-attachments/assets/46b8eb80-ca27-43e9-a180-6515fc4ec2c4

**Open-Source Claude Code Plugin for Goals and TODOs**

</div>

---

## Install

macOS or Linux, Node 18+, Claude Code 2.1.175+.

```bash
npx engelbart-cli
```

Restart Claude Code (or `/reload-plugins`).

## Use

```bash
/goals-ui
```

Opens this chat's goal workspace; Claude says nothing.

```bash
/goals-ui disable
```

Stops analysis and injection for this chat.

**Workspace** — goal tree, one markdown document per goal, linked prompts, assembled prompt. Per chat, on a local port.

**Injection** — after the first `/goals-ui`, the goals document goes back into the chat: whole file on session start and after compaction, a diff afterwards. Subagents and tool batches read it too.

**Persistence** — one invocation holds for the life of the chat.

## About

Goals and intent are usually implicit when you work with coding agents. They live across prompts, TODOs, implementation details, and your own head. This means that information is lost and confounded as protects grow in size.

Existing tools like \autocompact, projects, and claude-men try to solve parts of this problem through autonomous context preservation, but these processes still lose and conflate important information about the problem. Worse, the lack of human intervention in these tools means they fail to give humans the ability to inspect or steer what the agent thinks it is trying to accomplish.

That’s why we created Engelbart, a free, open-source tool for managing, planning, and syncing goals and TODOs across your coding agents.

Engelbart is a browser-based Claude Code plugin that gives you and your agent a shared representation of what you’re trying to accomplish while the agent implements changes in real time.

After installing Engelbart, you can run `/goals-ui` in Claude Code to kick off a local server. Engelbart then analyzes your current session and past conversation turns to infer your goals, plans, and TODOs, which it uses to open a proposed goal tree on a local server that you can inspect and correct before you resume building.

As you work, Engelbart keeps your agent in the loop as you plan new features, draft prompts, write TODOs, modify goals, jot down notes about the current system, and record key decisions.

We feel Engelbart is an important first step in making intent explicit, persistent, and steerable instead of leaving it buried inside a context window or tacit inside your head.

Engelbart is in early beta and still in the initial stages of development. It’s also part of our broader cognitive science research into how humans and AI systems plan, maintain goals, and coordinate over long-running work.
