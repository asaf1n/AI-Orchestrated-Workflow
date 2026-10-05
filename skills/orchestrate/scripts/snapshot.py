"""Read-only fingerprint of a Git checkout, including untracked file contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any


def git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_file(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    if path.is_symlink():
        return {"path": name, "kind": "symlink", "sha256": sha256(os.fsencode(os.readlink(path)))}
    if path.is_file():
        return {"path": name, "kind": "file", "mode": stat.S_IMODE(path.stat().st_mode), "sha256": file_digest(path)}
    if not path.exists():
        return {"path": name, "kind": "deleted"}
    raise ValueError(f"Cannot fingerprint non-file entry (inspect submodules separately): {name}")


def snapshot(repo: Path) -> dict[str, Any]:
    root = Path(os.fsdecode(git_bytes(repo, "rev-parse", "--show-toplevel").strip()))
    head = git_bytes(root, "rev-parse", "HEAD").decode("ascii").strip()
    branch = os.fsdecode(git_bytes(root, "rev-parse", "--abbrev-ref", "HEAD").strip())
    status = git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    # Disable configured diff drivers: collecting evidence must not execute project helpers.
    diff_args = ("--binary", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none")
    worktree = git_bytes(root, "diff", *diff_args, "HEAD", "--")
    index = git_bytes(root, "diff", "--cached", *diff_args, "HEAD", "--")
    paths = git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    changed = git_bytes(root, "diff", "--name-only", "-z", "--no-renames", "HEAD", "--")
    names = sorted(set(part for part in (paths + changed).split(b"\0") if part))
    files = [describe_file(root, os.fsdecode(name)) for name in names]
    # Staging a new file must not change the identity of its actual contents.
    payload = json.dumps(files, sort_keys=True, ensure_ascii=True).encode("ascii")
    # A mixed snapshot could accept edits made while the evidence was being collected.
    if (
        git_bytes(root, "rev-parse", "HEAD").decode("ascii").strip() != head
        or git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") != status
        or git_bytes(root, "diff", *diff_args, "HEAD", "--") != worktree
        or git_bytes(root, "diff", "--cached", *diff_args, "HEAD", "--") != index
    ):
        raise ValueError("Checkout changed during snapshot; collect evidence again")
    for entry in files:
        if describe_file(root, entry["path"]) != entry:
            raise ValueError("File content changed during snapshot")
    return {
        "root": str(root),
        "head": head,
        "branch": branch,
        "clean": not status,
        "status": [os.fsdecode(item) for item in status.split(b"\0") if item],
        "worktree_sha256": sha256(payload),
        "index_sha256": sha256(index),
        "diff_sha256": sha256(worktree),
        "files": files,
        "coverage": "Changed and untracked file contents; ignored files, submodules and external state excluded",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    args = parser.parse_args()
    try:
        result = snapshot(args.repo)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Snapshot failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
