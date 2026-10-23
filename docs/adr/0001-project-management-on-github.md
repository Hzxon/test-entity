# ADR 0001: Project management lives in GitHub Issues, labels and milestones

Date: 2026-10-23. Status: accepted.

## Context

testgate is built mostly by AI coding agents working in short sessions, with
one human owning intent and release. Every session starts with no memory, so
the plan must live where both agents and the human can read and write it
from a terminal. A separate PM tool would add API tokens and a second source
of truth that drifts from the code.

## Decision

- **GitHub Issues** are the unit of work. One issue is one spec an agent may
  build. PRs close issues with `Fixes #N`.
- **Labels are the state machine.** `status:backlog → ready → in-progress →
  in-review → done`. `needs-human` is a hard stop. `P0..P3` is priority,
  `area:*` routes work.
- **Milestones** are the roadmap: `v0` (Action proven), `v0.1` (gates
  hardened), `v1` (second adapter, HTTP mode).
- **ADRs** in `docs/adr/` record decisions. Agents read them before building.
- **`BACKLOG.md`** is a one-page snapshot: what we build, current state, and
  links to the open issues. It is updated at the end of each session and
  never holds work that is not also an issue.
- **`main` is protected by a ruleset** (`main: PR + green checks`, no bypass):
  every change arrives by squash-merged PR, and the checks `test (3.11)`,
  `test (3.12)`, `test (3.13)`, `honest-passes` and `cheat-fails` must pass.
  0 approving reviews are required. `BACKLOG.md` updates go through a PR too.
- **testgate itself** is a required check; the verdict is the review.
- **Hidden tests** live in `Hzxon/testgate-hidden`, protected by a ruleset:
  PR required, one human approval, no direct pushes. Agents may propose hidden
  tests; only a human merges them (#23). The gate reads that repo's `main`.
- **Self-gate thresholds** ratchet by PR to `main`, judged by the rules on
  `main` at that moment (#24). `testgate.yaml` stays hard-protected, so such
  a PR always fails the `protected` gate by design. Landing it is a human
  ceremony (decision B, 2026-10-23): the owner adds the repository admin role
  as a bypass actor on the `main` ruleset, squash-merges the PR, and removes
  the bypass again, all in one sitting. Nothing else ever bypasses the gate.

## Rules for agents

1. Only pick up issues labeled `status:ready`. A human sets that label; that
   is where intent lives.
2. One issue, one branch, one PR, squash-merged to `main`.
3. Anything the agent cannot settle gets `needs-human` and work stops.
4. Move the label when the state changes; do not leave it to the human.

## Consequences

- No board outside GitHub. Projects v2 may be added as a view over the same
  labels, never as a separate store.
- Jira, Linear and Beads were considered and rejected for now: two trackers
  always drift, and no team member needs a tool outside GitHub.
