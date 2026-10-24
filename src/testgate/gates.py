"""The seven gates. Each takes a RunContext and returns a GateResult.

Public output holds counts, scores and file:line only. Test names, assertion
text and the seed go to GateResult.private.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import random
import re
import shutil
import tempfile
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from testgate import gitinfo, sandbox
from testgate.core import (
    ChangedFile,
    CoverageData,
    GateError,
    GateResult,
    Mutant,
    ProjectConfig,
    RunContext,
    Sandbox,
    Status,
    TestRun,
)

GATE_ORDER = ["protected", "hygiene", "build", "tests", "hidden", "coverage", "mutation"]

# Strings that mean code is trying to detect or steer the test harness.
# Language-neutral; each Language adds its own via detection_tokens.
FORBIDDEN_TOKENS = [
    "TESTGATE",
    "tests/hidden",
    "hidden_dir",
]
_IMPORT_TESTS = re.compile(r"^\s*(?:from\s+tests\b|import\s+tests\b)")
_IMPORT_GOLDEN_HIDDEN = re.compile(
    r"^\s*(?:from\s+(?:tests\.)?(?:golden|hidden)\b"
    r"|import\s+(?:tests\.)?(?:golden|hidden)\b"
    r"|from\s+tests\s+import\s+.*\b(?:golden|hidden)\b)"
)
MAX_UNCOVERED_PER_FILE = 20

# Patterns that FAIL hygiene when a changed line adds them (all lines under --no-git).
# Coverage exclusions hide a line from coverage and from mutation (#64).
CHANGED_LINE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"pragma:\s*no\s*(?:cover|branch)", re.IGNORECASE), "coverage exclusion pragma"),
    (re.compile(r"\b(?:v8|c8|istanbul)\s+ignore\b", re.IGNORECASE), "coverage ignore hint"),
]
# Stands in for the output of any run that included hidden tests (#51): pytest and
# vitest print node ids, file paths and assertion source, and the job log, the
# private log and every report are readable by the author of the change.
HIDDEN_REDACTED = "<redacted: run included hidden tests>"


def _output(run: TestRun, names: Any) -> str:
    """Run output safe to keep: redacted when the run included hidden tests."""
    return HIDDEN_REDACTED if "hidden" in names else run.output


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


def _source_files(root: Path, rel: str, suffixes: tuple[str, ...]) -> list[Path]:
    base = root / rel
    if base.is_file():
        return [base] if base.suffix in suffixes else []
    if not base.is_dir():
        return []
    return sorted(
        p
        for p in base.rglob("*")
        if p.is_file()
        and p.suffix in suffixes
        and "__pycache__" not in p.parts
        and "node_modules" not in p.parts
    )


def _sandbox(
    ctx: RunContext,
    dirs: dict[str, Path],
    overrides: dict[str, str] | None = None,
    cfg: ProjectConfig | None = None,
) -> Sandbox:
    """Build a sandbox and let the language write its own files into it.

    `cfg` is only given for the baseline run, whose copy of the package lives in
    an archive dir; the language still gets the real project root.
    """
    cfg = cfg or ctx.config
    sb = sandbox.build_sandbox(
        cfg, ctx.python, dirs, overrides=overrides, dest=ctx.language.package_dest(cfg)
    )
    ctx.language.prepare_sandbox(sb, cfg, ctx.config.root)
    return sb


def _posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# --- protected -------------------------------------------------------------


def gate_protected(ctx: RunContext) -> GateResult:
    if ctx.base_ref is None:
        return _skip("protected", "no base ref")
    if ctx.config.raw.get("_source") == "worktree":
        # First adoption: nothing at the base ref defines the protected surface yet.
        return _skip("protected", "first adoption: testgate.yaml is not at the base ref")
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

    lang = ctx.language
    tokens = [*FORBIDDEN_TOKENS, *lang.detection_tokens]
    auto_files = _source_files(root, cfg.auto, lang.source_suffixes)
    auto_set = set(auto_files)
    for path in auto_files:
        if not lang.auto_file_ok(path):
            add(_posix(root, path), None, f"only {lang.test_glob} files are allowed in auto")

    scanned = sorted({*auto_files, *_source_files(root, cfg.package, lang.source_suffixes)})
    changed_by_path = {c.path: c.lines for c in ctx.changed}
    for path in scanned:
        rel = _posix(root, path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        # without git every line counts as changed
        changed_lines = changed_by_path.get(rel, set()) if ctx.changed else None
        for no, text in enumerate(lines, 1):
            for token in tokens:
                if token in text:
                    add(rel, no, f"forbidden reference {token}")
            if changed_lines is None or no in changed_lines:
                for pattern, reason in CHANGED_LINE_RULES:
                    if pattern.search(text):
                        add(rel, no, reason)
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
    sb = _sandbox(ctx, {})
    try:
        proc = ctx.language.build(sb, cfg, cfg.timeout_s)
        if proc.returncode != 0:
            return _result(
                "build",
                Status.FAIL,
                f"build failed (exit {proc.returncode})",
                {"exit_code": proc.returncode},
                {"output": f"$ {' '.join(proc.args)}\n{proc.stdout}{proc.stderr}"},
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
    sb = _sandbox(ctx, dirs)
    outputs: list[str] = []
    first: TestRun | None = None
    try:
        for i in range(1, cfg.repeats + 1):
            run = ctx.language.run_tests(sb, names, cfg.timeout_s, ctx.seed)
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
    sb = _sandbox(ctx, dirs)
    try:
        run = ctx.language.run_tests(sb, ["hidden"], cfg.timeout_s, ctx.seed)
    finally:
        sandbox.destroy(sb)
    # counts only: names and assertion text stay in the private log
    public = {"passed": run.passed, "failed": run.failed, "errors": run.errors}
    private = {
        "exit_code": run.exit_code
    }  # output, ids and paths of hidden tests never leave memory
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
    sb = _sandbox(ctx, dirs)
    try:
        run, data = ctx.language.run_coverage(sb, list(dirs), cfg.timeout_s, ctx.seed, cfg.package)
    finally:
        sandbox.destroy(sb)
    ctx.shared["coverage"] = data
    ctx.shared["coverage_dirs"] = list(dirs)
    return run, data


def _hidden_passing_at_base(
    ctx: RunContext, base_cfg: ProjectConfig, tmp: Path
) -> tuple[Path | None, int]:
    """Copy of the hidden dir without the test files that fail against the base ref.

    Returns (dir or None when nothing passes, number of files excluded).
    """
    assert ctx.hidden_dir is not None
    sb = _sandbox(ctx, {"hidden": ctx.hidden_dir}, cfg=base_cfg)
    try:
        run = ctx.language.run_tests(sb, ["hidden"], ctx.config.timeout_s, ctx.seed)
    finally:
        sandbox.destroy(sb)
    ctx.log(f"hidden at base exit={run.exit_code}")
    if run.ok:
        return ctx.hidden_dir, 0
    failed = {Path(f).name for f in ctx.language.failed_files(run)}
    if not failed:
        return None, 0  # failed for a reason we cannot attribute to files
    filtered = tmp / "hidden-at-base"
    shutil.copytree(
        ctx.hidden_dir, filtered, ignore=lambda _d, names: [n for n in names if n in failed]
    )
    remaining = [p for p in filtered.rglob(ctx.language.test_glob) if p.is_file()]
    ctx.log(f"hidden files excluded at base: {len(failed)}")
    return (filtered if remaining else None), len(failed)


def _baseline_coverage(ctx: RunContext) -> tuple[CoverageData | None, str, int]:
    """Coverage of the package as it is at the base ref, with the same test dirs.

    Golden and auto tests come from the base ref; hidden tests are the current
    ones minus the files that fail against the base ref (#25), since they live
    outside the repository and may cover code the base ref lacks. Returns
    (data, reason, excluded hidden files); data is None when no comparison is possible.
    """
    cfg = ctx.config
    if ctx.base_ref is None:
        return None, "no base ref", 0
    tmp = Path(tempfile.mkdtemp(prefix="testgate-base-"))
    try:
        present = gitinfo.archive(cfg.root, ctx.base_ref, [cfg.package, cfg.golden, cfg.auto], tmp)
        if cfg.package not in present:
            return None, "package absent at base ref", 0
        base_cfg = dataclasses.replace(cfg, root=tmp)
        dirs: dict[str, Path] = {}
        for name, rel in (("golden", cfg.golden), ("auto", cfg.auto)):
            if rel in present and (tmp / rel).is_dir():
                dirs[name] = tmp / rel
        excluded = 0
        if ctx.hidden_dir is not None:
            hidden, excluded = _hidden_passing_at_base(ctx, base_cfg, tmp)
            if hidden is not None:
                dirs["hidden"] = hidden
        if not dirs:
            return None, "no tests at base ref", excluded
        sb = _sandbox(ctx, dirs, cfg=base_cfg)
        try:
            run, data = ctx.language.run_coverage(
                sb, list(dirs), cfg.timeout_s, ctx.seed, cfg.package
            )
        finally:
            sandbox.destroy(sb)
        ctx.log(f"baseline coverage exit={run.exit_code}\n{_output(run, dirs)}")
        if not run.ok:
            return None, "tests fail at base ref", excluded
        ctx.shared["baseline_coverage"] = data
        return data, "ok", excluded
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def _changed_sources(ctx: RunContext) -> list[str]:
    """Changed files under the package with a source suffix of the language."""
    pkg = ctx.config.package.strip("/")
    prefix = "" if pkg in ("", ".") else pkg + "/"
    suffixes = ctx.language.source_suffixes
    return sorted(
        c.path
        for c in ctx.changed
        if c.path.startswith(prefix)
        and c.path.endswith(suffixes)
        and (ctx.config.root / c.path).is_file()
    )


def gate_coverage(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    run, data = _measure_coverage(ctx)
    total = data.total()
    # an adapter may redefine the changed units (http: routes whose spec changed)
    overlay = getattr(ctx.language, "changed_units", None)
    changed = (overlay(ctx) if overlay else None) or ctx.changed
    changed_ratio, uncovered = _changed_ratio(changed, data) if changed else (None, [])
    # Missing data never counts as covered (#63): a changed source file the coverage
    # run did not measure at all, or no measured statements while sources changed.
    unmeasured = [] if overlay else [p for p in _changed_sources(ctx) if p not in data.statements]
    public: dict[str, Any] = {
        "total": round(total, 4),
        "changed": None if changed_ratio is None else round(changed_ratio, 4),
        "uncovered_changed": uncovered,
    }
    private = {
        "output": _output(run, ctx.shared.get("coverage_dirs", [])),
        "exit_code": run.exit_code,
    }
    problems: list[str] = []
    if not run.ok:
        problems.append("tests failed under coverage")
    if unmeasured:
        public["unmeasured_changed"] = unmeasured
        problems.append(f"{len(unmeasured)} changed source file(s) not measured")
    elif not any(data.statements.values()) and (overlay is None and _changed_sources(ctx)):
        problems.append("no statements measured")
    baseline, reason, excluded = _baseline_coverage(ctx)
    public["baseline_hidden_excluded"] = excluded  # count only; file names stay private
    if baseline is None:
        public["baseline_total"] = None
        public["drop"] = None
        public["baseline_skipped"] = reason
    else:
        drop = baseline.total() - total
        public["baseline_total"] = round(baseline.total(), 4)
        public["drop"] = round(drop, 4)
        if drop > cfg.coverage_max_drop:
            problems.append(
                f"dropped {drop:.1%} from {baseline.total():.0%} (max {cfg.coverage_max_drop:.0%})"
            )
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


def _auto_usefulness(
    ctx: RunContext, survivors: list[Mutant]
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """ACH rule: an auto test file is useful only if it kills a mutant golden + hidden missed.

    Returns (checked files, useless files, {file: killed mutant ids}).
    """
    cfg = ctx.config
    auto = _test_dirs(ctx, ("auto",))
    if not auto or not survivors:
        return [], [], {}
    files = sorted(p.name for p in auto["auto"].glob(ctx.language.test_glob))
    killed_by: dict[str, list[str]] = {}
    pairs = [(name, m) for name in files for m in survivors]

    def check(pair: tuple[str, Mutant]) -> tuple[str, Mutant, TestRun]:
        name, m = pair
        sb = _sandbox(ctx, auto, overrides={m.path: m.source})
        try:
            run = ctx.language.run_tests(
                sb,
                [],
                cfg.mutation_timeout_s,
                ctx.seed,
                files=[f"tests/auto/{name}"],
                fail_fast=True,
            )
        finally:
            sandbox.destroy(sb)
        return name, m, run

    # Each pair gets its own sandbox, so the pairs are independent; results are
    # consumed in list order, so the report does not depend on timing.
    with ThreadPoolExecutor(max_workers=cfg.mutation_jobs) as pool:
        results = list(pool.map(check, pairs))
    for name in files:
        killed_by[name] = []
    for name, m, run in results:
        ctx.log(f"auto {name} vs {m.id}: exit={run.exit_code}\n{run.output}")
        if not run.ok:
            killed_by[name].append(m.id)
    useless = [name for name in files if not killed_by[name]]
    return files, useless, killed_by


def gate_mutation(ctx: RunContext) -> GateResult:
    cfg = ctx.config
    # an adapter may replace mutation with its own judge (http: differential vs the base ref)
    alt = getattr(ctx.language, "mutation_gate", None)
    if alt is not None:
        result: GateResult = alt(ctx)
        return result
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
            ctx.language.generate_mutants(path, source, lines, ctx.seed, cfg.mutation_max_mutants)
        )
    if len(mutants) > cfg.mutation_max_mutants:
        picked = random.Random(ctx.seed).sample(mutants, cfg.mutation_max_mutants)
        mutants = sorted(picked, key=lambda m: (m.path, m.line, m.operator))
    if not mutants:
        return _skip("mutation", "no eligible lines")

    # Kills are judged by golden + hidden only; auto tests are the author's.
    dirs = _test_dirs(ctx, ("golden", "hidden"))
    if not dirs:
        raise GateError("no golden or hidden tests to judge mutants")
    names = list(dirs)
    # A failing baseline would count every mutant as killed, so check it first.
    sb = _sandbox(ctx, dirs)
    try:
        baseline = ctx.language.run_tests(sb, names, cfg.timeout_s, ctx.seed)
    finally:
        sandbox.destroy(sb)
    ctx.log(f"mutation baseline exit={baseline.exit_code}\n{_output(baseline, names)}")
    if not baseline.ok:
        # fail closed (#63): a failing baseline would otherwise hide every mutant
        return _result(
            "mutation",
            Status.FAIL,
            "golden/hidden tests fail on unmutated code",
            {"baseline_exit_code": baseline.exit_code},
        )

    def judge(m: Mutant) -> TestRun:
        sb = _sandbox(ctx, dirs, overrides={m.path: m.source})
        try:
            run: TestRun = ctx.language.run_tests(
                sb, names, cfg.mutation_timeout_s, ctx.seed, fail_fast=True
            )
            return run
        finally:
            sandbox.destroy(sb)

    # One sandbox per mutant, so runs are independent. `mutants` is sorted by id
    # and map() keeps that order, so the report is the same at any job count.
    mutants.sort(key=lambda m: m.id)
    with ThreadPoolExecutor(max_workers=cfg.mutation_jobs) as pool:
        for m, run in zip(mutants, pool.map(judge, mutants), strict=True):
            m.run = run
            m.killed = not run.ok  # a timeout is not ok, so it counts as killed

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
    auto_checked, auto_useless, auto_kills = _auto_usefulness(
        ctx, [m for m in mutants if not m.killed]
    )
    public = {
        "generated": len(mutants),
        "killed": killed,
        "score": round(score, 4),
        "survivors": survivors,
        "auto_checked": len(auto_checked),
        "auto_useless": auto_useless,
    }
    private = {
        "auto_kills": auto_kills,
        "mutants": [
            {
                "id": m.id,
                "killed": m.killed,
                "exit_code": m.run.exit_code if m.run else None,
                "timed_out": m.run.timed_out if m.run else None,
                "output": _output(m.run, names) if m.run else "",
            }
            for m in mutants
        ],
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
    if auto_useless:
        summary += f"; {len(auto_useless)} auto test file(s) kill nothing golden+hidden missed"
        status = Status.FAIL if cfg.mutation_auto_useless_blocks else Status.WARN
        return _result("mutation", status, summary, public, private)
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


def derive_seed(key: str, head: str, gate: str) -> str:
    """Per-run, per-gate seed: HMAC-SHA256(key, head + gate), first 16 hex digits (#55).

    Only derived seeds reach a sandbox, so code under test never sees the secret,
    and a seed leaked on one head SHA predicts nothing about another.
    """
    msg = f"{head}:{gate}".encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()[:16]


def run_gates(ctx: RunContext, only: list[str] | None = None) -> list[GateResult]:
    """Run the gates in order. ``ctx.seed`` is the key; each gate gets a derived seed."""
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
        seed = derive_seed(ctx.seed, ctx.head, name)
        ctx.log(f"[{name}] seed {seed}")  # derived, safe to keep for reproduction
        # a shallow copy: private_log and shared stay the same objects
        gate_ctx = dataclasses.replace(ctx, seed=seed)
        start = time.monotonic()
        try:
            result = GATES[name](gate_ctx)
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
