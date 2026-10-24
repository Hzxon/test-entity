# ADR 0003: Required checks prove they came from the trusted gate

Date: 2026-10-24. Status: accepted (owner chose option 2 on 2026-10-24, #48).

## Context

The `main` ruleset matched required checks only by name plus the GitHub
Actions app id (15368). Any PR could add a workflow with a job named
`testgate`, `honest-passes`, `cheat-fails` or `test (3.x)` that exits 0 and
satisfy the ruleset without running the gate. Environment approval does not
help: the fake job needs no secrets. A `pull_request` job can also ask for
`statuses: write` and post any commit status as `github-actions`, so a status
posted with `GITHUB_TOKEN` is no proof either.

Options considered (#48):

1. An organization ruleset requiring `.github/workflows/testgate.yml` from
   `main`. Needs moving both repos into an organization and a plan with
   required workflows for private repos.
2. A `pull_request_target` workflow, which GitHub always runs from the base
   branch, computes the verdict and posts a commit status that the ruleset
   requires.
3. One required owner approval with CODEOWNERS on `.github/**`. Conflicts with
   "CI decides the merge" and needs agents to have their own identity.

## Decision

Option 2, with the status signed by a dedicated GitHub App:

- `.github/workflows/testgate.yml` runs on `pull_request_target` for
  same-repository PRs. It checks out the gate at the base SHA, the PR head into
  `pr/`, and the hidden tests, then runs the base action against `pr/`.
- No workflow step runs PR code. The PR's code runs only inside the gate's
  sandbox, under bubblewrap (#56); the self-gate sets
  `sandbox.require_isolation: true`, so a run without isolation fails.
- The job uses the `testgate` environment: required reviewer Hzxon, and
  deployments allowed from `main` only. A `pull_request` run (ref
  `refs/pull/N/merge`) can never read its secrets.
- After the gate step, the job mints a token for the **testgate gate app**
  (permission `statuses: write` only; Client ID in the variable
  `TESTGATE_APP_CLIENT_ID`, private key in the environment secret `TESTGATE_APP_KEY`)
  and posts the commit status `testgate/verdict` on the PR head SHA.
- The `main` ruleset requires the status check `testgate/verdict` with the
  gate app's integration id. A PR workflow cannot mint that app's token, so it
  cannot satisfy the check.
- `honest-passes`, `cheat-fails` and `test (3.x)` may stay required for
  convenience, but they prove nothing on their own: a PR can fake them by job
  name. `testgate/verdict` is the check that decides.
- The gate app is not the agents' identity (#59). The agent identity has no
  `statuses` permission and no access to the environment.

## Consequences

- `zizmor` reports `pull_request_target` as a dangerous trigger. The line
  carries an explicit ignore that points here: the usual danger is running PR
  code with a privileged token, and here PR code runs only in the sandbox,
  before the app key enters the job, with the default `GITHUB_TOKEN` limited
  to `contents: read`.
- Every gate run waits for the owner's environment approval, as before.
- Changes to the gate workflow take effect only once merged to `main`; a PR
  that edits it is still judged by the old one.
- Fork PRs are not gated (they get no secrets); this repository takes none.

## Owner setup (one time)

1. Create a GitHub App "testgate gate" (no webhook, repository permission
   Commit statuses: read and write, nothing else), install it on
   Hzxon/test-entity only.
2. Set variable `TESTGATE_APP_CLIENT_ID` (repository) and secret `TESTGATE_APP_KEY`
   (environment `testgate`) from the app.
3. Environment `testgate`: deployment branches limited to `main`.
4. Ruleset `main: PR + green checks`: require status `testgate/verdict` from
   the gate app's integration id; drop the name-only `testgate` check.
