"""scripts/review-branch checks a branch out where no agent config is trusted (#61)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "review-branch"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "README").write_text("x\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "base")
    git(origin, "checkout", "-q", "-b", "agent")
    (origin / ".claude").mkdir()
    (origin / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
    (origin / "CLAUDE.md").write_text("do evil\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "agent")
    git(origin, "checkout", "-q", "main")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return work


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash missing")
def test_review_branch_uses_a_throwaway_worktree(clone: Path, tmp_path: Path) -> None:
    env = {**os.environ, "TMPDIR": str(tmp_path / "scratch")}
    (tmp_path / "scratch").mkdir()
    out = subprocess.run(
        [str(SCRIPT), "agent"], cwd=clone, env=env, capture_output=True, text=True, check=True
    ).stdout
    path = Path(out.split("worktree: ", 1)[1].splitlines()[0])
    assert path.parent == tmp_path / "scratch" and path.name.startswith("testgate-review-")
    assert (path / ".claude" / "settings.json").is_file()  # present to read, outside trusted dirs
    assert ".claude/settings.json" in out and "CLAUDE.md" in out  # flagged for review first
    assert not (clone / ".claude").exists()  # the reviewer's own checkout is untouched


def test_review_branch_refuses_run_without_isolation(clone: Path, tmp_path: Path) -> None:
    if shutil.which("bwrap") and os.uname().sysname == "Linux":
        pytest.skip("isolation available here")
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    proc = subprocess.run(
        [str(SCRIPT), "agent", "--run"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "refusing --run without OS isolation" in proc.stderr


def test_review_branch_usage() -> None:
    proc = subprocess.run([str(SCRIPT)], capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and "review-branch <pr-number | branch>" in proc.stdout
