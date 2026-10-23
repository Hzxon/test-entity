"""Shared types for every testgate module.

Every module imports from here and nowhere else inside the package, so the
builders can work in parallel against one contract. Keep this file free of
imports from sibling modules.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class Status(str, enum.Enum):
    """Outcome of one gate."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"  # finding worth reading, does not block
    SKIP = "skip"  # gate disabled or its inputs are missing (e.g. no git, no hidden dir)
    ERROR = "error"  # the gate itself crashed; treated as FAIL for the verdict


@dataclass
class ChangedFile:
    """Lines added or modified in one file between the base ref and the worktree."""

    path: str  # repo-relative, posix separators
    lines: set[int] = field(default_factory=set)


@dataclass
class ProjectConfig:
    """Parsed testgate.yaml. Field names mirror the YAML keys.

    Paths are repo-relative strings; resolve them against ``root`` at use time.
    """

    root: Path  # absolute project root (the directory holding testgate.yaml)
    package: str  # source root to copy into the sandbox, e.g. "src/discount" or "discount"
    import_name: str  # top-level module the build gate imports, e.g. "discount"; js: entry file
    language: str = "python"  # "python" | "js" | "http"; picks the adapter in languages/
    http_start: list[str] = field(default_factory=list)  # service command; "{port}" is substituted
    http_health: str = ""  # URL polled until 2xx; "{port}" is substituted
    http_startup_timeout_s: int = 60
    http_openapi: str | None = None  # OpenAPI document, repo-relative; enables route coverage
    http_env: dict[str, str] = field(default_factory=dict)  # extra environment for the service
    golden: str = "tests/golden"  # protected, visible tests
    auto: str = "tests/auto"  # agent-writable, visible tests
    hidden: str | None = None  # local path to hidden tests (may be outside root); overrides env
    hidden_env: str = "TESTGATE_HIDDEN_DIR"  # env var naming the hidden test dir
    protected: list[str] = field(
        default_factory=lambda: ["tests/golden", "testgate.yaml", ".github/workflows"]
    )
    seed_env: str = "TESTGATE_SEED"  # env var holding the secret Hypothesis seed
    repeats: int = 5  # test runs for the flaky check (TestGen-LLM uses 5)
    hidden_required: bool = True  # FAIL (not SKIP) when no hidden dir is available
    coverage_min_total: float = 0.0  # 0..1 minimum total line coverage
    coverage_min_changed: float = 0.9  # 0..1 minimum coverage of changed lines
    coverage_max_drop: float = 0.0  # 0..1 allowed drop of total coverage vs the base ref
    mutation_min_score: float = 0.8  # 0..1 killed / generated
    mutation_max_mutants: int = 50  # sampled with the seed when more are eligible
    mutation_max_shown: int = 7  # survivors listed per changed file (Google caps at 7 x files)
    mutation_timeout_s: int = 60  # per mutant run
    mutation_auto_useless_blocks: bool = (
        False  # FAIL (not WARN) when an auto test file kills nothing new
    )
    mutation_jobs: int = max(1, (os.cpu_count() or 2) // 2)  # mutant runs in flight at once
    network: bool = False  # sandbox blocks sockets when False
    timeout_s: int = 600  # per pytest invocation
    python: str | None = None  # interpreter for the sandbox; None = sys.executable
    gates: dict[str, bool] = field(
        default_factory=lambda: {
            "protected": True,
            "hygiene": True,
            "build": True,
            "tests": True,
            "hidden": True,
            "coverage": True,
            "mutation": True,
        }
    )
    raw: dict[str, Any] = field(default_factory=dict)  # the YAML as loaded, for reports


@dataclass
class RunContext:
    """Everything a gate may read. Gates must not write to the project tree."""

    config: ProjectConfig
    base_ref: str | None  # git ref the change is compared with; None = no git (--no-git)
    changed: list[ChangedFile]  # empty when base_ref is None; gates then treat all lines as changed
    hidden_dir: Path | None  # resolved hidden test dir, or None
    seed: str  # secret seed if the env var is set, else a per-run random seed
    seed_is_secret: bool
    python: str  # interpreter path used inside the sandbox
    language: Any = None  # the Language adapter (typed Any: core must not import languages/)
    private_log: list[str] = field(default_factory=list)  # full detail; never shown to the author
    shared: dict[str, Any] = field(default_factory=dict)  # gate-to-gate hand-off (coverage data)

    def log(self, line: str) -> None:
        self.private_log.append(line)


@dataclass
class GateResult:
    """What one gate decided.

    ``public`` is safe to show the author of the change (an AI agent): counts,
    scores, file:line of surviving mutants. It must never contain hidden test
    names, assertion messages, or the seed.
    ``private`` may contain anything and goes to the private log only.
    """

    name: str
    status: Status
    summary: str  # one line, public
    public: dict[str, Any] = field(default_factory=dict)
    private: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    @property
    def blocks(self) -> bool:
        return self.status in (Status.FAIL, Status.ERROR)


@dataclass
class TestRun:
    """One pytest invocation inside the sandbox."""

    exit_code: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration_s: float
    output: str  # full stdout+stderr; private
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class CoverageData:
    """Line coverage keyed by repo-relative source path."""

    covered: dict[str, set[int]] = field(default_factory=dict)
    statements: dict[str, set[int]] = field(default_factory=dict)

    def total(self) -> float:
        n = sum(len(s) for s in self.statements.values())
        c = sum(len(s) for s in self.covered.values())
        return (c / n) if n else 1.0


@dataclass
class Mutant:
    """One single-point change to one source file."""

    id: str  # stable: "<path>:<line>:<operator>"
    path: str  # repo-relative source path
    line: int
    operator: str  # ROR, AOR, LCR, UOI, CRP, SBR, RVR
    before: str  # unparsed original node, short
    after: str  # unparsed mutated node, short
    source: str  # full mutated file text
    killed: bool | None = None  # None until run
    run: TestRun | None = None


@dataclass
class Report:
    """Final verdict plus every gate result."""

    verdict: Status  # PASS or FAIL
    gates: list[GateResult]
    base_ref: str | None
    changed_files: int
    changed_lines: int
    seed_is_secret: bool
    version: str = "0.1.0"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "verdict": self.verdict.value,
            "base_ref": self.base_ref,
            "changed_files": self.changed_files,
            "changed_lines": self.changed_lines,
            "seed_is_secret": self.seed_is_secret,
            "gates": [
                {
                    "name": g.name,
                    "status": g.status.value,
                    "summary": g.summary,
                    "duration_s": round(g.duration_s, 2),
                    **g.public,
                }
                for g in self.gates
            ],
        }


