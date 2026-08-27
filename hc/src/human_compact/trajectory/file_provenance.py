"""What the work touched: files, the runs that changed them, and the few
excerpts worth reading.

``project_sync`` answers "what was intended" -- goals, TODO rows, the prompts
behind them. This module answers the other half, the one that until now never
left the machine: which files that intent actually landed on, what state they
are in, and -- for the handful that matter -- what they say.

Three kinds of fact, and they are deliberately not the same kind:

* METADATA is free and safe. Path, size, mtime, extension, the SHA-256 of the
  bytes, and what git thinks (tracked, dirty, its blob sha, the commit that
  last touched it). None of it is content; all of it is what a reader needs to
  ask "is what I am looking at still current?".
* CHANGE HISTORY is observed, not reconstructed. Every file-touching tool call
  is already in the session's event stream, and a run bound to a goal already
  records the commit its session began and ended on. A run row is those two
  facts joined: the range, and the files inside it, with git's own ``--numstat``
  for how much moved.
* CONTENT IS THE EXCEPTION. Everything else here describes files; excerpts
  *are* files, in small pieces, and that is a different promise to the person
  whose disk they came off. So the rule is narrow and written down rather than
  implied:

    - only files this project's own runs actually changed;
    - only the hunks git says changed, plus three lines either side -- never
      the whole file, unless the whole file is shorter than a hunk would be;
    - never a file git ignores, never a binary, never anything whose name
      looks like a credential (see ``SECRET_RE``), whatever its status;
    - capped, per file and per sync, so that no sequence of edits turns this
      into a copy of the repository.

  And it is switchable: ``content=False`` builds the same payload with the
  excerpt list empty, so a project can carry the whole provenance graph and
  none of its source.

The payload::

    {"schema_version", "generated_at", "project_id", "user_id",
     "files": [...], "runs": [...], "run_files": [...], "excerpts": [...]}

Unlike ``project_sync`` this is NOT a complete snapshot of the world. Files
and runs accumulate -- a file that left the disk is marked ``missing`` rather
than dropped, because the run that edited it still happened. Only run_files
(within the runs carried) and excerpts (within the files carried) are complete
enough to prune against, and ``hc_sync_files`` prunes exactly that far.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import agent_exec as AE
from . import chat_state as CS
from . import project_sync as SY

SCHEMA_VERSION = 1

# The tools whose calls leave something behind. Read is deliberately absent:
# a file that was looked at is not a file that was changed, and conflating
# them makes "what did this run do" unreadable.
WRITE_TOOLS = ("edit", "write", "multiedit", "notebookedit")

# Caps. Each one is a policy, not a performance knob:
MAX_FILES = 400            # metadata rows per sync
MAX_EXCERPT_FILES = 40     # files allowed to contribute content at all
MAX_EXCERPTS_PER_FILE = 4  # hunks kept from any one file
MAX_EXCERPT_LINES = 160    # lines in one excerpt
MAX_EXCERPT_CHARS = 8000   # and its hard byte-ish ceiling (DB allows 20000)
MAX_HASH_BYTES = 8 * 1024 * 1024   # beyond this a file is hashed by nothing
MAX_EXCERPT_FILE_BYTES = 512 * 1024
MAX_LOG_LOOKUPS = 60       # `git log -1 -- path` calls per sync
GIT_TIMEOUT_S = 10
CONTEXT_LINES = 3

# Names that are credentials whatever their extension, and extensions that
# are credentials whatever their name. Checked against the file's own name
# and every directory above it inside the project -- a key in `secrets/` is
# a key. This list is the last line, not the first: ignored files are
# already excluded, and most secrets are ignored.
SECRET_RE = re.compile(
    r"(^\.env($|\.)|(^|[._-])secret|(^|[._-])credential|(^|[._-])password"
    r"|^id_(rsa|dsa|ecdsa|ed25519)$|\.pem$|\.key$|\.p12$|\.pfx$|\.keystore$"
    r"|^\.netrc$|^\.npmrc$|^\.pypirc$|^\.htpasswd$|(^|[._-])token(s)?$)",
    re.IGNORECASE)

# Directories whose contents are never anybody's project, even when a repo
# tracks them by accident.
SKIP_DIRS = frozenset((".git", "node_modules", "__pycache__", ".venv",
                       "venv", ".mypy_cache", ".pytest_cache", "dist",
                       "build", ".next", ".claude-vault"))

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _spans_of(diff: str) -> List[Tuple[int, int]]:
    """The new-side line ranges of a unified diff's hunks."""
    spans: List[Tuple[int, int]] = []
    for line in diff.splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        length = int(match.group(2) or 1)
        if length <= 0:
            continue
        spans.append((max(1, start), start + length - 1))
    return spans


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uuid5(namespace: uuid.UUID, *parts: str) -> str:
    return str(uuid.uuid5(namespace, "\x1f".join(parts)))


