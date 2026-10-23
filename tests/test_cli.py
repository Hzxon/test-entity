"""cli: init, run, mutants, exit codes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("testgate.sandbox")
pytest.importorskip("testgate.runner")
pytest.importorskip("testgate.mutator")
pytest.importorskip("testgate.gitinfo")

from testgate.cli import main


def test_init_writes_files_and_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--package", "src/widget", "--import-name", "widget"]) == 0
    cfg = (tmp_path / "testgate.yaml").read_text()
    assert "package: src/widget" in cfg
    assert "import_name: widget" in cfg
    assert "__PACKAGE__" not in cfg
    example = (tmp_path / "tests/golden/test_example.py").read_text()
    assert "import widget" in example
    assert "from hypothesis import given, strategies as st" in example
    assert (tmp_path / "tests/auto/.gitkeep").exists()
    wf = (tmp_path / ".github/workflows/testgate.yml").read_text()
    assert "env.HIDDEN_REPO != ''" in wf
    out = capsys.readouterr().out
    assert "TESTGATE_SEED" in out
    assert "hidden tests repo" in out
    assert '"testgate" required check' in out


def test_init_language_js(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from testgate.config import load_config

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--language", "js"]) == 0
    cfg = load_config(tmp_path, None)
    assert (cfg.language, cfg.package, cfg.import_name) == ("js", "src", "index.js")
    assert "\nlanguage: js\n" in (tmp_path / "testgate.yaml").read_text()
    example = (tmp_path / "tests/golden/example.test.js").read_text()
    assert "../../src/index.js" in example
    assert "fast-check" in example
    assert not (tmp_path / "tests/golden/test_example.py").exists()


def test_init_python_config_keeps_language_commented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert "# language: python" in (tmp_path / "testgate.yaml").read_text()


def test_init_refuses_overwrite_then_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "testgate.yaml").write_text("mine\n")
    assert main(["init"]) == 2
    assert (tmp_path / "testgate.yaml").read_text() == "mine\n"
    assert not (tmp_path / ".github").exists()
    assert main(["init", "--force"]) == 0
    assert "package:" in (tmp_path / "testgate.yaml").read_text()


def test_init_template_config_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from testgate.config import load_config

    monkeypatch.chdir(tmp_path)
    assert main(["init", "--package", "pkg"]) == 0
    cfg = load_config(tmp_path, None)
    assert (cfg.package, cfg.import_name) == ("pkg", "pkg")
    assert cfg.hidden is None


def test_usage_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert main(["run", "--root", str(tmp_path), "--no-git"]) == 2
    assert "testgate.yaml not found" in capsys.readouterr().err
    (tmp_path / "testgate.yaml").write_text("package: p\nimport_name: p\nbogus: 1\n")
    assert main(["run", "--root", str(tmp_path), "--no-git"]) == 2
    assert "bogus" in capsys.readouterr().err
    assert main(["run", "--root", str(tmp_path), "--no-git", "--only", "nope"]) == 2


def test_run_without_base_ref_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=tmp_path, check=True)
    (tmp_path / "testgate.yaml").write_text("package: p\nimport_name: p\n")
    assert main(["run", "--root", str(tmp_path)]) == 2
    assert "no base ref found; pass --base or use --no-git" in capsys.readouterr().err


def make_project(root: Path, good: bool) -> Path:
    pkg = root / "src/calc"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    golden = root / "tests/golden"
    golden.mkdir(parents=True)
    (golden / "test_ops.py").write_text(
        "from calc.ops import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    hidden = root.parent / (root.name + "-hidden")
    hidden.mkdir()
    expected = "7" if good else "8"
    (hidden / "test_h.py").write_text(
        "from calc.ops import add\n\n\n"
        f"def test_hidden_secret_name():\n    assert add(3, 4) == {expected}\n"
    )
    (root / "testgate.yaml").write_text(
        "package: src/calc\nimport_name: calc\n"
        f"tests:\n  hidden: ../{hidden.name}\n"
        "gates:\n  tests: {repeats: 2}\n  coverage: {min_changed: 0.0}\n"
        "  mutation: {min_score: 0.0, timeout_s: 60}\n"
    )
    return hidden


@pytest.mark.parametrize(("good", "code"), [(True, 0), (False, 1)])
def test_run_no_git_reaches_verdict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    good: bool,
    code: int,
) -> None:
    monkeypatch.delenv("TESTGATE_SEED", raising=False)
    monkeypatch.delenv("TESTGATE_HIDDEN_DIR", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    make_project(root, good)
    json_path = tmp_path / "out.json"
    log_path = tmp_path / "private.log"
    rc = main(
        [
            "run",
            "--root",
            str(root),
            "--no-git",
            "--json",
            str(json_path),
            "--private-log",
            str(log_path),
        ]
    )
    out = capsys.readouterr().out
    assert rc == code
    assert ("testgate: PASS" if good else "testgate: FAIL") in out
    data = json.loads(json_path.read_text())
    assert data["verdict"] == ("pass" if good else "fail")
    assert data["seed_is_secret"] is False
    by_name = {g["name"]: g for g in data["gates"]}
    assert by_name["protected"]["status"] == "skip"
    assert "test_hidden_secret_name" not in json_path.read_text()
    assert "test_hidden_secret_name" not in out
    if not good:
        assert "test_hidden_secret_name" in log_path.read_text()


def test_run_quiet_and_step_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    make_project(root, True)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("TESTGATE_SEED", "abc")
    rc = main(["run", "--root", str(root), "--no-git", "--quiet", "--only", "build"])
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert "testgate: PASS" in summary.read_text()
    assert "seed_is_secret: true" in summary.read_text()


def test_mutants_lists_without_running_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    make_project(root, True)
    assert main(["mutants", "--root", str(root), "--no-git", "--max", "5"]) == 0
    out = capsys.readouterr().out
    assert "src/calc/ops.py:2:" in out
    assert " -> " in out


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_run_fails_when_coverage_drops_below_base(tmp_path: Path) -> None:
    """A change that deletes the test covering a branch drops coverage and fails the gate."""
    root = tmp_path / "proj"
    root.mkdir()
    hidden = make_project(root, good=True)
    ops = root / "src/calc/ops.py"
    ops.write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sign(x):\n    if x < 0:\n"
        "        return -1\n    return 1\n"
    )
    golden = root / "tests/golden/test_ops.py"
    golden.write_text(
        golden.read_text() + "\n\nfrom calc.ops import sign\n\n\n"
        "def test_sign():\n    assert sign(-5) == -1\n    assert sign(5) == 1\n"
    )
    (root / "testgate.yaml").write_text(
        "package: src/calc\nimport_name: calc\n"
        f"tests:\n  hidden: ../{hidden.name}\n"
        "protected: []\n"
        "gates:\n  tests: {repeats: 1}\n  coverage: {min_changed: 0.0, max_drop: 0.0}\n"
        "  mutation: false\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    # the "PR": drop the test of the negative branch
    golden.write_text(golden.read_text().replace("    assert sign(-5) == -1\n", ""))

    out = tmp_path / "out.json"
    code = main(["run", "--root", str(root), "--base", "main", "--json", str(out), "--quiet"])
    report = json.loads(out.read_text())
    coverage = next(g for g in report["gates"] if g["name"] == "coverage")
    assert coverage["status"] == "fail"
    assert "dropped" in coverage["summary"]
    assert coverage["baseline_total"] == 1.0
    assert coverage["drop"] > 0
    assert code == 1


def test_run_baseline_survives_hidden_tests_for_new_code(tmp_path: Path) -> None:
    """A hidden test that imports a module added by the PR is excluded at base, not fatal (#27)."""
    root = tmp_path / "proj"
    root.mkdir()
    hidden = make_project(root, good=True)
    (root / "testgate.yaml").write_text(
        "package: src/calc\nimport_name: calc\n"
        f"tests:\n  hidden: ../{hidden.name}\n"
        "protected: []\n"
        "gates:\n  tests: {repeats: 1}\n  coverage: {min_changed: 0.0, max_drop: 1.0}\n"
        "  mutation: false\n"
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    # the PR adds a module, and a hidden test for it lands alongside
    (root / "src/calc/extra.py").write_text("def triple(x):\n    return x * 3\n")
    (hidden / "test_extra.py").write_text(
        "from calc.extra import triple\n\n\ndef test_hidden_triple():\n    assert triple(2) == 6\n"
    )
    out = tmp_path / "out.json"
    code = main(["run", "--root", str(root), "--base", "main", "--json", str(out), "--quiet"])
    report = json.loads(out.read_text())
    coverage = next(g for g in report["gates"] if g["name"] == "coverage")
    assert "baseline_skipped" not in coverage
    assert coverage["baseline_hidden_excluded"] == 1
    assert coverage["baseline_total"] == 1.0
    assert coverage["drop"] is not None
    assert code == 0
