# testgate module contract

testgate decides whether a change to a Python project is safe to merge, using
gates the author of the change (usually an AI agent) cannot edit or trick.
Every module below is built against `src/testgate/core.py`, which holds all
shared types. Modules may import `core` and the stdlib; cross-imports between
sibling modules are listed per module and are the only ones allowed.

Design sources: Meta TestGen-LLM (build, pass, 5x flaky, coverage delta),
Meta ACH (a test counts when it kills a mutant the suite missed), Google
mutation testing (changed+covered lines only, one mutant per line, skip arid
nodes, cap findings at 7 x files), SDD-Factory sandbox (fresh temp root, judged
surface hashed, `--noconftest`, pass/fail-only feedback to the author).

## Threat model

The author can edit anything in the working tree, including tests, CI config
and `testgate.yaml`. The gates stay honest because:

1. `testgate.yaml` and the list of protected paths are read from the **base
   ref** (`git show <base>:testgate.yaml`), never from the working tree.
2. The `protected` gate fails the run if any protected path differs from the
   base ref. It runs first; nothing else runs after it fails.
3. Hidden tests live **outside the repo** (`TESTGATE_HIDDEN_DIR` or `hidden:`).
4. The Hypothesis seed is a CI secret (`TESTGATE_SEED`). Without it, a random
   per-run seed is used and the report says `seed_is_secret: false`.
5. Every pytest run happens in a **fresh temp root** containing only the
   package and the chosen test dirs, with `--noconftest -p no:cacheprovider`,
   a scrubbed environment, and sockets blocked unless `network: true`.
6. The public report shows **counts, scores and file:line only**. Assertion
   text, hidden test names and the seed go to the private log.

## Layout

```
src/testgate/
  core.py        shared types (done, do not change without updating this file)
  config.py      testgate.yaml -> ProjectConfig
  gitinfo.py     base ref, changed lines, file content/hash at a ref
  sandbox.py     temp root builder + subprocess runner
  runner.py      pytest / coverage invocations on a sandbox -> TestRun / CoverageData
  mutator.py     changed+covered lines -> list[Mutant]
  gates.py       the seven gates, each (RunContext) -> GateResult
  report.py      Report -> JSON (public), markdown (public), private log text
  cli.py         argparse entry point: init, run, mutants
  templates/     files written by `testgate init`
tests/           testgate's own tests (pytest)
examples/        demo projects used by the e2e tests
action.yml       GitHub composite action
```

## config.py

```python
def load_config(root: Path, base_ref: str | None) -> ProjectConfig
```
- When `base_ref` is given, read `testgate.yaml` with `gitinfo.show_file(root, base_ref, "testgate.yaml")`. If the file does not exist at the base ref (first adoption), fall back to the working tree and record `raw["_source"] = "worktree"`; otherwise `raw["_source"] = base_ref`.
- When `base_ref` is None, read the working tree file.
- Missing file -> `GateError("testgate.yaml not found")`.
- Validate: `package` and `import_name` required; numeric ranges 0..1; `repeats >= 1`. Unknown keys -> `GateError` naming the key (so a typo cannot silently disable a gate).
- YAML shape (all keys optional except `package`, `import_name`):

```yaml
version: 1
package: src/discount
import_name: discount
tests:
  golden: tests/golden
  auto: tests/auto
  hidden: ../discount-hidden        # optional local path, relative to root
  hidden_env: TESTGATE_HIDDEN_DIR
protected: [tests/golden, testgate.yaml, .github/workflows]
seed_env: TESTGATE_SEED
gates:
  protected: true
  hygiene: true
  build: true
  tests: {enabled: true, repeats: 5}
  hidden: {enabled: true, required: true}
  coverage: {enabled: true, min_total: 0.0, min_changed: 0.9, max_drop: 0.0}
  mutation: {enabled: true, min_score: 0.8, max_mutants: 50, max_shown: 7, timeout_s: 60, auto_useless_blocks: false, jobs: <cpus/2>}
sandbox:
  network: false
  timeout_s: 600
  python: null
```
- A gate value may be a bool or a mapping with `enabled` plus that gate's options. Map options onto the flat `ProjectConfig` fields.
- Allowed imports: `core`, `gitinfo`.