# --- git, asked politely -------------------------------------------------

def _git(cwd: str, *args: str) -> Optional[str]:
    """One git command, or None if git could not answer.

    Every failure mode -- not a repository, git not installed, a command
    that takes too long on a huge tree -- lands in the same place: None,
    meaning "we do not know", which the rows then say honestly rather than
    reporting a zero.
    """
    try:
        done = subprocess.run(("git", "-C", cwd, *args),
                              capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


class GitView:
    """What git knows about one working tree, read once per sync.

    Asking per file would be dozens of processes; asking once and indexing
    is the same answers at a hundredth of the cost. A tree that is not a
    repository produces an empty view whose every answer is "unknown", which
    is the normal case for a good number of these projects.
    """

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.is_repo = False
        self.branch = ""
        self.head = ""
        self.status: Dict[str, str] = {}
        self.blobs: Dict[str, str] = {}
        self.ignored: Set[str] = set()
        self._log_budget = MAX_LOG_LOOKUPS
        head = _git(cwd, "rev-parse", "HEAD")
        if head is None:
            # Not a repository, or an empty one. `rev-parse --git-dir`
            # separates those two, and an empty repo is still a repo.
            if _git(cwd, "rev-parse", "--git-dir") is None:
                return
        self.is_repo = True
        self.head = (head or "").strip()
        self.branch = (_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
                       or "").strip()
        self._read_status()
        self._read_blobs()

    def _read_status(self) -> None:
        out = _git(self.cwd, "status", "--porcelain=v1",
                   "--ignored=matching", "-uall")
        if out is None:
            return
        for line in out.splitlines():
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:].strip()
            if " -> " in path:            # a rename: the new name is the file
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            if code == "!!":
                self.ignored.add(path)
                self.status[path] = "ignored"
            elif code == "??":
                self.status[path] = "untracked"
            elif code[0] in "MARC" and code[1] == " ":
                self.status[path] = "staged"
            elif "D" in code:
                self.status[path] = "deleted"
            else:
                self.status[path] = "modified"

    def _read_blobs(self) -> None:
        out = _git(self.cwd, "ls-files", "-s")
        if out is None:
            return
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            fields = parts[0].split()
            if len(fields) >= 2:
                self.blobs[parts[1].strip('"')] = fields[1]

    def state_of(self, rel: str) -> Tuple[bool, str, str]:
        """``(tracked, status, blob sha)`` for one project-relative path."""
        if not self.is_repo:
            return False, "", ""
        blob = self.blobs.get(rel, "")
        status = self.status.get(rel, "clean" if blob else "untracked")
        return bool(blob), status, blob

    def last_commit(self, rel: str) -> Tuple[str, Optional[str]]:
        """The commit that last touched a file, within a per-sync budget.

        The budget exists because this is the one question git will not
        answer in bulk, and a project with a thousand touched files should
        not spend a thousand processes to decorate rows nobody reads.
        """
        if not self.is_repo or self._log_budget <= 0:
            return "", None
        self._log_budget -= 1
        out = _git(self.cwd, "log", "-1", "--format=%H%x1f%cI", "--", rel)
        if not out or "\x1f" not in out:
            return "", None
        sha, _, when = out.strip().partition("\x1f")
        return sha[:40], SY._ts(when.strip())

    def numstat(self, rel: str, before: str = "", after: str = "") -> Tuple[
            Optional[int], Optional[int]]:
        """Lines added and removed, from git rather than from guesswork.

        With a commit range it is that range; without one it is the working
        tree against HEAD, which is what a run that never committed left
        behind. None means git declined to say -- never zero, which is a
        different and much stronger claim.
        """
        if not self.is_repo:
            return None, None
        args = ["diff", "--numstat"]
        if before and after and before != after:
            args.append(f"{before}..{after}")
        else:
            args.append("HEAD")
        out = _git(self.cwd, *args, "--", rel)
        if out is None:
            return None, None
        if not out.strip():
            # git answered, and its answer was "nothing". That is a fact,
            # and reporting it as unknown would lose it.
            return 0, 0
        for line in out.splitlines():
            fields = line.split("\t")
            if len(fields) >= 3:
                try:
                    return int(fields[0]), int(fields[1])
                except ValueError:      # "-" for a binary file
                    return None, None
        return 0, 0

    def commit_hunks(self, rel: str, sha: str) -> List[Tuple[int, int]]:
        """The lines one commit changed in one file.

        The fallback for work that has already been committed: the run's
        edits are no longer in the working tree, so the diff that shows what
        it did is the commit's own. Better than the file's opening lines,
        which is what "relevant" degrades to when git can say nothing.
        """
        if not self.is_repo or not sha:
            return []
        out = _git(self.cwd, "diff", f"--unified={CONTEXT_LINES}",
                   f"{sha}^", sha, "--", rel)
        if out is None:
            # A root commit has no parent; ask for it against the empty tree
            # rather than giving up on the repository's first file.
            out = _git(self.cwd, "show", f"--unified={CONTEXT_LINES}",
                       "--format=", sha, "--", rel)
        return _spans_of(out or "")

    def hunks(self, rel: str, before: str = "",
              after: str = "") -> List[Tuple[int, int]]:
        """The line ranges git says changed, on the *new* side of the diff.

        Ranges, not the diff text: the excerpt is then read out of the file
        as it stands, so what is stored is real current source rather than a
        patch of it, and its hash can be checked against the file's.
        """
        if not self.is_repo:
            return []
        args = ["diff", f"--unified={CONTEXT_LINES}"]
        if before and after and before != after:
            args.append(f"{before}..{after}")
        else:
            args.append("HEAD")
        return _spans_of(_git(self.cwd, *args, "--", rel) or "")


