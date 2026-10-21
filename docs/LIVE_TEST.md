# Live test checklist

Everything so far is verified against mocked Atlassian responses. This is the first run against a
real Jira Cloud site. Use a throwaway project (or one where extra tickets are harmless) and a
profile with `write_mode: confirm_each`.

## 1. Create a profile

```bash
uv sync
uv run jira-agent profile create live --site-url https://YOURSITE.atlassian.net \
  --projects YOURKEY --kb-path ~/jira-kb/live
uv run jira-agent session check --offline
```

Expect `next` to contain `auth-login`. There is nothing to register first.

## 2. Sign in (browser page, API token)

```bash
uv run jira-agent auth login
```

A page opens in your browser (`http://127.0.0.1:<port>/...`).

1. Click the link to Atlassian's token page, sign in if asked, click **Create API token**, name it
   `jira-agent`, pick a short expiry (for example 30 days), click **Create**, then **Copy**.
2. Back on the page, enter your Jira email, paste the token, click **Connect**.
3. Expect "Connected ... as <your name>" in the browser and `ok: true` in the terminal.

Also try a deliberately wrong token once and confirm the page shows a clear message and lets you
retry.

```bash
uv run jira-agent auth status
uv run jira-agent session check
```

Expect your display name and `auth.signed_in: true`. Never paste the token into the chat.

## 3. (Optional) OAuth instead

Only if you want scoped permissions and a consent screen. Register an OAuth 2.0 (3LO) app at
https://developer.atlassian.com/console/myapps/ (Jira API classic scopes `read:jira-work`,
`write:jira-work`, `read:jira-user`; callback `http://127.0.0.1:8765/callback`), then
`auth setup --client-id <ID>` (secret at the hidden prompt) and `auth login --method oauth`.

## 4. Read paths

```bash
uv run jira-agent jira types YOURKEY
uv run jira-agent jira meta YOURKEY Task
uv run jira-agent jira search "project = YOURKEY ORDER BY created DESC" --limit 5
uv run jira-agent jira get YOURKEY-1
uv run jira-agent jira catalog refresh
```

Check: issue types match Jira; `meta` lists the project's real required fields; descriptions come
back as readable text.

## 5. Knowledge base

```bash
uv run jira-agent kb sync
uv run jira-agent kb status
uv run jira-agent kb search "a few words from a real ticket"
uv run jira-agent kb show YOURKEY-1
uv run jira-agent kb summary
```

Check: ticket count equals Jira's; a Markdown file exists per ticket; comments and status history
are present; search finds the ticket you described. Edit a ticket in Jira, run `kb sync` again,
and confirm only that ticket is re-fetched (`mode: incremental`).

## 6. Writes (previews first)

```bash
uv run jira-agent jira create --summary "jira-agent live test" --description "Safe to delete."
# exit 4 with a preview; then, after reading it:
uv run jira-agent jira create --summary "jira-agent live test" --description "Safe to delete." --confirm
uv run jira-agent jira update YOURKEY-N --summary "jira-agent live test (edited)"   # preview shows before/after
uv run jira-agent jira comment YOURKEY-N --body "hello"                            # preview, then --confirm
uv run jira-agent jira transitions YOURKEY-N
```

Check: required-field errors are readable if the project demands custom fields; the created
ticket looks right in Jira; `~/.jira-agent/audit.log` has one line per executed write.

## 7. Duplicates and grooming

```bash
uv run jira-agent dedupe --summary "jira-agent live test"          # should find the ticket just made
uv run jira-agent groom check --key YOURKEY-N
uv run jira-agent groom questions --round 1
```

## 8. Structure plan

```bash
uv run jira-agent structure example > /tmp/plan.json    # edit project and wording
uv run jira-agent structure validate /tmp/plan.json
uv run jira-agent structure apply /tmp/plan.json        # preview, exit 4
uv run jira-agent structure apply /tmp/plan.json --confirm
```

Check: epic, stories and sub-tasks exist with the right parents; the `Blocks` link points the right
way. If `parent` is rejected for a type, note which project type (team-managed or
company-managed) it was.

## 9. Through the agent

Interactive: `claude --plugin-dir /path/to/jira-agent`, then ask the agent to "groom YOURKEY-N" and
"create a ticket for ...". Confirm it runs `session check` first, shows previews, asks before writing,
and runs the duplicate check.

Headless, read-only (what was run on 2026-10-22, and passed):

```bash
claude -p "Is there already a Jira ticket about <topic>? Read-only: do not create, edit, comment on or transition anything." \
  --plugin-dir /path/to/jira-agent --agent jira-agent:jira-agent \
  --allowedTools 'Bash(jira-agent:*)' --disallowedTools Write Edit \
  --output-format json --max-turns 15 --no-session-persistence < /dev/null
```

The JSON result includes real token usage and cost (see [TOKEN_USAGE.md](TOKEN_USAGE.md)). Findings:
the plugin and agent load by that name, the agent follows the session-start procedure, answers from a
single `kb context` call, and changes nothing. While a plugin is enabled, Claude puts its `bin/` folder
on the PATH, so the agent runs plain `jira-agent <command>`; a permission rule `Bash(jira-agent:*)`
covers every step. Shell variables, `cd ... &&` and pipes are refused by Claude's permission checks, so
the agent is told to run one plain command per call.

## 10. Clean up

Delete the test tickets in Jira (the plugin never deletes), revoke the test API token on
Atlassian's token page, then optionally `uv run jira-agent auth logout` and
`rm -rf ~/jira-kb/live`.

## Results of the first live write run (2026-10-22)

Run against a real project, creating 7 clearly labelled test tickets (label `jira-agent-test`, prefix
"[jira-agent test]", nobody assigned, nothing deleted). Each write was previewed, then confirmed.

**Passed live:** create (Story, Epic, Sub-task), update with before/after preview, comment, status
transitions (by transition name and by target status name, and back), link, the epic > story >
sub-task plan with correct parents, re-running the same plan creating nothing, the audit log (one
entry per write), the knowledge base staying current after each write, and the agent creating a
ticket through a real headless Claude session (session check, duplicate check, preview, confirm).

**Bugs found only by running live (all fixed, each with a regression test):**

1. *Search lag.* Jira's search index trails a write by a few seconds, so refreshing the knowledge base
   by search right after a create found nothing, silently. Refresh now reads the ticket directly,
   falls back to a direct read for anything search misses, and warns if a ticket still cannot be found.
2. *Link direction.* `Blocks` was applied backwards ("A is blocked by B"). Jira sends the blocker as
   `inwardIssue`. The CLI is now `jira link TYPE FROM TO` ("FROM blocks TO") and the preview says so.
3. *Inheritance depth.* Labels, components and priority reached stories but not their sub-tasks. They
   now inherit through every level.

**Clean-up:** the tool never deletes. Find the test tickets with `labels = jira-agent-test` and
delete them in Jira (bulk change > delete, which needs delete permission). One structure-test link was
created with the old, reversed direction; it disappears with the tickets.

## Record the results

Note anything that differed from expectations (response shapes, field names, link directions,
permissions). Those are the places to adjust the code and tests.
