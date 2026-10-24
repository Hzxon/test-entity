from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
HONEST = EXAMPLES / "discount-js"
CHEAT = EXAMPLES / "discount-js-cheat"
HIDDEN = EXAMPLES / "discount-js-hidden"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (HONEST / "node_modules").is_dir(),
    reason="node or examples/discount-js/node_modules missing (run npm ci)",
)

needs_adapter = pytest.mark.skipif(
    importlib.util.find_spec("testgate.languages.js") is None,
    reason="js adapter not built yet",
)


def _vitest(root: Path, subdir: str, out: Path) -> int:
    proc = subprocess.run(
        [
            NODE or "node",
            "node_modules/vitest/vitest.mjs",
            "run",
            "--root",
            str(root),
            "--dir",
            subdir,
            "--reporter=json",
            f"--outputFile={out}",
        ],
        check=False,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode


def _run_golden(example: Path, tmp_path: Path) -> int:
    return _vitest(example, "tests/golden", tmp_path / "out.json")


def _run_hidden(example: Path, tmp_path: Path) -> int:
    work = tmp_path / "work"
    shutil.copytree(example, work, ignore=shutil.ignore_patterns("node_modules", "coverage"))
    shutil.copytree(HIDDEN, work / "tests" / "hidden")
    (work / "node_modules").symlink_to(HONEST / "node_modules", target_is_directory=True)
    return _vitest(work, "tests/hidden", tmp_path / "out.json")


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


def test_golden_passes_on_honest_example(tmp_path):
    assert _run_golden(HONEST, tmp_path) == 0
    assert json.loads((tmp_path / "out.json").read_text())["numPassedTests"] == 5


def test_golden_passes_on_cheat_example(tmp_path):
    assert _run_golden(CHEAT, tmp_path) == 0


def test_hidden_passes_on_honest_example(tmp_path):
    assert _run_hidden(HONEST, tmp_path) == 0
    assert json.loads((tmp_path / "out.json").read_text())["numPassedTests"] == 9


def test_hidden_fails_on_cheat_example(tmp_path):
    assert _run_hidden(CHEAT, tmp_path) != 0


@needs_adapter
def test_honest_example_passes(tmp_path):
    code, out, _ = _run_cli(HONEST, tmp_path)
    assert code == 0
    assert json.loads(out.read_text())["verdict"] == "pass"


@needs_adapter
def test_cheat_example_fails_without_leaking_hidden_names(tmp_path):
    code, out, priv = _run_cli(CHEAT, tmp_path)
    assert code == 1
    public = out.read_text()
    gates = {g["name"]: g for g in json.loads(public)["gates"]}
    assert gates["hidden"]["status"] == "fail"
    names = re.findall(r'test\("([^"]+)"', (HIDDEN / "hidden.test.js").read_text())
    assert len(names) == 9
    for name in names:
        assert name not in public
    private = priv.read_text()
    assert "hidden.test.js" not in private
    for name in names:
        assert name not in private  # #51: hidden names stay out of the private log too
