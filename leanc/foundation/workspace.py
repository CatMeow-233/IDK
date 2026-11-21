"""Workspace boundary: relative, symlink-free paths inside the project root and atomic, non-following writes."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from leanc.foundation.errors import WorkspaceError

DEFAULT_MAX_READ_BYTES = 16 * 1024 * 1024
NEW_FILE_MODE = 0o644
PERMISSION_BITS = 0o7777
TEMP_FILE_PREFIX = ".leanc-tmp-"


class Workspace:
    def __init__(self, root: str | os.PathLike[str]):
        root_path = Path(root)
        if not root_path.exists():
            raise WorkspaceError(f"workspace root does not exist: {root_path}")
        self.root = Path(os.path.realpath(root_path))

    def normalize(self, rel: str | os.PathLike[str]) -> str:
        """Validate a workspace-relative path and return it in POSIX form."""
        return normalize_relpath(rel)

    def resolve(self, rel: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
        """Resolve a relative path inside the workspace, rejecting symlinks."""
        norm = self.normalize(rel)
        self._reject_symlinked_components(norm, must_exist=must_exist)
        full = self.root / norm
        if full.parent.exists() and not _is_within(Path(os.path.realpath(full.parent)), self.root):
            raise WorkspaceError(f"path escapes the workspace: {norm}")
        return full

    def _reject_symlinked_components(self, norm: str, *, must_exist: bool) -> None:
        current = self.root
        for part in norm.split("/"):
            current = current / part
            status = _lstat_or_none(current)
            if status is None:
                if must_exist:
                    raise WorkspaceError(f"file not found in workspace: {norm}")
                return
            if stat.S_ISLNK(status.st_mode):
                raise WorkspaceError(f"symbolic links are not allowed in workspace paths: {norm}")

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

    def read_bytes(self, rel: str, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> bytes:
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

    def read_text(self, rel: str, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> str:
        data = self.read_bytes(rel, max_bytes)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{rel} is not valid UTF-8 (byte offset {exc.start}); convert it to UTF-8") from None

    def write_text(self, rel: str, content: str, *, mode: int | None = None) -> Path:
        return self.write_bytes(rel, content.encode("utf-8"), mode=mode)

    def write_bytes(self, rel: str, data: bytes, *, mode: int | None = None) -> Path:
        """Atomically replace ``rel``; ``mode=None`` keeps an existing file's permission bits (0644 for new files)."""
        target = self._resolve_again_after_creating_parents(rel)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise WorkspaceError(f"refusing to overwrite non-regular file: {rel}")
        if mode is None:
            mode = _existing_mode_or_default(target)
        fd, tmp = tempfile.mkstemp(prefix=TEMP_FILE_PREFIX, dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        finally:
            _remove_if_present(tmp)
        return target

    def _resolve_again_after_creating_parents(self, rel: str) -> Path:
        self.resolve(rel).parent.mkdir(parents=True, exist_ok=True)
        return self.resolve(rel)

    def copy_into(self, rels: list[str], dest_dir: str | os.PathLike[str]) -> dict[str, Path]:
        """Copy workspace files into ``dest_dir`` keeping their relative layout; returns rel -> destination path."""
        dest = Path(dest_dir)
        out: dict[str, Path] = {}
        for rel in rels:
            norm = self.normalize(rel)
            data = self.read_bytes(norm)
            destination = dest / norm
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            out[norm] = destination
        return out


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _existing_mode_or_default(target: Path) -> int:
    return (target.stat().st_mode & PERMISSION_BITS) if target.exists() else NEW_FILE_MODE


def _remove_if_present(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _is_absolute_or_drive_path(path: str) -> bool:
    has_drive_letter = len(path) > 1 and path[1] == ":"
    return path.startswith("/") or path.startswith("\\") or has_drive_letter


def normalize_relpath(rel: str | os.PathLike[str]) -> str:
    """Validate a relative path (not absolute, no ``..``) and return it in normalised POSIX form; pure string check."""
    s = os.fspath(rel)
    if not isinstance(s, str) or not s or "\x00" in s:
        raise WorkspaceError(f"invalid path {s!r}")
    if _is_absolute_or_drive_path(s):
        raise WorkspaceError(f"absolute paths are not allowed: {s!r}")
    clean: list[str] = []
    for part in PurePosixPath(s.replace("\\", "/")).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspaceError(f"path traversal ('..') is not allowed: {s!r}")
        clean.append(part)
    if not clean:
        raise WorkspaceError(f"invalid path {s!r}")
    return "/".join(clean)


def _is_within(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def safe_rmtree(path: str | os.PathLike[str], within: str | os.PathLike[str]) -> None:
    """Remove ``path`` only if it lies inside ``within`` (defence in depth)."""
    p = Path(os.path.realpath(path))
    w = Path(os.path.realpath(within))
    if p == w or not _is_within(p, w):
        raise WorkspaceError(f"refusing to delete {p}: not inside {w}")
    shutil.rmtree(p)
