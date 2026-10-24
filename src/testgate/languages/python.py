"""Python adapter: pytest, coverage.py and the AST mutator behind ``core.Language``."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from testgate import mutator, runner
from testgate.core import CoverageData, Mutant, ProjectConfig, Sandbox, TestRun
from testgate.sandbox import run_in_sandbox

_SITECUSTOMIZE = """\
import socket

_real_socket = socket.socket


# A subclass, not a function: ssl and others subclass socket.socket at import time.
class _BlockedSocket(_real_socket):
    def __init__(self, *args, **kwargs):
        raise OSError("testgate: network disabled")


def _blocked_connection(*args, **kwargs):
    raise OSError("testgate: network disabled")


socket.socket = _BlockedSocket
socket.create_connection = _blocked_connection
"""

_PYTEST_INI = "[pytest]\naddopts = -p no:cacheprovider --noconftest\ntestpaths = tests\n"


class PythonLanguage:
    name = "python"
    source_suffixes: tuple[str, ...] = (".py",)
    test_glob = "test_*.py"
    detection_tokens: tuple[str, ...] = (
        "PYTEST_CURRENT_TEST",
        "HYPOTHESIS_SEED",
        'sys.modules["pytest"]',
        "sys.modules['pytest']",
        "monkeypatch.setattr(pytest",
        'os.environ.get("CI"',
        "os.environ.get('CI'",
    )

    def package_dest(self, cfg: ProjectConfig) -> str:
        return f"src/{cfg.import_name}"

    def prepare_sandbox(self, sb: Sandbox, cfg: ProjectConfig, project_root: Path) -> None:
        (sb.root / "pytest.ini").write_text(_PYTEST_INI, encoding="utf-8")
        if not sb.network:
            (sb.root / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    def auto_file_ok(self, path: Path) -> bool:
        return path.name == "__init__.py" or (
            path.name.startswith("test_") and path.name.endswith(".py")
        )

    def build(
        self, sb: Sandbox, cfg: ProjectConfig, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        steps = [
            [sb.python, "-c", f"import {cfg.import_name}"],
            [sb.python, "-m", "compileall", "-q", "src"],
        ]
        proc: subprocess.CompletedProcess[str] | None = None
        for argv in steps:
            proc = run_in_sandbox(sb, argv, timeout_s)
            if proc.returncode != 0:
                return proc
        assert proc is not None
        return proc

    def run_tests(
        self,
        sb: Sandbox,
        names: list[str],
        timeout_s: int,
        seed: str,
        files: list[str] | None = None,
        fail_fast: bool = False,
    ) -> TestRun:
        return runner.run_pytest(
            sb, names, timeout_s, seed, extra_args=["-x"] if fail_fast else None, files=files
        )

    def run_coverage(
        self, sb: Sandbox, names: list[str], timeout_s: int, seed: str, repo_package: str
    ) -> tuple[TestRun, CoverageData]:
        return runner.run_coverage(sb, names, timeout_s, seed, sb.root, repo_package)

    def generate_mutants(
        self, path: str, source: str, lines: set[int], seed: str, max_mutants: int
    ) -> list[Mutant]:
        return mutator.generate_mutants(path, source, lines, seed, max_mutants)

    def failed_files(self, run: TestRun) -> set[str]:
        if run.failed_ids:  # node ids from the result channel: "tests/hidden/test_x.py::test_y"
            return {nid.split("::", 1)[0] for nid in run.failed_ids}
        # pytest -rA lines: "FAILED tests/hidden/test_x.py::test_y - ..." / "ERROR tests/hidden/test_x.py"
        return {m.group(1) for m in _FAILED_LINE.finditer(run.output)}


_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?\.py)(?:::|\s|$)", re.MULTILINE)
