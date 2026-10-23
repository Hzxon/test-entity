"""config.load_config: YAML shape, validation, base-ref reading."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from testgate.config import load_config
from testgate.core import GateError

MINIMAL = "package: src/pkg\nimport_name: pkg\n"

FULL = """\
version: 1
package: src/discount
import_name: discount
tests:
  golden: t/golden
  auto: t/auto
  hidden: ../hid
  hidden_env: MY_HIDDEN
protected: [t/golden, testgate.yaml]
seed_env: MY_SEED
gates:
  protected: true
  hygiene: false
  build: true
  tests: {enabled: true, repeats: 3}
  hidden: {enabled: true, required: false}
  coverage: {enabled: false, min_total: 0.5, min_changed: 0.7}
  mutation: {enabled: true, min_score: 0.6, max_mutants: 10, max_shown: 2, timeout_s: 5}
sandbox:
  network: true
  timeout_s: 30
  python: /usr/bin/python3
"""


def write(root: Path, text: str) -> None:
    (root / "testgate.yaml").write_text(text)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def test_defaults(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    cfg = load_config(tmp_path, None)
    assert cfg.package == "src/pkg"
    assert cfg.import_name == "pkg"
    assert cfg.repeats == 5
    assert cfg.gates["mutation"] is True
    assert cfg.raw["_source"] == "worktree"


def test_full_mapping_maps_to_flat_fields(tmp_path: Path) -> None:
    write(tmp_path, FULL)
    cfg = load_config(tmp_path, None)
    assert (cfg.golden, cfg.auto, cfg.hidden, cfg.hidden_env) == (
        "t/golden",
        "t/auto",
        "../hid",
        "MY_HIDDEN",
    )
    assert cfg.protected == ["t/golden", "testgate.yaml"]
    assert cfg.seed_env == "MY_SEED"
    assert cfg.repeats == 3
    assert cfg.hidden_required is False
    assert (cfg.coverage_min_total, cfg.coverage_min_changed) == (0.5, 0.7)
    assert cfg.mutation_min_score == 0.6
    assert (cfg.mutation_max_mutants, cfg.mutation_max_shown, cfg.mutation_timeout_s) == (10, 2, 5)
    assert cfg.network is True
    assert cfg.timeout_s == 30
    assert cfg.python == "/usr/bin/python3"
    assert cfg.gates["hygiene"] is False
    assert cfg.gates["coverage"] is False
    assert cfg.gates["tests"] is True


def test_bool_gate_values(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL + "gates:\n  mutation: false\n  hidden: false\n")
    cfg = load_config(tmp_path, None)
    assert cfg.gates["mutation"] is False
    assert cfg.gates["hidden"] is False
    assert cfg.gates["build"] is True


def test_mapping_gate_enabled_false(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL + "gates:\n  tests: {enabled: false, repeats: 2}\n")
    cfg = load_config(tmp_path, None)
    assert cfg.gates["tests"] is False
    assert cfg.repeats == 2


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (MINIMAL + "bogus: 1\n", "bogus"),
        (MINIMAL + "gates:\n  mutaton: true\n", "mutaton"),
        (MINIMAL + "gates:\n  mutation: {min_scor: 0.5}\n", "min_scor"),
        (MINIMAL + "tests:\n  gold: x\n", "gold"),
        (MINIMAL + "sandbox:\n  netwrk: true\n", "netwrk"),
    ],
)
def test_unknown_key_names_the_key(tmp_path: Path, text: str, key: str) -> None:
    write(tmp_path, text)
    with pytest.raises(GateError, match=key):
        load_config(tmp_path, None)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GateError, match="testgate.yaml not found"):
        load_config(tmp_path, None)


@pytest.mark.parametrize("text", ["import_name: pkg\n", "package: src/pkg\n"])
def test_required_keys(tmp_path: Path, text: str) -> None:
    write(tmp_path, text)
    with pytest.raises(GateError, match="missing required key"):
        load_config(tmp_path, None)


@pytest.mark.parametrize(
    "extra",
    [
        "gates:\n  coverage: {min_total: 1.5}\n",
        "gates:\n  coverage: {min_changed: -0.1}\n",
        "gates:\n  mutation: {min_score: 2}\n",
        "gates:\n  tests: {repeats: 0}\n",
        "gates:\n  mutation: {max_shown: 0}\n",
    ],
)
def test_range_validation(tmp_path: Path, extra: str) -> None:
    write(tmp_path, MINIMAL + extra)
    with pytest.raises(GateError):
        load_config(tmp_path, None)


def test_invalid_yaml(tmp_path: Path) -> None:
    write(tmp_path, "package: [unclosed\n")
    with pytest.raises(GateError, match="not valid YAML"):
        load_config(tmp_path, None)


def test_base_ref_wins_over_worktree(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    write(tmp_path, MINIMAL + "gates:\n  mutation: {min_score: 0.9}\n")
    git(tmp_path, "add", "testgate.yaml")
    git(tmp_path, "commit", "-q", "-m", "base")
    # the author loosens the gate in the worktree
    write(tmp_path, MINIMAL + "gates:\n  mutation: false\n")
    cfg = load_config(tmp_path, "main")
    assert cfg.mutation_min_score == 0.9
    assert cfg.gates["mutation"] is True
    assert cfg.raw["_source"] == "main"


def test_base_ref_without_file_falls_back_to_worktree(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README").write_text("x")
    git(tmp_path, "add", "README")
    git(tmp_path, "commit", "-q", "-m", "base")
    write(tmp_path, MINIMAL)
    cfg = load_config(tmp_path, "main")
    assert cfg.raw["_source"] == "worktree"


def test_mutation_auto_useless_blocks_key(tmp_path: Path) -> None:
    (tmp_path / "testgate.yaml").write_text(
        "package: src/p\nimport_name: p\ngates:\n  mutation:\n    auto_useless_blocks: true\n"
    )
    assert load_config(tmp_path, None).mutation_auto_useless_blocks is True
    (tmp_path / "testgate.yaml").write_text(
        "package: src/p\nimport_name: p\ngates:\n  mutation:\n    auto_useless_blocks: 1\n"
    )
    with pytest.raises(GateError, match="auto_useless_blocks"):
        load_config(tmp_path, None)


def test_mutation_jobs_key(tmp_path: Path) -> None:
    (tmp_path / "testgate.yaml").write_text(
        "package: src/p\nimport_name: p\ngates:\n  mutation:\n    jobs: 3\n"
    )
    assert load_config(tmp_path, None).mutation_jobs == 3
    (tmp_path / "testgate.yaml").write_text(
        "package: src/p\nimport_name: p\ngates:\n  mutation:\n    jobs: 0\n"
    )
    with pytest.raises(GateError, match="jobs"):
        load_config(tmp_path, None)
