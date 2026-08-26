"""One JSON file per project: everything its chats know about it.

A project is the directory a chat was started in -- what its manifest
recorded as ``cwd`` -- so every chat started in that directory is a chat of
the same project. This module keeps one file per directory, holding both
halves of what is known about it:

* the AUTHORED half -- the objective and description in the reader's words,
  and the sources they saved to the project. Written by the workspace, never
  inferred, and never overwritten by a regeneration.
* the DERIVED half -- every goal of every chat in the directory, each with
  where it sits in its tree (parent, children, siblings, depth, the titles
  above it), its notes, its TODO rows with the status of each, its prompt,
  and the prompts marked as related to it. Regenerated from the chat stores.

The shape, in one glance::

    {"schema_version", "generated_at",
     "project": {"cwd", "name", "objective", "description", "sources"},
     "chats": [{"session_id", "created_at", "updated_at",
                "prompt_count", "goal_count"}],
     "goals": [{"key": "<session>:<goal id>", "id", "session_id",
                "title", "status", "priority", "origin", "description",
                "updated_at",
                "location": {"parent_id", "parent_key", "child_ids",
                             "child_keys", "sibling_ids", "sibling_keys",
                             "depth", "title_path"},
                "notes", "todos", "todos_md", "attachments",
                "prompt", "related_prompts", "sources",
                "evidence_ids", "important"}]}

A goal's id is unique only within its chat, so goals are keyed by both --
two chats in one directory each have a ``g1``.

The derived half is a snapshot, not a source of truth: it is rebuilt from
``goals.json``/``todos.json``/``prompts.json`` after every goal save, so
deleting the file loses nothing but the authored lines -- which is why those
are read out and written back around each regeneration rather than rebuilt.

Reads of the other chats' stores are taken without their locks on purpose. A
save already holds its own session's lock, and reaching for a second one is
how two chats saving at the same moment deadlock; every writer here replaces
files atomically, so a lock-free read sees one whole version or another,
never half of one.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import chat_state as CS
from . import goals as GM
from .secure_io import atomic_write_json

SCHEMA_VERSION = 1
PROJECT_OBJECTIVE_LIMIT = 2000
PROJECT_DESCRIPTION_LIMIT = 8000
PROJECT_NAME_LIMIT = 80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolved(cwd) -> str:
    """A directory as one comparable string, links and ``~`` resolved."""
    try:
        return str(Path(str(cwd)).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(cwd)


def _slug(name: str) -> str:
    """A name as a directory name: its words, lowercased, joined by dashes.

    Only ever a file name -- what the project is called is written in its
    record, so a slug that collapses two spellings into one folder still
    shows the reader the name they typed.
    """
    flat = "".join(c.lower() if c.isalnum() else "-" for c in str(name))
    return "-".join(part for part in flat.split("-") if part)[:60]


def workspace_home(root: Optional[Path], name) -> Optional[Path]:
    """Where a project made from a name alone keeps its directory.

    Everything here is keyed by a directory, and a project typed as a name
    has none yet -- so it is given one, inside the vault beside the records
    rather than anywhere on the reader's disk. ``None`` when the name has no
    letters or digits in it at all and so cannot become a folder.
    """
    slug = _slug(name)
    if not slug:
        return None
    return CS._state_location(root)[1] / "workspaces" / slug


def create_under(root: Optional[Path], parent, name) -> Optional[str]:
    """Make a project's directory inside one the reader chose.

    The vault is where a project goes when nobody said where, not where a
    project belongs: code kept beside the records is code in a strange
    place, and the reader who picks a parent has said where they want it.
    The folder is created -- unlike a path that was typed, which must exist
    already, because a typo there is a mistake and a picked parent is not.
    """
    slug = _slug(name)
    if not slug:
        return None
    try:
        base = Path(str(parent)).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not base.is_dir():
        return None
    home = base / slug
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    save_project(root, str(home), {"name": " ".join(str(name or "").split())
                                   [:PROJECT_NAME_LIMIT]})
    return str(home)


def project_named(root: Optional[Path], name) -> Optional[Dict[str, Any]]:
    """The project already called this, if there is one.

    Two projects with one name are two answers to "which one did I mean",
    so a name that is taken is reported back rather than quietly reused --
    the reader picks the one they meant off the list, or renames the one
    they are making. Matched as a reader reads a name, ignoring case and
    spacing, and also by the folder a name is given, so a project renamed
    since it was made is still found by the home it sits in.
    """
    wanted = " ".join(str(name or "").split()).casefold()
    home = workspace_home(root, name)
    seat = _resolved(home) if home is not None else ""
    for row in list_projects(root):
        here = " ".join(str(row.get("name") or "").split()).casefold()
        if wanted and here == wanted:
            return row
        if seat and _resolved(row.get("cwd")) == seat:
            return row
    return None


def create_named(root: Optional[Path], name) -> Optional[str]:
    """Make a project out of a name: give it a home, write it a record, and
    keep the name as it was typed.

    Idempotent by name: typing one that already exists hands back the
    project it names rather than a second copy of it, and leaves everything
    written about it alone. Whether being handed the first one is what the
    reader wanted is not settled here -- ``project_named`` is what asks that
    question, before this is called.
    """
    text = str(name or "").strip()[:PROJECT_NAME_LIMIT]
    home = workspace_home(root, text)
    if home is None:
        return None
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    cwd = str(home)
    if not read_file(root, cwd).get("project"):
        save_project(root, cwd, {"name": text})
    return cwd


_REPO_HOME_CACHE: Dict[str, str] = {}


def repo_home(cwd) -> str:
    """The directory that IS this project, for a path inside a checkout.

    A git worktree is a second directory of the same repository, opened to
    hold another branch. Keyed by its own path it became a second project
    with a second goal tree -- so the reader working one repo from two
    checkouts saw two sets of goals and no way to say they were one thing.
    The repository's main worktree is the answer for every checkout of it.

    Anything that is not a checkout is its own project, which is every
    directory git has never heard of and every path that no longer exists.
    Cached: this is asked on the way in to a bind and a launch, and spawning
    git for an answer that cannot change within a run is a cost for nothing.
    """
    import subprocess
    here = _resolved(cwd)
    cached = _REPO_HOME_CACHE.get(here)
    if cached is not None:
        return cached
    answer = here
    try:
        common = subprocess.run(
            ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=here, capture_output=True, text=True, timeout=5, check=False)
        found = (common.stdout or "").strip()
        if common.returncode == 0 and found:
            # The common dir is the repository's own .git; the project is the
            # directory holding it. A bare repository has no working tree to
            # be a project, so it is left as the path that was asked about.
            parent = Path(found)
            if parent.name == ".git" and parent.parent.is_dir():
                found_home = _resolved(parent.parent)
                # A home directory kept under version control -- dotfiles --
                # would otherwise swallow every project inside it into one.
                # A repository that IS the reader's home is not the project
                # every directory under it belongs to.
                if found_home != _resolved(Path.home()):
                    answer = found_home
    except (OSError, ValueError, subprocess.SubprocessError):
        answer = here
    _REPO_HOME_CACHE[here] = answer
    return answer


def project_path(root: Optional[Path], cwd) -> Path:
    """Where the project's file lives: beside the chat sessions, keyed by the
    digest of the resolved directory -- never by the path itself, which is
    not a safe file name."""
    digest = hashlib.sha256(repo_home(cwd).encode("utf-8")).hexdigest()[:16]
    return CS._state_base(root) / "projects" / f"{digest}.json"


def read_file(root: Optional[Path], cwd) -> Dict[str, Any]:
    """The file as it stands, migrating the flat shape it was first written
    in (``{"cwd": ..., "objective": ...}``) into its ``project`` section.

    The flat keys are moved rather than copied: the file is meant to be
    opened and read, and a top-level ``objective`` left beside the one in
    ``project`` is a second answer to the same question that no writer
    afterwards keeps up to date.
    """
    if not cwd:
        return {}
    try:
        value = json.loads(project_path(root, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    if not isinstance(value.get("project"), dict):
        flat = ("cwd", "objective")
        section = {k: value[k] for k in flat if k in value}
        value = {k: v for k, v in value.items() if k not in flat}
        value["project"] = section
    return value


def load_project(root: Optional[Path], cwd) -> Dict[str, Any]:
    """The authored half only: what the reader wrote about the project."""
    section = read_file(root, cwd).get("project")
    section = section if isinstance(section, dict) else {}
    out: Dict[str, Any] = {}
    # What the reader calls it. A directory name is where a project sits
    # rather than what it is called, so a name they wrote wins over it --
    # and only a written one is carried here, so an unnamed project still
    # falls back to its directory.
    name = section.get("name")
    if isinstance(name, str) and name.strip():
        out["name"] = name.strip()[:PROJECT_NAME_LIMIT]
    objective = section.get("objective")
    if isinstance(objective, str):
        out["objective"] = objective[:PROJECT_OBJECTIVE_LIMIT]
    description = section.get("description")
    if isinstance(description, str):
        out["description"] = description[:PROJECT_DESCRIPTION_LIMIT]
    sources = GM.normalize_sources(section.get("sources"))
    if sources:
        out["sources"] = sources
    identity = section.get("id")
    if isinstance(identity, str) and identity:
        out["id"] = identity[:64]
    return out


def save_project(root: Optional[Path], cwd, authored: Dict[str, Any],
                 revive: bool = True) -> Path:
    """Write the authored half, leaving the derived goals where they are.

    Writing about a project is how a deleted one comes back: the reader
    naming it again means they want it, and the tombstone that keeps a
    regeneration from resurrecting it must not also keep them out. A writer
    that is only stamping bookkeeping onto a record -- an id, the store its
    tree sits in -- passes *revive* false, because none of that is anybody
    saying the project should exist again.
    """
    if revive:
        revive_project(root, cwd)
    record = read_file(root, cwd)
    section = record.get("project")
    section = dict(section) if isinstance(section, dict) else {}
    section.update(authored)
    return _write(root, cwd, dict(record, project=_project_section(
        cwd, section)))


def _write(root: Optional[Path], cwd, record: Dict[str, Any]) -> Path:
    path = project_path(root, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    record["schema_version"] = SCHEMA_VERSION
    record["generated_at"] = _now()
    atomic_write_json(path, record, root=path.parent)
    return path


def _project_section(cwd, authored: Dict[str, Any]) -> Dict[str, Any]:
    """The project's own metadata: where it is, what it is called, what it is
    for. ``description`` falls back to the objective -- the reader writes one
    line about a project in the workspace, and a consumer asking for the
    project's description should get it rather than an empty string."""
    objective = str(authored.get("objective") or "")[:PROJECT_OBJECTIVE_LIMIT]
    description = str(authored.get("description") or "")[
        :PROJECT_DESCRIPTION_LIMIT]
    # The directory's name is the fallback, not the answer: a project can be
    # renamed, and what it is called is written here rather than recovered
    # from the path it happens to sit at.
    named = str(authored.get("name") or "").strip()[:PROJECT_NAME_LIMIT]
    section = {"cwd": str(cwd), "name": named or Path(str(cwd)).name,
               "objective": objective,
               "description": description or objective,
               "sources": GM.normalize_sources(authored.get("sources"))}
    # The project's own identity, when one has been minted: a directory is
    # where a project sits today, not what it is, so anything keyed on the
    # path alone calls the same repository on two machines two projects.
    identity = authored.get("id")
    if isinstance(identity, str) and identity:
        section["id"] = identity[:64]
    # Which store holds the project's goals. Carried through explicitly: this
    # section is rebuilt from a whitelist on every write, so a key merely
    # present in the record it was read from would be dropped by the next
    # unrelated edit -- and the project would forget where its tree is.
    held = authored.get("tree_session")
    if isinstance(held, str) and held:
        section["tree_session"] = held[:200]
    return section


