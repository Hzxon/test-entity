"""The verdict comes from a runner-owned result channel, never the child exit code (#53)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from testgate import runner
from testgate.cli import main
from testgate.core import ProjectConfig
from testgate.languages.python import PythonLanguage
from testgate.remote import _testgate_channel as channel
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


def test_package_skip_cannot_pass_hidden(tmp_path: Path) -> None:
    # No literal `pytest.skip` on the line, so the hygiene rule does not see it.
    skipping = (
        "def f(x):\n"
        "    if x == 2:\n"
        "        getattr(__import__('py' + 'test'), 'sk' + 'ip')('hard case')\n"
        "    return x + 1\n"
    )
    hidden = (
        "from tiny.mod import f\n\n"
        "def test_easy():\n    assert f(1) == 2\n\n"
        "def test_hard():\n    assert f(2) == 99\n"
    )
    root = tmp_path / "proj"
    hidden_dir = make_project(root, "", skipping, TESTS, hidden)
    cfg = ProjectConfig(root=root, package="src/tiny", import_name="tiny")
    sb = build_sandbox(cfg, sys.executable, {"hidden": hidden_dir})
    PythonLanguage().prepare_sandbox(sb, cfg, root)
    try:
        run = runner.run_pytest(sb, ["hidden"], 120, None)
    finally:
        destroy(sb)
    assert not run.ok
    assert run.skipped == 0
    assert "tests/hidden/test_h.py::test_hard" in run.failed_ids


def test_missing_channel_is_not_ok() -> None:
    run = runner._make_run(0, "=== 3 passed in 0.1s ===", 0.1, "")
    assert not run.ok


COLLECTED_1 = {"t": "collected", "n": 1}
PASSED_A = {"t": "report", "id": "a", "when": "call", "outcome": "passed"}
END = {"t": "end", "status": 0}
SETUP_SKIP_A = {"t": "report", "id": "a", "when": "setup", "outcome": "skipped"}
CALL_SKIP_A = {**SETUP_SKIP_A, "when": "call"}


def lines(records: list[dict[str, object]]) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


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
        ([COLLECTED_1, CALL_SKIP_A, END], False),
        ([COLLECTED_1, {**SETUP_SKIP_A, "mark": True}, END], True),
        ([COLLECTED_1, SETUP_SKIP_A, END], False),  # skipped at setup by package code
        ([COLLECTED_1, {**CALL_SKIP_A, "xfail": True}, END], False),
        ([COLLECTED_1, {**SETUP_SKIP_A, "mark": True, "xfail": True}, END], False),
        ([{"t": "collect_skip", "id": "tests/x.py"}, {"t": "collected", "n": 0}, END], False),
    ],
)
def test_fold_without_key_matches_today(records: list[dict[str, object]], ok: bool) -> None:
    folded = channel.fold("garbage\n" + lines(records), None)
    assert folded is not None
    assert folded.ok is ok
    if not ok and any(r.get("outcome") == "skipped" for r in records):
        assert folded.failed_ids == ["a"]
        assert folded.skipped == 0


def test_fold_counts_a_marked_skip() -> None:
    folded = channel.fold(lines([COLLECTED_1, {**SETUP_SKIP_A, "mark": True}, END]), None)
    assert folded is not None
    assert (folded.skipped, folded.failed_ids) == (1, [])


def test_fold_names_a_collect_skip() -> None:
    records = [{"t": "collect_skip", "id": "tests/x.py"}, {"t": "collected", "n": 0}, END]
    folded = channel.fold(lines(records), None)
    assert folded is not None
    assert folded.failed_ids == ["tests/x.py"]


@pytest.mark.parametrize(
    "end", [{**END, "violations": ["verdict-op"]}, {**END, "leaked_modules": 1}]
)
def test_fold_fails_an_end_record_with_violations(end: dict[str, object]) -> None:
    folded = channel.fold(lines([COLLECTED_1, PASSED_A, end]), None)
    assert folded is not None
    assert not folded.ok
    assert folded.passed == 1


KEY = bytes(range(32))


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """Channel lines a real pytest run wrote under KEY: two passing tests."""
    work = tmp_path_factory.mktemp("sealed")
    (work / "test_two.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    out = work / "channel.jsonl"
    key_read, key_write = os.pipe()
    os.write(key_write, KEY)
    os.close(key_write)
    with out.open("wb") as fh:
        env = {
            **os.environ,
            channel.ENV_RESULT_FD: str(fh.fileno()),
            channel.ENV_KEY_FD: str(key_read),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(runner._runtime_dirs().package),
        }
        argv = [sys.executable, "-m", "pytest", "-q", "-p", "_testgate_channel", "test_two.py"]
        proc = subprocess.run(
            argv,
            cwd=work,
            env=env,
            pass_fds=(fh.fileno(), key_read),
            capture_output=True,
            check=False,
        )
    os.close(key_read)
    assert proc.returncode == 0, proc.stdout
    return out.read_text().splitlines()


def test_fold_with_key_accepts_sealed_records(sealed: list[str]) -> None:
    folded = channel.fold("\n".join(sealed), KEY)
    assert folded is not None
    assert folded.ok
    assert (folded.passed, folded.failed, folded.errors) == (2, 0, 0)


def ok_without_key(text: str) -> bool:
    folded = channel.fold(text, None)
    return folded is not None and folded.ok


def test_fold_with_key_rejects_an_unsealed_line(sealed: list[str]) -> None:
    text = "\n".join([*sealed[:-1], json.dumps(END)])
    assert ok_without_key(text)  # a well-formed end record, just not sealed
    folded = channel.fold(text, KEY)
    assert folded is not None
    assert not folded.ok


def test_fold_with_key_rejects_reordered_records(sealed: list[str]) -> None:
    text = "\n".join([sealed[0], sealed[2], sealed[1], *sealed[3:]])
    assert ok_without_key(text)  # call before setup folds to the same counts
    folded = channel.fold(text, KEY)
    assert folded is not None
    assert not folded.ok


def test_fold_with_key_rejects_a_sealed_line_under_another_key(sealed: list[str]) -> None:
    folded = channel.fold("\n".join(sealed), b"other")
    assert folded is not None
    assert not folded.ok


def test_runtime_dirs_are_removed_at_exit() -> None:
    script = (
        "from testgate import runner\n"
        "d = runner._runtime_dirs()\n"
        "assert (d.package / '_testgate_channel.py').is_file()\n"
        "assert (d.oracle / '_testgate_channel.py').is_file()\n"
        "print(d.oracle)\nprint(d.package)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    dirs = proc.stdout.split()
    assert len(dirs) == 2
    assert not any(Path(d).exists() for d in dirs)


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
