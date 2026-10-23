"""The seven gates. Each takes a RunContext and returns a GateResult.

Public output holds counts, scores and file:line only. Test names, assertion
text and the seed go to GateResult.private.
"""

from __future__ import annotations

import random
import re
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from testgate import gitinfo, mutator, runner, sandbox
from testgate.core import (
    ChangedFile,
    CoverageData,
    GateError,
    GateResult,
    RunContext,
    Status,
    TestRun,
)

GATE_ORDER = ["protected", "hygiene", "build", "tests", "hidden", "coverage", "mutation"]

# Strings that mean code is trying to detect or steer the test harness.
FORBIDDEN_TOKENS = [
    "PYTEST_CURRENT_TEST",
    "TESTGATE",
    "HYPOTHESIS_SEED",
    "tests/hidden",
    "hidden_dir",
    'sys.modules["pytest"]',
    "sys.modules['pytest']",
    "monkeypatch.setattr(pytest",
    'os.environ.get("CI"',
    "os.environ.get('CI'",
]
_IMPORT_TESTS = re.compile(r"^\s*(?:from\s+tests\b|import\s+tests\b)")
_IMPORT_GOLDEN_HIDDEN = re.compile(
    r"^\s*(?:from\s+(?:tests\.)?(?:golden|hidden)\b"
    r"|import\s+(?:tests\.)?(?:golden|hidden)\b"
    r"|from\s+tests\s+import\s+.*\b(?:golden|hidden)\b)"
)
MAX_UNCOVERED_PER_FILE = 20


def _result(
    name: str,
    status: Status,
    summary: str,
    public: dict[str, Any] | None = None,
    private: dict[str, Any] | None = None,
) -> GateResult:
    return GateResult(name, status, summary, public or {}, private or {})


def _skip(name: str, summary: str) -> GateResult:
    return _result(name, Status.SKIP, summary)


def _test_dirs(ctx: RunContext, names: tuple[str, ...]) -> dict[str, Path]:
    """Existing test dirs among golden/auto/hidden, in the order given."""
    cfg = ctx.config
    candidates: dict[str, Path | None] = {
        "golden": cfg.root / cfg.golden,
        "auto": cfg.root / cfg.auto,
        "hidden": ctx.hidden_dir,
    }
    found: dict[str, Path] = {}
    for name in names:
        path = candidates[name]
        if path is not None and path.is_dir():
            found[name] = path
    return found


def _counts(run: TestRun) -> dict[str, int]:
    return {
        "passed": run.passed,
        "failed": run.failed,
        "errors": run.errors,
        "skipped": run.skipped,
    }


def _collected(run: TestRun) -> int:
    return run.passed + run.failed + run.errors


def _py_files(root: Path, rel: str) -> list[Path]:
    base = root / rel
    if base.is_file():
        return [base] if base.suffix == ".py" else []
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# --- protected -------------------------------------------------------------


def gate_protected(ctx: RunContext) -> GateResult:
    if ctx.base_ref is None:
        return _skip("protected", "no base ref")
    root = ctx.config.root
    changed: list[str] = []
    for path in ctx.config.protected:
        now = gitinfo.tree_hash(root, None, path)
        base = gitinfo.tree_hash(root, ctx.base_ref, path)
        if now is None and base is None:
            continue
        if now != base:
            changed.append(path)
    if changed:
        return _result(
            "protected",
            Status.FAIL,
            f"protected surface changed: {', '.join(changed)}",
            {"changed": changed},
        )
    return _result("protected", Status.PASS, "protected paths match the base ref")


# --- hygiene ---------------------------------------------------------------