# --- files on disk -------------------------------------------------------

def _sha256(path: Path, size: int) -> str:
    if size > MAX_HASH_BYTES:
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(131072), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(8192)
    except OSError:
        return False


def _relative(cwd: Path, path: str) -> str:
    """A file as the reader would name it: relative when it is inside the
    project, absolute when it is not -- and it sometimes is not, because a
    session in one directory may edit a file in another."""
    try:
        candidate = Path(path).expanduser()
    except (OSError, RuntimeError):
        return path
    try:
        return str(candidate.resolve().relative_to(cwd))
    except (ValueError, OSError, RuntimeError):
        return str(candidate)


def _skipped_dir(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in Path(rel).parts[:-1])


def _secretish(rel: str) -> bool:
    return any(SECRET_RE.search(part) for part in Path(rel).parts)


# --- what each session touched -------------------------------------------

def _payload_of(event: Dict[str, Any]) -> Dict[str, Any]:
    """The tool call's arguments, which the event stores as JSON text."""
    text = event.get("text")
    if not isinstance(text, str) or not text.strip().startswith("{"):
        return {}
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _touched_paths(session_id: str, root: Optional[Path]
                   ) -> Dict[str, Dict[str, Any]]:
    """Every file one session wrote, with how often and when.

    Read from the session's own event stream rather than from git: git knows
    what changed in the tree, not who changed it, and a session that edited
    a file and reverted it still edited it.
    """
    try:
        paths = CS.paths(session_id, root)
    except ValueError:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        handle = paths.events.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return out
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if event.get("kind") != "tool_use":
                continue
            tool = str(event.get("tool_name") or "").lower()
            if tool not in WRITE_TOOLS:
                continue
            body = _payload_of(event)
            path = (body.get("file_path") or body.get("notebook_path")
                    or body.get("path"))
            if not isinstance(path, str) or not path.strip():
                continue
            when = SY._ts(event.get("timestamp"))
            entry = out.get(path)
            if entry is None:
                out[path] = {"edits": 1, "tool": tool,
                             "first_at": when, "last_at": when}
            else:
                entry["edits"] += 1
                entry["last_at"] = when or entry["last_at"]
    return out


