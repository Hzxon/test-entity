"""pytest and coverage invocations inside a sandbox."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path, PurePosixPath

from testgate.core import CoverageData, Sandbox, TestRun
from testgate.sandbox import run_in_sandbox

# Result of the "is hypothesis importable" probe, keyed by sandbox root.
_HYPOTHESIS: dict[str, bool] = {}

_SUMMARY = re.compile(r"\bin [\d.]+s\b")


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
    args = ["-m", "pytest", "-q", "--tb=short", "-rA", *paths]
    if seed and _has_hypothesis(sb):
        args.append(f"--hypothesis-seed={seed}")
    args.extend(extra or [])
    return args


def _count(line: str, word: str) -> int:
    m = re.search(rf"(\d+) {word}", line)
    return int(m.group(1)) if m else 0


def _make_run(code: int, output: str, duration: float) -> TestRun:
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
    proc = run_in_sandbox(sb, argv, timeout_s)
    return _make_run(proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start)


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
    proc = run_in_sandbox(sb, argv, timeout_s)
    test_run = _make_run(proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start)

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
