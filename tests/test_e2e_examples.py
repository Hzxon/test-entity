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
    report = json.loads(out.read_text())
    assert report["verdict"] == "pass"
    mutation = next(g for g in report["gates"] if g["name"] == "mutation")
    # every mutant is killed, so there is nothing left to credit the agent's test with
    assert mutation["status"] == "pass"
    assert mutation["survivors"] == []
    assert mutation["auto_checked"] == 0


@needs_cli
def test_cheat_example_fails_without_leaking_hidden_names(tmp_path, capsys, monkeypatch):
    from testgate.cli import main

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out, priv = tmp_path / "out.json", tmp_path / "priv.log"
    argv = ["run", "--root", str(CHEAT), "--no-git", "--json", str(out), "--private-log", str(priv)]
    code = main(argv)
    assert code == 1
    public = out.read_text()
    gates = {g["name"]: g for g in json.loads(public)["gates"]}
    assert gates["hidden"]["status"] == "fail"
    source = (HIDDEN / "test_hidden.py").read_text()
    names = re.findall(r"def (test_\w+)", source)
    assert names
    asserts = [line.strip() for line in source.splitlines() if line.strip().startswith("assert")]
    assert asserts
    captured = capsys.readouterr()
    # #51: no hidden id, path or source line in any output the author can read
    for text in (public, priv.read_text(), summary.read_text(), captured.out, captured.err):
        assert "test_hidden.py" not in text
        for name in names:
            assert name not in text
        for line in asserts:
            assert line not in text


def test_golden_passes_on_honest_example():
    assert _pytest(HONEST / "tests" / "golden", HONEST / "src") == 0


def test_golden_passes_on_cheat_example():
    assert _pytest(CHEAT / "tests" / "golden", CHEAT / "src") == 0


def test_hidden_fails_on_cheat_and_passes_on_honest():
    assert _pytest(HIDDEN, CHEAT / "src") != 0
    assert _pytest(HIDDEN, HONEST / "src") == 0