# --- deleting a project ------------------------------------------------------
#
# Deleting is not forgetting. What the reader asks for on the projects screen
# is that the project stop existing: its record, the records any other
# checkout of the same repository left behind, the window it was pointing at,
# and the vault directories of its chats -- the goals, the TODO rows, the
# notes, the prompts. Nothing outside the vault is touched: the code the
# project is about is the reader's, not this tool's.
#
# A deletion also has to survive the writers that would put the record back.
# Every goal save regenerates the project file of the directory its chat
# works in, so deleting the files alone means the project returns the moment
# anything is saved. The homes that were deleted are written down, and a
# regeneration skips them; anybody authoring the project again clears the
# note, because saying what a project is for is saying it exists.

def _projects_dir(root: Optional[Path]) -> Path:
    return CS._state_base(root) / "projects"


def _deleted_path(root: Optional[Path]) -> Path:
    return _projects_dir(root) / "deleted.json"


def _deleted(root: Optional[Path]) -> Dict[str, str]:
    try:
        value = json.loads(_deleted_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    homes = value.get("homes") if isinstance(value, dict) else None
    if not isinstance(homes, dict):
        return {}
    return {k: str(v) for k, v in homes.items() if isinstance(k, str) and k}


def _write_deleted(root: Optional[Path], homes: Dict[str, str]) -> None:
    path = _deleted_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"homes": homes}, root=path.parent)