def _sessions_of(root: Optional[Path], cwd: str) -> List[Dict[str, Any]]:
    """The chat sessions started in this project's directory.

    A session IS a run here. The ``agent-runs`` record adds a goal binding
    and a commit range when one exists, but a session that was never
    launched against a goal still changed files, and leaving it out would
    make the file history a record of ceremony rather than of work.
    """
    base = CS._state_base(root)
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            manifest = json.loads(
                (entry / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, dict):
            continue
        if str(manifest.get("cwd") or "") != cwd:
            continue
        out.append(manifest)
    return out


def _runs_by_session(root: Optional[Path], cwd: str) -> Dict[str, Dict[str, Any]]:
    """The ``agent-runs`` records for this project, keyed by session.

    These are the runs that were launched against a goal, and they carry
    what a session's own store cannot: which goal, and the commit the tree
    stood at before and after.
    """
    out: Dict[str, Dict[str, Any]] = {}
    base = CS._state_base(root)
    directories = [base]
    try:
        directories.extend(p for p in base.iterdir() if p.is_dir())
    except OSError:
        pass
    for directory in directories:
        try:
            runs = AE.load_runs(directory)
        except (OSError, ValueError):
            continue
        for run in runs:
            if str(run.get("cwd") or "") != cwd:
                continue
            session = str(run.get("claude_session_id") or "")
            if session:
                out[session] = run
    return out


# --- excerpts ------------------------------------------------------------

def _read_lines(path: Path) -> Optional[List[str]]:
    try:
        if path.stat().st_size > MAX_EXCERPT_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _excerpt_text(lines: List[str], start: int, end: int) -> str:
    body = "\n".join(lines[start - 1:end])
    return body[:MAX_EXCERPT_CHARS]


def _spans_for(view: GitView, rel: str, lines: List[str], before: str,
               after: str, commit: str) -> List[Tuple[int, int, str]]:
    """Which passages of one file are worth keeping, and why.

    Three answers in descending order of how well they are evidenced, and
    the reason string says which one this is -- an excerpt whose provenance
    is a guess should not read like one whose provenance is a diff:

    1. the hunks of the run's own commit range, or of the working tree when
       the run never committed -- what this work actually changed;
    2. the hunks of the commit that last touched the file, for work that has
       since been committed and is no longer visible in a working diff;
    3. the file's opening, when git can say nothing at all -- an untracked
       file, or a project that is not a repository.
    """
    def bounded(spans, why):
        out = []
        for start, end in spans[:MAX_EXCERPTS_PER_FILE]:
            low = max(1, start - CONTEXT_LINES)
            high = min(len(lines), end + CONTEXT_LINES)
            if high < low:
                continue
            if high - low + 1 > MAX_EXCERPT_LINES:
                high = low + MAX_EXCERPT_LINES - 1
            out.append((low, high, why))
        return out

    spans = bounded(view.hunks(rel, before, after),
                    "changed by this work, and not yet committed"
                    if not (before and after) else
                    f"changed between {before[:8]} and {after[:8]}")
    if spans:
        return spans
    if commit:
        spans = bounded(view.commit_hunks(rel, commit),
                        f"changed in commit {commit[:8]}, the last to touch "
                        "this file")
        if spans:
            return spans
    if not lines:
        return []
    high = min(len(lines), MAX_EXCERPT_LINES)
    return [(1, high, "the opening of the file -- git had no diff to point at")]


# --- the payload ---------------------------------------------------------

def snapshot(root: Optional[Path], cwd, user_id: str, mint: bool = True,
             content: bool = True) -> Dict[str, Any]:
    """Every file fact this project has, ready for ``hc_sync_files``.

    *content* is the switch on the one part of this that is a copy of the
    user's source. With it false the payload is metadata and history alone,
    and ``hc_sync_files`` will prune away any excerpt a previous sync left.
    """
    owner = SY._text(user_id)
    if not owner:
        raise ValueError("user_id is required: every row is owned")
    project_dir = str(Path(str(cwd)).expanduser().resolve()) if cwd else ""
    if not project_dir:
        raise ValueError("cwd is required: a file belongs to a project")
    pid = SY.project_uuid(root, cwd, mint=mint)
    namespace = uuid.UUID(pid)
    base = Path(project_dir)
    view = GitView(project_dir)

    goal_ids = _goal_ids(root, cwd, namespace)
    runs_meta = _runs_by_session(root, project_dir)

    files: Dict[str, Dict[str, Any]] = {}
    runs: List[Dict[str, Any]] = []
    run_files: List[Dict[str, Any]] = []
    excerpts: List[Dict[str, Any]] = []
    # Files ranked by how much this project's work leaned on them; only the
    # top of that ranking is allowed to contribute content.
    weight: Dict[str, int] = {}

    for manifest in _sessions_of(root, project_dir):
        session_id = SY._text(manifest.get("session_id"))
        if not session_id:
            continue
        touched = _touched_paths(session_id, root)
        record = runs_meta.get(session_id) or {}
        before = SY._text(record.get("git_head_before"))
        after = SY._text(record.get("git_head_after"))
        goal_local = SY._text(record.get("vault_goal_id"))
        run_id = _uuid5(namespace, "run", session_id)
        if not touched and not record:
            continue
        runs.append({
            "id": run_id,
            "user_id": owner, "project_id": pid,
            "session_id": session_id,
            "goal_id": goal_ids.get((session_id, goal_local)),
            "status": SY._text(record.get("status"), 40),
            "state": SY._text(record.get("state") or record.get("run_state"), 40),
            "started_at": SY._ts(record.get("started_at")
                                 or manifest.get("created_at")),
            "finished_at": SY._ts(record.get("finished_at")
                                  or manifest.get("updated_at")),
            "cwd": project_dir,
            "git_branch": SY._text(record.get("git_branch") or view.branch, 200),
            "git_head_before": before[:40],
            "git_head_after": after[:40],
            "committed": bool(before and after and before != after),
            "summary": SY._text(record.get("summary"), 2000),
            "task_total": len(record.get("tasks") or []),
            "files_total": len(touched),
        })

        for path, touch in touched.items():
            rel = _relative(base, path)
            if _skipped_dir(rel):
                continue
            entry = files.get(rel)
            if entry is None:
                if len(files) >= MAX_FILES:
                    continue
                entry = _file_row(namespace, owner, pid, base, rel, view)
                files[rel] = entry
            entry["edits"] += int(touch.get("edits") or 0)
            weight[rel] = weight.get(rel, 0) + int(touch.get("edits") or 0)
            added, removed = view.numstat(rel, before, after)
            run_files.append({
                "id": _uuid5(namespace, "run_file", session_id, rel),
                "user_id": owner, "project_id": pid,
                "run_id": run_id, "file_id": entry["id"], "path": rel,
                "tool": SY._text(touch.get("tool"), 40),
                "edits": int(touch.get("edits") or 0),
                "lines_added": added, "lines_removed": removed,
                # The commit that carries this change: the run's own end
                # commit when it made one, else whatever last touched the
                # file. Both are real; neither is invented.
                "commit_sha": (after[:40] if after and after != before
                               else entry.get("last_commit_sha") or ""),
                "first_at": touch.get("first_at"),
                "last_at": touch.get("last_at"),
            })

    if content:
        excerpts = _excerpts_for(namespace, owner, pid, base, view, files,
                                 weight, runs_meta, run_files)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "project_id": pid,
        "user_id": owner,
        "files": list(files.values()),
        "runs": runs,
        "run_files": run_files,
        "excerpts": excerpts,
    }