## gitinfo.py

```python
def is_git_repo(root: Path) -> bool
def resolve_base(root: Path, base: str | None) -> str | None
    # base given -> verify it exists (`git rev-parse --verify`), return it.
    # base None -> try "origin/main", "origin/master", "main", "master" in that order; None if none exist.
def changed_lines(root: Path, base_ref: str) -> list[ChangedFile]
    # `git diff --unified=0 --no-color <base_ref>...HEAD` PLUS uncommitted changes:
    # use `git diff --unified=0 --no-color <merge-base>` where merge-base = `git merge-base <base_ref> HEAD`,
    # so the working tree (not only HEAD) is compared. Parse "@@ -a,b +c,d @@" hunks; collect added/modified
    # line numbers on the + side. Only .py files. Deleted files are skipped. Untracked .py files
    # (`git ls-files --others --exclude-standard`) count with every line changed.
def show_file(root: Path, ref: str, path: str) -> str | None
    # content at ref, or None if absent
def tree_hash(root: Path, ref: str | None, path: str) -> str | None
    # ref given: `git rev-parse <ref>:<path>` (blob or tree hash), None if absent.
    # ref None: hash of the working tree path computed the same way git would:
    #   file -> `git hash-object <file>`; directory -> `git add`-free approach: build a hash
    #   over sorted (relative path, blob hash) pairs with hashlib.sha1. Document that directory
    #   hashes for the worktree are compared against a hash computed the same way for the ref
    #   (list `git ls-tree -r <ref> <path>` and hash the same pairs), NOT against the git tree id.
def archive(root: Path, ref: str, paths: list[str], dest: Path) -> list[str]
    # `git archive <ref> -- <paths that exist at ref>` extracted into dest; returns the paths present.
    # Paths relative to root (root may be a subdirectory of the repository).
def worktree_pairs(root: Path, path: str) -> list[tuple[str, str]]   # helper used above
def ref_pairs(root: Path, ref: str, path: str) -> list[tuple[str, str]]
```
- All git calls via `subprocess.run([...], cwd=root, capture_output=True, text=True)`. Never shell=True.
- Allowed imports: `core`.

## sandbox.py

```python
@dataclass
class Sandbox:
    root: Path            # temp dir
    src_root: Path        # <root>/src  (package copied under here)
    test_dirs: dict[str, Path]   # name -> <root>/tests/<name>
    python: str
    env: dict[str, str]
    network: bool

def build_sandbox(config: ProjectConfig, python: str, tests: dict[str, Path],
                  overrides: dict[str, str] | None = None) -> Sandbox
    # Fresh tempfile.mkdtemp(prefix="testgate-"). Copy config.root/config.package -> <root>/src/<import_name>
    # (if package dir basename != import_name, still name the copy import_name).
    # Copy each tests[name] dir -> <root>/tests/<name>, only *.py files and data files, EXCLUDING
    # conftest.py, __pycache__, .pytest_cache, and anything under a dir starting with ".".
    # overrides: {repo-relative source path: file text} written over the copy (used for mutants).
    # env: minimal: PATH, HOME=<root>, PYTHONPATH="<root>:<root>/src", PYTHONDONTWRITEBYTECODE=1,
    #      PYTHONHASHSEED=0, LANG/LC_ALL=C.UTF-8, TMPDIR=<root>/tmp, plus whatever `extra_env` the caller adds.
    #      Never copy os.environ wholesale. Explicitly drop TESTGATE_*, HIDDEN*, GITHUB_TOKEN, AWS_*, *_KEY, *_SECRET.
    # If not network: write <root>/sitecustomize.py that replaces socket.socket / socket.create_connection
    #      with functions raising OSError("testgate: network disabled"). PYTHONPATH already puts <root> first.
    # Also write <root>/pytest.ini with:
    #      [pytest]\naddopts = -p no:cacheprovider --noconftest\ntestpaths = tests
    #   so project-level pytest config is never used.

def run_in_sandbox(sb: Sandbox, argv: list[str], timeout_s: int, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]
    # cwd=sb.root, env=sb.env|extra_env, capture text, timeout -> on TimeoutExpired return a CompletedProcess
    # with returncode=124 and stderr "testgate: timed out after Ns".

def destroy(sb: Sandbox) -> None   # shutil.rmtree, ignore errors
```
- Allowed imports: `core`.

