"""OS-level isolation of sandbox children with bubblewrap (#56)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from testgate import sandbox
from testgate.cli import main
from testgate.core import ProjectConfig
from testgate.sandbox import build_sandbox, destroy, run_in_sandbox

isolated = pytest.mark.skipif(
    not sandbox.isolation_available(), reason="bubblewrap unavailable (Linux CI only)"
)


@pytest.fixture
def sb(tmp_path: Path):
    pkg = tmp_path / "proj" / "src" / "tiny"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    cfg = ProjectConfig(root=tmp_path / "proj", package="src/tiny", import_name="tiny")
    box = build_sandbox(cfg, sys.executable, {})
    yield box
    destroy(box)


def test_wrap_is_a_no_op_without_bwrap(sb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_ISOLATION", [None])
    assert sandbox.wrap(sb, ["python", "-c", "1"]) == ["python", "-c", "1"]
    assert not sandbox.isolation_available()


def test_wrap_builds_the_bwrap_command(sb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_ISOLATION", ["/usr/bin/bwrap"])
    monkeypatch.setenv("RUNNER_TEMP", str(sb.root.parent))
    argv = sandbox.wrap(sb, ["python", "-c", "1"], binds={"/opt/x": False, "/opt/y": True})
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in argv and "--die-with-parent" in argv and "--unshare-pid" in argv
    assert argv[-4:] == ["--", "python", "-c", "1"]
    text = " ".join(argv)
    assert f"--tmpfs {Path.home()}" in text
    assert f"--tmpfs {sb.root.parent}" in text
    assert "--ro-bind /opt/x /opt/x" in text and "--bind /opt/y /opt/y" in text
    # the sandbox root is mounted back after the masks
    assert text.index(f"--bind {sb.root} {sb.root}") > text.index(f"--tmpfs {sb.root.parent}")
    assert "--unshare-net" not in sandbox.wrap(sb, ["x"], network=True)


def test_require_isolation_fails_without_bwrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_ISOLATION", [None])
    root = tmp_path / "proj"
    (root / "src" / "tiny").mkdir(parents=True)
    (root / "src" / "tiny" / "__init__.py").write_text("")
    (root / "tests" / "golden").mkdir(parents=True)
    (root / "testgate.yaml").write_text(
        "package: src/tiny\nimport_name: tiny\nsandbox:\n  require_isolation: true\n"
    )
    out = tmp_path / "out.json"
    assert main(["run", "--root", str(root), "--no-git", "--json", str(out), "--quiet"]) == 1
    report = json.loads(out.read_text())
    assert report["isolation"] == "best-effort"
    assert report["gates"][0] == {**report["gates"][0], "name": "isolation", "status": "fail"}
    assert all(g["status"] == "skip" for g in report["gates"][1:])


def _py(sb, code: str, *flags: str):
    return run_in_sandbox(sb, [sys.executable, *flags, "-c", code], 30)


@isolated
@pytest.mark.parametrize(
    "code, flags",
    [
        ("import _socket\ns = _socket.socket()\ns.settimeout(3)\ns.connect(('1.1.1.1', 53))", ()),
        ("import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)", ("-I",)),
        (
            "import subprocess\nsubprocess.run(['curl', '-sS', '-m', '3', 'https://example.com'], check=True)",
            (),
        ),
    ],
    ids=["_socket", "python -I", "curl"],
)
def test_network_is_unreachable(sb, code: str, flags: tuple[str, ...]) -> None:
    if "curl" in code and shutil.which("curl") is None:
        pytest.skip("curl missing")
    proc = _py(sb, code, *flags)
    assert proc.returncode != 0, proc.stdout + proc.stderr


@isolated
def test_home_and_runner_dirs_are_hidden(sb, tmp_path: Path) -> None:
    canaries = [Path.home() / f".testgate-canary-{uuid.uuid4().hex}"]
    for var in ("RUNNER_TEMP", "GITHUB_WORKSPACE"):
        if os.environ.get(var):
            canaries.append(Path(os.environ[var]) / f"testgate-canary-{uuid.uuid4().hex}")
    canaries.append(Path.cwd() / f".testgate-canary-{uuid.uuid4().hex}")
    try:
        for c in canaries:
            c.write_text("secret")
        for c in canaries:
            proc = _py(sb, f"print(open({str(c)!r}).read())")
            assert proc.returncode != 0 and "secret" not in proc.stdout, c
        ok = _py(sb, "open('inside.txt', 'w').write('x'); print(open('inside.txt').read())")
        assert ok.returncode == 0 and ok.stdout.strip() == "x"  # the sandbox root is writable
    finally:
        for c in canaries:
            c.unlink(missing_ok=True)
