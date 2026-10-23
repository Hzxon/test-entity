"""JsLanguage against a real vitest install; skipped when node or npm is unavailable."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from testgate.core import GateError, ProjectConfig, Sandbox
from testgate.languages.js import JsLanguage

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

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
    if shutil.which("npm") is None:
        pytest.skip("npm not installed")
    d = tmp_path_factory.mktemp("jsdeps")
    (d / "package.json").write_text(json.dumps({"name": "deps", "private": True, "type": "module"}))
    proc = subprocess.run(
        [
            "npm",
            "install",
            "--no-audit",
            "--no-fund",
            "vitest@3",
            "@vitest/coverage-v8@3",
            "fast-check@3",
        ],
        cwd=d,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"npm install failed: {proc.stderr[-300:]}")
    return d


@pytest.fixture
def project(tmp_path: Path, jsdeps: Path) -> Path:
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


def test_prepare_sandbox_requires_node_modules(project: Path, tmp_path: Path) -> None:
    (project / "node_modules").unlink()
    box = make_sandbox(project, tmp_path)
    with pytest.raises(GateError, match="npm ci"):
        LANG.prepare_sandbox(box, config(project), project)


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

    def spy(box: Sandbox, argv: list[str], timeout_s: int, extra_env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        seen.append(argv)
        return real(box, argv, timeout_s, extra_env)

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