def _file_row(namespace: uuid.UUID, owner: str, pid: str, base: Path,
              rel: str, view: GitView) -> Dict[str, Any]:
    """One file, as facts about it and none of it."""
    path = base / rel if not os.path.isabs(rel) else Path(rel)
    tracked, status, blob = view.state_of(rel)
    row: Dict[str, Any] = {
        "id": _uuid5(namespace, "file", rel),
        "user_id": owner, "project_id": pid,
        "path": rel, "name": Path(rel).name,
        "ext": Path(rel).suffix.lstrip(".")[:20],
        "size_bytes": None, "modified_at": None, "content_sha256": "",
        "git_tracked": tracked, "git_status": status, "git_blob_sha": blob,
        "last_commit_sha": "", "last_commit_at": None,
        "edits": 0, "missing": True, "binary_file": False,
    }
    try:
        stat = path.stat()
    except OSError:
        # Gone. The row stays: a run that edited it is still true, and the
        # reader is better told "this file is no longer here" than left to
        # wonder why the history mentions a name that does not exist.
        if status != "deleted":
            row["git_status"] = "deleted" if tracked else status
        return row
    row["missing"] = False
    row["size_bytes"] = int(stat.st_size)
    row["modified_at"] = SY._ts(stat.st_mtime)
    row["binary_file"] = _looks_binary(path)
    row["content_sha256"] = _sha256(path, int(stat.st_size))
    sha, when = view.last_commit(rel)
    row["last_commit_sha"] = sha
    row["last_commit_at"] = when
    return row