def deleted(root: Optional[Path], cwd) -> bool:
    """Whether this project was deleted and has not been remade since."""
    if not cwd:
        return False
    return repo_home(cwd) in _deleted(root)


def revive_project(root: Optional[Path], cwd) -> bool:
    """Lift the deletion: the reader is writing about this project again."""
    if not cwd:
        return False
    homes = _deleted(root)
    if homes.pop(repo_home(cwd), None) is None:
        return False
    _write_deleted(root, homes)
    return True


def _note_deleted(root: Optional[Path], cwd) -> None:
    homes = _deleted(root)
    homes[repo_home(cwd)] = _now()
    _write_deleted(root, homes)


def records_for(root: Optional[Path], cwd) -> List[Path]:
    """Every record file this repository has, not only the current one.

    A vault written before worktrees were folded into one project holds a
    file per checkout, all naming the same repository. Deleting the file the
    digest points at and stopping there leaves those behind, and the switcher
    -- which folds by repository -- goes on listing the project from one of
    them. That is the deletion that did not take.
    """
    target = repo_home(cwd)
    out: List[Path] = []
    try:
        entries = sorted(_projects_dir(root).glob("*.json"))
    except OSError:
        return out
    for entry in entries:
        if entry.name.endswith(".server.json") or entry == _deleted_path(root):
            continue
        try:
            value = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        section = value.get("project")
        here = (section or {}).get("cwd") if isinstance(section, dict) else None
        here = here or value.get("cwd")
        if isinstance(here, str) and here and repo_home(here) == target:
            out.append(entry)
    canonical = project_path(root, cwd)
    if canonical.is_file() and canonical not in out:
        out.append(canonical)
    return out


