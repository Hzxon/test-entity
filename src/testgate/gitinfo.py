"""Git queries: base ref, changed lines, file content and hashes at a ref."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from testgate.core import ChangedFile

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_BASE_CANDIDATES = ("origin/main", "origin/master", "main", "master")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )


def is_git_repo(root: Path) -> bool:
    try:
        proc = _git(root, "rev-parse", "--is-inside-work-tree")
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def resolve_base(root: Path, base: str | None) -> str | None:
    """Return `base` if it exists, else the first default candidate that does."""
    if base is not None:
        return base if _ref_exists(root, base) else None
    for cand in _BASE_CANDIDATES:
        if _ref_exists(root, cand):
            return cand
    return None


def _parse_diff(diff: str) -> dict[str, set[int]]:
    """Map each .py file in a --unified=0 diff to its added/modified line numbers."""
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null" or not target.startswith("b/"):
                current = None
            else:
                path = target[2:]
                current = path if path.endswith(".py") else None
                if current is not None:
                    out.setdefault(current, set())
            continue
        if current is None:
            continue
        m = _HUNK.match(line)
        if m:
            start = int(m.group(1))
            count = 1 if m.group(2) is None else int(m.group(2))
            # count 0 is a pure deletion: no line exists on the + side
            out[current].update(range(start, start + count))
    return out


def changed_lines(root: Path, base_ref: str) -> list[ChangedFile]:
    """Lines added or modified in the worktree relative to merge-base(base_ref, HEAD)."""
    mb = _git(root, "merge-base", base_ref, "HEAD")
    if mb.returncode != 0:
        raise RuntimeError(f"git merge-base failed: {mb.stderr.strip()}")
    diff = _git(
        root, "diff", "--unified=0", "--no-color", "--no-ext-diff", "--relative", mb.stdout.strip()
    )
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed: {diff.stderr.strip()}")
    result = _parse_diff(diff.stdout)

    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    for rel in untracked.stdout.splitlines():
        if not rel.endswith(".py"):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n = len(text.splitlines())
        result[rel] = set(range(1, n + 1))

    return [ChangedFile(path=p, lines=ls) for p, ls in sorted(result.items()) if ls]


def show_file(root: Path, ref: str, path: str) -> str | None:
    proc = _git(root, "show", f"{ref}:./{path}")
    return proc.stdout if proc.returncode == 0 else None


def worktree_pairs(root: Path, path: str) -> list[tuple[str, str]]:
    """Sorted (path relative to `path`, blob sha) pairs for a worktree file or directory."""
    target = root / path
    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = [p for p in target.rglob("*") if p.is_file() and ".git" not in p.parts]
    else:
        return []
    pairs: list[tuple[str, str]] = []
    for f in files:
        proc = _git(root, "hash-object", "--", str(f))
        if proc.returncode != 0:
            raise RuntimeError(f"git hash-object failed: {proc.stderr.strip()}")
        rel = "" if f == target else f.relative_to(target).as_posix()
        pairs.append((rel, proc.stdout.strip()))
    return sorted(pairs)


def ref_pairs(root: Path, ref: str, path: str) -> list[tuple[str, str]]:
    """Same pairs as worktree_pairs, read from `git ls-tree -r <ref> <path>`."""
    clean = path.rstrip("/")
    proc = _git(root, "ls-tree", "-r", ref, "--", clean)
    if proc.returncode != 0:
        return []
    pairs: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        meta, _, name = line.partition("\t")
        fields = meta.split()
        if len(fields) != 3 or fields[1] != "blob":
            continue
        rel = "" if name == clean else name[len(clean) + 1 :]
        pairs.append((rel, fields[2]))
    return sorted(pairs)


def _digest(pairs: list[tuple[str, str]]) -> str:
    h = hashlib.sha1()
    for rel, sha in pairs:
        h.update(f"{rel}\0{sha}\n".encode())
    return h.hexdigest()


def tree_hash(root: Path, ref: str | None, path: str) -> str | None:
    """Comparable hash of `path` at `ref` (None = worktree), or None if absent.

    A single file hashes to its git blob sha on both sides. A directory hashes
    sorted (relative path, blob sha) pairs with sha1 on both sides, so a
    worktree hash equals the ref hash when contents match. It is NOT the git
    tree id.
    """
    if ref is None:
        target = root / path
        if not target.exists():
            return None
        if target.is_file():
            proc = _git(root, "hash-object", "--", str(target))
            return proc.stdout.strip() if proc.returncode == 0 else None
        return _digest(worktree_pairs(root, path))

    kind = _git(root, "cat-file", "-t", f"{ref}:./{path}")
    if kind.returncode != 0:
        return None
    if kind.stdout.strip() == "blob":
        return _git(root, "rev-parse", f"{ref}:./{path}").stdout.strip()
    return _digest(ref_pairs(root, ref, path))