## runner.py

```python
def run_pytest(sb: Sandbox, test_names: list[str], timeout_s: int, seed: str | None,
               extra_args: list[str] | None = None) -> TestRun
    # argv: [sb.python, "-m", "pytest", "-q", "--tb=short", "-rA", *["tests/"+n for n in test_names], *extra_args]
    # If seed and hypothesis is importable in sb.python (check once, cache on sb): add "--hypothesis-seed=<seed>".
    # Also set env HYPOTHESIS_PROFILE? No. Keep to the flag.
    # Parse the final summary line(s) with regexes: r"(\d+) passed", r"(\d+) failed", r"(\d+) error", r"(\d+) skipped".
    # "no tests ran" -> passed=0, exit code 5 is treated as failure (TestRun.ok False) because an empty suite
    # must not pass a gate.

def run_coverage(sb: Sandbox, test_names: list[str], timeout_s: int, seed: str | None,
                 source_root: Path, repo_package: str) -> tuple[TestRun, CoverageData]
    # argv: [sb.python, "-m", "coverage", "run", "--branch=false"?, no: plain "coverage run", "--source=src", "-m", "pytest", ...same flags...]
    # Then [sb.python, "-m", "coverage", "json", "-o", "<root>/cov.json"]; parse files -> executed_lines / missing_lines.
    # Map sandbox paths "<root>/src/<import_name>/x.py" back to repo-relative "<config.package>/x.py" (repo_package).
    # Write a <root>/.coveragerc with [run] data_file = <root>/.coverage so nothing touches the project.
```
- Allowed imports: `core`, `sandbox`.

## mutator.py

```python
ARID_CALL_NAMES = {"print", "log", "logger", "logging", "warn", "warning", "debug", "info", "error",
                   "trace", "sleep", "assert_", "setdefault"?}  # keep to logging/timing/tracing; document the set

def is_arid(node: ast.AST) -> bool
    # True for: ast.Assert, ast.Pass, ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, docstring Expr,
    # ast.Expr whose value is a Call to a name/attr in ARID_CALL_NAMES (e.g. logger.info(...), time.sleep(...)),
    # any node inside a function whose name starts with "_log" or "log_".
    # Compound node is arid iff all its statement children are arid (Google's rule).

def generate_mutants(path: str, source: str, lines: set[int], seed: str, max_mutants: int) -> list[Mutant]
    # One mutant per eligible line. Eligible: line in `lines`, line holds a non-arid node that an operator applies to.
    # Operators (name -> rule), applied in this priority order per line, first applicable wins:
    #   ROR: Compare op swap  Lt<->LtE, Gt<->GtE, Eq<->NotEq, Is<->IsNot, In<->NotIn
    #   AOR: BinOp  Add<->Sub, Mult<->Div, FloorDiv<->Mult, Mod->Mult, Pow->Mult; AugAssign same table
    #   LCR: BoolOp And<->Or
    #   UOI: UnaryOp Not -> operand (drop the not); If/While test -> ast.UnaryOp(Not, test)
    #   CRP: Constant  int n -> n+1 (0 -> 1), float x -> x+1.0, True<->False, non-empty str -> "", None stays
    #   RVR: Return value -> `return None` (skip when value already None)
    #   SBR: delete a simple statement (Assign, AugAssign, Expr call that is not arid) -> replace with `pass`
    # Implementation: parse once; walk with a visitor that records (line, node, operator); for each line pick
    # the highest-priority candidate; deep-copy the tree (copy.deepcopy), mutate that node, ast.fix_missing_locations,
    # ast.unparse -> Mutant.source. before/after = ast.unparse(node) truncated to 80 chars.
    # If more than max_mutants lines are eligible, sample deterministically with random.Random(seed).
    # Mutant.id = f"{path}:{line}:{operator}".
    # Syntax errors in `source` -> return [].
    # Equivalent-mutant rules (skip the operator, other operators on the line stay eligible):
    #   ROR on a clamp: `if x < c: x = c` (also `x = Decimal(c)`-style numeric wraps) or
    #       `if x < c: return c` followed by `return x` -- swapping < and <= cannot change behaviour.
    #   CRP on message-only constants: inside raise, f-strings, `"...".format(...)` args and `"..." % x`.
    #   AOR on `"..." % x` (the % is formatting, not arithmetic).
    #   SBR on an Assign/AugAssign inside a function whose target Name is never read in that function.
```
- Allowed imports: `core`.

