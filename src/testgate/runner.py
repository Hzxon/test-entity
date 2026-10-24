"""pytest and coverage invocations inside a sandbox."""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

from testgate.core import CoverageData, Sandbox, TestRun
from testgate.remote import _testgate_channel as channel
from testgate.sandbox import run_in_sandbox

# Result of the "is hypothesis importable" probe, keyed by sandbox root.
_HYPOTHESIS: dict[str, bool] = {}

_SUMMARY = re.compile(r"\bin [\d.]+s\b")

# Runtime modules (testgate/remote) are copied into two runner-owned temp dirs, once per
# process. The oracle dir is never bound into a sandbox child. The package dir is bound
# read-only into in-process runs, which load the channel plugin from it with -p.
ORACLE_MODULES = ("_testgate_channel",)
PACKAGE_MODULES = ("_testgate_channel",)
_DRAIN_TIMEOUT_S = 5


@dataclass(frozen=True)
class RuntimeDirs:
    oracle: Path
    package: Path


_RUNTIME_DIRS: list[RuntimeDirs] = []
_RUNTIME_LOCK = threading.Lock()


def _runtime_dirs() -> RuntimeDirs:
    """Both runtime dirs, created on first use and removed when the process exits."""
    with _RUNTIME_LOCK:
        if not _RUNTIME_DIRS:
            dirs = RuntimeDirs(
                oracle=_copy_modules("testgate-oracle-", ORACLE_MODULES),
                package=_copy_modules("testgate-package-", PACKAGE_MODULES),
            )
            for d in (dirs.oracle, dirs.package):
                atexit.register(shutil.rmtree, d, ignore_errors=True)
            _RUNTIME_DIRS.append(dirs)
        return _RUNTIME_DIRS[0]


def _copy_modules(prefix: str, names: tuple[str, ...]) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    source = resources.files("testgate.remote")
    for name in names:
        (d / f"{name}.py").write_bytes(source.joinpath(f"{name}.py").read_bytes())
    return d


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
    package_dir = _runtime_dirs().package
    env = {
        channel.ENV_RESULT_FD: str(write_fd),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": f"{package_dir}{os.pathsep}{sb.env.get('PYTHONPATH', '')}",
    }
    try:
        proc = run_in_sandbox(
            sb, argv, timeout_s, env, pass_fds=(write_fd,), binds={str(package_dir): False}
        )
    finally:
        os.close(write_fd)
    # A process left holding the pipe would block EOF; whatever arrived so far is used.
    reader.join(_DRAIN_TIMEOUT_S)
    return proc, b"".join(list(chunks)).decode("utf-8", errors="replace")


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
    # Hidden runs print no tracebacks and no per-test summary lines (#51); the
    # channel still names failing tests, in memory only.
    report = ["--tb=no", "-rN"] if "hidden" in test_names else ["--tb=short", "-rA"]
    args = ["-m", "pytest", "-q", *report, "-p", "_testgate_channel", *paths]
    if seed and _has_hypothesis(sb):
        # plugin autoload is off (a PR must not inject plugins), so load hypothesis by name
        args += ["-p", "_hypothesis_pytestplugin", f"--hypothesis-seed={seed}"]
    args.extend(extra or [])
    return args


def _count(line: str, word: str) -> int:
    m = re.search(rf"(\d+) {word}", line)
    return int(m.group(1)) if m else 0


def _make_run(code: int, output: str, duration: float, records: str | None = None) -> TestRun:
    if records is not None:
        folded = channel.fold(records, None)
        if folded is None:  # nothing reached the channel: the run proves nothing
            return TestRun(code, 0, 0, 1, 0, duration, output, code == 124, reported_ok=False)
        return TestRun(
            exit_code=code,
            passed=folded.passed,
            failed=folded.failed,
            errors=folded.errors,
            skipped=folded.skipped,
            duration_s=duration,
            output=output,
            timed_out=code == 124,
            reported_ok=folded.ok,
            failed_ids=folded.failed_ids,
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
    proc, records = _run_with_channel(sb, argv, timeout_s)
    return _make_run(proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start, records)


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
    # Empty exclusion lists: no "# pragma: no cover" can drop a line from coverage
    # and with it from mutation (#64).
    rc.write_text(
        f"[run]\ndata_file = {sb.root / '.coverage'}\n[report]\nexclude_lines =\nexclude_also =\n",
        encoding="utf-8",
    )
    rcfile = f"--rcfile={rc}"
    argv = [
        sb.python, "-m", "coverage", "run", rcfile, "--source=src",
        *_pytest_args(sb, test_names, seed, None),
    ]  # fmt: skip
    start = time.monotonic()
    proc, records = _run_with_channel(sb, argv, timeout_s)
    test_run = _make_run(
        proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start, records
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
