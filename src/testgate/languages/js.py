"""JavaScript adapter: vitest for tests and coverage, a textual mutator for mutants."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import random
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from testgate import gitinfo
from testgate.core import CoverageData, GateError, Mutant, ProjectConfig, Sandbox, TestRun
from testgate.sandbox import run_in_sandbox

# A node_modules directory the caller already trusts, used instead of installing one
# (offline runs, tests). Set it in the workflow, never from the change under test.
TOOLCHAIN_ENV = "TESTGATE_JS_TOOLCHAIN"
_INSTALL_TIMEOUT_S = 900

_VITEST_CONFIG = """\
export default {
  cacheDir: ".vite-cache",  // inside the sandbox: node_modules is read-only
  test: {
    include: ["tests/**/*.test.*"],
    setupFiles: ["./fc-seed.mjs"],
    passWithNoTests: false,
  },
};
"""

_FC_SEED = """\
try {
  const mod = await import("fast-check");
  const fc = mod.default ?? mod;
  fc.configureGlobal({ seed: Number(process.env.TESTGATE_FC_SEED ?? 0), numRuns: 200 });
} catch {}
"""

_NO_NET = """\
const net = require("net");
const dns = require("dns");
const http = require("http");
const https = require("https");

function blocked() {
  throw new Error("testgate: network disabled");
}

// vite resolves "localhost" at startup; every other name is blocked.
function guard(real) {
  return function (host, ...rest) {
    if (host === "localhost" || net.isIP(host)) return real.call(this, host, ...rest);
    blocked();
  };
}

