from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from testgate import runner
from testgate.core import ProjectConfig
from testgate.languages.python import PythonLanguage
from testgate.sandbox import Sandbox, build_sandbox, destroy


def make_project(
    tmp_path: Path, test_files: dict[str, dict[str, str]]
) -> tuple[ProjectConfig, dict[str, Path]]:
    root = tmp_path / "proj"
    pkg = root / "src" / "tiny"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("def f(x):\n    if x > 0:\n        return 1\n    return 2\n")
    tests: dict[str, Path] = {}
    for name, files in test_files.items():
        d = root / "tests" / name
        d.mkdir(parents=True)
        for fname, body in files.items():
            (d / fname).write_text(body)
        tests[name] = d
    cfg = ProjectConfig(root=root, package="src/tiny", import_name="tiny")
    return cfg, tests


@pytest.fixture
def make_sb(tmp_path: Path) -> Iterator:
    boxes: list[Sandbox] = []

    def factory(test_files: dict[str, dict[str, str]]) -> tuple[Sandbox, ProjectConfig]:
        cfg, tests = make_project(tmp_path, test_files)
        box = build_sandbox(cfg, sys.executable, tests)
        PythonLanguage().prepare_sandbox(box, cfg, cfg.root)
        boxes.append(box)
        return box, cfg

    yield factory
    for b in boxes:
        destroy(b)


PASSING = {
    "test_a.py": "def test_one():\n    assert True\n\ndef test_two():\n    assert 1 + 1 == 2\n"
}
MIXED = {
    "test_b.py": (
        "import pytest\n"
        "def test_ok():\n    assert True\n"
        "def test_bad():\n    assert 1 == 2\n"
        "@pytest.mark.skip\ndef test_skipped():\n    pass\n"
        "def test_err(missing_fixture):\n    pass\n"
    )
}


def test_run_pytest_passing(make_sb) -> None:
    sb, _ = make_sb({"golden": PASSING})
    r = runner.run_pytest(sb, ["golden"], 60, None)
    assert r.ok
    assert (r.exit_code, r.passed, r.failed, r.errors, r.skipped) == (0, 2, 0, 0, 0)
    assert not r.timed_out
    assert "PASSED" in r.output


def test_run_pytest_failing_counts(make_sb) -> None:
    sb, _ = make_sb({"golden": MIXED})
    r = runner.run_pytest(sb, ["golden"], 60, None)
    assert not r.ok
    assert r.exit_code == 1
    assert (r.passed, r.failed, r.errors, r.skipped) == (1, 1, 1, 1)


def test_run_pytest_no_tests_is_not_ok(make_sb) -> None:
    sb, _ = make_sb({"golden": {"test_empty.py": "x = 1\n"}})
    r = runner.run_pytest(sb, ["golden"], 60, None)
    assert r.exit_code == 5
    assert r.passed == 0
    assert not r.ok


def test_run_pytest_timeout(make_sb) -> None:
    slow = {"test_slow.py": "import time\ndef test_slow():\n    time.sleep(30)\n"}
    sb, _ = make_sb({"golden": slow})
    r = runner.run_pytest(sb, ["golden"], 2, None)
    assert r.timed_out
    assert r.exit_code == 124
    assert not r.ok


def test_run_pytest_extra_args_and_multiple_dirs(make_sb) -> None:
    sb, _ = make_sb({"golden": MIXED, "auto": PASSING})
    r = runner.run_pytest(sb, ["golden", "auto"], 60, None, extra_args=["-x"])
    # -x stops at the first failure; dirs run in the order given
    assert r.failed == 1
    assert r.passed == 1
    r2 = runner.run_pytest(sb, ["auto"], 60, None)
    assert r2.passed == 2


def test_conftest_not_loaded(tmp_path: Path, make_sb) -> None:
    files = dict(PASSING)
    files["conftest.py"] = "raise RuntimeError('loaded')\n"
    sb, _ = make_sb({"golden": files})
    assert runner.run_pytest(sb, ["golden"], 60, None).ok


def test_hypothesis_seed_flag_added_and_cached(make_sb, monkeypatch: pytest.MonkeyPatch) -> None:
    sb, _ = make_sb({"golden": PASSING})
    seen: list[list[str]] = []
    orig = runner.run_in_sandbox

    def spy(box, argv, timeout_s, extra_env=None):
        seen.append(argv)
        return orig(box, argv, timeout_s, extra_env)

    monkeypatch.setattr(runner, "run_in_sandbox", spy)
    r = runner.run_pytest(sb, ["golden"], 60, "1234")
    assert r.ok
    assert "--hypothesis-seed=1234" in seen[-1]
    assert seen[-1][:6] == [sb.python, "-m", "pytest", "-q", "--tb=short", "-rA"]
    assert "tests/golden" in seen[-1]
    n_before = len(seen)
    runner.run_pytest(sb, ["golden"], 60, "1234")
    # second call: no extra hypothesis probe, only the pytest run
    assert len(seen) == n_before + 1
    runner.run_pytest(sb, ["golden"], 60, None)
    assert not any(a.startswith("--hypothesis-seed") for a in seen[-1])


def test_hypothesis_flag_skipped_when_unimportable(make_sb) -> None:
    sb, _ = make_sb({"golden": PASSING})
    runner._HYPOTHESIS[str(sb.root)] = False
    argv = runner._pytest_args(sb, ["golden"], "7", None)
    assert not any(a.startswith("--hypothesis-seed") for a in argv)


def test_run_coverage_maps_paths(make_sb) -> None:
    tests = {"test_f.py": "from tiny.mod import f\n\ndef test_f():\n    assert f(1) == 1\n"}
    sb, cfg = make_sb({"golden": tests})
    run, cov = runner.run_coverage(sb, ["golden"], 60, None, cfg.root, "src/tiny")
    assert run.ok
    path = "src/tiny/mod.py"
    assert path in cov.statements
    assert cov.statements[path] == {1, 2, 3, 4}
    assert cov.covered[path] == {1, 2, 3}
    assert all(not p.startswith("tests/") for p in cov.covered)
    assert 0 < cov.total() < 1
    # nothing written into the project tree
    assert not (cfg.root / ".coverage").exists()
    assert (sb.root / ".coverage").exists()


def test_run_coverage_failing_tests_still_returns_data(make_sb) -> None:
    tests = {"test_f.py": "from tiny.mod import f\n\ndef test_f():\n    assert f(1) == 99\n"}
    sb, cfg = make_sb({"golden": tests})
    run, cov = runner.run_coverage(sb, ["golden"], 60, None, cfg.root, "src/tiny")
    assert not run.ok
    assert run.failed == 1
    assert cov.covered["src/tiny/mod.py"] == {1, 2, 3}