## gates.py

```python
GATE_ORDER = ["protected", "hygiene", "build", "tests", "hidden", "coverage", "mutation"]

def run_gates(ctx: RunContext, only: list[str] | None = None) -> list[GateResult]
    # Runs gates in GATE_ORDER, skipping those with config.gates[name] False (Status.SKIP, summary "disabled").
    # Stops after `protected` FAILs (remaining gates get Status.SKIP, summary "skipped: protected surface changed").
    # Catches any exception per gate -> Status.ERROR with the exception text in private, type name in public.
    # Each gate records duration_s.
```

Gate semantics:

- **protected** — SKIP "first adoption" when the config came from the worktree (`raw["_source"] == "worktree"`), since nothing at the base ref defines the surface yet. Otherwise, for each path in `config.protected`: compare `gitinfo.tree_hash(root, None, path)` with `gitinfo.tree_hash(root, base_ref, path)`. Any difference -> FAIL, public `changed: [paths]`. Path absent in both -> ignore. Absent at base but present now -> FAIL (new protected file was not approved). `base_ref None` -> SKIP "no base ref".
- **hygiene** — static checks on the working tree, all public:
  - `auto` dir, if present, may only contain `test_*.py`, `__init__.py`, and non-`.py` data files; a `conftest.py` or any other `.py` -> FAIL.
  - No `.py` under `auto` or `package` may reference, as a string or name: `PYTEST_CURRENT_TEST`, `TESTGATE`, `HYPOTHESIS_SEED`, `tests/hidden`, `hidden_dir`, `sys.modules["pytest"]`, `monkeypatch.setattr(pytest`, `os.environ.get("CI"` — each hit -> FAIL with file:line (test-detection attempts).
  - `package` files must not import from any test dir (`from tests` / `import tests`) -> FAIL.
  - `auto` tests must not import `golden` or `hidden` tests -> FAIL.
  - Result WARN instead of FAIL when the only hits are in files that did not change (pre-existing), if `ctx.changed` is non-empty.
