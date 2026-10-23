# ADR 0002: HTTP black-box mode

Date: 2026-10-23. Status: proposed (issue #8). Becomes accepted when a human
merges this document; implementation issues are split from it afterwards.

## Context

The Python and JS adapters gate a change by running the project's own test
runner inside a sandbox. That needs an adapter per language and gives the
gate a view of the code (coverage, mutants). Many services are better judged
from the outside: the thing that must not change is the HTTP behaviour, the
implementation language is irrelevant, and a mutant of the source is not the
right question. Issue #8 asks for a mode that gates any service by its HTTP
behaviour alone.

The same threat model applies: the author of the change (usually an agent)
can edit anything in the repository, so the oracle must live outside it and
the gate must read its rules from the base ref.

## Decision

Add a third adapter, `language: http`, behind the existing `core.Language`
protocol, so the seven-gate pipeline, the report format, the hidden-suite
rules and the CI action stay the same. What each gate means changes:

| gate | python / js today | http mode |
|---|---|---|
| protected | hashes of golden, config, workflows | same |
| hygiene | test-detection tokens in source | same tokens, plus `TESTGATE_` in the start command's environment |
| build | import and compile | **start the service, wait for the health URL, stop it** |
| tests | golden suite, N repeats, secret seed | **golden fixtures** replayed against the service, N repeats; the seed orders the fixtures and fills `{{rand:*}}` placeholders |
| hidden | hidden suite, counts only | **hidden fixtures** from the private repo, counts only |
| coverage | line coverage, changed lines, drop vs base | **route coverage**: routes from the OpenAPI document (or a `routes.txt`) hit by golden + hidden; "changed lines" become routes whose spec entry differs from the base ref; drop vs base is computed the same way |
| mutation | mutants of changed covered lines | **differential**: the base ref's service runs on a second port; every fixture that targets an unchanged route must get an identical status and body from both; a difference is a surviving "mutant" and the score is identical / compared |

### Configuration

```yaml
language: http
package: .                       # tree copied into the sandbox and started from there
import_name: app                 # unused by http; kept for schema stability
http:
  start: ["python", "-m", "uvicorn", "app:app", "--port", "{port}"]
  health: "http://127.0.0.1:{port}/health"   # must return 2xx
  startup_timeout_s: 60
  openapi: openapi.yaml          # optional; enables route coverage and changed-route detection
  env: {}                        # extra environment for the service; scrubbed like the sandbox env
tests:
  golden: tests/golden           # *.http.yaml fixtures
  auto: tests/auto
  hidden_env: TESTGATE_HIDDEN_DIR
```

### Fixture format

One YAML file holds one or more cases. The runner is testgate's own and
needs nothing from the project.

```yaml
- name: discount applies at the threshold
  request: {method: POST, path: /price, json: {subtotal: 100, coupon: 0}}
  expect:
    status: 200
    json: {price: 90.0}           # subset match: extra keys in the response are fine
- name: negative subtotal is rejected
  request: {method: POST, path: /price, json: {subtotal: -1}}
  expect: {status: 400}
- name: unknown id is a 404
  request: {method: GET, path: "/orders/{{rand:int:100000:999999}}"}
  expect: {status: 404}
```

`expect` may also carry `headers`, `text` (substring) and `schema` (a JSON
Schema the body must satisfy). `{{rand:int:a:b}}` and `{{rand:hex:n}}` are
filled from the run's seed, so hidden fixtures can vary their inputs without
the author being able to predict them. When `openapi` is set, every response
is additionally validated against the document's response schema for that
route and status; a mismatch fails the case.

### Sandbox

The service starts as a subprocess from a fresh copy of `package` with the
scrubbed environment, on a free port passed through `{port}`. The network
block of the Python and JS sandboxes is relaxed to **loopback only**: the
runner must reach the service, the service must reach nothing else. The base
ref's service for the differential gate starts from a `git archive` of the
base ref on its own port. Both are killed, with their process group, when
the gate ends or when `startup_timeout_s` / the per-request timeout expire.

Out of scope for the first version: services that need a database, a broker
or Docker. Those arrive as a `start` variant (`docker compose up`) in a later
issue; the fixture runner and the gate semantics do not change.

### What stays the same

Hidden fixtures live in the private repo and are read-only to CI. The public
report shows counts, route names and the differential score; request and
response bodies of hidden cases go to the private log. `testgate.yaml` is
read from the base ref. The ACH rule has no equivalent here and is skipped
with a public note; auto fixtures run in the `tests` gate but earn no credit.

## Consequences

- One more adapter, no change to gates.py beyond what the `Language` protocol
  already allows. The fixture runner and the two service launchers are the
  new code.
- Route coverage is coarser than line coverage; `min_changed` means "every
  route whose spec changed was exercised", which is the right question for a
  black box.
- The differential gate catches unintended behaviour drift that unit-level
  mutation never could, and misses bugs inside a route nobody fixtures. Both
  are stated in the README limits.
- Without an OpenAPI document the coverage gate can only count fixtures per
  route prefix and the changed-route check is skipped; the report says so.

## Implementation issues (to be filed once this ADR is accepted)

1. `HttpLanguage` skeleton: config keys, service launcher (start, health,
   stop), fixture runner with `expect` matching and `{{rand}}` placeholders,
   `tests` and `hidden` gates reporting counts.
2. Route coverage from an OpenAPI document, changed-route detection against
   the base ref, `min_changed` and `max_drop` semantics.
3. Differential gate: base-ref service on a second port, identical responses
   on unchanged routes, score and survivors.
4. Example pair `examples/shop-http` (honest) and `shop-http-cheat` with a
   hidden fixture set, plus e2e tests; a dependency-free Python service so CI
   needs nothing new.
5. `start` variant for `docker compose` (later milestone).
