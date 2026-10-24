"""gates: each gate against fake gitinfo / sandbox / language adapter."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("testgate.sandbox")
pytest.importorskip("testgate.gitinfo")

from testgate import gates
from testgate.core import (
    SUITE_TRUST,
    ChangedFile,
    CoverageData,
    GateResult,
    Mutant,
    OracleMode,
    ProjectConfig,
    RunContext,
    Status,
)
from testgate.core import TestRun as _TestRun
from testgate.languages.python import PythonLanguage

HIDDEN_NAME = "test_hidden_secret_name"


def tr(
    exit_code: int = 0,
    passed: int = 3,
    failed: int = 0,
    errors: int = 0,
    output: str = "out",
    timed_out: bool = False,
) -> _TestRun:
    return _TestRun(exit_code, passed, failed, errors, 0, 0.1, output, timed_out)


class Fakes:
    """Records sandbox calls; it is also the Language adapter installed on the ctx.

    Test results come from `runs` or `decide`.
    """

    name = "python"
    source_suffixes = PythonLanguage.source_suffixes
    test_glob = PythonLanguage.test_glob
    detection_tokens = PythonLanguage.detection_tokens

    def __init__(self) -> None:
        self.builds: list[SimpleNamespace] = []
        self.runs: list[_TestRun] = []
        self.decide: Any = None
        self.pytest_calls: list[tuple[list[str], list[str] | None, bool]] = []
        self.generate: Any = None
        self.prepared: list[tuple[Any, Any, Path]] = []
        self.dests: list[str | None] = []
        self.build_proc: subprocess.CompletedProcess[str] | None = None
        self.coverage: tuple[_TestRun, CoverageData] = (tr(), CoverageData())
        self.coverage_runs: list[tuple[_TestRun, CoverageData]] = []
        self.coverage_calls: list[tuple[Any, ...]] = []
        self.proc_codes: list[int] = []
        self.destroyed = 0

    # sandbox
    def build_sandbox(
        self,
        config: Any,
        python: str,
        tests: dict[str, Path],
        overrides: dict[str, str] | None = None,
        dest: str | None = None,
        oracle: OracleMode = OracleMode.PROCESS,
    ) -> SimpleNamespace:
        sb = SimpleNamespace(
            python="py",
            root=Path("/nowhere"),
            test_dirs=tests,
            overrides=overrides or {},
            oracle=oracle,
        )
        # snapshot the hidden dir's files: the gate removes its temp copies afterwards
        hidden = tests.get("hidden")
        sb.hidden_files = sorted(p.name for p in hidden.glob("*.py")) if hidden else []
        self.builds.append(sb)
        self.dests.append(dest)
        return sb

    def run_in_sandbox(
        self, sb: Any, argv: list[str], timeout_s: int, extra_env: Any = None
    ) -> subprocess.CompletedProcess[str]:
        code = self.proc_codes.pop(0) if self.proc_codes else 0
        return subprocess.CompletedProcess(argv, code, "so", "se")

    def destroy(self, sb: Any) -> None:
        self.destroyed += 1

    # language adapter
    def package_dest(self, cfg: Any) -> str:
        return f"src/{cfg.import_name}"

    def prepare_sandbox(self, sb: Any, cfg: Any, project_root: Path) -> None:
        self.prepared.append((sb, cfg, project_root))

    def auto_file_ok(self, path: Path) -> bool:
        return path.name == "__init__.py" or (
            path.name.startswith("test_") and path.name.endswith(".py")
        )

    def build(self, sb: Any, cfg: Any, timeout_s: int) -> subprocess.CompletedProcess[str]:
        if self.build_proc is not None:
            return self.build_proc
        code = self.proc_codes.pop(0) if self.proc_codes else 0
        return subprocess.CompletedProcess(["build"], code, "so", "se")

    def failed_files(self, run: _TestRun) -> set[str]:
        return {
            line.split()[1].split("::")[0]
            for line in run.output.splitlines()
            if line.startswith(("FAILED ", "ERROR "))
        }

    def generate_mutants(
        self, path: str, source: str, lines: set[int], seed: str, max_mutants: int
    ) -> list[Mutant]:
        assert self.generate is not None
        result: list[Mutant] = self.generate(path, source, lines, seed, max_mutants)
        return result

    def run_tests(
        self,
        sb: Any,
        names: list[str],
        timeout_s: int,
        seed: str | None,
        files: list[str] | None = None,
        fail_fast: bool = False,
    ) -> _TestRun:
        self.pytest_calls.append((names, files, fail_fast))
        if self.decide is not None:
            result: _TestRun = self.decide(sb)
            return result
        return self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]

    def run_coverage(self, *args: Any) -> tuple[_TestRun, CoverageData]:
        self.coverage_calls.append(args)
        if self.coverage_runs:
            return self.coverage_runs.pop(0)
        return self.coverage


# The fixture publishes its Fakes here so make_ctx can install it as ctx.language.
_ACTIVE: Fakes | None = None


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fakes]:
    global _ACTIVE
    f = _ACTIVE = Fakes()
    monkeypatch.setattr(
        gates,
        "sandbox",
        SimpleNamespace(
            build_sandbox=f.build_sandbox, run_in_sandbox=f.run_in_sandbox, destroy=f.destroy
        ),
    )
    yield f
    _ACTIVE = None


def make_ctx(
    tmp_path: Path,
    hidden: bool = True,
    auto: bool = True,
    changed: list[ChangedFile] | None = None,
    **cfg: Any,
) -> RunContext:
    (tmp_path / "tests/golden").mkdir(parents=True)
    if auto:
        (tmp_path / "tests/auto").mkdir(parents=True)
    hidden_dir = None
    if hidden:
        hidden_dir = tmp_path / "hid"
        hidden_dir.mkdir()
    config = ProjectConfig(root=tmp_path, package="src/pkg", import_name="pkg", **cfg)
    return RunContext(
        config=config,
        base_ref="main",
        changed=changed or [],
        hidden_dir=hidden_dir,
        seed="s3cret",
        seed_is_secret=True,
        python="python",
        language=_ACTIVE or Fakes(),
    )


# --- run_gates --------------------------------------------------------------


def test_protected_fail_skips_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(
        gates,
        "gitinfo",
        SimpleNamespace(tree_hash=lambda root, ref, path: "a" if ref is None else "b"),
    )
    results = gates.run_gates(ctx)
    assert [r.name for r in results] == gates.GATE_ORDER
    assert results[0].status is Status.FAIL
    assert results[0].public["changed"] == ctx.config.protected
    assert all(r.status is Status.SKIP for r in results[1:])
    assert all(r.summary == "skipped: protected surface changed" for r in results[1:])
    assert fakes.builds == []


def test_disabled_gate_is_skipped_and_timed(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    ctx.config.gates = {name: False for name in gates.GATE_ORDER}
    ctx.config.gates["build"] = True
    results = gates.run_gates(ctx)
    by_name = {r.name: r for r in results}
    assert by_name["tests"].status is Status.SKIP
    assert by_name["tests"].summary == "disabled"
    assert by_name["build"].status is Status.PASS
    assert by_name["build"].duration_s >= 0


def test_only_filter(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    results = gates.run_gates(ctx, only=["build"])
    assert [r.status for r in results].count(Status.PASS) == 1
    assert next(r for r in results if r.name == "hygiene").status is Status.SKIP


def test_exception_becomes_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx(tmp_path)

    def boom(ctx: RunContext) -> GateResult:
        raise ValueError("secret detail /private/path")

    monkeypatch.setitem(gates.GATES, "build", boom)
    ctx.config.gates = {n: n == "build" for n in gates.GATE_ORDER}
    result = next(r for r in gates.run_gates(ctx) if r.name == "build")
    assert result.status is Status.ERROR
    assert result.blocks
    assert result.public == {"error": "ValueError"}
    assert "ValueError" in result.summary
    assert "secret detail" not in result.summary
    assert "secret detail" in result.private["error"]
    assert "Traceback" in result.private["traceback"]


# --- protected --------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "base", "status"),
    [
        ("x", "x", Status.PASS),
        (None, None, Status.PASS),
        ("x", None, Status.FAIL),
        ("x", "y", Status.FAIL),
        (None, "y", Status.FAIL),
    ],
)
def test_protected_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    now: str | None,
    base: str | None,
    status: Status,
) -> None:
    ctx = make_ctx(tmp_path)
    ctx.config.protected = ["p"]
    monkeypatch.setattr(
        gates,
        "gitinfo",
        SimpleNamespace(tree_hash=lambda root, ref, path: now if ref is None else base),
    )
    assert gates.gate_protected(ctx).status is status


def test_protected_no_base_ref(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.base_ref = None
    result = gates.gate_protected(ctx)
    assert result.status is Status.SKIP
    assert result.summary == "no base ref"


# --- hygiene ----------------------------------------------------------------


def write(root: Path, rel: str, text: str = "x = 1\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_hygiene_clean(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write(tmp_path, "tests/auto/test_a.py")
    write(tmp_path, "tests/auto/__init__.py", "")
    write(tmp_path, "tests/auto/data.json", "{}")
    write(tmp_path, "src/pkg/mod.py")
    assert gates.gate_hygiene(ctx).status is Status.PASS


def test_hygiene_conftest_in_auto(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write(tmp_path, "tests/auto/conftest.py")
    write(tmp_path, "tests/auto/helper.py")
    result = gates.gate_hygiene(ctx)
    assert result.status is Status.FAIL
    assert len(result.public["findings"]) == 2


@pytest.mark.parametrize("token", gates.FORBIDDEN_TOKENS)
def test_hygiene_forbidden_tokens(tmp_path: Path, token: str) -> None:
    ctx = make_ctx(tmp_path)
    write(
        tmp_path,
        "src/pkg/mod.py",
        f"a = 1\nb = {token!r}\n" if '"' in token else f'a = 1\nb = "{token}"\n',
    )
    result = gates.gate_hygiene(ctx)
    assert result.status is Status.FAIL
    assert "src/pkg/mod.py:2" in result.public["findings"][0]


def test_hygiene_package_imports_tests(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write(tmp_path, "src/pkg/mod.py", "from tests.golden import x\n")
    assert gates.gate_hygiene(ctx).status is Status.FAIL


def test_hygiene_auto_imports_golden_or_hidden(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    write(tmp_path, "tests/auto/test_a.py", "from tests.golden.test_x import y\n")
    write(tmp_path, "tests/auto/test_b.py", "import hidden\n")
    result = gates.gate_hygiene(ctx)
    assert result.status is Status.FAIL
    assert len(result.public["findings"]) == 2


def test_hygiene_warn_for_unchanged_files(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, changed=[ChangedFile("src/pkg/other.py", {1})])
    write(tmp_path, "src/pkg/mod.py", 'x = "TESTGATE"\n')
    assert gates.gate_hygiene(ctx).status is Status.WARN
    ctx.changed = [ChangedFile("src/pkg/mod.py", {1})]
    assert gates.gate_hygiene(ctx).status is Status.FAIL


# --- build ------------------------------------------------------------------


def test_build_pass_and_fail(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    assert gates.gate_build(ctx).status is Status.PASS
    fakes.proc_codes = [1]
    result = gates.gate_build(ctx)
    assert result.status is Status.FAIL
    assert result.public == {"exit_code": 1}
    assert "so" in result.private["output"]
    assert fakes.destroyed == 2


# --- tests ------------------------------------------------------------------


def test_tests_pass_runs_repeats(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, repeats=5)
    fakes.runs = [tr()]
    result = gates.gate_tests(ctx)
    assert result.status is Status.PASS
    assert len(fakes.pytest_calls) == 5
    assert fakes.pytest_calls[0][0] == ["golden", "auto"]
    assert result.public["passed"] == 3
    assert ctx.shared["visible_run"].passed == 3


def test_tests_failed_run_one(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    fakes.runs = [tr(1, passed=2, failed=1)]
    result = gates.gate_tests(ctx)
    assert result.status is Status.FAIL
    assert result.summary == "tests failed"
    assert len(fakes.pytest_calls) == 1
    assert result.public["failed"] == 1


def test_tests_flaky(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, repeats=5)
    fakes.runs = [tr(), tr(), tr(1, passed=2, failed=1)]
    result = gates.gate_tests(ctx)
    assert result.status is Status.FAIL
    assert result.summary == "flaky: passed run 1, failed run 3"
    assert result.public["runs"] == 3
    assert len(fakes.pytest_calls) == 3


def test_tests_zero_collected(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    fakes.runs = [tr(0, passed=0)]
    result = gates.gate_tests(ctx)
    assert result.status is Status.FAIL
    assert "no tests" in result.summary


def test_tests_without_dirs_fails(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    (tmp_path / "tests/golden").rmdir()
    (tmp_path / "tests/auto").rmdir()
    assert gates.gate_tests(ctx).status is Status.FAIL


# --- hidden -----------------------------------------------------------------


def test_suite_names_are_single_sourced(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert set(gates._suite_paths(ctx)) == set(SUITE_TRUST)
    assert list(gates._test_dirs(ctx, tuple(SUITE_TRUST))) == list(SUITE_TRUST)


@pytest.mark.parametrize("mode", list(OracleMode))
def test_sandbox_placement_follows_the_adapter_mode(
    tmp_path: Path, fakes: Fakes, mode: OracleMode
) -> None:
    fakes.oracle = mode  # type: ignore[attr-defined]
    ctx = make_ctx(tmp_path)
    fakes.runs = [tr(0, passed=1)]
    gates.gate_hidden(ctx)
    assert fakes.builds[0].oracle is mode


def test_adapter_without_a_mode_is_in_process(tmp_path: Path, fakes: Fakes) -> None:
    assert make_ctx(tmp_path).oracle is OracleMode.IN_PROCESS


def test_hidden_required_vs_optional(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, hidden=False)
    result = gates.gate_hidden(ctx)
    assert (result.status, result.summary) == (Status.FAIL, "no hidden tests")
    ctx.config.hidden_required = False
    assert gates.gate_hidden(ctx).status is Status.SKIP


def test_hidden_public_is_counts_only(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    out = f"FAILED {HIDDEN_NAME} - AssertionError: assert 41 == 42"
    fakes.runs = [tr(1, passed=7, failed=2, output=out)]
    result = gates.gate_hidden(ctx)
    assert result.status is Status.FAIL
    assert result.summary == "hidden: 7 passed, 2 failed"
    assert result.public == {"passed": 7, "failed": 2, "errors": 0}
    blob = repr(result.public) + result.summary
    assert HIDDEN_NAME not in blob
    assert "AssertionError" not in blob
    assert HIDDEN_NAME not in repr(result.private)  # #51: not even the private log
    assert list(fakes.builds[0].test_dirs) == ["hidden"]
    assert fakes.pytest_calls[0][0] == ["hidden"]


def test_hidden_pass_and_empty(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    fakes.runs = [tr(0, passed=4)]
    assert gates.gate_hidden(ctx).status is Status.PASS
    fakes.runs = [tr(0, passed=0)]
    assert gates.gate_hidden(ctx).status is Status.FAIL


# --- coverage ---------------------------------------------------------------


def cov(statements: dict[str, set[int]], covered: dict[str, set[int]]) -> CoverageData:
    return CoverageData(covered=covered, statements=statements)


def test_coverage_thresholds(tmp_path: Path, fakes: Fakes) -> None:
    changed = [ChangedFile("src/pkg/a.py", {1, 2, 3, 4, 99})]
    ctx = make_ctx(tmp_path, changed=changed, coverage_min_total=0.5, coverage_min_changed=0.5)
    data = cov({"src/pkg/a.py": {1, 2, 3, 4, 5, 6}}, {"src/pkg/a.py": {1, 2, 3}})
    fakes.coverage = (tr(), data)
    result = gates.gate_coverage(ctx)
    assert result.status is Status.PASS
    assert ctx.shared["coverage"] is data
    assert result.public["total"] == 0.5
    assert result.public["changed"] == 0.75  # line 99 is not a statement
    assert result.public["uncovered_changed"] == ["src/pkg/a.py:4"]

    ctx.config.coverage_min_changed = 0.9
    failed = gates.gate_coverage(ctx)
    assert failed.status is Status.FAIL
    assert "changed 75% < 90%" in failed.summary

    ctx.config.coverage_min_changed = 0.5
    ctx.config.coverage_min_total = 0.6
    assert gates.gate_coverage(ctx).status is Status.FAIL


def test_coverage_without_changed_skips_changed_check(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path, coverage_min_changed=1.0)
    fakes.coverage = (tr(), cov({"a.py": {1, 2}}, {"a.py": {1}}))
    result = gates.gate_coverage(ctx)
    assert result.status is Status.PASS
    assert result.public["changed"] is None


def test_coverage_uncovered_capped_at_20_per_file(tmp_path: Path, fakes: Fakes) -> None:
    lines = set(range(1, 41))
    ctx = make_ctx(tmp_path, changed=[ChangedFile("a.py", lines)])
    fakes.coverage = (tr(), cov({"a.py": lines}, {"a.py": set()}))
    assert len(gates.gate_coverage(ctx).public["uncovered_changed"]) == 20


def test_coverage_fails_when_tests_fail(tmp_path: Path, fakes: Fakes) -> None:
    ctx = make_ctx(tmp_path)
    fakes.coverage = (tr(1, failed=1), cov({"a.py": {1}}, {"a.py": {1}}))
    assert gates.gate_coverage(ctx).status is Status.FAIL


# --- mutation ---------------------------------------------------------------


def install_mutator(fakes: Fakes, per_file: dict[str, int]) -> list[Any]:
    calls: list[Any] = []

    def generate(
        path: str, source: str, lines: set[int], seed: str, max_mutants: int
    ) -> list[Mutant]:
        calls.append((path, set(lines), max_mutants))
        return [
            Mutant(
                f"{path}:{i}:ROR",
                path,
                i,
                "ROR",
                "a < b",
                "a <= b",
                f"SURVIVE {i}" if i % 2 else f"KILL {i}",
            )
            for i in range(1, per_file.get(path, 0) + 1)
        ]

    fakes.generate = generate
    return calls


def mutation_ctx(tmp_path: Path, **cfg: Any) -> RunContext:
    changed = [ChangedFile("src/pkg/a.py", {1, 2, 3, 4, 50}), ChangedFile("src/pkg/b.py", {1})]
    cfg.setdefault("mutation_jobs", 1)  # the fakes pop queued results, so keep runs ordered
    ctx = make_ctx(tmp_path, changed=changed, **cfg)
    write(tmp_path, "src/pkg/a.py", "a = 1\n")
    write(tmp_path, "src/pkg/b.py", "b = 1\n")
    ctx.shared["coverage"] = cov(
        {"src/pkg/a.py": {1, 2, 3, 4, 50}, "src/pkg/b.py": {1}},
        {"src/pkg/a.py": {1, 2, 3, 4}, "src/pkg/b.py": set()},
    )
    return ctx


def test_mutation_score_survivors_and_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    calls = install_mutator(fakes, {"src/pkg/a.py": 4})
    ctx = mutation_ctx(tmp_path, mutation_min_score=0.5, mutation_max_shown=1)
    fakes.decide = lambda sb: (
        tr(0) if not sb.overrides or "SURVIVE" in next(iter(sb.overrides.values())) else tr(1, 1, 1)
    )
    result = gates.gate_mutation(ctx)
    # candidate lines = changed ∩ covered; b.py has none covered
    assert calls == [("src/pkg/a.py", {1, 2, 3, 4}, 50)]
    assert result.public["generated"] == 4
    assert result.public["killed"] == 2
    assert result.public["score"] == 0.5
    assert result.status is Status.PASS
    assert len(result.public["survivors"]) == 1  # capped per file
    assert result.public["survivors"][0]["id"] == "src/pkg/a.py:1:ROR"
    assert set(result.public["survivors"][0]) == {"id", "line", "operator", "before", "after"}
    # golden + hidden only, never auto; baseline first without -x, then -x on every mutant
    assert all(list(sb.test_dirs) == ["golden", "hidden"] for sb in fakes.builds)
    assert fakes.pytest_calls[0] == (["golden", "hidden"], None, False)
    assert all(
        names == ["golden", "hidden"] and files is None and fail_fast
        for names, files, fail_fast in fakes.pytest_calls[1:]
    )
    assert len(result.private["mutants"]) == 4

    ctx.config.mutation_min_score = 0.6
    assert gates.gate_mutation(ctx).status is Status.FAIL


def test_mutation_timeout_counts_as_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_mutator(fakes, {"src/pkg/a.py": 2})
    ctx = mutation_ctx(tmp_path, mutation_min_score=1.0)
    fakes.runs = [tr(0), tr(124, passed=0, timed_out=True)]  # baseline passes, mutants time out
    result = gates.gate_mutation(ctx)
    assert result.public["killed"] == 2
    assert result.status is Status.PASS


def test_mutation_no_mutants_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_mutator(fakes, {})
    result = gates.gate_mutation(mutation_ctx(tmp_path))
    assert (result.status, result.summary) == (Status.SKIP, "no eligible lines")


def test_mutation_without_changed_uses_all_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    calls = install_mutator(fakes, {})
    ctx = mutation_ctx(tmp_path)
    ctx.changed = []
    gates.gate_mutation(ctx)
    assert calls == [("src/pkg/a.py", {1, 2, 3, 4}, 50)]


def test_mutation_total_capped_at_max_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_mutator(fakes, {"src/pkg/a.py": 4, "src/pkg/b.py": 4})
    ctx = mutation_ctx(tmp_path, mutation_max_mutants=5, mutation_min_score=0.0)
    ctx.shared["coverage"].covered["src/pkg/b.py"] = {1}
    fakes.runs = [tr(0), tr(1, 0, 1)]
    assert gates.gate_mutation(ctx).public["generated"] == 5


def test_mutation_runs_coverage_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_mutator(fakes, {"src/pkg/a.py": 1})
    ctx = mutation_ctx(tmp_path)
    ctx.shared.pop("coverage")
    fakes.coverage = (tr(), cov({"src/pkg/a.py": {1}}, {"src/pkg/a.py": {1}}))
    fakes.runs = [tr(0), tr(1, 0, 1)]
    result = gates.gate_mutation(ctx)
    assert result.public["generated"] == 1
    assert "coverage" in ctx.shared


def test_mutation_fails_when_baseline_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_mutator(fakes, {"src/pkg/a.py": 2})
    ctx = mutation_ctx(tmp_path)
    fakes.runs = [tr(1, 1, 1)]
    result = gates.gate_mutation(ctx)
    assert result.status is Status.FAIL  # fail closed (#63)
    assert "unmutated" in result.summary
    assert len(fakes.pytest_calls) == 1  # no mutant was run


def test_mutation_auto_useless_warns_then_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_mutator(fakes, {"src/pkg/a.py": 2})  # line 1 survives, line 2 is killed
    ctx = mutation_ctx(tmp_path, mutation_min_score=0.5)
    write(tmp_path, "tests/auto/test_weak.py", "def test_t(): pass\n")
    write(tmp_path, "tests/auto/test_strong.py", "def test_t(): pass\n")

    def decide(sb: Any) -> _TestRun:
        if not sb.overrides:
            return tr(0)  # baseline
        if list(sb.test_dirs) == ["auto"]:
            files = fakes.pytest_calls[-1][1] or []
            return tr(1, 0, 1) if "tests/auto/test_strong.py" in files else tr(0)
        return tr(0) if "SURVIVE" in next(iter(sb.overrides.values())) else tr(1, 0, 1)

    fakes.decide = decide
    result = gates.gate_mutation(ctx)
    assert result.status is Status.WARN
    assert result.public["auto_checked"] == 2
    assert result.public["auto_useless"] == ["test_weak.py"]
    assert result.private["auto_kills"] == {
        "test_strong.py": ["src/pkg/a.py:1:ROR"],
        "test_weak.py": [],
    }
    # auto runs: one per (file, survivor), each on the auto dir only with -x
    auto_calls = [c for c in fakes.pytest_calls if c[0] == []]
    assert sorted(c[1] or [] for c in auto_calls) == [
        ["tests/auto/test_strong.py"],
        ["tests/auto/test_weak.py"],
    ]
    assert all(c[2] for c in auto_calls)

    ctx.config.mutation_auto_useless_blocks = True
    assert gates.gate_mutation(ctx).status is Status.FAIL


def test_mutation_no_survivors_skips_auto_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_mutator(fakes, {"src/pkg/a.py": 1})
    ctx = mutation_ctx(tmp_path, mutation_min_score=0.5)
    write(tmp_path, "tests/auto/test_weak.py", "def test_t(): pass\n")
    fakes.decide = lambda sb: tr(0) if not sb.overrides else tr(1, 0, 1)
    result = gates.gate_mutation(ctx)
    assert result.status is Status.PASS
    assert result.public["auto_checked"] == 0
    assert result.public["auto_useless"] == []


def install_archive(monkeypatch: pytest.MonkeyPatch, present: list[str]) -> list[Any]:
    calls: list[Any] = []

    def archive(root: Path, ref: str, paths: list[str], dest: Path) -> list[str]:
        calls.append((ref, list(paths)))
        for rel in present:
            (dest / rel).mkdir(parents=True, exist_ok=True)
            (dest / rel / "__init__.py").write_text("")
        return [p for p in paths if p in present]

    monkeypatch.setattr(gates, "gitinfo", SimpleNamespace(archive=archive))
    return calls


def test_coverage_baseline_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    calls = install_archive(monkeypatch, ["src/pkg", "tests/golden"])
    ctx = make_ctx(tmp_path, coverage_min_changed=0.0)
    current = cov({"src/pkg/a.py": {1, 2, 3, 4}}, {"src/pkg/a.py": {1, 2}})
    baseline = cov({"src/pkg/a.py": {1, 2, 3, 4}}, {"src/pkg/a.py": {1, 2, 3, 4}})
    fakes.coverage_runs = [(tr(), current), (tr(), baseline)]
    fakes.runs = [tr(0)]  # the hidden suite passes against the base ref
    result = gates.gate_coverage(ctx)
    assert result.status is Status.FAIL
    assert "dropped 50.0% from 100%" in result.summary
    assert result.public["baseline_total"] == 1.0
    assert result.public["drop"] == 0.5
    assert result.public["baseline_hidden_excluded"] == 0
    assert ctx.shared["baseline_coverage"] is baseline
    assert calls == [("main", ["src/pkg", "tests/golden", "tests/auto"])]
    # builds: current coverage, hidden-at-base probe, baseline coverage
    assert list(fakes.builds[1].test_dirs) == ["hidden"]
    base_root = fakes.builds[2].test_dirs["golden"].parents[1]
    assert base_root != ctx.config.root
    # the language gets the real root, and package_dest comes from the adapter
    assert [p for _, _, p in fakes.prepared] == [ctx.config.root] * 3
    assert fakes.dests == ["src/pkg"] * 3
    assert list(fakes.builds[2].test_dirs) == ["golden", "hidden"]
    assert fakes.builds[2].test_dirs["golden"] == base_root / "tests/golden"
    assert fakes.builds[2].test_dirs["hidden"] == ctx.hidden_dir

    ctx.config.coverage_max_drop = 0.5
    fakes.coverage_runs = [(tr(), current), (tr(), baseline)]
    fakes.runs = [tr(0)]
    assert gates.gate_coverage(ctx).status is Status.PASS


def test_coverage_baseline_excludes_hidden_files_failing_at_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_archive(monkeypatch, ["src/pkg", "tests/golden"])
    ctx = make_ctx(tmp_path, coverage_min_changed=0.0)
    assert ctx.hidden_dir is not None
    (ctx.hidden_dir / "test_old.py").write_text("def test_o(): pass\n")
    (ctx.hidden_dir / "test_new.py").write_text("def test_n(): pass\n")  # covers code main lacks
    current = cov({"src/pkg/a.py": {1, 2}}, {"src/pkg/a.py": {1, 2}})
    baseline = cov({"src/pkg/a.py": {1, 2}}, {"src/pkg/a.py": {1}})
    fakes.coverage_runs = [(tr(), current), (tr(), baseline)]
    fakes.runs = [
        tr(
            1,
            1,
            1,
            output="PASSED tests/hidden/test_old.py::test_o\nFAILED tests/hidden/test_new.py::test_n - ImportError\n",
        )
    ]
    result = gates.gate_coverage(ctx)
    assert result.status is Status.PASS  # 50% -> 100% is a rise, not a drop
    assert result.public["baseline_hidden_excluded"] == 1
    assert result.public["drop"] == -0.5
    assert "baseline_skipped" not in result.public
    assert fakes.builds[2].test_dirs["hidden"] != ctx.hidden_dir
    assert fakes.builds[2].hidden_files == ["test_old.py"]
    assert not any("test_new.py" in line for line in ctx.private_log)  # #51: count only
    assert "hidden files excluded at base: 1" in ctx.private_log


def test_coverage_baseline_no_hidden_left_at_base_falls_back_to_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_archive(monkeypatch, ["src/pkg", "tests/golden"])
    ctx = make_ctx(tmp_path, coverage_min_changed=0.0)
    assert ctx.hidden_dir is not None
    (ctx.hidden_dir / "test_new.py").write_text("def test_n(): pass\n")
    fakes.coverage_runs = [
        (tr(), cov({"src/pkg/a.py": {1}}, {"src/pkg/a.py": {1}})),
        (tr(), cov({"src/pkg/a.py": {1}}, {"src/pkg/a.py": {1}})),
    ]
    fakes.runs = [tr(1, 0, 1, output="FAILED tests/hidden/test_new.py::test_n\n")]
    result = gates.gate_coverage(ctx)
    assert result.public["baseline_hidden_excluded"] == 1
    assert list(fakes.builds[2].test_dirs) == ["golden"]  # baseline from golden alone
    assert result.public["drop"] == 0.0


def test_coverage_baseline_skipped_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    install_archive(monkeypatch, ["tests/golden"])  # package absent at base
    ctx = make_ctx(tmp_path, coverage_min_changed=0.0)
    fakes.coverage = (tr(), cov({"src/pkg/a.py": {1}}, {"src/pkg/a.py": set()}))
    result = gates.gate_coverage(ctx)
    assert result.status is Status.PASS
    assert result.public["baseline_skipped"] == "package absent at base ref"
    assert result.public["drop"] is None

    install_archive(monkeypatch, ["src/pkg", "tests/golden"])
    fakes.coverage_runs = [fakes.coverage, (tr(1, 0, 1), CoverageData())]
    fakes.runs = [tr(0)]
    assert gates.gate_coverage(ctx).public["baseline_skipped"] == "tests fail at base ref"

    ctx.base_ref = None
    assert gates.gate_coverage(ctx).public["baseline_skipped"] == "no base ref"


def test_protected_skips_on_first_adoption(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.config.raw["_source"] = "worktree"
    result = gates.gate_protected(ctx)
    assert result.status is Status.SKIP
    assert "first adoption" in result.summary


def test_mutation_parallel_jobs_give_same_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fakes: Fakes
) -> None:
    import threading
    import time

    install_mutator(fakes, {"src/pkg/a.py": 4})
    seen: list[str] = []
    lock = threading.Lock()

    def decide(sb: Any) -> _TestRun:
        if not sb.overrides:
            return tr(0)
        text = next(iter(sb.overrides.values()))
        with lock:
            seen.append(threading.current_thread().name)
        time.sleep(0.05)  # let the pool overlap
        return tr(0) if "SURVIVE" in text else tr(1, 0, 1)

    fakes.decide = decide
    serial = gates.gate_mutation(
        mutation_ctx(tmp_path / "serial", mutation_min_score=0.0, mutation_jobs=1)
    )
    seen.clear()
    parallel = gates.gate_mutation(
        mutation_ctx(tmp_path / "parallel", mutation_min_score=0.0, mutation_jobs=4)
    )
    assert parallel.public == serial.public
    assert [m["id"] for m in parallel.private["mutants"]] == [
        m["id"] for m in serial.private["mutants"]
    ]
    assert [m["id"] for m in parallel.private["mutants"]] == sorted(
        m["id"] for m in parallel.private["mutants"]
    )
    assert len(set(seen)) > 1  # more than one worker thread ran mutants


# --- fail closed on missing data (#63) ---------------------------------------


def test_coverage_fails_when_changed_source_is_not_measured(tmp_path: Path, fakes: Fakes) -> None:
    write(tmp_path, "src/pkg/a.py", "x = 1\n")
    write(tmp_path, "src/pkg/b.py", "y = 2\n")
    changed = [ChangedFile("src/pkg/a.py", {1}), ChangedFile("src/pkg/b.py", {1})]
    ctx = make_ctx(tmp_path, changed=changed, coverage_min_changed=0.0)
    fakes.coverage = (tr(), cov({"src/pkg/a.py": {1}}, {"src/pkg/a.py": {1}}))
    result = gates.gate_coverage(ctx)
    assert result.status is Status.FAIL
    assert result.public["unmeasured_changed"] == ["src/pkg/b.py"]
    assert "not measured" in result.summary


def test_coverage_fails_on_empty_data_with_changed_sources(tmp_path: Path, fakes: Fakes) -> None:
    write(tmp_path, "src/pkg/a.py", "x = 1\n")
    ctx = make_ctx(tmp_path, changed=[ChangedFile("src/pkg/a.py", {1})], coverage_min_changed=0.0)
    fakes.coverage = (tr(), cov({"src/pkg/a.py": set()}, {}))  # total() alone would say 100%
    result = gates.gate_coverage(ctx)
    assert result.status is Status.FAIL
    assert "no statements measured" in result.summary


def test_coverage_comment_only_change_in_measured_file_passes(tmp_path: Path, fakes: Fakes) -> None:
    write(tmp_path, "src/pkg/a.py", "# note\nx = 1\n")
    ctx = make_ctx(tmp_path, changed=[ChangedFile("src/pkg/a.py", {1})])
    fakes.coverage = (tr(), cov({"src/pkg/a.py": {2}}, {"src/pkg/a.py": {2}}))
    result = gates.gate_coverage(ctx)
    assert result.status is Status.PASS
    assert result.public["changed"] is None


def test_coverage_ignores_changes_outside_the_package(tmp_path: Path, fakes: Fakes) -> None:
    write(tmp_path, "docs/x.py", "x = 1\n")
    ctx = make_ctx(tmp_path, changed=[ChangedFile("docs/x.py", {1})])
    fakes.coverage = (tr(), cov({}, {}))
    assert gates.gate_coverage(ctx).status is Status.PASS


# --- changed lines cannot hide from coverage (#64) --------------------------


@pytest.mark.parametrize(
    "line",
    [
        "x = 1  # pragma: no cover",
        "x = 1  # PRAGMA: NO COVER",
        "if x:  # pragma: no branch",
        "/* v8 ignore next */",
        "/* c8 ignore start */",
        "/* istanbul ignore if */",
    ],
)
def test_hygiene_fails_coverage_exclusion_on_changed_line(tmp_path: Path, line: str) -> None:
    write(tmp_path, "src/pkg/a.py", f"y = 0\n{line}\n")
    ctx = make_ctx(tmp_path, changed=[ChangedFile("src/pkg/a.py", {2})])
    result = gates.gate_hygiene(ctx)
    assert result.status is Status.FAIL
    assert any(f.startswith("src/pkg/a.py:2: coverage") for f in result.public["findings"])


def test_hygiene_ignores_exclusion_on_unchanged_line(tmp_path: Path) -> None:
    write(tmp_path, "src/pkg/a.py", "x = 1  # pragma: no cover\ny = 2\n")
    ctx = make_ctx(tmp_path, changed=[ChangedFile("src/pkg/a.py", {2})])
    assert gates.gate_hygiene(ctx).status is Status.PASS


def test_hygiene_without_git_checks_every_line(tmp_path: Path) -> None:
    write(tmp_path, "src/pkg/a.py", "x = 1  # pragma: no cover\n")
    ctx = make_ctx(tmp_path)
    assert gates.gate_hygiene(ctx).status is Status.FAIL


# --- exit overrides and test weakening (#67) --------------------------------


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("os._exit(0)", "exit override"),
        ('getattr(os, "_" + "exit")(0)', "exit override"),
        ("getattr(OS, '_EX' 'IT')(0)", "exit override"),
        ("process.exit(0);", "exit override"),
        ("process.reallyExit(0);", "exit override"),
        ("process.exitCode = 0;", "exit override"),
        ("atexit.register(done)", "exit hook"),
        ('__import__("at" + "exit")', "exit hook"),
        ("signal.signal(signal.SIGTERM, h)", "signal or exit handler"),
        ('process.on("exit", () => {});', "signal or exit handler"),
        ("@pytest.mark.skip", "skip, xfail or only marker"),
        ("@pytest.mark.xfail(strict=False)", "skip, xfail or only marker"),
        ('pytest.skip("later")', "skip, xfail or only marker"),
        ("@unittest.skip('x')", "skip, xfail or only marker"),
        ('test.only("a", () => {});', "skip, xfail or only marker"),
        ('it.skip("a", () => {});', "skip, xfail or only marker"),
        ('describe.todo("a");', "skip, xfail or only marker"),
        ('xit("a", () => {});', "skip, xfail or only marker"),
        ("assert True", "trivial assertion"),
        ("    assert 1", "trivial assertion"),
        ("expect(true).toBe(true);", "trivial assertion"),
        ("frames = inspect.stack()", "frame or module introspection"),
        ("f = sys._getframe(1)", "frame or module introspection"),
        ("for o in gc.get_objects():", "frame or module introspection"),
        ("for m in list(sys.modules):", "frame or module introspection"),
        ("caller = frame.f_back", "frame or module introspection"),
    ],
)
def test_hygiene_fails_test_weakening_on_changed_line(
    tmp_path: Path, line: str, reason: str
) -> None:
    ctx = make_ctx(tmp_path, changed=[ChangedFile("tests/auto/test_x.py", {2})])
    write(tmp_path, "tests/auto/test_x.py", f"y = 0\n{line}\n")
    result = gates.gate_hygiene(ctx)
    assert result.status is Status.FAIL
    assert f"tests/auto/test_x.py:2: {reason}" in result.public["findings"]


@pytest.mark.parametrize(
    "line",
    [
        "sys.exit(main())",
        "exit_code = run()",
        "assert result is True",
        "assert total == 1",
        "expect(add(1, 2)).toBe(3);",
        'test("adds", () => {});',
        "if os.path.exists(p):",
        "signals = []",
    ],
)
def test_hygiene_allows_ordinary_lines(tmp_path: Path, line: str) -> None:
    write(tmp_path, "src/pkg/a.py", f"{line}\n")
    ctx = make_ctx(tmp_path, changed=[ChangedFile("src/pkg/a.py", {1})])
    assert gates.gate_hygiene(ctx).status is Status.PASS
