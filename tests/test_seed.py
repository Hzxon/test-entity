"""The secret seed never reaches code under test; gates see derived seeds (#55)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from testgate import gates
from testgate.cli import main
from testgate.core import GateResult, ProjectConfig, RunContext, Status

SECRET = "s3cret-seed-0123456789abcdef"


def make_project(root: Path) -> Path:
    pkg = root / "src" / "tiny"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("def f(x):\n    return x + 1\n")
    golden = root / "tests" / "golden"
    golden.mkdir(parents=True)
    (golden / "test_g.py").write_text(
        "from tiny.mod import f\n\ndef test_f():\n    assert f(1) == 2\n"
    )
    hidden = root.parent / "hidden"
    hidden.mkdir()
    (hidden / "test_h.py").write_text(
        "from tiny.mod import f\n\ndef test_h():\n    assert f(2) == 3\n"
    )
    (root / "testgate.yaml").write_text(
        "package: src/tiny\nimport_name: tiny\ngates:\n  tests: {repeats: 1}\n"
        "  mutation: {jobs: 1}\n"
    )
    return hidden


def test_secret_never_reaches_a_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    hidden = make_project(root)
    monkeypatch.setenv("TESTGATE_SEED", SECRET)
    seen: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.Popen

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    out = tmp_path / "out.json"
    priv = tmp_path / "priv.log"
    argv = ["run", "--root", str(root), "--no-git", "--hidden", str(hidden)]
    main([*argv, "--json", str(out), "--private-log", str(priv), "--quiet"])
    sandbox_runs = [(a, e) for a, e in seen if e]  # git calls inherit the env; sandbox runs set it
    assert sandbox_runs
    for args, env in sandbox_runs:
        assert not any(SECRET in a for a in args)
        assert not any(SECRET in v for v in env.values())
    assert SECRET not in out.read_text()
    assert SECRET not in priv.read_text()
    assert "seed " in priv.read_text()  # derived seeds are logged for reproduction


def test_derived_seed_depends_on_head_and_gate() -> None:
    a = gates.derive_seed(SECRET, "a" * 40, "hidden")
    assert a != gates.derive_seed(SECRET, "b" * 40, "hidden")
    assert a != gates.derive_seed(SECRET, "a" * 40, "mutation")
    assert a == gates.derive_seed(SECRET, "a" * 40, "hidden")
    assert SECRET not in a


def test_gates_see_derived_seeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    got: dict[str, str] = {}

    def fake(ctx: RunContext) -> GateResult:
        got[ctx.head] = ctx.seed
        return GateResult("hidden", Status.PASS, "ok")

    monkeypatch.setitem(gates.GATES, "hidden", fake)
    cfg = ProjectConfig(root=tmp_path, package="src/x", import_name="x")
    for head in ("a" * 40, "b" * 40):
        ctx = RunContext(cfg, None, [], None, SECRET, True, "python", head=head)
        gates.run_gates(ctx, only=["hidden"])
    assert len(set(got.values())) == 2
    assert SECRET not in got.values()