net.Socket.prototype.connect = blocked;
dns.lookup = guard(dns.lookup);
dns.promises.lookup = guard(dns.promises.lookup);
http.request = blocked;
http.get = blocked;
https.request = blocked;
https.get = blocked;
globalThis.fetch = async function () {
  blocked();
};
"""

_TEST_FILE = re.compile(r"^.+\.test\.(js|mjs|ts)$")
_CHECKED_SUFFIXES = (".js", ".mjs", ".cjs")
_CHECK_TIMEOUT_S = 30


def _toolchain_bind(sb: Sandbox) -> dict[str, bool]:
    """The trusted node_modules the sandbox links to, mounted read-only under isolation."""
    link = sb.root / "node_modules"
    return {str(link.resolve()): False} if link.is_symlink() else {}


def _fc_seed(seed: str) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)


def _ok(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class JsLanguage:
    name = "js"
    source_suffixes: tuple[str, ...] = (".js", ".mjs", ".cjs", ".ts")
    test_glob: str = "*.test.*"
    detection_tokens: tuple[str, ...] = (
        "process.env.VITEST",
        "import.meta.vitest",
        "process.env.CI",
        "VITEST_",
    )

    def package_dest(self, cfg: ProjectConfig) -> str:
        return cfg.package

    def prepare_sandbox(self, sb: Sandbox, cfg: ProjectConfig, project_root: Path) -> None:
        # Never the change's own node_modules (#65): a PR could ship a fake vitest.
        modules = trusted_modules(cfg, project_root)
        link = sb.root / "node_modules"
        if not link.is_symlink() and not link.exists():
            link.symlink_to(modules.resolve(), target_is_directory=True)
        manifest = project_root / "package.json"
        if manifest.is_file():
            shutil.copy2(manifest, sb.root / "package.json")
        tsconfig = project_root / "tsconfig.json"
        if tsconfig.is_file():
            shutil.copy2(tsconfig, sb.root / "tsconfig.json")  # the build gate runs tsc with it
        (sb.root / "vitest.config.mjs").write_text(_VITEST_CONFIG)
        (sb.root / "fc-seed.mjs").write_text(_FC_SEED)
        if not sb.network:
            preload = sb.root / "no-net.cjs"
            preload.write_text(_NO_NET)
            path = str(preload.resolve())
            sb.env["NODE_OPTIONS"] = f'--require "{path}"' if " " in path else f"--require {path}"

    def auto_file_ok(self, path: Path) -> bool:
        return _TEST_FILE.match(path.name) is not None

    def build(
        self, sb: Sandbox, cfg: ProjectConfig, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        pkg = sb.root / self.package_dest(cfg)
        files = sorted(p for p in pkg.rglob("*") if p.is_file() and p.suffix in _CHECKED_SUFFIXES)
        last = _ok(["node", "--check"])
        for f in files:
            last = run_in_sandbox(
                sb, ["node", "--check", str(f)], timeout_s, binds=_toolchain_bind(sb)
            )
            if last.returncode != 0:
                return last
        tsconfig = sb.root / "tsconfig.json"
        if tsconfig.is_file():
            # node --check skips .ts files; tsc type-checks the whole project instead
            tsc = sb.root / "node_modules" / "typescript" / "bin" / "tsc"
            if not tsc.is_file():
                raise GateError("typescript is not installed; add it to devDependencies")
            last = run_in_sandbox(
                sb,
                ["node", str(tsc), "--noEmit", "-p", str(tsconfig)],
                timeout_s,
                binds=_toolchain_bind(sb),
            )
        return last

    def _vitest(
        self,
        sb: Sandbox,
        names: list[str],
        timeout_s: int,
        seed: str,
        files: list[str] | None,
        fail_fast: bool,
        extra: list[str],
    ) -> TestRun:
        # The JSON report is the result channel (#53): written by vitest's main process
        # to a runner-owned directory outside the sandbox root, then checked for counts.
        out_dir = Path(tempfile.mkdtemp(prefix="testgate-vitest-"))
        out_file = out_dir / "vitest.json"
        argv = [
            "node",
            str(sb.root / "node_modules" / "vitest" / "vitest.mjs"),
            "run",
            "--root",
            str(sb.root),
            "--config",
            str(sb.root / "vitest.config.mjs"),
            "--reporter=json",
            # load the config in memory: bundling it writes node_modules/.vite-temp,
            # and node_modules is the read-only trusted toolchain (#65, #56)
            "--configLoader",
            "runner",
            f"--outputFile={out_file}",
            *extra,
        ]
        if fail_fast:
            argv.append("--bail=1")
        # Positional filters, not --dir: --dir rebases the include globs and finds nothing.
        argv.extend(files or [f"tests/{n}/" for n in names])
        t0 = time.monotonic()
        try:
            proc = run_in_sandbox(
                sb,
                argv,
                timeout_s,
                {"TESTGATE_FC_SEED": str(_fc_seed(seed))},
                binds={str(out_dir): True, **_toolchain_bind(sb)},
            )
            duration = time.monotonic() - t0
            try:
                data = json.loads(out_file.read_text())
            except (OSError, ValueError):
                data = None
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        passed = failed = errors = skipped = 0
        details: list[str] = []  # per-test lines for the private log, like pytest -rA
        failed_ids: list[str] = []
        reported_ok = False
        if isinstance(data, dict):
            passed = int(data.get("numPassedTests", 0))
            failed = int(data.get("numFailedTests", 0))
            skipped = int(data.get("numPendingTests", 0)) + int(data.get("numTodoTests", 0))
            errors = int(data.get("numFailedTestSuites", 0))
            suites = data.get("testResults", [])
            cases = 0
            for suite in suites:
                rel = _rel_to(sb.root, str(suite.get("name", "")))
                if suite.get("status") not in (None, "passed") and not suite.get(
                    "assertionResults"
                ):
                    details.append(f"ERROR {rel}")  # suite failed to load
                    failed_ids.append(rel)
                for case in suite.get("assertionResults", []):
                    cases += 1
                    status = str(case.get("status", "?"))
                    nid = f"{rel}::{case.get('fullName', '')}"
                    details.append(f"{status.upper()} {nid}")
                    details.extend(str(m) for m in case.get("failureMessages", []))
                    if status == "failed":
                        failed_ids.append(nid)
                if suite.get("message"):
                    details.append(str(suite["message"]))
            total = int(data.get("numTotalTests", -1))
            reported_ok = (
                data.get("success") is True
                and failed == 0
                and errors == 0
                and total == passed + failed + skipped == cases
                and int(data.get("numTotalTestSuites", -1)) == len(suites)
            )
        exit_code = proc.returncode
        if exit_code == 0 and (passed + failed == 0 or errors > 0):
            exit_code = 1
        if "hidden" in names:
            details = []  # no hidden test names or assertion text, even in memory (#51)
        return TestRun(
            exit_code=exit_code,
            passed=passed,
            failed=failed,
            errors=errors,
            skipped=skipped,
            duration_s=duration,
            output=(proc.stdout or "") + (proc.stderr or "") + "\n".join(details),
            timed_out=proc.returncode == 124,
            reported_ok=reported_ok,
            failed_ids=failed_ids,
        )

    def run_tests(
        self,
        sb: Sandbox,
        names: list[str],
        timeout_s: int,
        seed: str,
        files: list[str] | None = None,
        fail_fast: bool = False,
    ) -> TestRun:
        return self._vitest(sb, names, timeout_s, seed, files, fail_fast, [])

    def run_coverage(
        self, sb: Sandbox, names: list[str], timeout_s: int, seed: str, repo_package: str
    ) -> tuple[TestRun, CoverageData]:
        dest = self._dest(sb)
        cov_dir = sb.root / "cov"
        shutil.rmtree(cov_dir, ignore_errors=True)
        extra = [
            "--coverage.enabled",
            "--coverage.provider=v8",
            "--coverage.reporter=json",
            f"--coverage.reportsDirectory={cov_dir}",
            f"--coverage.include={dest}/**",
        ]
        run = self._vitest(sb, names, timeout_s, seed, None, False, extra)
        report = cov_dir / "coverage-final.json"
        if not report.is_file():
            if "@vitest/coverage-v8" in run.output:
                raise GateError("@vitest/coverage-v8 is not installed; add it to devDependencies")
            return run, CoverageData()
        return run, _parse_coverage(report, sb.root / dest, repo_package)

    def failed_files(self, run: TestRun) -> set[str]:
        if run.failed_ids:  # from the vitest JSON report: "tests/hidden/x.test.js::name"
            return {nid.split("::", 1)[0] for nid in run.failed_ids}
        return {m.group(1) for m in _FAILED_LINE.finditer(run.output)}

    def _dest(self, sb: Sandbox) -> str:
        return sb.src_root.relative_to(sb.root).as_posix()

    def generate_mutants(
        self, path: str, source: str, lines: set[int], seed: str, max_mutants: int
    ) -> list[Mutant]:
        src_lines = source.splitlines(keepends=True)
        candidates: list[Mutant] = []
        for n in sorted(lines):
            if not 1 <= n <= len(src_lines):
                continue
            raw = src_lines[n - 1]
            body = raw.rstrip("\r\n")
            made = _mutate_line(body)
            if made is None:
                continue
            op, new = made
            text = "".join(src_lines[: n - 1]) + new + raw[len(body) :] + "".join(src_lines[n:])
            candidates.append(
                Mutant(
                    id=f"{path}:{n}:{op}",
                    path=path,
                    line=n,
                    operator=op,
                    before=body.strip(),
                    after=new.strip(),
                    source=text,
                )
            )
        if len(candidates) > max_mutants:
            picked = random.Random(seed).sample(candidates, max_mutants)
            candidates = sorted(picked, key=lambda m: m.line)
        suffix = _check_suffix(path, source)
        if suffix is None:
            return []
        with (
            tempfile.TemporaryDirectory(prefix="testgate-mut-") as tmp,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            valid = list(
                pool.map(lambda m: _syntax_ok(Path(tmp), m, suffix), enumerate(candidates))
            )
        return [m for m, ok in zip(candidates, valid, strict=True) if ok]


def _parse_coverage(report: Path, pkg_dir: Path, repo_package: str) -> CoverageData:
    data = json.loads(report.read_text())
    base = Path(pkg_dir).resolve()
    cov = CoverageData()
    for file_path, entry in data.items():
        try:
            rel = Path(file_path).resolve().relative_to(base)
        except ValueError:
            continue
        key = f"{repo_package}/{rel.as_posix()}"
        stmts: dict[str, Any] = entry.get("statementMap", {})
        counts: dict[str, int] = entry.get("s", {})
        cov.statements[key] = {int(s["start"]["line"]) for s in stmts.values()}
        cov.covered[key] = {
            int(stmts[i]["start"]["line"]) for i, c in counts.items() if c > 0 and i in stmts
        }
    return cov


def _node_check(file: Path) -> bool:
    try:
        proc = subprocess.run(
            ["node", "--check", str(file)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _check_suffix(path: str, source: str) -> str | None:
    """File suffix under which the unmutated source parses, or None."""
    if path.endswith(".ts"):
        order = [".ts"]
    elif re.search(r"^\s*(import|export)\s", source, re.MULTILINE):
        order = [".mjs", ".cjs"]
    else:
        order = [".cjs", ".mjs"]
    with tempfile.TemporaryDirectory(prefix="testgate-mut-") as tmp:
        for suffix in order:
            f = Path(tmp) / f"orig{suffix}"
            f.write_text(source)
            if _node_check(f):
                return suffix
    return None


def _syntax_ok(tmp: Path, item: tuple[int, Mutant], suffix: str) -> bool:
    i, mutant = item
    f = tmp / f"m{i}{suffix}"
    f.write_text(mutant.source)
    return _node_check(f)


_ARID = re.compile(r"console\.|logger\.|debug\(|\.log\(")
_IMPORT = re.compile(r"^\s*(import\b(?!\s*\()|export\b.*\bfrom\b)")
_COMMENT = ("//", "/*", "*")
_THROW_CALL = re.compile(r"\bthrow\b.*\(")
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:::|\s|$)", re.MULTILINE)


def _rel_to(root: Path, file: str) -> str:
    try:
        return Path(file).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return file


# A bare call statement: its string arguments are labels or messages, not behaviour.
_CALL_STMT = re.compile(r"^\s*[\w.$]+\(.*\);?\s*$")

_ROR = re.compile(
    r"===|!==|(?<![<>=!])==(?!=)|(?<!=)!=(?!=)|(?<!<)<=|(?<!>)>=(?!=)|(?<![<=>-])<(?![<=])|(?<![=>-])>(?![>=])"
)
_ROR_SWAP = {
    "<=": "<",
    "<": "<=",
    ">=": ">",
    ">": ">=",
    "===": "!==",
    "!==": "===",
    "==": "!=",
    "!=": "==",
}
_AOR = re.compile(r"(?<=\s)([+\-*/%])(?=\s)")
_AOR_SWAP = {"+": "-", "-": "+", "*": "/", "/": "*", "%": "*"}
_LCR = re.compile(r"(?<![&|])(&&|\|\|)(?![=&|])")
_LCR_SWAP = {"&&": "||", "||": "&&"}
_UOI_NOT = re.compile(r"\bif \(!(?!=)")
_IF = re.compile(r"\bif \(")
_BOOL = re.compile(r"(?<![\w$.])(true|false)(?![\w$])")
_INT = re.compile(r"(?<![\w$.])\d+(?![\w.])")
_STR = re.compile(r"\"x+\"|'x+'")
_RETURN = re.compile(r"^(\s*)return\s+\S.*;\s*$")
_ASSIGN = re.compile(r"^\s*(?:(?:const|let|var)\s+)?[\w$.\[\]]+\s*[-+*/%]?=(?![=>]).*;\s*$")
_CALL = re.compile(r"^\s*(?:await\s+)?[\w$.]+\(.*\);\s*$")


def _mask(line: str) -> str:
    """Same-length copy with string bodies as 'x' and trailing // comments blanked."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and i + 1 < len(line):
                out.append("xx")
                i += 2
                continue
            if c == quote:
                quote = ""
                out.append(c)
            else:
                out.append("x")
        elif c in "\"'`":
            quote = c
            out.append(c)
        elif c == "/" and line[i + 1 : i + 2] == "/":
            out.append(" " * (len(line) - i))
            break
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _swap_at(line: str, m: re.Match[str], table: dict[str, str], group: int = 0) -> str:
    start, end = m.span(group)
    return line[:start] + table[m.group(group)] + line[end:]