def _within(path: Path, base: Path) -> bool:
    """Whether *path* is inside *base* -- asked before anything is removed."""
    try:
        return path.resolve().is_relative_to(base.resolve())
    except (AttributeError, OSError, RuntimeError, ValueError):
        try:
            path.resolve().relative_to(base.resolve())
            return True
        except (OSError, RuntimeError, ValueError):
            return False


def delete_project(root: Optional[Path], cwd,
                   sessions: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Delete a project and everything the vault keeps for it.

    What goes: every record the repository has, the window it was pointing
    at, and the session directory of every chat in it -- which is where the
    goals, the TODO rows, the notes and the prompts actually live, so this is
    the only deletion a reader would call one. A project given a home inside
    the vault, because it was made from a name alone, loses that too.

    What stays: everything outside the vault. A project's directory is the
    reader's repository, and no button on a projects screen should be able to
    remove it.

    *sessions* names chats the caller already knows belong here -- a caller
    that has just cut them loose has to, because a chat unbound no longer
    says which project it was in.

    ``None`` when there was nothing here to delete.
    """
    if not cwd:
        return None
    base = CS._state_base(root)
    target = repo_home(cwd)
    records = records_for(root, cwd)
    members = sorted(set(project_sessions(root, cwd))
                     | set(CS.chats_in_project(cwd, root))
                     | {str(s) for s in (sessions or []) if s})
    chats, goals_gone = 0, 0
    for session_id in members:
        try:
            seat = CS.paths(session_id, root).session_dir
        except (ValueError, TypeError):
            continue
        try:
            goals_gone += len([g for g in CS.load_goals(session_id, root)[0]
                               .get("goals", []) if isinstance(g, dict)])
        except (OSError, ValueError, TypeError):
            pass
        if not seat.is_dir() or not _within(seat, base):
            continue
        shutil.rmtree(seat, ignore_errors=True)
        chats += 1
    for entry in records:
        try:
            entry.unlink()
        except OSError:
            continue
        try:
            entry.with_name(entry.stem + ".server.json").unlink(missing_ok=True)
        except OSError:
            pass
    clear_server_record(root, cwd)
    # A project made from a name alone was given a folder in the vault; it is
    # this tool's to remove. A directory anywhere else is the reader's.
    workspaces = CS._state_location(root)[1] / "workspaces"
    seat = Path(target)
    workspace = False
    if seat.is_dir() and _within(seat, workspaces):
        shutil.rmtree(seat, ignore_errors=True)
        workspace = True
    if not (records or chats or workspace):
        # Nothing was here. Noting a deletion would be noting one that never
        # happened, and the note is what keeps a project off the screen.
        return None
    _note_deleted(root, cwd)
    return {"cwd": target, "records": len(records), "chats": chats,
            "goals": goals_gone, "workspace": workspace}


def list_projects(root: Optional[Path]) -> List[Dict[str, Any]]:
    """Every project this vault has a file for, newest first.

    The files are keyed by a digest of the directory, so the directory has
    to be read back out of each one rather than recovered from its name. A
    file whose ``cwd`` is missing is skipped: without it there is nothing to
    point the switcher at.
    """
    base = _projects_dir(root)
    gone = _deleted(root)
    out: Dict[str, Dict[str, Any]] = {}
    try:
        entries = sorted(base.glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        if entry == _deleted_path(root):
            continue
        try:
            value = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        section = value.get("project")
        section = section if isinstance(section, dict) else {}
        cwd = section.get("cwd") or value.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        # Folded by repository, not by path: a vault written before worktrees
        # were understood holds a file per checkout, and the switcher listing
        # the same repository three times is the confusion this removes. The
        # record actually keyed by the repository wins, whatever else is
        # lying around beside it.
        key = repo_home(cwd)
        # A project the reader deleted stays deleted, whatever wrote a record
        # back afterwards.
        if key in gone:
            continue
        if key in out and entry != project_path(root, key):
            continue
        out[key] = {
            "cwd": key,
            "name": str(section.get("name") or Path(key).name or key),
            "objective": str(section.get("objective") or ""),
            "description": str(section.get("description") or ""),
            "generated_at": str(value.get("generated_at") or ""),
            "goals": len(value.get("goals") or []),
            "chats": len(value.get("chats") or []),
        }
    return sorted(out.values(),
                  key=lambda row: (row["generated_at"], row["name"]),
                  reverse=True)


def touch(root: Optional[Path], cwd) -> Dict[str, Any]:
    """Write an empty record for a directory that has none, so it is a
    project the switcher can see before any chat has been started in it."""
    if read_file(root, cwd).get("project"):
        return load_project(root, cwd)
    save_project(root, cwd, {})
    return load_project(root, cwd)


def tree_session(root: Optional[Path], cwd) -> str:
    """Which chat's store holds this project's goals, as the project says.

    Kept on the project rather than worked out per chat: two chats scanning
    for it independently can disagree, and did -- session directories are
    UUIDs, so any ordering of them is arbitrary.
    """
    section = read_file(root, cwd).get("project")
    held = (section or {}).get("tree_session") if isinstance(section, dict) else ""
    return str(held or "")


def set_tree_session(root: Optional[Path], cwd, session_id: str) -> str:
    """Name the store, once. Never moved by a later chat: the project's goals
    do not change address because somebody new opened it."""
    said = str(session_id or "").strip()
    if not said:
        return ""
    held = tree_session(root, cwd)
    if held:
        return held
    save_project(root, cwd, {"tree_session": said}, revive=False)
    return said


def server_path(root: Optional[Path], cwd) -> Path:
    """Where a project notes the workspace it has running.

    Beside the project's record and keyed the same way, so every checkout of
    a repository and every chat bound to it asks one file. Its own file
    rather than a key in the record: the record's project section is rebuilt
    from a whitelist on every save, and a server that comes and goes many
    times an hour has no business rewriting the reader's objective.
    """
    target = project_path(root, cwd)
    return target.with_name(target.stem + ".server.json")


def server_record(root: Optional[Path], cwd) -> Optional[Dict[str, Any]]:
    """The workspace this project has running, as last recorded.

    A record here is a claim, not a promise -- the process it names may be
    gone. Whoever reads it probes before believing it.
    """
    if not cwd:
        return None
    try:
        value = json.loads(server_path(root, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def set_server_record(root: Optional[Path], cwd,
                      value: Dict[str, Any]) -> Optional[Path]:
    """Note which workspace is this project's, for the next chat to find."""
    if not cwd or not isinstance(value, dict):
        return None
    target = server_path(root, cwd)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, dict(value, cwd=repo_home(cwd)),
                      root=target.parent)
    return target


