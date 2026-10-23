# testgate

testgate decides whether a change to a Python project is safe to merge. It is built for changes written by AI agents. An agent can edit the code, the tests, the CI config and `testgate.yaml`, so a green test run from the agent's own tests proves little. testgate reads its config from the base ref, fails the run if protected paths changed, keeps hidden tests outside the repo, and keeps the Hypothesis seed in a CI secret.

The gates run in order: protected, hygiene, build, tests, hidden, coverage, mutation. Every pytest run happens in a fresh temp root that holds only the package and the chosen test dirs, with `--noconftest`, a scrubbed environment and blocked sockets. The public report has counts, scores and file:line only. Assertion text, hidden test names and the seed go to a private log.

## Install

```
uv tool install git+https://github.com/OWNER/testgate
# or from a checkout
pip install .
```

## Quick start

1. Run `testgate init` in your project. It writes `testgate.yaml`, a golden test example and a workflow.
2. Write golden tests in `tests/golden`. They are protected: a change to them fails the run.
3. Create a separate repo with hidden tests. They use the same rules as the golden tests with different values and names. In CI, check it out into `.testgate-hidden`, or set `TESTGATE_HIDDEN_DIR`.
4. Set `TESTGATE_SEED` as a CI secret.
5. Add the `testgate` check as required on the main branch.

Run it locally with `testgate run --base origin/main`, or `testgate run --no-git` for a project that is not a git repo. `testgate mutants` lists the mutants that would be generated.

## Gates

| Gate | What it checks | Public output | Blocks |
|---|---|---|---|
| protected | Protected paths equal the base ref | changed paths | yes |
| hygiene | No test-detection code, no `conftest.py` in auto, no package import of tests | file:line | yes (warn if pre-existing) |
| build | Package imports and compiles | exit code | yes |
| tests | Golden and auto tests pass, `repeats` times (flaky check) | pass/fail counts | yes |
| hidden | Hidden tests pass | pass/fail counts | yes |
| coverage | Total and changed-line coverage, and total coverage must not drop vs the base ref by more than `max_drop` | percentages, uncovered file:line, baseline total and drop | yes |
| mutation | Mutants of changed, covered lines are killed by golden + hidden tests; each auto test file must kill a mutant they missed (the ACH rule) | generated, killed, score, survivor file:line, `auto_useless` file names | yes below `min_score`; useless auto tests warn, or fail with `auto_useless_blocks: true` |

## Hidden tests and the seed

Hidden tests live outside the repo. The path comes from `hidden:` in `testgate.yaml` or from the env var named by `tests.hidden_env` (default `TESTGATE_HIDDEN_DIR`). The hidden gate runs them against the package alone. If no hidden dir is available the gate fails, unless `hidden.required` is false.

`TESTGATE_SEED` is passed to pytest as `--hypothesis-seed`. Without it, testgate uses a random seed per run and the report says `seed_is_secret: false`. Auto tests are not used to kill mutants.

## GitHub Action

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- uses: actions/checkout@v4
  with:
    repository: ${{ secrets.TESTGATE_HIDDEN_REPO }}
    ssh-key: ${{ secrets.TESTGATE_HIDDEN_SSH_KEY }}  # read-only deploy key of the hidden repo
    path: .testgate-hidden
- uses: OWNER/testgate@v0
  env:
    TESTGATE_SEED: ${{ secrets.TESTGATE_SEED }}
  with:
    base: origin/${{ github.base_ref }}
```

The action uploads `testgate-private.log` as the `testgate-private` artifact and sets the `verdict` output.

## Limits

- Python only in v1.
- Mutation testing runs the suite once per mutant, so it is slow on large changes. Use `max_mutants` and `timeout_s`.
- Baseline coverage comparison needs a base ref; `--no-git` runs only check the fixed thresholds. Changed-line coverage is checked against a fixed threshold.
- The sandbox is a subprocess with a scrubbed environment and blocked sockets. It is not a container.

## Credits

- Meta TestGen-LLM: build, pass, repeated runs for flakiness, coverage delta.
- Meta ACH: a test counts when it kills a mutant the suite missed.
- Google mutation testing: changed and covered lines only, one mutant per line, skip arid nodes, cap findings.
- SDD-Factory sandbox: fresh temp root, hashed judged surface, `--noconftest`, pass/fail-only feedback to the author.
