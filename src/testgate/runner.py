"""pytest and coverage invocations inside a sandbox."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from testgate.core import CoverageData, Sandbox, TestRun
from testgate.sandbox import run_in_sandbox

# Result of the "is hypothesis importable" probe, keyed by sandbox root.
_HYPOTHESIS: dict[str, bool] = {}

_SUMMARY = re.compile(r"\bin [\d.]+s\b")

# The result channel (#53). pytest loads this plugin with -p from a runner-owned
# directory outside the sandbox; it writes one JSON line per test report plus an
# end marker to a pipe the runner opened, whose fd number is the only thing passed
# in. The verdict comes from these records, never from the child's exit code.
_CHANNEL_MODULE = "_testgate_channel"
_CHANNEL_PLUGIN = """\
import json
import os

_fd = int(os.environ.pop("TESTGATE_RESULT_FD", "-1"))
if _fd >= 0:
    os.set_inheritable(_fd, False)


def _emit(record):
    if _fd < 0:
        return
    data = (json.dumps(record) + "\\n").encode()
    try:
        while data:
            data = data[os.write(_fd, data):]
    except OSError:
        pass


def pytest_collectreport(report):
    if report.failed:
        _emit({"t": "collect_error", "id": report.nodeid})


def pytest_collection_finish(session):
    _emit({"t": "collected", "n": len(session.items)})


def pytest_runtest_logreport(report):
    _emit({"t": "report", "id": report.nodeid, "when": report.when, "outcome": report.outcome})


def pytest_sessionfinish(session, exitstatus):
    _emit({"t": "end", "status": int(exitstatus)})