- **build** — sandbox with package only; run `python -c "import <import_name>"` then `python -m compileall -q src`. Non-zero -> FAIL; public has `exit_code`; private has output.
- **tests** — sandbox with golden + auto (whichever exist). Run `config.repeats` times with the seed. Run 1 fails -> FAIL "tests failed". Any later run fails -> FAIL "flaky: passed run 1, failed run k". Zero tests collected -> FAIL. Public: `runs`, `passed`, `failed`, `errors`, `skipped` for run 1 and `repeats`. Private: all outputs. Store `ctx.shared["visible_run"]`.
- **hidden** — `ctx.hidden_dir` None -> FAIL "no hidden tests" when `hidden_required` else SKIP. Sandbox with package + hidden only. One run with the seed. Public: `passed`, `failed`, `errors` counts only; summary like "hidden: 7 passed, 2 failed". Private: full output. FAIL on any failure/error or zero tests.
- **coverage** — sandbox with golden + auto + hidden (whichever exist). `run_coverage`. Store `ctx.shared["coverage"] = CoverageData`. Compute total and, when `ctx.changed` non-empty, changed-line coverage = covered changed stmt lines / changed stmt lines (only lines that are statements count). Below `coverage_min_total` or `coverage_min_changed` -> FAIL. Public: `total`, `changed`, `uncovered_changed: [file:line...]` capped at 20 per file.
  - **Baseline comparison.** When `base_ref` is set, archive `package`, `golden` and `auto` as they are at the base ref into a temp dir, build a sandbox from it (hidden = the current hidden dir), run coverage, and store `ctx.shared["baseline_coverage"]`. FAIL when `baseline.total() - total > coverage_max_drop`. Public: `baseline_total`, `drop`, or `baseline_skipped` with the reason (`no base ref`, `package absent at base ref`, `no tests at base ref`, `tests fail at base ref`). A skipped comparison never fails the gate.
