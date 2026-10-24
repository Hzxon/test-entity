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
prints a one-hour installation token. `scripts/agent-gh-refresh` keeps that token
in a gh config dir of its own (`hosts.yml`, mode 600) and re-mints it when it is
older than 45 minutes. With `--env` it also prints the session environment:

- `GH_CONFIG_DIR` points at that dir, and `GH_TOKEN` is unset. gh reads the token
  from the file on every call, so a refreshed token takes effect at once, and gh
  never sees the owner's keyring login, even if an agent unsets variables.
- `GIT_CONFIG_*` clears every git credential helper (osxkeychain holds the
  owner's login) and adds `gh auth git-credential`, so `git push` runs as the app.

Copy both scripts next to the key (hooks run them by path), then:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "~/.config/testgate/agent-gh-refresh --env <app-id> ~/.config/testgate/agent-key.pem ~/.cache/testgate-agent/gh >> \"$CLAUDE_ENV_FILE\"",
        "timeout": 30
      }]
    }],
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "~/.config/testgate/agent-gh-refresh <app-id> ~/.config/testgate/agent-key.pem ~/.cache/testgate-agent/gh",
        "timeout": 30
      }]
    }]
  }
}
```

The PreToolUse hook is a no-op until the token is 45 minutes old, so a session
that runs for hours keeps a live token without ever reading the key itself.
Keep the key outside `~/dev` and outside any repository, readable only by the
owner (`chmod 600`).

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
      "Read(~/.config/gh/**)",
      "Read(~/.config/testgate/**)",
      "Bash(*.config/gh*)",
      "Bash(*.config/testgate*)",
      "Bash(*GH_CONFIG_DIR=*)",
      "Bash(*GIT_CONFIG_*)",
      "Bash(*credential.helper*)",
      "Bash(*credential-osxkeychain*)",
      "Bash(*security find-*)",
      "Bash(*security dump-keychain*)"
    ]
  }
}
```

The deny rules are speed bumps: a script can still reach the keychain. As long
as the owner's `gh` login and git keychain entry exist for the same macOS user
the agent runs as, an agent that tries hard enough can find them. The boundary
is to keep that login out of the agent's user: run agent sessions as a separate
macOS user, or `gh auth logout` and remove the github.com keychain entry on the
machine where agents run, and use the browser for owner actions.

## Done when

For the agent identity, `gh api repos/Hzxon/testgate-hidden` returns 404 and a
ruleset edit returns 403, and the fine-grained PAT that used to sit in
`~/.claude/settings.json` is revoked.

## Hidden-test reviews (#60)

The agent identity cannot see Hzxon/testgate-hidden, so an agent proposes
hidden tests as a patch in its session. The owner reads it, opens the PR under
their own name, and merges it. Ruleset 25734449 requires one approving review;
the owner cannot approve their own PR, so the repository admin role is a
bypass actor in **pull-request mode**: the owner can merge a reviewed PR, but
nobody can push to `main` directly. ADR 0001 matches.