def _matching_paren(masked: str, open_idx: int) -> int | None:
    depth = 0
    for i in range(open_idx, len(masked)):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _mutate_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(_COMMENT) or _IMPORT.match(line) or _ARID.search(line):
        return None
    masked = _mask(line)

    m = _ROR.search(masked)
    if m:
        return "ROR", _swap_at(line, m, _ROR_SWAP)
    m = _AOR.search(masked)
    if m:
        return "AOR", _swap_at(line, m, _AOR_SWAP, 1)
    m = _LCR.search(masked)
    if m:
        return "LCR", _swap_at(line, m, _LCR_SWAP, 1)

    m = _UOI_NOT.search(masked)
    if m:
        return "UOI", line[: m.start()] + "if (" + line[m.end() :]
    m = _IF.search(masked)
    if m:
        close = _matching_paren(masked, m.end() - 1)
        if close is not None:
            cond = line[m.end() : close]
            return "UOI", f"{line[: m.end()]}!({cond}){line[close:]}"

    if "`" not in line and not _THROW_CALL.search(masked):
        m = _BOOL.search(masked)
        if m:
            flipped = "false" if m.group(1) == "true" else "true"
            return "CRP", line[: m.start()] + flipped + line[m.end() :]
        m = _INT.search(masked)
        if m:
            return "CRP", line[: m.start()] + str(int(m.group()) + 1) + line[m.end() :]
        m = _STR.search(masked)
        if m and not _CALL_STMT.match(masked):
            return "CRP", line[: m.start()] + line[m.start()] * 2 + line[m.end() :]

    m = _RETURN.match(masked)
    if m:
        return "RVR", f"{m.group(1)}return undefined;"
    if _ASSIGN.match(masked) or _CALL.match(masked):
        indent = line[: len(line) - len(line.lstrip())]
        return "SBR", f"{indent};"
    return None


