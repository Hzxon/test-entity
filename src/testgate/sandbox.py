"""Fresh temp root holding only the package and chosen tests, with a scrubbed env."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from testgate.core import ProjectConfig, Sandbox


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
    dest: str | None = None,
) -> Sandbox:
    # resolve(): macOS puts temp dirs behind the /var -> /private/var symlink, and tools that
    # realpath imported files (vitest coverage) would otherwise miss the include glob.
    root = Path(tempfile.mkdtemp(prefix="testgate-")).resolve()
    src_root = root / (dest if dest is not None else f"src/{config.import_name}")
    shutil.copytree(config.root / config.package, src_root, ignore=_ignore_package)

    test_dirs: dict[str, Path] = {}
    for name, src in tests.items():
        dst = root / "tests" / name
        shutil.copytree(src, dst, ignore=_ignore_tests)
        test_dirs[name] = dst

    for rel_path, text in (overrides or {}).items():
        # "<config.package>/x.py" -> "<root>/<dest>/x.py"
        rel = Path(rel_path).relative_to(config.package)
        target = src_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    tmp = root / "tmp"
    tmp.mkdir()
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root),
        "PYTHONPATH": f"{root}:{root / 'src'}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(tmp),
    }

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
