"""Fresh temp root holding only the package and chosen tests, with a scrubbed env."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from testgate.core import ProjectConfig

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


@dataclass
class Sandbox:
    root: Path  # temp dir
    src_root: Path  # <root>/src, package copied under here
    test_dirs: dict[str, Path]  # name -> <root>/tests/<name>
    python: str
    env: dict[str, str]
    network: bool


def _ignore_tests(_dir: str, names: list[str]) -> set[str]:
    return {
        n
        for n in names
        if n in ("conftest.py", "__pycache__", ".pytest_cache") or n.startswith(".")
    }


def _ignore_package(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__"}


def build_sandbox(
    config: ProjectConfig,
    python: str,
    tests: dict[str, Path],
    overrides: dict[str, str] | None = None,
) -> Sandbox:
    root = Path(tempfile.mkdtemp(prefix="testgate-"))
    src_root = root / "src"
    pkg_dst = src_root / config.import_name
    shutil.copytree(config.root / config.package, pkg_dst, ignore=_ignore_package)

    test_dirs: dict[str, Path] = {}
    for name, src in tests.items():
        dst = root / "tests" / name
        shutil.copytree(src, dst, ignore=_ignore_tests)
        test_dirs[name] = dst

    for rel_path, text in (overrides or {}).items():
        # "<config.package>/x.py" -> "<root>/src/<import_name>/x.py"
        rel = Path(rel_path).relative_to(config.package)
        target = pkg_dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    tmp = root / "tmp"
    tmp.mkdir()
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root),
        "PYTHONPATH": f"{root}:{src_root}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(tmp),
    }

    (root / "pytest.ini").write_text(_PYTEST_INI, encoding="utf-8")
    if not config.network:
        (root / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")

    return Sandbox(
        root=root,
        src_root=src_root,
        test_dirs=test_dirs,
        python=python,
        env=env,
        network=config.network,
    )


def run_in_sandbox(
    sb: Sandbox,
    argv: list[str],
    timeout_s: int,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**sb.env, **(extra_env or {})}
    try:
        return subprocess.run(
            argv,
            check=False,
            cwd=sb.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, stdout="", stderr=f"testgate: timed out after {timeout_s}s"
        )


def destroy(sb: Sandbox) -> None:
    shutil.rmtree(sb.root, ignore_errors=True)
