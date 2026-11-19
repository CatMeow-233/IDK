"""Workspace boundary enforcement.

Every file Lean C reads from or writes to a project goes through
:class:`Workspace`, which guarantees that

* paths are relative and stay inside the project root (no ``..`` escape, no
  absolute paths),
* no path component is a symbolic link (symlinks could redirect reads or writes
  outside the root, or onto protected files),
* writes are atomic (temp file + ``os.replace`` in the same directory) and never
  follow symlinks.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from leanc.core.errors import WorkspaceError


class Workspace:
    def __init__(self, root: str | os.PathLike[str]):
        root_path = Path(root)
        if not root_path.exists():
            raise WorkspaceError(f"workspace root does not exist: {root_path}")
        self.root = Path(os.path.realpath(root_path))

    # ------------------------------------------------------------------ paths
    def normalize(self, rel: str | os.PathLike[str]) -> str:
        """Validate a workspace-relative path and return it in POSIX form."""
        return normalize_relpath(rel)

    def resolve(self, rel: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
        """Resolve a relative path inside the workspace, rejecting symlinks."""
        norm = self.normalize(rel)
        cur = self.root
        for part in norm.split("/"):
            cur = cur / part
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                if must_exist:
                    raise WorkspaceError(f"file not found in workspace: {norm}") from None
                # Remaining components don't exist yet; nothing to follow.
                break
            if stat.S_ISLNK(st.st_mode):
                raise WorkspaceError(f"symbolic links are not allowed in workspace paths: {norm}")
        full = self.root / norm
        real_parent = Path(os.path.realpath(full.parent)) if full.parent.exists() else None
        if real_parent is not None and not _is_within(real_parent, self.root):
            raise WorkspaceError(f"path escapes the workspace: {norm}")
        return full

    def relpath(self, path: str | os.PathLike[str]) -> str:
        """Convert an absolute path inside the workspace to a relative one."""
        real = Path(os.path.realpath(path))
        if not _is_within(real, self.root):
            raise WorkspaceError(f"path is outside the workspace: {path}")
        return real.relative_to(self.root).as_posix()

    def exists(self, rel: str) -> bool:
        try:
            return self.resolve(rel).exists()
        except WorkspaceError:
            return False

    # ------------------------------------------------------------------ I/O
    def read_bytes(self, rel: str, max_bytes: int = 16 * 1024 * 1024) -> bytes:
        p = self.resolve(rel, must_exist=True)
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise WorkspaceError(f"not a regular file: {rel}")
            if st.st_size > max_bytes:
                raise WorkspaceError(f"file too large ({st.st_size} bytes): {rel}")
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                return fh.read()
        finally:
            if fd >= 0:
                os.close(fd)

    def read_text(self, rel: str, max_bytes: int = 16 * 1024 * 1024) -> str:
        data = self.read_bytes(rel, max_bytes)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{rel} is not valid UTF-8 (byte offset {exc.start}); convert it to UTF-8") from None

    def write_text(self, rel: str, content: str, *, mode: int | None = None) -> Path:
        return self.write_bytes(rel, content.encode("utf-8"), mode=mode)

    def write_bytes(self, rel: str, data: bytes, *, mode: int | None = None) -> Path:
        """Atomically replace ``rel``.  ``mode=None`` keeps the permission bits
        of an existing file (0644 for new files)."""
        target = self.resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        # re-check after mkdir (a racing symlink would be caught here)
        target = self.resolve(rel)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise WorkspaceError(f"refusing to overwrite non-regular file: {rel}")
        if mode is None:
            mode = (target.stat().st_mode & 0o7777) if target.exists() else 0o644
        fd, tmp = tempfile.mkstemp(prefix=".leanc-tmp-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return target

    def copy_into(self, rels: list[str], dest_dir: str | os.PathLike[str]) -> dict[str, Path]:
        """Copy workspace files (by relative path) into ``dest_dir`` preserving
        the relative layout.  Returns rel -> destination path."""
        dest = Path(dest_dir)
        out: dict[str, Path] = {}
        for rel in rels:
            norm = self.normalize(rel)
            data = self.read_bytes(norm)
            d = dest / norm
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(data)
            out[norm] = d
        return out


def normalize_relpath(rel: str | os.PathLike[str]) -> str:
    """Validate a relative path (no absolute paths, no ``..``) and return it in
    normalised POSIX form.  Pure string check; does not touch the filesystem."""
    s = os.fspath(rel)
    if not isinstance(s, str) or not s or "\x00" in s:
        raise WorkspaceError(f"invalid path {s!r}")
    if s.startswith("/") or s.startswith("\\") or (len(s) > 1 and s[1] == ":"):
        raise WorkspaceError(f"absolute paths are not allowed: {s!r}")
    parts = PurePosixPath(s.replace("\\", "/")).parts
    clean: list[str] = []
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            raise WorkspaceError(f"path traversal ('..') is not allowed: {s!r}")
        clean.append(p)
    if not clean:
        raise WorkspaceError(f"invalid path {s!r}")
    return "/".join(clean)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_rmtree(path: str | os.PathLike[str], within: str | os.PathLike[str]) -> None:
    """Remove ``path`` only if it lies inside ``within`` (defence in depth)."""
    p = Path(os.path.realpath(path))
    w = Path(os.path.realpath(within))
    if p == w or not _is_within(p, w):
        raise WorkspaceError(f"refusing to delete {p}: not inside {w}")
    shutil.rmtree(p)
