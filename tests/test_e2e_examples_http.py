"""The HTTP example pair: the honest service passes, the cheat fails on hidden fixtures."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from testgate.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
HONEST = EXAMPLES / "shop-http"
CHEAT = EXAMPLES / "shop-http-cheat"
HIDDEN = EXAMPLES / "shop-http-hidden"

pytestmark = pytest.mark.skipif(shutil.which("python3") is None, reason="python3 not on PATH")


def _run_cli(example: Path, tmp_path: Path) -> tuple[int, Path, Path]:
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


def test_honest_http_example_passes(tmp_path: Path) -> None:
    code, out, _ = _run_cli(HONEST, tmp_path)
    report = json.loads(out.read_text())
    gates = {g["name"]: g for g in report["gates"]}
    assert report["verdict"] == "pass", gates
    assert gates["tests"]["passed"] == 7  # 6 golden + 1 auto
    assert gates["hidden"]["passed"] == 7 and gates["hidden"]["failed"] == 0
    assert gates["coverage"]["total"] == 1.0  # every documented route is exercised
    assert gates["mutation"]["status"] == "skip"  # no base ref under --no-git
    assert code == 0


def test_cheat_http_example_fails_without_leaking_hidden_names(tmp_path: Path) -> None:
    code, out, priv = _run_cli(CHEAT, tmp_path)
    public = out.read_text()
    gates = {g["name"]: g for g in json.loads(public)["gates"]}
    assert gates["tests"]["status"] == "pass"  # the golden inputs are all it knows
    assert gates["hidden"]["status"] == "fail"
    assert gates["hidden"]["failed"] >= 3
    names = re.findall(r"^- name: (.+)$", (HIDDEN / "shop.http.yaml").read_text(), re.MULTILINE)
    assert len(names) == 7
    for name in names:
        assert name not in public
    private = priv.read_text()
    assert "tests/hidden/" not in private
    for name in names:
        assert name not in private  # #51: hidden names stay out of the private log too
    assert code == 1


COMPOSE = EXAMPLES / "shop-http-compose"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not on PATH")
def test_compose_http_example_passes(tmp_path: Path) -> None:
    code, out, _ = _run_cli(COMPOSE, tmp_path)
    report = json.loads(out.read_text())
    gates = {g["name"]: g for g in report["gates"]}
    assert report["verdict"] == "pass", gates
    assert gates["build"]["status"] == "pass"
    assert gates["hidden"]["passed"] == 7
    assert code == 0
