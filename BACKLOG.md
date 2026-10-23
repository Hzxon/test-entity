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

## Current state (2026-10-23, main after PR #14, tag v0 at ac5b38c)

Done and verified locally:
- Seven gates in `src/testgate/gates.py`, run in order:
  protected, hygiene, build, tests, hidden, coverage, mutation.
  The mutation gate also applies the ACH rule: an auto test file that kills
  no mutant golden + hidden missed is `auto_useless` (WARN, or FAIL with
  `gates.mutation.auto_useless_blocks: true`).
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

Verified on GitHub (2026-10-23, tag `v0` at ac5b38c):
- `.github/workflows/testgate-self.yml` runs the action on every PR; PR #12
  showed the honest example PASS and the cheat example FAIL on hidden tests,
  PR #13 showed that editing a protected golden test fails `protected`.
- `--root` may be a subdirectory of the repository (monorepo layout).
- testgate gates its own repo: `testgate.yaml` at the root, frozen tests in
  `tests/golden`, hidden tests in the private repo `Hzxon/testgate-hidden`
  (read-only deploy key, secret `TESTGATE_HIDDEN_SSH_KEY`), workflow
  `.github/workflows/testgate.yml`, check `testgate` required on `main`.
  Thresholds start low (`min_score: 0.3`, `min_changed: 0.0`); ratchet them
  as golden + hidden grow.

Not verified:
- The wheel has not been built; templates ship via `packages = ["src/testgate"]`
  but nobody checked the built artifact.

Known limits (documented in README.md):
- Python only.
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

## Process

Work is tracked in GitHub Issues at https://github.com/Hzxon/test-entity/issues.
Labels are the state machine; see `docs/adr/0001-project-management-on-github.md`.
An agent may only start an issue labeled `status:ready`.
`main` is protected: PR plus green CI and self-test checks, squash merge only,
no direct pushes (ruleset `main: PR + green checks`, added 2026-10-23).

## Next to implement, in order

| # | Issue | Milestone |
|---|---|---|
| 1 | ~~[Prove the GitHub Action on a real PR and tag v0](https://github.com/Hzxon/test-entity/issues/1)~~ done | v0 |
| 2 | ~~[ACH rule: flag agent tests that kill no mutant the suite misses](https://github.com/Hzxon/test-entity/issues/2)~~ done | v0.1 |
| 3 | ~~[Baseline coverage comparison against the base ref](https://github.com/Hzxon/test-entity/issues/3)~~ done | v0.1 |
| 4 | [Reduce equivalent mutants](https://github.com/Hzxon/test-entity/issues/4) | v0.1 |
| 5 | [Run mutants in parallel](https://github.com/Hzxon/test-entity/issues/5) | v0.1 |
| 6 | ~~[Run testgate on itself](https://github.com/Hzxon/test-entity/issues/6)~~ done | v0.1 |
| 7 | [JS/TS adapter (vitest, fast-check, Stryker)](https://github.com/Hzxon/test-entity/issues/7) | v1 |
| 8 | [HTTP black-box mode](https://github.com/Hzxon/test-entity/issues/8) | v1 |

## Open questions for the user (`needs-human`)

- [#9 Should hidden tests be required by default?](https://github.com/Hzxon/test-entity/issues/9)
- [#10 Should useless agent tests block merges or only warn?](https://github.com/Hzxon/test-entity/issues/10)
- [#11 GitHub only, or GitLab CI too?](https://github.com/Hzxon/test-entity/issues/11)