def clear_server_record(root: Optional[Path], cwd) -> None:
    """Forget the workspace: it has been stopped, or it was never there."""
    if not cwd:
        return
    try:
        server_path(root, cwd).unlink(missing_ok=True)
    except OSError:
        pass


def project_sessions(root: Optional[Path], cwd) -> List[str]:
    """Every chat started in this directory, oldest state first.

    The manifest is read as written rather than through ``load_manifest``,
    which hands back a blank default when a seeded or copied workspace's
    manifest disagrees with its directory name -- the directory a chat was
    started in is still the directory it was started in.
    """
    base = CS._state_base(root)
    target = repo_home(cwd)
    out = []
    try:
        entries = sorted(base.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            manifest = CS.paths(entry.name, root).manifest
        except ValueError:
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        here = value.get("cwd") if isinstance(value, dict) else None
        if isinstance(here, str) and here and repo_home(here) == target:
            out.append(entry.name)
    return out


def _location(goal: Dict[str, Any], goals: List[Dict[str, Any]],
              session_id: str) -> Dict[str, Any]:
    """Where a goal sits in its chat's tree: the parent it hangs from, the
    goals under it, the ones beside it, and the titles above it."""
    gid = goal.get("id")
    parent_id = goal.get("parent_goal_id")
    by_id = {g.get("id"): g for g in goals}

    def key(other):
        return f"{session_id}:{other}" if other else None

    path, seen, walk = [], set(), goal
    while isinstance(walk, dict) and walk.get("id") not in seen:
        seen.add(walk.get("id"))
        path.append(str(walk.get("title") or ""))
        walk = by_id.get(walk.get("parent_goal_id"))
    path.reverse()
    children = [g.get("id") for g in goals if g.get("parent_goal_id") == gid]
    siblings = [g.get("id") for g in goals
                if g.get("parent_goal_id") == parent_id and g.get("id") != gid]
    return {"parent_id": parent_id, "parent_key": key(parent_id),
            "child_ids": children, "child_keys": [key(c) for c in children],
            "sibling_ids": siblings, "sibling_keys": [key(s) for s in siblings],
            "depth": len(path), "title_path": path}


def _related_prompts(goal: Dict[str, Any],
                     prompts: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The prompts marked as belonging with this goal, whole.

    ``auto`` says which of them inference linked from the evidence it had
    already cited, so a reader can tell the reader's own marks apart from the
    machine's. A prompt the user detached is not here: a detach is a
    decision, and the store keeps it out of ``prompt_ids`` for good.
    """
    automatic = set(goal.get("auto_prompt_ids") or [])
    out = []
    for pid in goal.get("prompt_ids") or []:
        prompt = prompts.get(pid)
        if not isinstance(prompt, dict):
            continue
        out.append({"id": pid, "text": str(prompt.get("text") or ""),
                    "created_at": prompt.get("created_at"),
                    "session_id": prompt.get("session_id"),
                    "auto": pid in automatic})
    return out


def _goal_record(goal: Dict[str, Any], goals: List[Dict[str, Any]],
                 session_id: str, prompts: Dict[str, Dict[str, Any]],
                 items: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    todos = GM.normalize_todo_items(goal.get("todo_items"))
    return {
        "key": f"{session_id}:{goal.get('id')}",
        "id": goal.get("id"),
        "session_id": session_id,
        "title": str(goal.get("title") or ""),
        "status": str(goal.get("status") or "active"),
        "priority": str(goal.get("priority") or "normal"),
        "origin": str(goal.get("origin") or ""),
        # How this goal stands to the project's objective, and the objective
        # it was judged against -- a verdict outlives the sentence that made
        # it, so the two travel together.
        "relevance": str(goal.get("relevance") or "core"),
        "relevance_why": str(goal.get("relevance_why") or ""),
        "relevance_for": str(goal.get("relevance_for") or ""),
        "description": str(goal.get("description") or ""),
        "updated_at": goal.get("updated_at"),
        "location": _location(goal, goals, session_id),
        # The goal's whole markdown document, as written.
        "notes": str(goal.get("notes") or ""),
        # The rail's rows are the list; the markdown beside them is derived
        # from the same rows, for a consumer that wants it as text.
        "todos": todos,
        "todos_md": GM.render_todos(todos),
        "attachments": GM.todo_attachments(todos),
        "prompt": str(goal.get("prompt_md") or ""),
        "related_prompts": _related_prompts(goal, prompts),
        "sources": GM.normalize_sources(goal.get("sources")),
        "evidence_ids": [e for e in goal.get("evidence_ids") or []
                         if isinstance(e, str)],
        "important": [items[iid] for iid in goal.get("important_item_ids") or []
                      if iid in items],
    }


def build(root: Optional[Path], cwd) -> Dict[str, Any]:
    """The whole record for one project, authored half and derived half."""
    authored = load_project(root, cwd)
    chats, goal_rows = [], []
    for session_id in project_sessions(root, cwd):
        try:
            goals, important = CS.load_goals(session_id, root)
            prompts = {p.get("id"): p for p in CS.load_prompts(session_id, root)
                       if isinstance(p, dict)}
        except (OSError, ValueError):
            continue
        items = {i.get("id"): i for i in important.get("items", [])
                 if isinstance(i, dict)}
        rows = [g for g in goals.get("goals", []) if isinstance(g, dict)]
        manifest = CS.load_manifest(session_id, root)
        chats.append({"session_id": session_id,
                      "updated_at": manifest.get("updated_at"),
                      "created_at": manifest.get("created_at"),
                      "prompt_count": len(prompts),
                      "goal_count": len(rows)})
        goal_rows.extend(_goal_record(g, rows, session_id, prompts, items)
                         for g in rows)
    return {"schema_version": SCHEMA_VERSION, "generated_at": _now(),
            "project": _project_section(cwd, authored),
            "chats": chats, "goals": goal_rows}


def write(root: Optional[Path], cwd) -> Optional[Path]:
    """Regenerate the project's file. ``None`` when there is no directory to
    write one for -- a chat whose manifest never recorded a cwd belongs to no
    project, and inventing a digest for "" would collect all of them.

    ``None`` too for a project the reader deleted: this is a regeneration,
    not somebody asking for the project, and a snapshot rewritten by the next
    goal save is exactly how a deletion used to undo itself.
    """
    if not cwd or deleted(root, cwd):
        return None
    return _write(root, cwd, build(root, cwd))


def refresh_for_session(session_id: str,
                        root: Optional[Path] = None) -> Optional[Path]:
    """Rewrite the project file of whichever directory this chat works in.

    Called after a goal save. Never raises into its caller: the file is a
    snapshot of state that is already durable elsewhere, so failing to
    refresh it must not fail the save that state just came from.
    """
    try:
        manifest = CS.paths(session_id, root).manifest
        value = json.loads(manifest.read_text(encoding="utf-8"))
        cwd = value.get("cwd") if isinstance(value, dict) else None
        if not isinstance(cwd, str) or not cwd:
            return None
        return write(root, cwd)
    except Exception:                                    # noqa: BLE001
        return None
