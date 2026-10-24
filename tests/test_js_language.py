"""JsLanguage against a real vitest install; skipped when node or npm is unavailable."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from testgate.core import GateError, ProjectConfig, Sandbox
from testgate.languages.js import TOOLCHAIN_ENV, JsLanguage, trusted_modules

# In CI a missing toolchain is a failure, not a skip (#72).
CI = bool(os.environ.get("CI"))
pytestmark = pytest.mark.skipif(
    not CI and shutil.which("node") is None, reason="node not installed"
)

LANG = JsLanguage()

M_JS = "export function add(a, b) {\n  return a + b;\n}\n\nexport function unused() {\n  return 1;\n}\n"
GOLDEN = 'import { test, expect } from "vitest";\nimport { add } from "../../src/m.js";\n\ntest("add", () => {\n  expect(add(1, 2)).toBe(3);\n});\n'
HIDDEN = 'import { test, expect } from "vitest";\nimport { add } from "../../src/m.js";\n\ntest("add neg", () => {\n  expect(add(-1, 1)).toBe(0);\n});\n'
FAILING = (
    'import { test, expect } from "vitest";\n\ntest("bad", () => {\n  expect(1).toBe(2);\n});\n'
)
NET = (
    'import { test, expect } from "vitest";\n\n'
    'test("net", async () => {\n  await expect(fetch("http://127.0.0.1:9")).rejects.toThrow("network disabled");\n});\n'
)


@pytest.fixture(scope="session")
def jsdeps(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("npm") is None and not CI:
        pytest.skip("npm not installed")
    d = tmp_path_factory.mktemp("jsdeps")
    # the example's lockfile pins vitest, coverage-v8, fast-check and typescript;
    # `npm ci` from it is reproducible where an ad-hoc `npm install` is not (npm 10)
    example = Path(__file__).resolve().parent.parent / "examples" / "discount-js"
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(example / name, d / name)
    proc = subprocess.run(
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=d,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        (pytest.fail if CI else pytest.skip)(f"npm ci failed: {proc.stderr[-300:]}")
    return d


@pytest.fixture
def project(tmp_path: Path, jsdeps: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(TOOLCHAIN_ENV, str(jsdeps / "node_modules"))
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "tests" / "golden").mkdir(parents=True)
    (root / "tests" / "hidden").mkdir(parents=True)
    (root / "package.json").write_text('{"name": "p", "type": "module"}')
    (root / "src" / "m.js").write_text(M_JS)
    (root / "tests" / "golden" / "m.test.js").write_text(GOLDEN)
    (root / "tests" / "hidden" / "h.test.js").write_text(HIDDEN)
    (root / "node_modules").symlink_to(jsdeps / "node_modules", target_is_directory=True)
    return root


def make_sandbox(project: Path, tmp_path: Path, network: bool = False) -> Sandbox:
    root = tmp_path / "sb"
    shutil.copytree(project / "src", root / "src")
    test_dirs = {}
    for name in ("golden", "hidden"):
        shutil.copytree(project / "tests" / name, root / "tests" / name)
        test_dirs[name] = root / "tests" / name
    return Sandbox(
        root=root,
        src_root=root / "src",
        test_dirs=test_dirs,
        python=sys.executable,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)},
        network=network,
    )


def config(project: Path) -> ProjectConfig:
    return ProjectConfig(root=project, package="src", import_name="m.js", language="js")


@pytest.fixture
def sb(project: Path, tmp_path: Path) -> Sandbox:
    box = make_sandbox(project, tmp_path)
    which = shutil.which("node")
    assert which is not None
    box.env["PATH"] = f"{Path(which).parent}:{box.env['PATH']}"
    LANG.prepare_sandbox(box, config(project), project)
    return box


def test_package_dest_keeps_layout(project: Path) -> None:
    assert LANG.package_dest(config(project)) == "src"


def test_prepare_sandbox_writes_files(sb: Sandbox, project: Path) -> None:
    assert (sb.root / "node_modules").is_symlink()
    assert (sb.root / "node_modules").resolve() == (project / "node_modules").resolve()
    assert (sb.root / "package.json").is_file()
    cfg = (sb.root / "vitest.config.mjs").read_text()
    assert "fc-seed.mjs" in cfg and "tests/**/*.test.*" in cfg
    assert "configureGlobal" in (sb.root / "fc-seed.mjs").read_text()
    assert (sb.root / "no-net.cjs").is_file()
    assert sb.env["NODE_OPTIONS"] == f"--require {(sb.root / 'no-net.cjs').resolve()}"


def test_prepare_sandbox_network_on_skips_preload(project: Path, tmp_path: Path) -> None:
    box = make_sandbox(project, tmp_path, network=True)
    LANG.prepare_sandbox(box, config(project), project)
    assert not (box.root / "no-net.cjs").exists()
    assert "NODE_OPTIONS" not in box.env


def test_prepare_sandbox_requires_a_lockfile(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TOOLCHAIN_ENV)
    box = make_sandbox(project, tmp_path)
    with pytest.raises(GateError, match="package-lock.json not found"):
        LANG.prepare_sandbox(box, config(project), project)


def test_fake_vitest_in_the_change_is_never_run(project: Path, tmp_path: Path) -> None:
    # The change ships node_modules/vitest that reports success; the sandbox must not use it.
    (project / "node_modules").unlink()
    fake = project / "node_modules" / "vitest"
    fake.mkdir(parents=True)
    (fake / "vitest.mjs").write_text(
        'import fs from "fs";\n'
        "const out = process.argv.find((a) => a.startsWith('--outputFile=')).slice(13);\n"
        "fs.writeFileSync(out, JSON.stringify({success: true, numTotalTests: 1, numPassedTests: 1, "
        "numFailedTests: 0, numPendingTests: 0, numTodoTests: 0, numFailedTestSuites: 0, "
        'numTotalTestSuites: 1, testResults: [{name: "x", status: "passed", '
        'assertionResults: [{status: "passed", fullName: "x"}]}]}));\n'
    )
    (project / "tests" / "golden" / "bad.test.js").write_text(FAILING)
    box = make_sandbox(project, tmp_path)
    which = shutil.which("node")
    assert which is not None
    box.env["PATH"] = f"{Path(which).parent}:{box.env['PATH']}"
    LANG.prepare_sandbox(box, config(project), project)
    assert (box.root / "node_modules").resolve() != (project / "node_modules").resolve()
    run = LANG.run_tests(box, ["golden"], 120, "s")
    assert not run.ok
    assert run.failed == 1


EMPTY_LOCK = (
    '{"name": "p", "lockfileVersion": 3, "requires": true, "packages": {"": {"name": "p"}}}'
)


def test_trusted_modules_install_from_the_base_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("npm") is None and not CI:
        pytest.skip("npm not installed")
    monkeypatch.delenv(TOOLCHAIN_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "p"}')
    (repo / "package-lock.json").write_text(EMPTY_LOCK)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    (repo / "package.json").write_text('{"name": "p", "devDependencies": {"evil": "1.0.0"}}')
    cfg = ProjectConfig(root=repo, package="src", import_name="m.js", language="js")
    cfg.raw = {"_source": "main"}
    modules = trusted_modules(cfg, repo)
    assert modules.is_dir()
    assert (modules.parent / "package.json").read_text() == '{"name": "p"}'  # the base copy
    assert not os.access(modules, os.W_OK)  # read-only for the code under test
    assert trusted_modules(cfg, repo) == modules  # installed once per process


def test_auto_file_ok() -> None:
    assert LANG.auto_file_ok(Path("a.test.js"))
    assert LANG.auto_file_ok(Path("a.test.mjs"))
    assert LANG.auto_file_ok(Path("a.test.ts"))
    assert not LANG.auto_file_ok(Path("vitest.config.mjs"))
    assert not LANG.auto_file_ok(Path("setup.js"))
    assert not LANG.auto_file_ok(Path("helpers.mjs"))


def test_run_tests_passes_on_golden(sb: Sandbox) -> None:
    run = LANG.run_tests(sb, ["golden"], 120, "s")
    assert run.ok, run.output
    assert (run.passed, run.failed, run.errors) == (1, 0, 0)


def test_run_tests_several_dirs(sb: Sandbox) -> None:
    run = LANG.run_tests(sb, ["golden", "hidden"], 120, "s")
    assert run.ok, run.output
    assert run.passed == 2


def test_run_tests_fails_on_failing_test(sb: Sandbox) -> None:
    (sb.test_dirs["golden"] / "bad.test.js").write_text(FAILING)
    run = LANG.run_tests(sb, ["golden"], 120, "s")
    assert not run.ok
    assert (run.passed, run.failed) == (1, 1)


def test_run_tests_zero_tests_not_ok(sb: Sandbox) -> None:
    (sb.root / "tests" / "empty").mkdir()
    run = LANG.run_tests(sb, ["empty"], 120, "s")
    assert not run.ok
    assert run.passed == 0


def test_run_tests_files_restricts(sb: Sandbox) -> None:
    (sb.test_dirs["golden"] / "bad.test.js").write_text(FAILING)
    run = LANG.run_tests(sb, ["golden"], 120, "s", files=["tests/golden/m.test.js"])
    assert run.ok, run.output
    assert run.passed == 1


def test_fail_fast_adds_bail(sb: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    from testgate.languages import js

    seen: list[list[str]] = []
    real = js.run_in_sandbox

    def spy(box: Sandbox, argv: list[str], timeout_s: int, extra_env=None, **kw):  # type: ignore[no-untyped-def]
        seen.append(argv)
        return real(box, argv, timeout_s, extra_env, **kw)

    monkeypatch.setattr(js, "run_in_sandbox", spy)
    LANG.run_tests(sb, ["golden"], 120, "s", fail_fast=True)
    LANG.run_tests(sb, ["golden"], 120, "s")
    assert "--bail=1" in seen[0]
    assert "--bail=1" not in seen[1]


def test_fail_fast_stops_after_first_failure(sb: Sandbox) -> None:
    body = 'import { test, expect } from "vitest";\n' + "".join(
        f'test("t{i}", () => {{ expect(1).toBe(2); }});\n' for i in range(3)
    )
    (sb.test_dirs["golden"] / "many.test.js").write_text(body)
    run = LANG.run_tests(sb, ["golden"], 120, "s", fail_fast=True)
    assert not run.ok
    assert run.failed == 1


def test_no_net_preload_blocks_fetch(sb: Sandbox) -> None:
    (sb.test_dirs["golden"] / "net.test.js").write_text(NET)
    run = LANG.run_tests(sb, ["golden"], 120, "s", files=["tests/golden/net.test.js"])
    assert run.ok, run.output


def test_run_coverage_reports_lines(sb: Sandbox) -> None:
    run, cov = LANG.run_coverage(sb, ["golden"], 120, "s", "src")
    assert run.ok, run.output
    assert {2, 6} <= cov.statements["src/m.js"]
    assert 2 in cov.covered["src/m.js"]
    assert 6 not in cov.covered["src/m.js"]


def test_build_ok_and_syntax_error(sb: Sandbox, project: Path) -> None:
    cfg = config(project)
    assert LANG.build(sb, cfg, 60).returncode == 0
    (sb.src_root / "broken.js").write_text("export function (\n")
    assert LANG.build(sb, cfg, 60).returncode != 0


TSCONFIG = json.dumps(
    {
        "compilerOptions": {
            "strict": True,
            "noEmit": True,
            "module": "esnext",
            "moduleResolution": "bundler",
            "target": "es2020",
            "skipLibCheck": True,
            "types": [],
        },
        "include": ["src"],
    }
)


def test_build_runs_tsc_when_tsconfig_exists(project: Path, tmp_path: Path) -> None:
    (project / "tsconfig.json").write_text(TSCONFIG)
    (project / "src" / "typed.ts").write_text("export const n: number = 1;\n")
    box = make_sandbox(project, tmp_path)
    box.env["PATH"] = f"{Path(shutil.which('node')).parent}:{box.env['PATH']}"
    LANG.prepare_sandbox(box, config(project), project)
    assert (box.root / "tsconfig.json").is_file()
    ok = LANG.build(box, config(project), 120)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    (box.src_root / "typed.ts").write_text("export const n: number = 'one';\n")
    bad = LANG.build(box, config(project), 120)
    assert bad.returncode != 0
    assert "TS2322" in bad.stdout  # Type 'string' is not assignable to type 'number'


def test_build_errors_when_tsconfig_but_no_typescript(project: Path, tmp_path: Path) -> None:
    (project / "tsconfig.json").write_text(TSCONFIG)
    bare = tmp_path / "bare"
    (bare / "node_modules").mkdir(parents=True)  # no typescript inside
    (bare / "src").mkdir()
    (bare / "src" / "m.js").write_text(M_JS)
    (bare / "package.json").write_text('{"name": "b", "type": "module"}')
    shutil.copy2(project / "tsconfig.json", bare / "tsconfig.json")
    box = Sandbox(
        root=tmp_path / "sb2",
        src_root=tmp_path / "sb2" / "src",
        test_dirs={},
        python=sys.executable,
        env={"PATH": f"{Path(shutil.which('node')).parent}:/usr/bin:/bin", "HOME": str(tmp_path)},
        network=False,
    )
    shutil.copytree(bare / "src", box.src_root)
    os.environ[TOOLCHAIN_ENV] = str(bare / "node_modules")  # restored by the project fixture
    LANG.prepare_sandbox(box, config(bare), bare)
    with pytest.raises(GateError, match="typescript"):
        LANG.build(box, config(bare), 120)


def test_build_no_files_succeeds(sb: Sandbox, project: Path) -> None:
    for f in sb.src_root.iterdir():
        f.unlink()
    assert LANG.build(sb, config(project), 60).returncode == 0


def mutate(line: str, extra: str = "") -> tuple[str, str] | None:
    n = extra.count("\n") + 1
    src = f"{extra}{line}\n" + ("}\n" if line.endswith("{") else "")
    out = LANG.generate_mutants("src/x.js", src, {n}, "s", 10)
    return (out[0].operator, out[0].after) if out else None


@pytest.mark.parametrize(
    ("line", "op", "after"),
    [
        ("  if (a <= b) {", "ROR", "if (a < b) {"),
        ("  const ok = a === b;", "ROR", "const ok = a !== b;"),
        ("  const ok = a >= b;", "ROR", "const ok = a > b;"),
        ("  const s = a + b;", "AOR", "const s = a - b;"),
        ("  const s = a % b;", "AOR", "const s = a * b;"),
        ("  const v = a && b;", "LCR", "const v = a || b;"),
        ("  if (!ready) {", "UOI", "if (ready) {"),
        ("  if (ready) {", "UOI", "if (!(ready)) {"),
        ("  const on = true;", "CRP", "const on = false;"),
        ("  const n = x(3);", "CRP", "const n = x(4);"),
        ('  const s = f("abc");', "CRP", 'const s = f("");'),
        ('  check("subtotal", x);', "SBR", ";"),  # a label in a bare call is not mutated
        ("  return value;", "RVR", "return undefined;"),
        ("  total = total.concat(x);", "SBR", ";"),
        ("  save(x);", "SBR", ";"),
    ],
)
def test_one_operator(line: str, op: str, after: str) -> None:
    assert mutate(line) == (op, after)


def test_operator_priority() -> None:
    assert mutate("  const v = a < b && c + d;") == ("ROR", "const v = a <= b && c + d;")
    assert mutate("  const v = a + b && c;") == ("AOR", "const v = a - b && c;")


@pytest.mark.parametrize(
    "line",
    [
        "",
        "  // a < b",
        "  /* a < b */",
        "  * a < b",
        '  import { a } from "./a.js";',
        '  export * from "./a.js";',
        "  console.log(a + b);",
        "  logger.info(a + b);",
        "  debug(a + b);",
        "  x.log(a + b);",
        "  i++;",
    ],
)
def test_arid_and_non_candidates_skipped(line: str) -> None:
    assert mutate(line) is None


def test_crp_skipped_in_throw_and_template() -> None:
    assert mutate('  throw new RangeError("bad");') is None
    assert mutate("  const s = `n ${x} 3`;") == ("SBR", ";")


@pytest.mark.parametrize(
    "line", ["  const f = (a) => a;", "  x += 1;", "  const p = a ** b;", "  y -= 2;"]
)
def test_aor_and_ror_ignore_arrows_compound_and_power(line: str) -> None:
    result = mutate(line)
    assert result is None or result[0] not in ("ROR", "AOR")


def test_operators_in_strings_and_comments_ignored() -> None:
    assert mutate('  const s = "a < b"; // x + y') is not None
    assert mutate('  const s = "a < b"; // x + y')[0] == "CRP"  # type: ignore[index]


def test_only_requested_lines() -> None:
    src = "const a = b + c;\nconst d = e + f;\n"
    out = LANG.generate_mutants("src/x.js", src, {2}, "s", 10)
    assert [m.line for m in out] == [2]
    m = out[0]
    assert m.id == "src/x.js:2:AOR"
    assert m.before == "const d = e + f;"
    assert m.source == "const a = b + c;\nconst d = e - f;\n"


def test_broken_mutant_dropped_by_node_check() -> None:
    src = "foo(1,\n  bar(b));\n"
    assert LANG.generate_mutants("src/x.js", src, {2}, "s", 10) == []


def test_unparseable_source_yields_nothing() -> None:
    assert LANG.generate_mutants("src/x.js", "export function (\n  a + b;\n", {2}, "s", 10) == []


def test_sampling_deterministic_and_capped() -> None:
    src = "".join(f"const v{i} = a + {i};\n" for i in range(30))
    lines = set(range(1, 31))
    a = LANG.generate_mutants("src/x.js", src, lines, "seed1", 5)
    b = LANG.generate_mutants("src/x.js", src, lines, "seed1", 5)
    c = LANG.generate_mutants("src/x.js", src, lines, "seed2", 5)
    assert len(a) == 5
    assert [m.id for m in a] == [m.id for m in b]
    assert [m.id for m in a] != [m.id for m in c]
    assert [m.line for m in a] == sorted(m.line for m in a)


EXIT_IN_TEST = (
    'import { test, expect } from "vitest";\n\n'
    'test("bad", () => {\n  expect(1).toBe(2);\n});\n\n'
    'test("exit", () => {\n  process.exit(0);\n});\n'
)
EXIT_AFTER = (
    'import { test, expect, afterAll } from "vitest";\n\n'
    "afterAll(() => {\n  process.reallyExit ? process.reallyExit(0) : process.exit(0);\n});\n\n"
    'test("bad", () => {\n  expect(1).toBe(2);\n});\n'
)


@pytest.mark.parametrize("body", [EXIT_IN_TEST, EXIT_AFTER], ids=["exit-in-test", "exit-after"])
def test_process_exit_cannot_pass_failing_tests(sb: Sandbox, body: str) -> None:
    (sb.test_dirs["golden"] / "m.test.js").write_text(body)
    run = LANG.run_tests(sb, ["golden"], 120, "s")
    assert run.reported_ok is False
    assert not run.ok


def test_report_lives_outside_the_sandbox(sb: Sandbox) -> None:
    run = LANG.run_tests(sb, ["golden"], 120, "s")
    assert run.ok and run.reported_ok is True
    assert not (sb.root / "vitest.json").exists()


def test_failed_ids_name_failing_files(sb: Sandbox) -> None:
    (sb.test_dirs["golden"] / "bad.test.js").write_text(FAILING)
    run = LANG.run_tests(sb, ["golden"], 120, "s")
    assert run.failed_ids == ["tests/golden/bad.test.js::bad"]
    assert LANG.failed_files(run) == {"tests/golden/bad.test.js"}
