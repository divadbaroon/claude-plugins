"""Private filesystem primitives for conversation-derived global Vault state.

The global trajectory is local but sensitive: summaries and goal metadata can
reconstruct user prompts even when the raw transcript itself is protected.
Every writer therefore goes through these helpers so umask, pre-existing
permissions, and interrupted writes cannot make an artifact world-readable.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from ..platform_compat import maybe_fchmod


DIR_MODE = 0o700
FILE_MODE = 0o600


def _chmod_nofollow(path: Path, mode: int, *, directory: bool = False) -> None:
    # Windows has no mode bits to tighten (see maybe_fchmod), and its CRT
    # cannot open a directory descriptor at all, so the os.open below would
    # raise PermissionError on every directory. Privacy there comes from the
    # tree living under the user's own profile.
    if not hasattr(os, "fchmod"):
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        maybe_fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def secure_dir(path: Path, root: Optional[Path] = None) -> Path:
    """Create ``path`` and tighten it through ``root`` (inclusive)."""
    path = Path(os.path.abspath(os.fspath(path)))
    root = (Path(os.path.abspath(os.fspath(root)))
            if root is not None else path)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"private state path escaped its root: {path}") from exc
    relative = path.relative_to(root)
    # Walk from the trust boundary outward. ``parents=True`` would traverse an
    # existing symlink before we could inspect it and could create files outside
    # the Vault.
    components = [root]
    current = root
    for part in relative.parts:
        current /= part
        components.append(current)
    for component in components:
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            # Another hook may create the same queue/state directory between
            # lstat and mkdir. ``exist_ok`` preserves idempotence; the lstat
            # immediately below still rejects a racing symlink.
            component.mkdir(mode=DIR_MODE, exist_ok=True)
            metadata = os.lstat(component)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(
                f"refusing non-directory private state component: {component}")
        _chmod_nofollow(component, DIR_MODE, directory=True)
    return path


def secure_existing_tree(root: Path, boundary: Optional[Path] = None) -> Path:
    """Tighten a legacy private tree without following any symlink entry."""
    root = secure_dir(root, boundary or root)

    def visit(directory: Path) -> None:
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            return
        with entries:
            for entry in entries:
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                child = Path(entry.path)
                if stat.S_ISLNK(metadata.st_mode):
                    raise RuntimeError(
                        f"refusing symlink in private state tree: {child}")
                if stat.S_ISDIR(metadata.st_mode):
                    try:
                        _chmod_nofollow(child, DIR_MODE, directory=True)
                        visit(child)
                    except FileNotFoundError:
                        continue
                elif stat.S_ISREG(metadata.st_mode):
                    try:
                        _chmod_nofollow(child, FILE_MODE)
                    except FileNotFoundError:
                        continue
                else:
                    raise RuntimeError(
                        f"refusing unsupported private state entry: {child}")

    visit(root)
    return root


def atomic_write_text(path: Path, text: str, *, root: Optional[Path] = None,
                      encoding: str = "utf-8") -> Path:
    """Atomically replace a private text file and enforce mode 0600."""
    path = Path(path)
    secure_dir(path.parent, root or path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        maybe_fchmod(descriptor, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding=encoding) as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, FILE_MODE)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path


def atomic_write_json(path: Path, value, *, root: Optional[Path] = None,
                      indent: int = 1) -> Path:
    return atomic_write_text(
        path, json.dumps(value, indent=indent) + "\n", root=root)


@contextmanager
def open_private_append(path: Path, *, root: Optional[Path] = None,
                        binary: bool = False, secure_parent: bool = True):
    """Open an append-only private file, tightening an existing file first."""
    path = Path(path)
    if secure_parent:
        secure_dir(path.parent, root or path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, FILE_MODE)
    try:
        maybe_fchmod(descriptor, FILE_MODE)
        kwargs = {} if binary else {"encoding": "utf-8"}
        with os.fdopen(descriptor, "ab" if binary else "a", **kwargs) as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def touch_private(path: Path, *, root: Optional[Path] = None) -> Path:
    """Create or tighten a private marker without changing its contents."""
    path = Path(path)
    secure_dir(path.parent, root or path.parent)
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, FILE_MODE)
    try:
        maybe_fchmod(descriptor, FILE_MODE)
    finally:
        os.close(descriptor)
    return path
