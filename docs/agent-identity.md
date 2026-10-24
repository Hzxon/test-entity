# Agent identity (#59)

Agents never act with the owner's credentials. The owner's `gh` login and any
personal access token have admin on both repositories, so with them no
ruleset, review rule or environment is a boundary an agent cannot lift.

## The identity

A GitHub App, "testgate agent", owned by Hzxon:

- repository permissions: **Contents** and **Pull requests** read/write, nothing
  else (no Administration, Workflows, Commit statuses, Environments, Secrets);
- installed on Hzxon/test-entity only, never on Hzxon/testgate-hidden.

The owner keeps admin, the only approval rights, the `testgate` environment and
the hidden repository. The agent app is not the "testgate gate" app (ADR 0003):
only the gate app may post `testgate/verdict`.

## Agent sessions

`scripts/agent-gh-token <app-id> <key.pem>` signs a JWT with the app key and
prints a one-hour installation token. A Claude Code `SessionStart` hook exports
it for the session (`CLAUDE_ENV_FILE`), so every `gh` and `git push` in the
session runs as the app:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "echo \"export GH_TOKEN=$(~/dev/testgate/scripts/agent-gh-token <app-id> <key.pem>)\" >> \"$CLAUDE_ENV_FILE\""
      }]
    }]
  }
}
```

Keep the key outside `~/dev` and outside any repository, readable only by the
owner (`chmod 600`). Tokens expire after an hour; a new session mints a new one.

Deny rules in the owner's Claude Code settings stop the obvious escalations even
if a stronger token is around:

```json
{
  "permissions": {
    "deny": [
      "Bash(gh api *rulesets*)",
      "Bash(gh api *branches/*/protection*)",
      "Bash(gh api *environments*)",
      "Bash(gh api *actions/secrets*)",
      "Bash(gh api *actions/permissions*)",
      "Bash(gh secret *)",
      "Bash(gh pr merge*)",
      "Bash(gh repo clone Hzxon/testgate-hidden*)",
      "Read(~/.config/gh/**)"
    ]
  }
}
```

## Done when

For the agent identity, `gh api repos/Hzxon/testgate-hidden` returns 404 and a
ruleset edit returns 403, and the fine-grained PAT that used to sit in
`~/.claude/settings.json` is revoked.

## Hidden-test reviews (#60)

Once agents use this identity, an agent's hidden-test PR has an author other
than the owner, so the owner can approve it: ruleset 25734449 on
Hzxon/testgate-hidden requires one approving review, as ADR 0001 says.