def _excerpts_for(namespace: uuid.UUID, owner: str, pid: str, base: Path,
                  view: GitView, files: Dict[str, Dict[str, Any]],
                  weight: Dict[str, int], runs_meta: Dict[str, Dict[str, Any]],
                  run_files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The passages worth reading, and nothing else.

    The order of the tests matters: exclusions first and unconditionally, so
    that no amount of relevance can promote a credential or a file git was
    told to ignore. Only what survives all of them is ranked, and only the
    top of the ranking is read at all.
    """
    ranked = sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))
    by_file: Dict[str, Dict[str, Any]] = {}
    for row in run_files:
        by_file.setdefault(row["path"], row)

    out: List[Dict[str, Any]] = []
    kept = 0
    for rel, _score in ranked:
        if kept >= MAX_EXCERPT_FILES:
            break
        row = files.get(rel)
        if not row or row["missing"] or row["binary_file"]:
            continue
        if row["git_status"] == "ignored" or rel in view.ignored:
            continue
        if _secretish(rel) or _skipped_dir(rel):
            continue
        path = base / rel if not os.path.isabs(rel) else Path(rel)
        lines = _read_lines(path)
        if not lines:
            continue
        origin = by_file.get(rel) or {}
        run_id = origin.get("run_id")
        record = next((r for s, r in runs_meta.items()
                       if _uuid5(namespace, "run", s) == run_id), {})
        before = SY._text(record.get("git_head_before"))
        after = SY._text(record.get("git_head_after"))
        spans = _spans_for(view, rel, lines, before, after,
                           SY._text(row.get("last_commit_sha")))
        if not spans:
            continue
        kept += 1
        for position, (start, end, reason) in enumerate(
                spans[:MAX_EXCERPTS_PER_FILE]):
            body = _excerpt_text(lines, start, end)
            if not body.strip():
                continue
            out.append({
                "id": _uuid5(namespace, "excerpt", rel, str(start), str(end)),
                "user_id": owner, "project_id": pid,
                "file_id": row["id"], "goal_id": None, "run_id": run_id,
                "start_line": start, "end_line": end,
                "content": body,
                "file_sha256": row["content_sha256"],
                "reason": reason, "source": "run", "position": position,
            })
    return out


def _goal_ids(root: Optional[Path], cwd,
              namespace: uuid.UUID) -> Dict[Tuple[str, str], Optional[str]]:
    """``(session, local goal id) -> the goal's UUID``, so a run can name the
    goal it was launched against in the ids the database already uses."""
    from . import project_store as PS
    out: Dict[Tuple[str, str], Optional[str]] = {}
    try:
        record = PS.build(root, cwd)
    except (OSError, ValueError):
        return out
    for goal in record.get("goals") or []:
        session = SY._text(goal.get("session_id"))
        local = SY._text(goal.get("id"))
        if session and local:
            out[(session, local)] = _uuid5(namespace, "goal", session, local)
    return out


TABLES = ("files", "runs", "run_files", "excerpts")


def counts(payload: Dict[str, Any]) -> Dict[str, int]:
    """How many rows of each kind a payload holds -- so the pane can say
    what it is about to send, content included, before it sends it."""
    return {name: len(payload.get(name) or []) for name in TABLES}
