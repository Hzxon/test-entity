"""The harness talks to test processes only through the result channel module."""

from __future__ import annotations

import ast
from pathlib import Path

from testgate import runner

SRC = Path(__file__).resolve().parents[1] / "src" / "testgate"
REMOTE = SRC / "remote"
PROCESS_ONLY = {"_testgate_wire", "_testgate_oracle", "_testgate_worker"}


def imported_names(path: Path) -> set[str]:
    """Every dotted segment an import statement in ``path`` names."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
    return names


def test_harness_imports_only_the_channel() -> None:
    harness = [p for p in SRC.rglob("*.py") if REMOTE not in p.parents]
    assert SRC / "runner.py" in harness
    offenders = {p.relative_to(SRC).as_posix(): imported_names(p) & PROCESS_ONLY for p in harness}
    assert {p: n for p, n in offenders.items() if n} == {}
    channel_users = {
        p.relative_to(SRC).as_posix() for p in harness if "_testgate_channel" in imported_names(p)
    }
    assert channel_users == {"runner.py"}


def test_every_runtime_module_is_copied() -> None:
    modules = {p.stem for p in REMOTE.glob("_testgate_*.py")}
    assert modules == set(runner.ORACLE_MODULES) | set(runner.PACKAGE_MODULES)