- **mutation** — needs `ctx.shared["coverage"]` (run the coverage gate's logic inline if absent). Candidate lines per file = changed lines ∩ covered lines (if `ctx.changed` empty: all covered lines in the package). Read each file from the working tree, `generate_mutants`. For each mutant: sandbox with package + golden + hidden (NOT auto — auto tests are the author's and do not count as kills; this is the ACH rule), `overrides={path: mutant.source}`, `run_pytest(..., extra_args=["-x"])`, timeout `mutation_timeout_s`. killed = not run.ok (a timeout counts as killed). Score = killed / total. No mutants -> SKIP "no eligible lines". Score < `mutation_min_score` -> FAIL. Public: `generated`, `killed`, `score`, `survivors: [{id, line, operator}]` capped at `mutation_max_shown` per file (never `before`/`after` text, which would reveal the oracle? — `before`/`after` ARE allowed, they describe the author's own code). Private: every mutant with its run output.
  - **Parallel runs.** Mutants (and the ACH file x survivor pairs) run in a `ThreadPoolExecutor` with `mutation_jobs` workers (default half the CPUs, min 1). Each run has its own sandbox. Mutants are sorted by id before running and results are consumed in that order, so the report is identical at any job count.
  - **ACH rule (auto test usefulness).** After scoring, for each `test_*.py` file in `auto` (when the dir
    exists and at least one mutant survived golden + hidden), run every surviving mutant against that one
    file only (`run_pytest(sb, [], ..., extra_args=["tests/auto/<file>", "-x"])`). A file that kills none of
    them is useless: the author's tests add no detection power beyond the protected suite. Public:
    `auto_checked` (file count), `auto_useless: [file names]`. Private: per file, the mutant ids it killed.
    Any useless file -> WARN, or FAIL when `mutation_auto_useless_blocks`. A score FAIL still wins.
    When no mutant survived, auto files cannot be credited and the check is skipped (`auto_checked: 0`).

- Allowed imports: `core`, `gitinfo`, `sandbox`, `runner`, `mutator`.

## report.py

```python
def build_report(ctx: RunContext, results: list[GateResult]) -> Report
    # verdict FAIL if any result.blocks else PASS.
def to_markdown(report: Report) -> str
    # Title "testgate: PASS/FAIL", table | gate | status | summary |, then per-gate public details as bullet lists.
    # Must not include anything from GateResult.private or ctx.private_log.
def private_text(ctx: RunContext, results: list[GateResult]) -> str
    # Everything: ctx.private_log lines, then per gate json.dumps(private, default=str).
def write_outputs(report: Report, ctx: RunContext, results: list[GateResult],
                  json_path: Path | None, private_path: Path | None, summary_path: Path | None) -> None
    # json_path -> report.to_public_dict(); private_path -> private_text; summary_path (GITHUB_STEP_SUMMARY) -> append markdown.
```
- Allowed imports: `core`.

## cli.py

```
testgate init [--package PATH] [--import-name NAME] [--force]
    Writes testgate.yaml, tests/golden/test_example.py, tests/auto/.gitkeep, .github/workflows/testgate.yml
    from templates/ (never overwrites without --force). Prints next steps: set TESTGATE_SEED secret, create
    the hidden tests repo, protect main with the "testgate" required check.
testgate run [--root DIR] [--base REF] [--no-git] [--hidden DIR] [--only GATE,...]
             [--json PATH] [--private-log PATH] [--python PATH] [--quiet]
    1. root = --root or cwd. no_git = --no-git or not gitinfo.is_git_repo(root).
    2. base_ref = None if no_git else gitinfo.resolve_base(root, --base). If git but no base found -> exit 2 with
       "no base ref found; pass --base or use --no-git".
    3. config = load_config(root, base_ref). hidden_dir = --hidden or config.hidden (relative to root) or
       env[config.hidden_env]; must exist as a dir else None.
    4. seed = env[config.seed_env] if set (seed_is_secret True) else secrets.token_hex(8) (False).
    5. python = --python or config.python or sys.executable.
    6. changed = [] if no_git else gitinfo.changed_lines(root, base_ref).
    7. results = run_gates(ctx, only). report = build_report. Print markdown to stdout unless --quiet.
       write_outputs(json, private-log, env GITHUB_STEP_SUMMARY if set).
    8. exit 0 on PASS, 1 on FAIL.
testgate mutants [--root DIR] [--base REF] [--no-git] [--max N]
    Prints the mutants that WOULD be generated (id, operator, before -> after) without running tests.
```
- `main(argv: list[str] | None = None) -> int`; `if __name__ == "__main__": raise SystemExit(main())`.
- Allowed imports: everything in the package.

## templates/

- `testgate.yaml` — the YAML above with comments explaining every key, `package:`/`import_name:` filled by init.
- `test_example.py` — one golden test plus one Hypothesis property test showing the pattern.
- `testgate.yml` — GitHub workflow: on pull_request; checkout with fetch-depth 0; optional checkout of the hidden repo into `.testgate-hidden` using `secrets.TESTGATE_HIDDEN_REPO` and a read-only deploy key in `secrets.TESTGATE_HIDDEN_SSH_KEY`; run `uses: <owner>/testgate@v0` (placeholder) with `base: origin/${{ github.base_ref }}`; `TESTGATE_SEED: ${{ secrets.TESTGATE_SEED }}`; upload `testgate-private.log` as an artifact.

## action.yml (repo root)

Composite action. Inputs: `base` (default `origin/main`), `hidden-dir` (default `.testgate-hidden`), `python-version` (default `3.12`), `root` (default `.`). Steps: setup-python; `pip install "${{ github.action_path }}"`; run `testgate run --root <root> --base <base> --hidden <hidden-dir> --json testgate.json --private-log testgate-private.log`; always upload the private log as artifact `testgate-private`. Outputs: `verdict` read from testgate.json.

## examples/

```
examples/discount/            honest implementation + golden + one weak auto test
examples/discount-cheat/      same layout; core.py hardcodes the golden answers (overfitting)
examples/discount-hidden/     hidden tests: same rules, different values and names
```
Rules of `final_price(subtotal: Decimal, coupon: Decimal = 0) -> Decimal`:
10% off when subtotal >= 100; coupon subtracted after the discount; never below 0; two decimal places.
Both example projects have `testgate.yaml` with `hidden: ../discount-hidden`. Neither is a git repo
(they live inside the testgate repo), so the e2e tests run them with `--no-git`.

Expected: `discount` -> verdict PASS (mutation score may show survivors; min_score for the example is 0.5).
`discount-cheat` -> hidden gate FAIL, verdict FAIL, and the public JSON contains no hidden test names.

## Testing rules for every builder

- pytest, files under `tests/`, named `test_<module>.py`.
- No test may touch the network or the real git repo of testgate; create temp git repos with `git init` in `tmp_path`.
- Every public claim in this contract that is checkable gets one focused test.

## Language adapters (issue #7)

The gates do not know a language. Everything language-specific sits behind
`core.Language`, chosen by `language:` in `testgate.yaml` (`python`, the
default, or `js`). `ctx.language` holds the adapter instance; gates never
import `runner`, `mutator` or language modules directly.

```python
class Language(Protocol):                       # in core.py; no sibling imports
    name: str                                   # "python" | "js"
    source_suffixes: tuple[str, ...]            # (".py",) | (".js", ".mjs", ".cjs", ".ts")
    test_glob: str                              # "test_*.py" | "*.test.*"   (auto files + hygiene)
    detection_tokens: tuple[str, ...]           # extra hygiene tokens for this language
    def package_dest(self, cfg: ProjectConfig) -> str
        # where cfg.package is copied inside the sandbox, relative to its root:
        # python -> f"src/{cfg.import_name}"; js -> cfg.package (layout preserved so relative imports work)
    def prepare_sandbox(self, sb: Sandbox, cfg: ProjectConfig, project_root: Path) -> None
        # python: write pytest.ini (+ sitecustomize.py when not sb.network), PYTHONPATH already set
        # js: symlink project_root/node_modules -> sb.root/node_modules (GateError "run npm ci" if absent),
        #     copy project_root/package.json, write vitest.config.mjs (setupFiles: fc-seed.mjs; include tests/**),
        #     fc-seed.mjs (fast-check configureGlobal({seed}) if fast-check imports), and when not sb.network
        #     no-net.cjs + env NODE_OPTIONS="--require <root>/no-net.cjs" (patch net.Socket.prototype.connect,
        #     dns.lookup, http/https.request, globalThis.fetch to throw "testgate: network disabled").
        # project_root is the real project (the baseline sandbox is built from an archive without node_modules).
    def auto_file_ok(self, path: Path) -> bool
        # python: name matches test_*.py or __init__.py; js: *.test.js|mjs|ts (no vitest.config.*, no setup files)
    def build(self, sb: Sandbox, cfg: ProjectConfig, timeout_s: int) -> subprocess.CompletedProcess[str]
        # python: python -c "import <import_name>" then compileall -q src
        # js: node --check on every .js/.mjs/.cjs under the package dir (ts files skipped), first failure returned
    def run_tests(self, sb: Sandbox, names: list[str], timeout_s: int, seed: str,
                  files: list[str] | None = None, fail_fast: bool = False) -> TestRun
        # names: sandbox test dirs ("golden", "auto", "hidden"); files: restrict to these paths relative to sb.root
        # python: pytest -q --tb=short -rA [tests/<name>...|files] [--hypothesis-seed=<seed>] [-x]
        # js: node_modules/.bin/vitest run --root <root> --reporter=json --outputFile=<root>/vitest.json
        #     --dir tests/<name> (one dir) or the file paths; [--bail=1]; env TESTGATE_FC_SEED=<int from seed>
        #     counts from vitest.json numPassedTests/numFailedTests; exit code != 0 or zero tests -> not ok
    def run_coverage(self, sb: Sandbox, names: list[str], timeout_s: int, seed: str,
                     repo_package: str) -> tuple[TestRun, CoverageData]
        # python: coverage run --source=src -m pytest ...; coverage json
        # js: vitest run ... --coverage.enabled --coverage.provider=v8 --coverage.reporter=json
        #     --coverage.reportsDirectory=<root>/cov --coverage.include=<package_dest>/**
        #     parse cov/coverage-final.json (istanbul): statements = statementMap start lines, covered = s[id] > 0
        #     map <root>/<package_dest>/x -> <repo_package>/x. Missing @vitest/coverage-v8 -> GateError naming it.
    def generate_mutants(self, path: str, source: str, lines: set[int], seed: str, max_mutants: int) -> list[Mutant]
        # python: mutator.generate_mutants
        # js: line-based textual mutator (see below)
```

- `ProjectConfig.language: str = "python"`; `config.py` validates it is "python" or "js". `ProjectConfig.python`
  stays Python-only. `RunContext.language: Any` (the adapter; typed Any in core to avoid a cycle).
- `src/testgate/languages/__init__.py` exposes `get_language(name) -> Language`; `python.py` and `js.py` implement it.
- `sandbox.build_sandbox(cfg, python, tests, overrides, dest=...)`: copies `cfg.package` to `<root>/<dest>` where
  dest = `language.package_dest(cfg)`; overrides map `<cfg.package>/x` -> `<root>/<dest>/x`. It writes no
  language files any more; gates call `language.prepare_sandbox` right after. The `python` field stays.
- Gates: every `runner.run_pytest` -> `ctx.language.run_tests`, `runner.run_coverage` -> `ctx.language.run_coverage`,
  `mutator.generate_mutants` -> `ctx.language.generate_mutants`, build gate -> `ctx.language.build`. The ACH per-file
  run passes `files=[f"tests/auto/{name}"]`, mutant runs pass `fail_fast=True`. Hygiene uses `source_suffixes`,
  `test_glob`, `auto_file_ok` and `detection_tokens`; auto files are `sorted(auto.glob(test_glob))`.
- `cli.py`: `ctx.language = get_language(cfg.language)`; `init --language js` writes `package: src`,
  `import_name: index.js`, `language: js`, and a `tests/golden/example.test.js` template; the Python template is unchanged.
- `runner.py` and `mutator.py` keep their public functions (used by `languages/python.py` and the existing tests).

JS textual mutator (one mutant per line, first match on the line wins, same operator names):
- Skip lines that are blank, comments (`//`, `/*`, `*`), `import`/`export ... from`, or contain `console.`,
  `logger.`, `debug(`, `.log(`; skip CRP inside `throw ...(` and template literals.
- ROR `<=`->`<`, `<`->`<=`, `>=`->`>`, `>`->`>=`, `===`->`!==`, `!==`->`===`, `==`->`!=`, `!=`->`==`
- AOR `+`->`-`, `-`->`+`, `*`->`/`, `/`->`*`, `%`->`*` (binary, spaces around; not `++`/`--`/`+=`/`=>`/`**`)
- LCR `&&`->`||`, `||`->`&&`
- UOI `if (!x` -> `if (x`; else `if (cond)` -> `if (!(cond))`
- CRP `true`<->`false`, integer literal n -> n+1, non-empty string literal -> `""`
- RVR `return <expr>;` -> `return undefined;`
- SBR a statement line ending in `;` that is an assignment or call -> `;`
- Verify each mutant with `node --check` (as .mjs when package.json has "type": "module"); drop it if it fails.
- Equivalence rules from issue #4 are Python-only for now; document that in README limits.

Examples: `examples/discount-js`, `examples/discount-js-cheat`, `examples/discount-js-hidden`, same rules as the
Python pair in integer cents: `finalPrice(subtotalCents, couponCents = 0)`: 10% off at >= 10000, coupon after,
floor 0, round half up to a cent, RangeError on negatives. `package.json` (type module; devDependencies vitest,
@vitest/coverage-v8, fast-check; committed `package-lock.json`), `testgate.yaml` with `language: js`,
`package: src`, `import_name: discount.js`, `hidden: ../discount-js-hidden`. Hidden tests import
`../../src/discount.js` (they run from `tests/hidden/` in the sandbox). `node_modules/` is git-ignored; CI and the
e2e tests run `npm ci` in both example dirs first, and the e2e tests skip when `node` is missing.
