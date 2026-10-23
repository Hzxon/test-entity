from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from testgate import gitinfo


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def commit_all(repo: Path, msg: str = "c") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "pkg").mkdir()
    (r / "pkg" / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    (r / "pkg" / "b.py").write_text("x = 1\n")
    (r / "notes.txt").write_text("hello\n")
    commit_all(r, "base")
    return r


def by_path(files: list) -> dict[str, set[int]]:
    return {f.path: f.lines for f in files}


def test_is_git_repo(repo: Path, tmp_path: Path) -> None:
    assert gitinfo.is_git_repo(repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not gitinfo.is_git_repo(plain)


def test_resolve_base_explicit_and_default(repo: Path) -> None:
    assert gitinfo.resolve_base(repo, "main") == "main"
    assert gitinfo.resolve_base(repo, "nope") is None
    assert gitinfo.resolve_base(repo, None) == "main"


def test_resolve_base_none_found(tmp_path: Path) -> None:
    r = tmp_path / "empty"
    r.mkdir()
    git(r, "init", "-q", "-b", "dev")
    assert gitinfo.resolve_base(r, None) is None


def test_changed_lines_modified_added_deleted(repo: Path) -> None:
    (repo / "pkg" / "a.py").write_text("one\nTWO\nthree\nfive\nsix\nseven\n")
    changed = by_path(gitinfo.changed_lines(repo, "main"))
    # line 2 modified, "four" deleted (no line), lines 5-6 added
    assert changed == {"pkg/a.py": {2, 5, 6}}


def test_changed_lines_pure_deletion_has_no_lines(repo: Path) -> None:
    (repo / "pkg" / "a.py").write_text("one\ntwo\nthree\nfive\n")
    assert gitinfo.changed_lines(repo, "main") == []


def test_changed_lines_single_line_hunk_without_count(repo: Path) -> None:
    (repo / "pkg" / "b.py").write_text("x = 2\n")
    assert by_path(gitinfo.changed_lines(repo, "main")) == {"pkg/b.py": {1}}


def test_changed_lines_only_py_files(repo: Path) -> None:
    (repo / "notes.txt").write_text("changed\n")
    assert gitinfo.changed_lines(repo, "main") == []


def test_changed_lines_untracked_py_all_lines(repo: Path) -> None:
    (repo / "pkg" / "new.py").write_text("a = 1\nb = 2\nc = 3\n")
    (repo / "pkg" / "new.txt").write_text("ignored\n")
    assert by_path(gitinfo.changed_lines(repo, "main")) == {"pkg/new.py": {1, 2, 3}}


def test_changed_lines_deleted_file_skipped(repo: Path) -> None:
    (repo / "pkg" / "b.py").unlink()
    assert gitinfo.changed_lines(repo, "main") == []


def test_changed_lines_uses_merge_base_and_includes_commits(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "pkg" / "b.py").write_text("x = 1\ny = 2\n")
    commit_all(repo, "feature")
    # main advances; merge-base stays at the original commit
    git(repo, "checkout", "-q", "main")
    (repo / "pkg" / "a.py").write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
    commit_all(repo, "main moves")
    git(repo, "checkout", "-q", "feature")
    (repo / "pkg" / "b.py").write_text("x = 1\ny = 2\nz = 3\n")  # uncommitted
    assert by_path(gitinfo.changed_lines(repo, "main")) == {"pkg/b.py": {2, 3}}


def test_show_file(repo: Path) -> None:
    assert gitinfo.show_file(repo, "main", "notes.txt") == "hello\n"
    assert gitinfo.show_file(repo, "main", "missing.txt") is None


def test_tree_hash_file_matches_ref_and_changes(repo: Path) -> None:
    assert gitinfo.tree_hash(repo, None, "notes.txt") == gitinfo.tree_hash(
        repo, "main", "notes.txt"
    )
    (repo / "notes.txt").write_text("edited\n")
    assert gitinfo.tree_hash(repo, None, "notes.txt") != gitinfo.tree_hash(
        repo, "main", "notes.txt"
    )


def test_tree_hash_dir_equal_when_unchanged(repo: Path) -> None:
    h = gitinfo.tree_hash(repo, None, "pkg")
    assert h is not None
    assert h == gitinfo.tree_hash(repo, "main", "pkg")
    assert h == gitinfo.tree_hash(repo, "main", "pkg/")


def test_tree_hash_dir_differs_on_edit_add_remove(repo: Path) -> None:
    base = gitinfo.tree_hash(repo, "main", "pkg")
    (repo / "pkg" / "b.py").write_text("x = 99\n")
    assert gitinfo.tree_hash(repo, None, "pkg") != base
    (repo / "pkg" / "b.py").write_text("x = 1\n")
    assert gitinfo.tree_hash(repo, None, "pkg") == base
    (repo / "pkg" / "extra.py").write_text("")
    assert gitinfo.tree_hash(repo, None, "pkg") != base
    (repo / "pkg" / "extra.py").unlink()
    (repo / "pkg" / "b.py").unlink()
    assert gitinfo.tree_hash(repo, None, "pkg") != base


def test_tree_hash_nested_dir(repo: Path) -> None:
    (repo / "pkg" / "sub").mkdir()
    (repo / "pkg" / "sub" / "c.py").write_text("c = 1\n")
    commit_all(repo, "nested")
    assert gitinfo.tree_hash(repo, None, "pkg") == gitinfo.tree_hash(repo, "HEAD", "pkg")


def test_tree_hash_absent(repo: Path) -> None:
    assert gitinfo.tree_hash(repo, None, "nothing") is None
    assert gitinfo.tree_hash(repo, "main", "nothing") is None


def test_pairs_agree(repo: Path) -> None:
    assert gitinfo.worktree_pairs(repo, "pkg") == gitinfo.ref_pairs(repo, "main", "pkg")
    assert [p for p, _ in gitinfo.worktree_pairs(repo, "pkg")] == ["a.py", "b.py"]
