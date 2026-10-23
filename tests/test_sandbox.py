from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from testgate.core import ProjectConfig
from testgate.sandbox import Sandbox, build_sandbox, destroy, run_in_sandbox

EXPECTED_ENV_KEYS = {
    "PATH",
    "HOME",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONHASHSEED",
    "LANG",
    "LC_ALL",
    "TMPDIR",
}


@pytest.fixture
def project(tmp_path: Path) -> tuple[ProjectConfig, dict[str, Path]]:
    root = tmp_path / "proj"
    pkg = root / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    golden = root / "tests" / "golden"
    (golden / "__pycache__").mkdir(parents=True)
    (golden / "__pycache__" / "junk.pyc").write_text("x")
    (golden / ".hidden").mkdir()
    (golden / ".hidden" / "test_h.py").write_text("")
    (golden / "conftest.py").write_text("raise SystemExit('conftest loaded')\n")
    (golden / "test_calc.py").write_text("def test_ok():\n    assert True\n")
    (golden / "data.json").write_text("{}")
    cfg = ProjectConfig(root=root, package="src/mypkg", import_name="mypkg")
    return cfg, {"golden": golden}


@pytest.fixture
def sb(project: tuple[ProjectConfig, dict[str, Path]]):
    cfg, tests = project
    box = build_sandbox(cfg, sys.executable, tests)
    yield box
    destroy(box)


def test_layout(sb: Sandbox) -> None:
    assert sb.root.name.startswith("testgate-")
    assert (sb.src_root / "mypkg" / "calc.py").is_file()
    assert sb.test_dirs["golden"] == sb.root / "tests" / "golden"
    assert (sb.root / "tests" / "golden" / "test_calc.py").is_file()
    assert (sb.root / "tests" / "golden" / "data.json").is_file()
    assert (sb.root / "tmp").is_dir()


def test_excludes_conftest_cache_and_dot_dirs(sb: Sandbox) -> None:
    g = sb.root / "tests" / "golden"
    assert not (g / "conftest.py").exists()
    assert not (g / "__pycache__").exists()
    assert not (g / ".hidden").exists()


def test_package_renamed_to_import_name(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / "weird-dir").mkdir(parents=True)
    (root / "weird-dir" / "__init__.py").write_text("")
    cfg = ProjectConfig(root=root, package="weird-dir", import_name="named")
    box = build_sandbox(cfg, sys.executable, {})
    try:
        assert (box.src_root / "named" / "__init__.py").is_file()
    finally:
        destroy(box)


def test_overrides_written(project: tuple[ProjectConfig, dict[str, Path]]) -> None:
    cfg, tests = project
    box = build_sandbox(cfg, sys.executable, tests, overrides={"src/mypkg/calc.py": "X = 1\n"})
    try:
        assert (box.src_root / "mypkg" / "calc.py").read_text() == "X = 1\n"
        # original untouched
        assert "def add" in (cfg.root / "src" / "mypkg" / "calc.py").read_text()
    finally:
        destroy(box)


def test_env_is_scrubbed(
    project: tuple[ProjectConfig, dict[str, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESTGATE_CANARY", "leak")
    monkeypatch.setenv("TESTGATE_SEED", "s3cret")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    cfg, tests = project
    box = build_sandbox(cfg, sys.executable, tests)
    try:
        assert set(box.env) == EXPECTED_ENV_KEYS
        assert box.env["HOME"] == str(box.root)
        assert box.env["PYTHONPATH"] == f"{box.root}:{box.root}/src"
        assert box.env["TMPDIR"] == str(box.root / "tmp")
        assert box.env["PYTHONHASHSEED"] == "0"
        proc = run_in_sandbox(
            box,
            [sys.executable, "-c", "import os; print(os.environ.get('TESTGATE_CANARY'))"],
            30,
        )
        assert proc.stdout.strip() == "None"
    finally:
        destroy(box)


def test_pytest_ini(sb: Sandbox) -> None:
    text = (sb.root / "pytest.ini").read_text()
    assert text == "[pytest]\naddopts = -p no:cacheprovider --noconftest\ntestpaths = tests\n"


def test_network_blocked_by_default(sb: Sandbox) -> None:
    assert (sb.root / "sitecustomize.py").is_file()
    proc = run_in_sandbox(sb, [sys.executable, "-c", "import socket; socket.socket()"], 30)
    assert proc.returncode != 0
    assert "testgate: network disabled" in proc.stderr
    proc = run_in_sandbox(
        sb, [sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1', 9))"], 30
    )
    assert proc.returncode != 0
    assert "testgate: network disabled" in proc.stderr


def test_network_allowed_when_enabled(project: tuple[ProjectConfig, dict[str, Path]]) -> None:
    cfg, tests = project
    cfg.network = True
    box = build_sandbox(cfg, sys.executable, tests)
    try:
        assert not (box.root / "sitecustomize.py").exists()
        proc = run_in_sandbox(
            box, [sys.executable, "-c", "import socket; socket.socket().close()"], 30
        )
        assert proc.returncode == 0
    finally:
        destroy(box)


def test_run_in_sandbox_cwd_and_extra_env(sb: Sandbox) -> None:
    code = "import os; print(os.getcwd()); print(os.environ['EXTRA'])"
    proc = run_in_sandbox(sb, [sys.executable, "-c", code], 30, extra_env={"EXTRA": "v"})
    out = proc.stdout.split()
    assert os.path.realpath(out[0]) == os.path.realpath(sb.root)
    assert out[1] == "v"
    assert "EXTRA" not in sb.env


def test_timeout_returns_124(sb: Sandbox) -> None:
    proc = run_in_sandbox(sb, [sys.executable, "-c", "import time; time.sleep(30)"], 1)
    assert proc.returncode == 124
    assert proc.stderr == "testgate: timed out after 1s"


def test_destroy_removes_root(project: tuple[ProjectConfig, dict[str, Path]]) -> None:
    cfg, tests = project
    box = build_sandbox(cfg, sys.executable, tests)
    destroy(box)
    assert not box.root.exists()
    destroy(box)  # idempotent