@dataclass
class Sandbox:
    """A fresh temp root holding the package copy and the chosen test dirs."""

    root: Path  # temp dir
    src_root: Path  # <root>/<package_dest>
    test_dirs: dict[str, Path]  # name -> <root>/tests/<name>
    python: str
    env: dict[str, str]
    network: bool


class Language(Protocol):
    """Everything language-specific. See CONTRACT.md, "Language adapters"."""

    name: str
    source_suffixes: tuple[str, ...]
    test_glob: str
    detection_tokens: tuple[str, ...]

    def package_dest(self, cfg: ProjectConfig) -> str: ...
    def prepare_sandbox(self, sb: Sandbox, cfg: ProjectConfig, project_root: Path) -> None: ...
    def auto_file_ok(self, path: Path) -> bool: ...
    def build(self, sb: Sandbox, cfg: ProjectConfig, timeout_s: int) -> Any: ...
    def run_tests(
        self,
        sb: Sandbox,
        names: list[str],
        timeout_s: int,
        seed: str,
        files: list[str] | None = None,
        fail_fast: bool = False,
    ) -> TestRun: ...
    def run_coverage(
        self, sb: Sandbox, names: list[str], timeout_s: int, seed: str, repo_package: str
    ) -> tuple[TestRun, CoverageData]: ...
    def generate_mutants(
        self, path: str, source: str, lines: set[int], seed: str, max_mutants: int
    ) -> list[Mutant]: ...
    def failed_files(self, run: TestRun) -> set[str]:
        ...
        # test files (sandbox-relative, e.g. "tests/hidden/test_x.py") with a failure or error in `run`


class GateError(Exception):
    """Raised by a gate when it cannot run; the runner records Status.ERROR."""