def gate_hygiene(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    root = cfg.root
    hits: list[tuple[str, str]] = []  # (repo-relative file, "file:line: reason")

    def add(file: str, line: int | None, reason: str) -> None:
        loc = f"{file}:{line}" if line else file
        hits.append((file, f"{loc}: {reason}"))

    auto_files = _py_files(root, cfg.auto)
    auto_set = set(auto_files)
    for path in auto_files:
        if path.name != "__init__.py" and not (
            path.name.startswith("test_") and path.name.endswith(".py")
        ):
            add(_posix(root, path), None, "only test_*.py and __init__.py are allowed in auto")

    scanned = sorted({*auto_files, *_py_files(root, cfg.package)})
    for path in scanned:
        rel = _posix(root, path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for no, text in enumerate(lines, 1):
            for token in FORBIDDEN_TOKENS:
                if token in text:
                    add(rel, no, f"forbidden reference {token}")
            if path not in auto_set and _IMPORT_TESTS.match(text):
                add(rel, no, "package imports from a test dir")
            if path in auto_set and _IMPORT_GOLDEN_HIDDEN.match(text):
                add(rel, no, "auto test imports golden or hidden tests")

    if not hits:
        return _result("hygiene", Status.PASS, "no hygiene findings")
    changed_paths = {c.path for c in ctx.changed}
    findings = [h[1] for h in hits]
    public = {"findings": findings}
    if changed_paths and all(h[0] not in changed_paths for h in hits):
        return _result(
            "hygiene", Status.WARN, f"{len(hits)} pre-existing hygiene finding(s)", public
        )
    return _result("hygiene", Status.FAIL, f"{len(hits)} hygiene finding(s)", public)


# --- build -----------------------------------------------------------------


def gate_build(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    sb = sandbox.build_sandbox(cfg, ctx.python, {})
    try:
        steps = [
            [sb.python, "-c", f"import {cfg.import_name}"],
            [sb.python, "-m", "compileall", "-q", "src"],
        ]
        outputs: list[str] = []
        for argv in steps:
            proc = sandbox.run_in_sandbox(sb, argv, cfg.timeout_s)
            outputs.append(f"$ {' '.join(argv)}\n{proc.stdout}{proc.stderr}")
            if proc.returncode != 0:
                return _result(
                    "build",
                    Status.FAIL,
                    f"build failed (exit {proc.returncode})",
                    {"exit_code": proc.returncode},
                    {"output": "\n".join(outputs)},
                )
        return _result("build", Status.PASS, "package imports and compiles")
    finally:
        sandbox.destroy(sb)


# --- tests -----------------------------------------------------------------


def gate_tests(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    dirs = _test_dirs(ctx, ("golden", "auto"))
    if not dirs:
        return _result("tests", Status.FAIL, "no golden or auto tests found")
    names = list(dirs)
    sb = sandbox.build_sandbox(cfg, ctx.python, dirs)
    outputs: list[str] = []
    first: TestRun | None = None
    try:
        for i in range(1, cfg.repeats + 1):
            run = runner.run_pytest(sb, names, cfg.timeout_s, ctx.seed)
            outputs.append(f"--- run {i} (exit {run.exit_code}) ---\n{run.output}")
            if first is None:
                first = run
            public = {"runs": i, "repeats": cfg.repeats, **_counts(first)}
            private = {"outputs": outputs}
            ctx.shared["visible_run"] = first
            if i == 1 and (not run.ok or _collected(run) == 0):
                summary = (
                    "no tests collected" if _collected(run) == 0 and run.ok else ("tests failed")
                )
                return _result("tests", Status.FAIL, summary, public, private)
            if not run.ok:
                return _result(
                    "tests", Status.FAIL, f"flaky: passed run 1, failed run {i}", public, private
                )
        assert first is not None
        return _result(
            "tests",
            Status.PASS,
            f"tests: {first.passed} passed in {cfg.repeats} run(s)",
            {"runs": cfg.repeats, "repeats": cfg.repeats, **_counts(first)},
            {"outputs": outputs},
        )
    finally:
        sandbox.destroy(sb)


# --- hidden ----------------------------------------------------------------


def gate_hidden(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    dirs = _test_dirs(ctx, ("hidden",))
    if not dirs:
        if cfg.hidden_required:
            return _result("hidden", Status.FAIL, "no hidden tests")
        return _skip("hidden", "no hidden tests")
    sb = sandbox.build_sandbox(cfg, ctx.python, dirs)
    try:
        run = runner.run_pytest(sb, ["hidden"], cfg.timeout_s, ctx.seed)
    finally:
        sandbox.destroy(sb)
    # counts only: names and assertion text stay in the private log
    public = {"passed": run.passed, "failed": run.failed, "errors": run.errors}
    private = {"output": run.output, "exit_code": run.exit_code}
    summary = f"hidden: {run.passed} passed, {run.failed} failed"
    if run.errors:
        summary += f", {run.errors} errors"
    if _collected(run) == 0:
        return _result("hidden", Status.FAIL, "hidden: no tests collected", public, private)
    status = Status.PASS if run.ok else Status.FAIL
    return _result("hidden", status, summary, public, private)


# --- coverage --------------------------------------------------------------


def _measure_coverage(ctx: RunContext) -> tuple[TestRun, CoverageData]:
    cfg = ctx.config
    dirs = _test_dirs(ctx, ("golden", "auto", "hidden"))
    if not dirs:
        raise GateError("no tests available to measure coverage")
    sb = sandbox.build_sandbox(cfg, ctx.python, dirs)
    try:
        run, data = runner.run_coverage(
            sb, list(dirs), cfg.timeout_s, ctx.seed, cfg.root, cfg.package
        )
    finally:
        sandbox.destroy(sb)
    ctx.shared["coverage"] = data
    return run, data


def _changed_ratio(
    changed: list[ChangedFile], data: CoverageData
) -> tuple[float | None, list[str]]:
    """Covered / total changed statement lines, plus capped uncovered file:line."""
    total = 0
    covered = 0
    uncovered: list[str] = []
    for cf in sorted(changed, key=lambda c: c.path):
        stmts = cf.lines & data.statements.get(cf.path, set())
        hit = stmts & data.covered.get(cf.path, set())
        total += len(stmts)
        covered += len(hit)
        missing = sorted(stmts - hit)
        uncovered.extend(f"{cf.path}:{n}" for n in missing[:MAX_UNCOVERED_PER_FILE])
    return ((covered / total) if total else None), uncovered


def gate_coverage(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    run, data = _measure_coverage(ctx)
    total = data.total()
    changed_ratio, uncovered = _changed_ratio(ctx.changed, data) if ctx.changed else (None, [])
    public: dict[str, Any] = {
        "total": round(total, 4),
        "changed": None if changed_ratio is None else round(changed_ratio, 4),
        "uncovered_changed": uncovered,
    }
    private = {"output": run.output, "exit_code": run.exit_code}
    problems: list[str] = []
    if not run.ok:
        problems.append("tests failed under coverage")
    if total < cfg.coverage_min_total:
        problems.append(f"total {total:.0%} < {cfg.coverage_min_total:.0%}")
    if changed_ratio is not None and changed_ratio < cfg.coverage_min_changed:
        problems.append(f"changed {changed_ratio:.0%} < {cfg.coverage_min_changed:.0%}")
    changed_text = "n/a" if changed_ratio is None else f"{changed_ratio:.0%}"
    summary = f"coverage: total {total:.0%}, changed {changed_text}"
    if problems:
        return _result(
            "coverage", Status.FAIL, f"{summary} ({'; '.join(problems)})", public, private
        )
    return _result("coverage", Status.PASS, summary, public, private)


# --- mutation --------------------------------------------------------------


def _candidate_lines(ctx: RunContext, data: CoverageData) -> dict[str, set[int]]:
    """Changed lines that tests execute; every covered line when nothing is marked changed."""
    if not ctx.changed:
        return {p: set(lines) for p, lines in data.covered.items() if lines}
    out: dict[str, set[int]] = {}
    for cf in ctx.changed:
        lines = cf.lines & data.covered.get(cf.path, set())
        if lines:
            out[cf.path] = lines
    return out


def gate_mutation(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    data: CoverageData | None = ctx.shared.get("coverage")
    if data is None:
        _, data = _measure_coverage(ctx)

    mutants = []
    for path, lines in sorted(_candidate_lines(ctx, data).items()):
        file = cfg.root / path
        if not file.is_file():
            continue
        source = file.read_text(encoding="utf-8")
        mutants.extend(
            mutator.generate_mutants(path, source, lines, ctx.seed, cfg.mutation_max_mutants)
        )
    if len(mutants) > cfg.mutation_max_mutants:
        picked = random.Random(ctx.seed).sample(mutants, cfg.mutation_max_mutants)
        mutants = sorted(picked, key=lambda m: (m.path, m.line, m.operator))
    if not mutants:
        return _skip("mutation", "no eligible lines")

    # TODO(v2): per-auto-test usefulness (ACH rule)
    # Kills are judged by golden + hidden only; auto tests are the author's.
    dirs = _test_dirs(ctx, ("golden", "hidden"))
    if not dirs:
        raise GateError("no golden or hidden tests to judge mutants")
    names = list(dirs)
    # A failing baseline would count every mutant as killed, so check it first.
    sb = sandbox.build_sandbox(cfg, ctx.python, dirs)
    try:
        baseline = runner.run_pytest(sb, names, cfg.timeout_s, ctx.seed)
    finally:
        sandbox.destroy(sb)
    ctx.log(f"mutation baseline exit={baseline.exit_code}\n{baseline.output}")
    if not baseline.ok:
        return _skip("mutation", "skipped: golden/hidden tests fail on unmutated code")
    for m in mutants:
        sb = sandbox.build_sandbox(cfg, ctx.python, dirs, overrides={m.path: m.source})
        try:
            m.run = runner.run_pytest(
                sb, names, cfg.mutation_timeout_s, ctx.seed, extra_args=["-x"]
            )
        finally:
            sandbox.destroy(sb)
        m.killed = not m.run.ok  # a timeout is not ok, so it counts as killed

    killed = sum(1 for m in mutants if m.killed)
    score = killed / len(mutants)
    survivors: list[dict[str, Any]] = []
    per_file: dict[str, int] = {}
    for m in mutants:
        if m.killed:
            continue
        per_file[m.path] = per_file.get(m.path, 0) + 1
        if per_file[m.path] <= cfg.mutation_max_shown:
            survivors.append(
                {
                    "id": m.id,
                    "line": m.line,
                    "operator": m.operator,
                    "before": m.before,
                    "after": m.after,
                }
            )
    public = {
        "generated": len(mutants),
        "killed": killed,
        "score": round(score, 4),
        "survivors": survivors,
    }
    private = {
        "mutants": [
            {
                "id": m.id,
                "killed": m.killed,
                "exit_code": m.run.exit_code if m.run else None,
                "timed_out": m.run.timed_out if m.run else None,
                "output": m.run.output if m.run else "",
            }
            for m in mutants
        ]
    }
    summary = f"mutation: {killed}/{len(mutants)} killed (score {score:.2f})"
    if score < cfg.mutation_min_score:
        return _result(
            "mutation",
            Status.FAIL,
            f"{summary}, below {cfg.mutation_min_score:.2f}",
            public,
            private,
        )
    return _result("mutation", Status.PASS, summary, public, private)


GateFn = Callable[[RunContext], GateResult]
GATES: dict[str, GateFn] = {
    "protected": gate_protected,
    "hygiene": gate_hygiene,
    "build": gate_build,
    "tests": gate_tests,
    "hidden": gate_hidden,
    "coverage": gate_coverage,
    "mutation": gate_mutation,
}


def run_gates(ctx: RunContext, only: list[str] | None = None) -> list[GateResult]:
    results: list[GateResult] = []
    stopped = False
    for name in GATE_ORDER:
        if stopped:
            results.append(_skip(name, "skipped: protected surface changed"))
            continue
        if only is not None and name not in only:
            results.append(_skip(name, "not selected"))
            continue
        if not ctx.config.gates.get(name, True):
            results.append(_skip(name, "disabled"))
            continue
        start = time.monotonic()
        try:
            result = GATES[name](ctx)
        except Exception as exc:  # noqa: BLE001 - any gate crash becomes Status.ERROR
            result = _result(
                name,
                Status.ERROR,
                f"gate crashed: {type(exc).__name__}",
                {"error": type(exc).__name__},
                {"error": str(exc), "traceback": traceback.format_exc()},
            )
        result.duration_s = time.monotonic() - start
        ctx.log(f"[{name}] {result.status.value}: {result.summary}")
        results.append(result)
        if name == "protected" and result.blocks:
            stopped = True
    return results
