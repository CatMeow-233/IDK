from __future__ import annotations

import subprocess
from pathlib import Path

EXAMPLES_DIR = "examples"


class Git:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, check=False)

    def head(self, cwd: Path | None = None) -> str:
        return self._git("rev-parse", "HEAD", cwd=cwd).stdout.strip()

    def fetch(self, remote: str, branch: str) -> bool:
        refspec = f"+refs/heads/{branch}:{remote_ref(remote, branch)}"
        return self._git("fetch", "-q", remote, refspec).returncode == 0

    def resolve(self, ref: str) -> str:
        proc = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def is_clean(self, cwd: Path | None = None) -> bool:
        proc = self._git("status", "--porcelain", "--untracked-files=normal", cwd=cwd)
        return proc.returncode == 0 and not proc.stdout.strip()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._git("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0

    def example_trees(self, rev: str) -> dict[str, str]:
        proc = self._git("ls-tree", "-d", f"{rev}:{EXAMPLES_DIR}")
        entries = (line.split(None, 3) for line in proc.stdout.splitlines())
        return {name: tree for _, kind, tree, name in entries if kind == "tree"}

    def add_worktree(self, path: Path, sha: str) -> bool:
        return self._git("worktree", "add", "--detach", str(path), sha).returncode == 0

    def remove_worktree(self, path: Path) -> None:
        self._git("worktree", "remove", "--force", str(path))
        self._git("worktree", "prune")


def remote_ref(remote: str, branch: str) -> str:
    return f"refs/remotes/{remote}/{branch}"
