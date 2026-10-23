"""Language adapters: lookup and the Python adapter's behaviour."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from testgate.core import GateError, ProjectConfig
from testgate.languages import get_language
from testgate.languages.python import PythonLanguage
from testgate.sandbox import build_sandbox, destroy


def make_project(tmp_path: Path, init_body: str = "") -> ProjectConfig:
    pkg = tmp_path / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_body)
    tests = tmp_path / "tests" / "golden"
    tests.mkdir(parents=True)
    (tests / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tests / "test_b.py").write_text("def test_b():\n    assert False\n")
    return ProjectConfig(root=tmp_path, package="src/mypkg", import_name="mypkg")


def test_get_language_python() -> None:
    assert isinstance(get_language("python"), PythonLanguage)


def test_get_language_js_missing_ok() -> None:
    try:
        lang = get_language("js")
    except ModuleNotFoundError:
        pytest.fail("js.py absent must not break get_language outside the js path")
    except ImportError:
        pytest.skip("js adapter not importable yet")
    assert lang.name == "js"


def test_get_language_unknown() -> None:
    with pytest.raises(GateError, match="ruby"):
        get_language("ruby")


def test_package_dest() -> None:
    cfg = ProjectConfig(root=Path("."), package="lib/x", import_name="widget")
    assert PythonLanguage().package_dest(cfg) == "src/widget"


@pytest.mark.parametrize(
    ("name", "ok"),
    [("test_x.py", True), ("__init__.py", True), ("helper.py", False), ("test_x.txt", False)],
)
def test_auto_file_ok(name: str, ok: bool) -> None:
    assert PythonLanguage().auto_file_ok(Path(name)) is ok


@pytest.mark.parametrize("network", [False, True])
def test_prepare_sandbox_files(tmp_path: Path, network: bool) -> None:
    cfg = make_project(tmp_path)
    cfg.network = network
    sb = build_sandbox(cfg, sys.executable, {"golden": tmp_path / "tests/golden"})
    try:
        PythonLanguage().prepare_sandbox(sb, cfg, cfg.root)
        assert (sb.root / "pytest.ini").is_file()
        assert (sb.root / "sitecustomize.py").exists() is (not network)
    finally:
        destroy(sb)


def sandbox_for(cfg: ProjectConfig) -> Any:
    sb = build_sandbox(cfg, sys.executable, {"golden": cfg.root / "tests/golden"})
    PythonLanguage().prepare_sandbox(sb, cfg, cfg.root)
    return sb


def test_run_tests_with_files_passes_paths(tmp_path: Path) -> None:
    cfg = make_project(tmp_path)
    sb = sandbox_for(cfg)
    try:
        lang = PythonLanguage()
        only_a = lang.run_tests(sb, ["golden"], 60, "1", files=["tests/golden/test_a.py"])
        assert only_a.ok
        assert only_a.passed == 1
        both = lang.run_tests(sb, ["golden"], 60, "1")
        assert (both.passed, both.failed) == (1, 1)
    finally:
        destroy(sb)


def test_run_tests_fail_fast_adds_x(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake(*args: Any, **kwargs: Any) -> str:
        seen.append(kwargs)
        return "run"

    monkeypatch.setattr("testgate.languages.python.runner.run_pytest", fake)
    lang = PythonLanguage()
    lang.run_tests(None, ["golden"], 1, "s", fail_fast=True)  # type: ignore[arg-type]
    lang.run_tests(None, ["golden"], 1, "s", files=["a"])  # type: ignore[arg-type]
    assert seen[0] == {"extra_args": ["-x"], "files": None}
    assert seen[1] == {"extra_args": None, "files": ["a"]}


def test_build_ok(tmp_path: Path) -> None:
    cfg = make_project(tmp_path)
    sb = sandbox_for(cfg)
    try:
        proc = PythonLanguage().build(sb, cfg, 60)
        assert proc.returncode == 0
        assert "compileall" in " ".join(proc.args)
    finally:
        destroy(sb)


def test_build_returns_failing_process_on_bad_import(tmp_path: Path) -> None:
    cfg = make_project(tmp_path, "import not_a_real_module_xyz\n")
    sb = sandbox_for(cfg)
    try:
        proc: subprocess.CompletedProcess[str] = PythonLanguage().build(sb, cfg, 60)
        assert proc.returncode != 0
        assert "not_a_real_module_xyz" in proc.stderr
        assert "import mypkg" in " ".join(proc.args)
    finally:
        destroy(sb)