# --- trusted toolchain (#65) -------------------------------------------------

_TOOLCHAINS: dict[str, Path] = {}  # sha256(package.json, lockfile) -> node_modules
_TOOLCHAIN_LOCK = threading.Lock()


def trusted_modules(cfg: ProjectConfig, project_root: Path) -> Path:
    """node_modules installed by testgate from the base ref's manifest and lockfile.

    The install runs `npm ci --ignore-scripts` into a testgate-owned temp dir, once
    per manifest per process, and is then made read-only. Without a base ref (first
    adoption, --no-git) the working tree's files are used, still never its node_modules.
    """
    override = os.environ.get(TOOLCHAIN_ENV)
    if override:
        path = Path(override)
        if not path.is_dir():
            raise GateError(f"{TOOLCHAIN_ENV} is not a directory: {override}")
        return path
    source = cfg.raw.get("_source")
    from_ref = bool(source) and source != "worktree"
    files: dict[str, str | None] = {}
    for name in ("package.json", "package-lock.json"):
        if from_ref:
            files[name] = gitinfo.show_file(project_root, str(source), name)
        else:
            f = project_root / name
            files[name] = f.read_text(encoding="utf-8") if f.is_file() else None
    missing = [n for n, text in files.items() if text is None]
    if missing:
        where = f"at {source}" if from_ref else "in the project"
        raise GateError(f"{', '.join(missing)} not found {where}; testgate installs from them")
    key = hashlib.sha256("\0".join(t or "" for t in files.values()).encode()).hexdigest()
    with _TOOLCHAIN_LOCK:
        if key not in _TOOLCHAINS:
            _TOOLCHAINS[key] = _install(files)
        return _TOOLCHAINS[key]


