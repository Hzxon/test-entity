"""Frozen config behaviour: the base ref wins, unknown keys are errors."""

import subprocess
from pathlib import Path

import pytest

from testgate.config import load_config
from testgate.core import GateError

YAML = "package: src/p\nimport_name: p\ngates:\n  mutation:\n    min_score: {score}\n"


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_config_is_read_from_the_base_ref_not_the_worktree(tmp_path: Path) -> None:
    (tmp_path / "testgate.yaml").write_text(YAML.format(score="0.9"))
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "testgate.yaml").write_text(YAML.format(score="0.0"))  # the PR weakens it
    cfg = load_config(tmp_path, "main")
    assert cfg.mutation_min_score == 0.9
    assert cfg.raw["_source"] == "main"


def test_unknown_key_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "testgate.yaml").write_text(
        "package: src/p\nimport_name: p\ngates:\n  mutatoin: false\n"
    )
    with pytest.raises(GateError, match="mutatoin"):
        load_config(tmp_path, None)


def test_defaults_keep_every_gate_on(tmp_path: Path) -> None:
    (tmp_path / "testgate.yaml").write_text("package: src/p\nimport_name: p\n")
    cfg = load_config(tmp_path, None)
    assert all(cfg.gates.values())
    assert cfg.repeats == 5 and cfg.hidden_required is True
