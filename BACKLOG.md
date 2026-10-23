# testgate backlog

Read this first in a new session. It says what testgate is, where it stands,
and what to build next. Update it at the end of every working session.

## What we are building

testgate decides whether a change to a software project is safe to merge,
using gates the author of the change (usually an AI coding agent) cannot edit
or trick. It is the "CI decides the merge" half of an agent-driven SDLC: the
agent writes code, testgate judges it, a human owns intent and release.

Design sources (see CONTRACT.md for how each one maps to a gate):
Meta TestGen-LLM (build, pass, 5x flaky, coverage delta), Meta ACH (a test
counts only when it kills a mutant the suite missed), Google mutation testing
(changed+covered lines, one mutant per line, arid-node skipping, cap findings),
and the SDD-Factory sandbox rules (fresh temp root, protected surface hashed,
`--noconftest`, pass/fail-only feedback to the author).

Scope decisions made with the user on 2026-10-23:
- v1 targets Python libraries and CLIs. Other languages come later as adapters.
- Code under test is written by AI agents, so the full strict gate set is on.
- Ships as a CLI plus a GitHub composite action.
- Lives in `~/dev/testgate`, its own repo.

## Current state (2026-10-23)

Done and verified locally:
- Seven gates in `src/testgate/gates.py`, run in order:
  protected, hygiene, build, tests, hidden, coverage, mutation.
  `protected` failing stops the run. Config is read from the base ref
  (`git show <base>:testgate.yaml`), never from the working tree.
- `src/testgate/mutator.py`: AST mutator, operators ROR/AOR/LCR/UOI/CRP/RVR/SBR,
  one mutant per line, arid nodes skipped (logging, sleeps, asserts, docstrings,
  `__all__`, strings inside `raise`/f-strings), deterministic sampling by seed.
- `src/testgate/sandbox.py`: fresh temp root, scrubbed env, socket block via
  `sitecustomize.py`, own `pytest.ini` with `--noconftest -p no:cacheprovider`.
- `src/testgate/runner.py`: pytest and coverage runs, `--hypothesis-seed` when
  hypothesis is importable, exit code 5 (no tests) treated as failure.
- `src/testgate/cli.py`: `testgate init | run | mutants`. Exit 0 PASS, 1 FAIL,
  2 config/usage error. Writes public JSON, private log, GITHUB_STEP_SUMMARY.
- `action.yml` composite action and the `templates/testgate.yml` workflow.
- Examples: `examples/discount` (honest) passes; `examples/discount-cheat`
  (hardcodes golden answers) fails on the hidden gate with no hidden test name
  in the public report. `examples/discount-hidden` holds the hidden tests.
- 161 tests pass (`uv run pytest`), `ruff check .`, `ruff format --check .`
  and `mypy src/testgate` (strict) are clean.
- Manually verified on a temp git repo: editing a golden test fails
  `protected`; a one-line source change produces exactly one mutant.

Not verified:
- The GitHub Action and the workflow template have never run on GitHub.
- The wheel has not been built; templates ship via `packages = ["src/testgate"]`
  but nobody checked the built artifact.

Known limits (documented in README.md):
- Python only.
- No baseline coverage comparison against the base ref (only thresholds).
- Equivalent mutants are reported as survivors (e.g. `< 0` vs `<= 0` before a
  clamp). No equivalence detection.
- Sandbox is subprocess-based with a socket block, not a container.
- Mutation runs one sandbox per mutant, serially; slow on large diffs.

## How to run

```bash
cd ~/dev/testgate
uv run pytest                                         # the suite
uv run testgate run --root examples/discount --no-git # honest demo -> PASS
uv run testgate run --root examples/discount-cheat --no-git # cheat demo -> FAIL
uv run testgate mutants --root examples/discount --no-git   # list mutants only
```

Real use: `testgate init` in a project, commit, then
`testgate run --base origin/main --hidden <dir>` with `TESTGATE_SEED` set.

## Next to implement, in order

1. **Prove the Action on GitHub.** Push the repo, open a PR against itself
   using `action.yml`, set `TESTGATE_SEED`, use `examples/discount-hidden`
   as the hidden dir. Fix whatever breaks. Tag `v0`.
2. **ACH rule for the agent's tests** (`TODO(v2)` in `gates.py`). For each
   test in `tests/auto`, count the mutants it kills that golden + hidden do
   not. A test that kills none is `auto_useless`; report it (WARN first,
   make it blocking by config later).
3. **Baseline coverage comparison.** Build a sandbox from `git archive <base>`,
   run coverage there, and fail when total coverage drops by more than
   `max_drop` (new config key). Replaces the fixed `min_total` as the main check.
4. **Equivalent-mutant reduction.** Cheap rules first: skip ROR mutants whose
   result feeds a `max`/`min` clamp on the same value; skip CRP on constants
   only used in messages. Then consider an LLM judge like ACH's, kept
   advisory (never changes the score, only annotates survivors).
5. **Parallel mutant runs.** `concurrent.futures` over sandboxes with a
   `jobs` config key; keep results ordered by mutant id.
6. **Run testgate on itself.** Add `testgate.yaml` to this repo, golden =
   `tests/`, hidden = a private repo; make it the required check.
7. **Second language adapter.** Start with JS/TS (vitest + fast-check +
   Stryker). Needs: a `Runner` protocol in core.py, per-language sandbox
   layout, a mutator (or wrap Stryker), and `language:` in testgate.yaml.
8. **HTTP black-box mode.** Contract tests and replayed traffic against a
   running server, language-independent. Design only after 7 lands.

## Open questions for the user

- Should `hidden` be required by default (current: yes)? Teams without a
  hidden repo get a FAIL until they create one.
- Should `auto_useless` block merges or only warn?
- Hosting: GitHub only, or also GitLab CI later?