def _install(files: dict[str, str | None]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="testgate-node-")).resolve()
    atexit.register(_remove, root)
    for name, text in files.items():
        (root / name).write_text(text or "", encoding="utf-8")
    argv = ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"]
    try:
        proc = subprocess.run(
            argv, cwd=root, capture_output=True, text=True, check=False, timeout=_INSTALL_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"npm ci failed: {exc}") from exc
    if proc.returncode != 0:
        raise GateError(f"npm ci failed: {proc.stderr.strip()[-500:]}")
    modules = root / "node_modules"
    modules.mkdir(exist_ok=True)  # npm creates none for a manifest without dependencies
    _set_writable(modules, False)
    return modules


def _set_writable(path: Path, writable: bool) -> None:
    """Add or drop write permission on a tree; symlinks are left alone."""
    bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [*filenames, *dirnames]:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                mode = os.stat(p).st_mode
                os.chmod(p, (mode | stat.S_IWUSR) if writable else (mode & ~bits))
    mode = os.stat(path).st_mode
    os.chmod(path, (mode | stat.S_IWUSR) if writable else (mode & ~bits))


def _remove(root: Path) -> None:
    if (root / "node_modules").is_dir():
        _set_writable(root / "node_modules", True)
    shutil.rmtree(root, ignore_errors=True)
