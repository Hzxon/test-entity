from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
HONEST = EXAMPLES / "discount"
CHEAT = EXAMPLES / "discount-cheat"
HIDDEN = EXAMPLES / "discount-hidden"

_MODULES = ["cli", "config", "gitinfo", "sandbox", "runner", "mutator", "gates", "report"]


def _missing() -> bool:
    try:
        return any(importlib.util.find_spec(f"testgate.{m}") is None for m in _MODULES)
    except (ImportError, ValueError):
        return True


needs_cli = pytest.mark.skipif(_missing(), reason="testgate modules not built yet")


def _pytest(target: Path, src: Path) -> int:
    env = {**os.environ, "PYTHONPATH": str(src)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "-q", "-p", "no:cacheprovider"],
        check=False,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    ).returncode


def _run_cli(example: Path, tmp_path: Path) -> tuple[int, Path, Path]:
    from testgate.cli import main

    out, priv = tmp_path / "out.json", tmp_path / "priv.log"
    code = main(
        [
            "run",
            "--root",
            str(example),
            "--no-git",
            "--json",
            str(out),
            "--private-log",
            str(priv),
            "--quiet",
        ]
    )
    return code, out, priv


@needs_cli
def test_honest_example_passes(tmp_path):
    code, out, _ = _run_cli(HONEST, tmp_path)
    assert code == 0
    assert json.loads(out.read_text())["verdict"] == "pass"


@needs_cli
def test_cheat_example_fails_without_leaking_hidden_names(tmp_path):
    code, out, priv = _run_cli(CHEAT, tmp_path)
    assert code == 1
    public = out.read_text()
    gates = {g["name"]: g for g in json.loads(public)["gates"]}
    assert gates["hidden"]["status"] == "fail"
    names = re.findall(r"def (test_\w+)", (HIDDEN / "test_hidden.py").read_text())
    assert names
    for name in names:
        assert name not in public
    private = priv.read_text()
    assert any(name in private for name in names)


def test_golden_passes_on_honest_example():
    assert _pytest(HONEST / "tests" / "golden", HONEST / "src") == 0


def test_golden_passes_on_cheat_example():
    assert _pytest(CHEAT / "tests" / "golden", CHEAT / "src") == 0


def test_hidden_fails_on_cheat_and_passes_on_honest():
    assert _pytest(HIDDEN, CHEAT / "src") != 0
    assert _pytest(HIDDEN, HONEST / "src") == 0
