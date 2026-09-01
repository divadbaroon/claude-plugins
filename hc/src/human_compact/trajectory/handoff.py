"""The hand-off: one markdown file that carries a workspace to a teammate.

Everything the goal tree holds -- each goal's status, notes, prompt, TODO
rows with their build states and open questions, the screenshots the rows
cite -- laid out in tree order, with the repository's git and GitHub
metadata beside it, under a prompt written for the teammate's coding
agent: paste the file, and the agent writes an HTML page that says where to
pick up and opens it in the browser. Nothing here is inferred or
summarised by a model; the file IS the state, rendered.
"""
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import chat_state as CS

HANDOFF_FILE = "handoff.md"
RECENT_COMMITS = 12
STATUS_LINES = 40
PROMPTS_PER_GOAL = 8
GIT_TIMEOUT = 5
GH_TIMEOUT = 8

STATE_WORDS = {
    "": "not yet sent", "queued": "queued", "building": "building",
    "asking": "asking", "done": "done", "failed": "failed",
}
GOAL_WORDS = {
    "active": "active", "in_progress": "in progress",
    "completed": "completed", "archived": "archived",
}


# --- git / GitHub ----------------------------------------------------------

def _git(args, cwd, timeout=GIT_TIMEOUT):
    try:
        done = subprocess.run(
            ["git"] + list(args), cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.rstrip("\n")


def github_of(remote):
    """owner/repo and the https URL for a GitHub remote, in either syntax."""
    text = str(remote or "").strip()
    m = (re.match(r"^(?:https?://|ssh://)?(?:[\w.-]+@)?github\.com[:/]"
                  r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", text))
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return {"owner": owner, "repo": repo, "slug": f"{owner}/{repo}",
            "url": f"https://github.com/{owner}/{repo}"}


def _gh_pr(cwd, branch):
    """The open PR for the branch, if `gh` is installed and signed in."""
    if not branch:
        return None
    try:
        done = subprocess.run(
            ["gh", "pr", "view", branch, "--json",
             "number,title,url,state,baseRefName,isDraft"],
            cwd=cwd, capture_output=True, text=True, timeout=GH_TIMEOUT,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        pr = json.loads(done.stdout)
    except ValueError:
        return None
    if not isinstance(pr, dict) or not pr.get("number"):
        return None
    return {"number": pr.get("number"), "title": str(pr.get("title") or ""),
            "url": str(pr.get("url") or ""), "state": str(pr.get("state") or ""),
            "base": str(pr.get("baseRefName") or ""),
            "draft": bool(pr.get("isDraft"))}


def git_metadata(cwd, with_gh=True):
    """What git knows about the working tree at *cwd*; {} fields when not."""
    cwd = str(cwd or "") or os.getcwd()
    out = {"cwd": cwd, "available": False}
    if not Path(cwd).is_dir():
        return out
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    if not top:
        return out
    out["available"] = True
    out["root"] = top
    out["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or ""
    out["head"] = _git(["rev-parse", "HEAD"], cwd) or ""
    out["head_short"] = _git(["rev-parse", "--short", "HEAD"], cwd) or ""
    out["head_subject"] = _git(["log", "-1", "--format=%s"], cwd) or ""
    out["remote"] = _git(["remote", "get-url", "origin"], cwd) or ""
    out["github"] = github_of(out["remote"])
    upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                     "@{upstream}"], cwd)
    out["upstream"] = upstream or ""
    out["ahead"] = out["behind"] = None
    if upstream:
        counts = _git(["rev-list", "--left-right", "--count",
                       upstream + "...HEAD"], cwd)
        if counts:
            parts = counts.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                out["behind"], out["ahead"] = int(parts[0]), int(parts[1])
    log = _git(["log", "-n", str(RECENT_COMMITS),
                "--format=%h%x09%an%x09%ar%x09%s"], cwd) or ""
    commits = []
    for line in log.split("\n"):
        cols = line.split("\t", 3)
        if len(cols) == 4 and cols[0]:
            commits.append({"sha": cols[0], "author": cols[1],
                            "when": cols[2], "subject": cols[3]})
    out["commits"] = commits
    status = _git(["status", "--porcelain"], cwd) or ""
    lines = [ln for ln in status.split("\n") if ln.strip()]
    out["dirty"] = lines[:STATUS_LINES]
    out["dirty_count"] = len(lines)
    out["pr"] = _gh_pr(cwd, out["branch"]) if with_gh else None
    return out


def workspace_cwd(trajdir, chat_scoped):
    """The project directory this workspace stands in front of.

    A chat workspace remembers where its session ran; a global vault stands
    in front of nothing in particular and falls back to the server's cwd.
    """
    if chat_scoped:
        try:
            session_dir = Path(trajdir).expanduser().resolve()
            manifest = CS.load_manifest(session_dir.name, session_dir.parent)
            cwd = str(manifest.get("cwd") or "").strip()
            if cwd and Path(cwd).is_dir():
                return cwd
        except (OSError, ValueError):
            pass
    return os.getcwd()


# --- the markdown ------------------------------------------------------------

def _fence(text, lang="md"):
    body = str(text or "").rstrip("\n")
    if not body.strip():
        return ""
    ticks = "```"
    while ticks in body:
        ticks += "`"
    return f"{ticks}{lang}\n{body}\n{ticks}"


def _tree(goals):
    """Roots first, children under parents, each in stored order."""
    by_parent = {}
    ids = {g.get("id") for g in goals}
    for g in goals:
        pid = g.get("parent_goal_id")
        by_parent.setdefault(pid if pid in ids else None, []).append(g)
    out = []

    def walk(pid, depth, trail):
        for g in by_parent.get(pid, []):
            path = trail + [str(g.get("title") or "Untitled")]
            out.append((g, depth, path))
            walk(g.get("id"), depth + 1, path)

    walk(None, 0, [])
    return out


def _todo_lines(items):
    lines = []
    for row in items or []:
        text = str(row.get("text") or "")
        if not text.strip():
            continue
        depth = max(0, int(row.get("depth") or 0))
        state = STATE_WORDS.get(str(row.get("status") or ""), "not yet sent")
        lines.append("    " * depth + f"- [{state}] {text}")
        question = str(row.get("question") or "").strip()
        if question and row.get("status") == "asking":
            lines.append("    " * (depth + 1)
                         + "> Claude asked: " + " ".join(question.split()))
    return lines


def _attachment_lines(items):
    lines = []
    for row in items or []:
        for att in row.get("attachments") or []:
            try:
                n = int(att.get("n"))
            except (TypeError, ValueError):
                continue
            path = str(att.get("path") or "")
            if n > 0 and path:
                lines.append(f"- [attachment #{n}]: {path}")
    return lines


def _counts(goals):
    gc = {k: 0 for k in GOAL_WORDS}
    tc = {k: 0 for k in STATE_WORDS}
    for g in goals:
        gc[g.get("status") if g.get("status") in gc else "active"] += 1
        for row in g.get("todo_items") or []:
            if not str(row.get("text") or "").strip():
                continue
            st = str(row.get("status") or "")
            tc[st if st in tc else ""] += 1
    return gc, tc


def _pick_up_rows(tree):
    """Rows the next person acts on first: questions to answer, work that
    failed, and unsent rows under goals that are in progress."""
    out = []
    for g, _depth, path in tree:
        if g.get("status") in ("completed", "archived"):
            continue
        for row in g.get("todo_items") or []:
            text = " ".join(str(row.get("text") or "").split())
            if not text:
                continue
            st = str(row.get("status") or "")
            hot = st in ("asking", "failed", "building", "queued") or (
                st == "" and g.get("status") == "in_progress")
            if not hot:
                continue
            line = f"- [{STATE_WORDS.get(st, 'not yet sent')}] {text}"
            if st == "asking" and row.get("question"):
                line += " — asked: " + " ".join(str(row["question"]).split())
            line += f"  _(under: {' › '.join(path)})_"
            out.append(line)
    return out


def _goal_section(g, depth, path, prompts_by_id):
    level = min(6, 3 + depth)
    title = str(g.get("title") or "Untitled")
    status = GOAL_WORDS.get(g.get("status"), str(g.get("status") or "active"))
    head = f"{'#' * level} {title}  `[{status}]`"
    lines = [head]
    meta = [f"id `{g.get('id')}`"]
    if g.get("priority") and g.get("priority") != "normal":
        meta.append(f"priority {g['priority']}")
    if g.get("updated_at"):
        meta.append(f"updated {g['updated_at']}")
    if len(path) > 1:
        meta.append("under " + " › ".join(path[:-1]))
    lines.append("_" + " · ".join(meta) + "_")
    desc = str(g.get("description") or "").strip()
    if desc:
        lines += ["", "**Why it matters**", "", desc]
    notes = _fence(g.get("notes"))
    if notes:
        lines += ["", "**Notes** (the author's own document, verbatim)", "",
                  notes]
    todos = _todo_lines(g.get("todo_items"))
    if todos:
        lines += ["", "**TODOs** (each with its build state)", ""] + todos
        shots = _attachment_lines(g.get("todo_items"))
        if shots:
            lines += ["", "Attachments the rows cite (paths on the author's "
                      "machine):", ""] + shots
    prompt = _fence(g.get("prompt_md"))
    if prompt:
        lines += ["", "**Saved prompt**", "", prompt]
    sources = [s for s in (g.get("sources") or []) if isinstance(s, dict)]
    if sources:
        lines += ["", "**Sources attached**", ""] + [
            f"- {s.get('label', '')} ({s.get('type', '')})" for s in sources]
    said = []
    for pid in g.get("prompt_ids") or []:
        p = prompts_by_id.get(pid)
        if p and str(p.get("text") or "").strip():
            said.append("- \"" + " ".join(str(p["text"]).split()) + "\"")
        if len(said) >= PROMPTS_PER_GOAL:
            break
    if said:
        lines += ["", "**What the author asked for, in their words**", ""] + said
    return "\n".join(lines)


def _git_section(git):
    git = git or {}
    if not git.get("available"):
        where = git.get("cwd") or ""
        return "\n".join([
            "## Repository",
            "",
            "No git repository was found" + (f" at `{where}`" if where else "")
            + ". The goals above are the whole hand-off.",
        ])
    gh = git.get("github") or {}
    lines = ["## Repository", ""]
    if gh:
        lines.append(f"- GitHub: [{gh['slug']}]({gh['url']})")
    if git.get("remote"):
        lines.append(f"- Remote `origin`: `{git['remote']}`")
    lines.append(f"- Local root: `{git.get('root', '')}`")
    branch = git.get("branch") or ""
    if branch:
        link = (f" — [{gh['url']}/tree/{branch}]({gh['url']}/tree/{branch})"
                if gh else "")
        lines.append(f"- Branch: `{branch}`{link}")
    if git.get("head_short"):
        commit = git["head_short"]
        link = f" ([{gh['url']}/commit/{git.get('head', '')}]({gh['url']}/commit/{git.get('head', '')}))" if gh else ""
        lines.append(f"- HEAD: `{commit}` {git.get('head_subject', '')}{link}")
    if git.get("upstream"):
        ahead, behind = git.get("ahead"), git.get("behind")
        sync = (f"{ahead} ahead / {behind} behind"
                if ahead is not None else "sync unknown")
        lines.append(f"- Upstream: `{git['upstream']}` — {sync}")
        if gh and branch and "/" in git["upstream"]:
            up_branch = git["upstream"].split("/", 1)[1]
            if up_branch != branch:
                lines.append(f"- Compare: {gh['url']}/compare/{up_branch}...{branch}")
    pr = git.get("pr")
    if pr:
        flag = " (draft)" if pr.get("draft") else ""
        lines.append(f"- Pull request: #{pr['number']} \"{pr['title']}\" "
                     f"{pr.get('state', '').lower()}{flag} → `{pr.get('base', '')}` — {pr.get('url', '')}")
    elif gh and branch:
        lines.append(f"- Pull request: none found for `{branch}` "
                     f"(open one: {gh['url']}/pull/new/{branch})")
    n = git.get("dirty_count") or 0
    if n:
        lines += ["", f"Uncommitted changes ({n} path{'s' if n != 1 else ''}"
                  + (f", first {STATUS_LINES} shown" if n > STATUS_LINES else "")
                  + "):", "", "```", *git.get("dirty", []), "```"]
    else:
        lines += ["", "Working tree clean."]
    commits = git.get("commits") or []
    if commits:
        lines += ["", f"Recent commits (newest first):", ""] + [
            f"- `{c['sha']}` {c['subject']} — {c['author']}, {c['when']}"
            for c in commits]
    return "\n".join(lines)


def agent_prompt(stamp):
    """What the teammate pastes first: instructions for their agent."""
    return "\n".join([
        "## Prompt for your coding agent",
        "",
        "Paste this whole file into your coding agent (Claude Code, Cursor, "
        "Codex…) as one message. Agent: follow these steps, then stop.",
        "",
        "> You are receiving a hand-off from a teammate. Below is the "
        "complete state of their goal workspace — the goal tree with each "
        "goal's status, notes, saved prompt, and TODO rows with their build "
        "states and open questions — followed by the repository's git and "
        "GitHub metadata.",
        ">",
        f"> 1. Write ONE self-contained HTML file named `hc-handoff-{stamp}.html` "
        "in the current working directory (inline CSS, no network "
        "requests). It must read as a briefing on what to do next, in this "
        "order: (a) **Start here** — the repository, branch, open PR and "
        "uncommitted changes, and the goals that are in progress; (b) "
        "**Pick up where they left off** — the TODO rows that are asking a "
        "question (show the question), failed, building or queued, and the "
        "unsent rows under in-progress goals; (c) the **goal tree** with a "
        "status badge per goal, its notes rendered from markdown, and its "
        "TODO rows with a state badge each; (d) **Repository** metadata "
        "with the GitHub links clickable. Keep every goal, row and note — "
        "do not summarise them away.",
        "> 2. Open that file in the default browser: `open <file>` on macOS, "
        "`xdg-open <file>` on Linux, `start <file>` on Windows.",
        "> 3. Do not edit, commit or push anything in the repository, and do "
        "not start on any TODO. Ask the reader which goal to take up first.",
        "",
        "---",
    ])


def render(goals, prompts=None, git=None, scope="chat", session_id=None,
           generated_at=None, now=None):
    """The hand-off document for *goals* (flat list, as stored)."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M")
    goals = [g for g in (goals or []) if isinstance(g, dict)]
    tree = _tree(goals)
    prompts_by_id = {p.get("id"): p for p in (prompts or [])
                     if isinstance(p, dict) and isinstance(p.get("id"), str)}
    gh = (git or {}).get("github") or {}
    where = gh.get("slug") or Path((git or {}).get("root") or
                                   (git or {}).get("cwd") or "").name
    title = f"# Hand-off: {where}" if where else "# Hand-off"
    head = [
        f"<!-- hc hand-off · generated {now.isoformat(timespec='seconds')}"
        + (f" · chat {session_id}" if session_id else " · global vault")
        + " -->",
        title,
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%M %Z')} from the "
        + ("chat workspace" if scope == "chat" else "global vault")
        + (f" of session `{session_id}`" if session_id else "")
        + ". Everything below is the author's own state, not a model's "
        "summary of it.",
        "",
        agent_prompt(stamp),
        "",
    ]
    gc, tc = _counts(goals)
    glance = ["## Where I left off, at a glance", ""]
    if git and git.get("available"):
        bits = []
        if gh:
            bits.append(f"[{gh['slug']}]({gh['url']})")
        if git.get("branch"):
            bits.append(f"branch `{git['branch']}`")
        if git.get("head_short"):
            bits.append(f"HEAD `{git['head_short']}`")
        if git.get("ahead") is not None:
            bits.append(f"{git['ahead']} ahead / {git['behind']} behind "
                        f"`{git.get('upstream', '')}`")
        n = git.get("dirty_count") or 0
        bits.append(f"{n} uncommitted path{'s' if n != 1 else ''}"
                    if n else "working tree clean")
        glance.append("- Repository: " + " · ".join(bits))
        pr = git.get("pr")
        if pr:
            glance.append(f"- Open PR: #{pr['number']} \"{pr['title']}\" "
                          f"({pr.get('state', '').lower()}) — {pr.get('url', '')}")
    else:
        glance.append("- Repository: none found where this workspace runs")
    glance.append(
        f"- Goals: {len(goals)} total — "
        + ", ".join(f"{gc[k]} {GOAL_WORDS[k]}" for k in GOAL_WORDS if gc[k]))
    total_todos = sum(tc.values())
    glance.append(
        f"- TODO rows: {total_todos} — "
        + (", ".join(f"{tc[k]} {STATE_WORDS[k]}" for k in STATE_WORDS if tc[k])
           if total_todos else "none yet"))
    hot = _pick_up_rows(tree)
    glance += ["", "### Pick up here", ""]
    glance += hot if hot else [
        "- Nothing is mid-flight: no row is asking, failed, queued or "
        "building, and in-progress goals have no unsent rows."]
    body = ["## Goals", "", "Roots first; a goal's subgoals follow it, one "
            "heading level deeper. `[state]` on a TODO row is its build "
            "state: not yet sent, queued, building, asking, done, failed.",
            ""]
    if not tree:
        body.append("_No goals yet._")
    for g, depth, path in tree:
        body.append(_goal_section(g, depth, path, prompts_by_id))
        body.append("")
    foot = [_git_section(git), "",
            "---", "",
            "_Made by the hc goal workspace's hand-off button. Regenerate "
            "from the header ⇪ after anything changes; the file is also "
            f"saved as `{HANDOFF_FILE}` beside the workspace's goals.json._"]
    return "\n".join(head + glance + [""] + body + foot).rstrip("\n") + "\n"


def filename(git=None, now=None):
    now = now or datetime.now(timezone.utc)
    gh = (git or {}).get("github") or {}
    base = gh.get("repo") or Path((git or {}).get("root") or "").name or "workspace"
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-") or "workspace"
    return f"hc-handoff-{base}-{now.strftime('%Y%m%d-%H%M')}.md"


def build(trajdir, goals, prompts, chat_scoped, session_id=None,
          generated_at=None, with_gh=True):
    """Render the hand-off, write it beside the goals, and hand it back."""
    cwd = workspace_cwd(trajdir, chat_scoped)
    git = git_metadata(cwd, with_gh=with_gh)
    now = datetime.now(timezone.utc).astimezone()
    text = render(goals, prompts=prompts, git=git,
                  scope="chat" if chat_scoped else "global",
                  session_id=session_id, generated_at=generated_at, now=now)
    path = Path(trajdir) / HANDOFF_FILE
    saved = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        saved = str(path)
    except OSError:
        saved = ""
    return {"ok": True, "markdown": text, "path": saved,
            "filename": filename(git, now), "bytes": len(text.encode("utf-8")),
            "git": {"available": bool(git.get("available")),
                    "branch": git.get("branch") or "",
                    "github": git.get("github"),
                    "cwd": cwd}}