"""
_CHANNEL_DIR: list[Path] = []
_CHANNEL_LOCK = threading.Lock()
_DRAIN_TIMEOUT_S = 5


def _channel_dir() -> Path:
    """A runner-owned directory holding only the channel plugin, created once per process."""
    with _CHANNEL_LOCK:
        if not _CHANNEL_DIR:
            d = Path(tempfile.mkdtemp(prefix="testgate-channel-"))
            (d / f"{_CHANNEL_MODULE}.py").write_text(_CHANNEL_PLUGIN, encoding="utf-8")
            _CHANNEL_DIR.append(d)
        return _CHANNEL_DIR[0]


def _run_with_channel(
    sb: Sandbox, argv: list[str], timeout_s: int
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run pytest with the channel plugin; return the process and the channel text."""
    read_fd, write_fd = os.pipe()
    chunks: list[bytes] = []

    def drain() -> None:
        with os.fdopen(read_fd, "rb", buffering=0) as fh:
            while chunk := fh.read(65536):
                chunks.append(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    env = {
        "TESTGATE_RESULT_FD": str(write_fd),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": f"{_channel_dir()}{os.pathsep}{sb.env.get('PYTHONPATH', '')}",
    }
    try:
        proc = run_in_sandbox(sb, argv, timeout_s, env, pass_fds=(write_fd,))
    finally:
        os.close(write_fd)
    # A process left holding the pipe would block EOF; whatever arrived so far is used.
    reader.join(_DRAIN_TIMEOUT_S)
    return proc, b"".join(list(chunks)).decode("utf-8", errors="replace")


def _parse_channel(text: str) -> dict[str, Any] | None:
    """Fold channel records into counts and a verdict; None when nothing was collected."""
    collected: int | None = None
    ended = False
    collect_errors: list[str] = []
    state: dict[str, str | None] = {}
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("t")
        if kind == "collected":
            collected = int(rec.get("n", 0))
        elif kind == "collect_error":
            collect_errors.append(str(rec.get("id", "")))
        elif kind == "end":
            ended = True
        elif kind == "report":
            nid = str(rec.get("id", ""))
            when, outcome = rec.get("when"), rec.get("outcome")
            cur = state.get(nid)
            if outcome == "failed":
                state[nid] = "failed" if when == "call" else (cur if cur == "failed" else "error")
            elif outcome == "skipped" and cur is None:
                state[nid] = "skipped"
            elif outcome == "passed" and when == "call" and cur is None:
                state[nid] = "passed"
            else:
                state.setdefault(nid, None)
    if collected is None and not collect_errors:
        return None
    finals = [v for v in state.values() if v is not None]
    counts = {k: finals.count(k) for k in ("passed", "failed", "error", "skipped")}
    failed_ids = [n for n, v in state.items() if v in ("failed", "error")] + collect_errors
    complete = collected is not None and len(finals) == collected
    ok = ended and complete and not failed_ids
    return {
        "ok": ok,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["error"] + len(collect_errors),
        "skipped": counts["skipped"],
        "failed_ids": failed_ids,
    }


def _has_hypothesis(sb: Sandbox) -> bool:
    key = str(sb.root)
    if key not in _HYPOTHESIS:
        proc = run_in_sandbox(sb, [sb.python, "-c", "import hypothesis"], 60)
        _HYPOTHESIS[key] = proc.returncode == 0
    return _HYPOTHESIS[key]


def _pytest_args(
    sb: Sandbox,
    test_names: list[str],
    seed: str | None,
    extra: list[str] | None,
    files: list[str] | None = None,
) -> list[str]:
    paths = files if files else [f"tests/{n}" for n in test_names]
    args = ["-m", "pytest", "-q", "--tb=short", "-rA", "-p", _CHANNEL_MODULE, *paths]
    if seed and _has_hypothesis(sb):
        # plugin autoload is off (a PR must not inject plugins), so load hypothesis by name
        args += ["-p", "_hypothesis_pytestplugin", f"--hypothesis-seed={seed}"]
    args.extend(extra or [])
    return args


def _count(line: str, word: str) -> int:
    m = re.search(rf"(\d+) {word}", line)
    return int(m.group(1)) if m else 0


def _make_run(code: int, output: str, duration: float, channel: str | None = None) -> TestRun:
    if channel is not None:
        parsed = _parse_channel(channel)
        if parsed is None:  # nothing reached the channel: the run proves nothing
            return TestRun(code, 0, 0, 1, 0, duration, output, code == 124, reported_ok=False)
        return TestRun(
            exit_code=code,
            passed=parsed["passed"],
            failed=parsed["failed"],
            errors=parsed["errors"],
            skipped=parsed["skipped"],
            duration_s=duration,
            output=output,
            timed_out=code == 124,
            reported_ok=parsed["ok"],
            failed_ids=parsed["failed_ids"],
        )
    summary = ""
    for line in reversed(output.splitlines()):
        if _SUMMARY.search(line):
            summary = line
            break
    return TestRun(
        exit_code=code,
        passed=_count(summary, "passed"),
        failed=_count(summary, "failed"),
        errors=_count(summary, "error"),
        skipped=_count(summary, "skipped"),
        duration_s=duration,
        output=output,
        timed_out=code == 124,
    )


def run_pytest(
    sb: Sandbox,
    test_names: list[str],
    timeout_s: int,
    seed: str | None,
    extra_args: list[str] | None = None,
    files: list[str] | None = None,
) -> TestRun:
    argv = [sb.python, *_pytest_args(sb, test_names, seed, extra_args, files)]
    start = time.monotonic()
    proc, channel = _run_with_channel(sb, argv, timeout_s)
    return _make_run(proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start, channel)


def run_coverage(
    sb: Sandbox,
    test_names: list[str],
    timeout_s: int,
    seed: str | None,
    source_root: Path,
    repo_package: str,
) -> tuple[TestRun, CoverageData]:
    """Run pytest under coverage; return coverage keyed by repo-relative path.

    `source_root` is unused: the sandbox layout already fixes where sources live
    (``<root>/src/<import_name>``, the Python adapter's package_dest).
    """
    rc = sb.root / ".coveragerc"
    rc.write_text(f"[run]\ndata_file = {sb.root / '.coverage'}\n", encoding="utf-8")
    rcfile = f"--rcfile={rc}"
    argv = [
        sb.python, "-m", "coverage", "run", rcfile, "--source=src",
        *_pytest_args(sb, test_names, seed, None),
    ]  # fmt: skip
    start = time.monotonic()
    proc, channel = _run_with_channel(sb, argv, timeout_s)
    test_run = _make_run(
        proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start, channel
    )

    cov_json = sb.root / "cov.json"
    run_in_sandbox(sb, [sb.python, "-m", "coverage", "json", rcfile, "-o", str(cov_json)], 120)
    data = CoverageData()
    if not cov_json.is_file():
        return test_run, data

    files = json.loads(cov_json.read_text(encoding="utf-8")).get("files", {})
    src_real = os.path.realpath(sb.root / "src")
    for name, info in files.items():
        full = name if os.path.isabs(name) else str(sb.root / name)
        rel = os.path.relpath(os.path.realpath(full), src_real)
        parts = PurePosixPath(Path(rel).as_posix()).parts
        if rel.startswith("..") or len(parts) < 2:
            continue
        # parts[0] is the import_name directory inside the sandbox
        repo_path = (PurePosixPath(repo_package) / PurePosixPath(*parts[1:])).as_posix()
        executed = set(info.get("executed_lines", []))
        missing = set(info.get("missing_lines", []))
        data.covered[repo_path] = executed
        data.statements[repo_path] = executed | missing
    return test_run, data
