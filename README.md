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
| hygiene | No test-detection code, no `conftest.py` in auto, no package import of tests; no changed line adds a coverage exclusion (`pragma: no cover`, `v8`/`c8`/`istanbul ignore`) | file:line | yes (warn if pre-existing) |
| build | Package imports and compiles | exit code | yes |
| tests | Golden and auto tests pass, `repeats` times (flaky check) | pass/fail counts | yes |
| hidden | Hidden tests pass | pass/fail counts | yes |
| coverage | Total and changed-line coverage, and total coverage must not drop vs the base ref by more than `max_drop`. Missing data fails: a changed source file the run did not measure, or no measured statements at all | percentages, uncovered file:line, unmeasured changed files, baseline total and drop | yes |
| mutation | Mutants of changed, covered lines are killed by golden + hidden tests; each auto test file must kill a mutant they missed (the ACH rule). Golden + hidden tests failing on the unmutated code fails the gate | generated, killed, score, survivor file:line, `auto_useless` file names | yes below `min_score`; useless auto tests warn, or fail with `auto_useless_blocks: true` |

## Hidden tests and the seed

Hidden tests live outside the repo. The path comes from `hidden:` in `testgate.yaml` or from the env var named by `tests.hidden_env` (default `TESTGATE_HIDDEN_DIR`). The hidden gate runs them against the package alone. If no hidden dir is available the gate fails, unless `hidden.required` is false.

`TESTGATE_SEED` never reaches the code under test. Each gate gets `HMAC-SHA256(TESTGATE_SEED, head SHA + gate name)`, and only that derived value is passed to pytest as `--hypothesis-seed`, to fast-check, to HTTP placeholders and to mutant sampling. The derived seeds are written to the private log for reproduction. Without it, testgate uses a random seed per run and the report says `seed_is_secret: false`. Auto tests are not used to kill mutants.

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

The action sets the `verdict` output. It never uploads `testgate-private.log`: artifacts and job logs are readable by the author of the change, so the private log stays on the runner, and runs that include hidden tests are redacted even there (no hidden test names, paths or assertion text).

## Languages

| language | tests | property tests | coverage | mutation | build check |
|---|---|---|---|---|---|
| `python` (default) | pytest in a fresh root, `--noconftest` | Hypothesis with `--hypothesis-seed` | coverage.py | AST mutator, one mutant per changed covered line | `import <pkg>` + `compileall` |
| `js` | vitest, JSON reporter, positional test filters | fast-check seeded through `TESTGATE_FC_SEED` | `@vitest/coverage-v8` | textual mutator, `node --check` per mutant | `node --check` per `.js` file, plus `tsc --noEmit` when `tsconfig.json` exists |
| `http` | `*.http.yaml` request/expect fixtures replayed by testgate's own runner against the started service | `{{rand:int:a:b}}` / `{{rand:hex:n}}` placeholders filled from the seed | route coverage: routes in `http.openapi` hit by fixtures; `min_changed` applies to routes whose spec changed vs the base ref | differential: the base ref's service runs on a second port and every fixture on an unchanged route must answer identically; a difference is a survivor | start the service, poll the health URL |

HTTP mode (ADR 0002) starts the service from `http.start` with `{port}` substituted, waits for `http.health`, and replays fixtures such as:

```yaml
- name: discount at the threshold
  request: {method: POST, path: /price, json: {subtotal: 100}}
  expect: {status: 200, json: {price: 90.0}}   # json is a subset match
```

`expect` also takes `headers`, `text` (substring) and `schema` (JSON Schema). `examples/shop-http` and `examples/shop-http-cheat` are the HTTP pair: a stdlib `http.server` shop that needs nothing installed. `http.start` may also be `{compose: compose.yaml, service: shop, port: 8000}`: testgate runs `docker compose up -d --build` in the sandbox copy, asks compose for the published host port, and tears the project down afterwards (`examples/shop-http-compose`). The network block is best effort for the service process: proxy variables point at a dead port, and the sandbox cannot stop a service that ignores them.

JS sandboxes symlink the project's `node_modules`, copy `package.json`, and block the network with a `--require` preload (`localhost` DNS stays allowed so vitest can start). Hidden tests import sources as if they lived in `tests/hidden/` of the project.

## Limits

- Languages: Python (pytest, Hypothesis, AST mutator) and JavaScript/TypeScript (vitest, fast-check, a line-based textual mutator; `node --check` validates each mutant; with a `tsconfig.json` the build gate also runs `tsc --noEmit`, so `typescript` must be a devDependency). Set `language: js` in `testgate.yaml`; the project must have run `npm ci` before the gate runs, and needs `vitest` and `@vitest/coverage-v8` in devDependencies.
- Mutation testing runs golden + hidden once per mutant. Runs are parallel (`mutation.jobs`, default half the CPUs); use `max_mutants` and `timeout_s` to bound large changes.
- Baseline coverage comparison needs a base ref; `--no-git` runs only check the fixed thresholds. Changed-line coverage is checked against a fixed threshold.
- The sandbox is a subprocess with a scrubbed environment and blocked sockets. It is not a container.
- HTTP mode: route coverage and the differential gate need `http.openapi` (a document inside the package); without it the coverage gate reports 100% and the differential gate is skipped. The differential gate only sees routes that fixtures exercise, and only catches drift on routes whose spec did not change. Services with their own containers run through the compose form; the Docker daemon itself is outside the sandbox, so container networking is not restricted.
- Equivalent mutants are reduced by rules (Python: clamps, message-only constants, dead assignments; JS: message-only strings in throw/format/bare calls), not detected in general. A survivor can still be equivalent; read the `before`/`after` text before adding a test.

## Credits

- Meta TestGen-LLM: build, pass, repeated runs for flakiness, coverage delta.
- Meta ACH: a test counts when it kills a mutant the suite missed.
- Google mutation testing: changed and covered lines only, one mutant per line, skip arid nodes, cap findings.
- SDD-Factory sandbox: fresh temp root, hashed judged surface, `--noconftest`, pass/fail-only feedback to the author.
