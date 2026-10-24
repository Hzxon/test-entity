"""The verdict comes from a runner-owned result channel, never the child exit code (#53)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from testgate import runner
from testgate.cli import main
from testgate.core import ProjectConfig
from testgate.languages.python import PythonLanguage
from testgate.sandbox import build_sandbox, destroy

# Forces exit status 0 after pytest has reported its failures.
ATEXIT_TRICK = "import atexit, os\natexit.register(lambda: os._exit(0))\n"
SOURCE = "def f(x):\n    return x + 1\n"
TESTS = "from tiny.mod import f\n\ndef test_f():\n    assert f(1) == 2\n"


def make_project(root: Path, init: str, source: str, golden: str, hidden: str) -> Path:
    pkg = root / "src" / "tiny"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init)
    (pkg / "mod.py").write_text(source)
    (root / "tests" / "golden").mkdir(parents=True)
    (root / "tests" / "golden" / "test_g.py").write_text(golden)
    (root / "tests" / "auto").mkdir()
    hidden_dir = root.parent / "hidden"
    hidden_dir.mkdir()
    (hidden_dir / "test_h.py").write_text(hidden)
    (root / "testgate.yaml").write_text(
        "package: src/tiny\nimport_name: tiny\n"
        "gates:\n  hygiene: false\n  tests: {repeats: 1}\n"
        "  coverage: {min_changed: 0.0}\n  mutation: {min_score: 1.0, jobs: 2}\n"
    )
    return hidden_dir


def run_gates(tmp_path: Path, init: str, source: str, golden: str, hidden: str) -> dict:
    root = tmp_path / "proj"
    hidden_dir = make_project(root, init, source, golden, hidden)
    out = tmp_path / "out.json"
    main(
        [
            "run",
            "--root",
            str(root),
            "--no-git",
            "--hidden",
            str(hidden_dir),
            "--json",
            str(out),
            "--quiet",
        ]
    )
    report = json.loads(out.read_text())
    return {g["name"]: g for g in report["gates"]}


def sandbox_for(tmp_path: Path, init: str, golden: str):
    root = tmp_path / "proj"
    make_project(root, init, SOURCE, golden, TESTS)
    cfg = ProjectConfig(root=root, package="src/tiny", import_name="tiny")
    sb = build_sandbox(cfg, sys.executable, {"golden": root / "tests" / "golden"})
    PythonLanguage().prepare_sandbox(sb, cfg, root)
    return sb


def test_atexit_os_exit_cannot_pass_failing_tests(tmp_path: Path) -> None:
    failing = "from tiny.mod import f\n\ndef test_f():\n    assert f(1) == 99\n"
    sb = sandbox_for(tmp_path, ATEXIT_TRICK, failing)
    try:
        run = runner.run_pytest(sb, ["golden"], 60, None)
    finally:
        destroy(sb)
    assert run.exit_code == 0  # the trick works on the exit code...
    assert run.reported_ok is False  # ...but not on the channel
    assert not run.ok
    assert run.failed == 1
    assert run.failed_ids == ["tests/golden/test_g.py::test_f"]


def test_os_exit_inside_a_test_is_a_shortfall(tmp_path: Path) -> None:
    exits = "import os\n\ndef test_a():\n    os._exit(0)\n\ndef test_b():\n    assert False\n"
    sb = sandbox_for(tmp_path, "", exits)
    try:
        run = runner.run_pytest(sb, ["golden"], 60, None)
    finally:
        destroy(sb)
    assert run.exit_code == 0
    assert not run.ok


def test_fake_summary_line_does_not_count(tmp_path: Path) -> None:
    fake = "def test_a():\n    print('=== 5 passed in 0.01s ===')\n    assert False\n"
    sb = sandbox_for(tmp_path, "", fake)
    try:
        run = runner.run_pytest(sb, ["golden"], 60, None)
    finally:
        destroy(sb)
    assert (run.passed, run.failed) == (0, 1)
    assert not run.ok


def test_honest_run_reports_through_the_channel(tmp_path: Path) -> None:
    sb = sandbox_for(tmp_path, "", TESTS)
    try:
        run = runner.run_pytest(sb, ["golden"], 60, None)
    finally:
        destroy(sb)
    assert run.ok
    assert run.reported_ok is True
    assert (run.passed, run.failed, run.errors) == (1, 0, 0)


def test_missing_channel_is_not_ok() -> None:
    run = runner._make_run(0, "=== 3 passed in 0.1s ===", 0.1, channel="")
    assert not run.ok


COLLECTED_1 = {"t": "collected", "n": 1}
PASSED_A = {"t": "report", "id": "a", "when": "call", "outcome": "passed"}
END = {"t": "end", "status": 0}


@pytest.mark.parametrize(
    "records, ok",
    [
        ([COLLECTED_1, PASSED_A, END], True),
        ([COLLECTED_1, PASSED_A], False),  # no end marker
        ([{"t": "collected", "n": 2}, PASSED_A, END], False),  # fewer reported than collected
        (
            [COLLECTED_1, PASSED_A, {**PASSED_A, "when": "teardown", "outcome": "failed"}, END],
            False,
        ),  # teardown error after a passing call
        ([{"t": "collect_error", "id": "tests/x.py"}, {"t": "collected", "n": 0}, END], False),
    ],
)
def test_parse_channel(records: list[dict[str, object]], ok: bool) -> None:
    text = "garbage\n" + "".join(json.dumps(r) + "\n" for r in records)
    parsed = runner._parse_channel(text)
    assert parsed is not None
    assert parsed["ok"] is ok


def test_gates_fail_when_code_forces_exit_zero(tmp_path: Path) -> None:
    failing = "from tiny.mod import f\n\ndef test_f():\n    assert f(1) == 99\n"
    gates = run_gates(tmp_path, ATEXIT_TRICK, SOURCE, failing, failing)
    assert gates["tests"]["status"] == "fail"
    assert gates["hidden"]["status"] == "fail"


def test_mutants_are_killed_despite_forced_exit_zero(tmp_path: Path) -> None:
    gates = run_gates(tmp_path, ATEXIT_TRICK, SOURCE, TESTS, TESTS)
    assert gates["tests"]["status"] == "pass"
    mutation = gates["mutation"]
    assert mutation["generated"] >= 1
    assert mutation["killed"] == mutation["generated"]
