# jira-agent

A Claude Code plugin that creates, grooms and de-duplicates Jira tickets, turns requirements
into epics/stories/sub-tasks, and keeps a searchable knowledge base of existing tickets. It talks
to Jira Cloud's REST API directly (no MCP). Sign-in is a page in your browser where you paste an
Atlassian API token (no app registration, no admin); browser OAuth consent is available for teams
that want it.

## What is in it

| Piece | Purpose |
|---|---|
| `agents/jira-agent.md` | Orchestrator: session start-up, routing, and the safety rules |
| `jira-setup` | Pick, create or clone a saved profile (site URL, projects, KB path, sync and write mode) |
| `jira-auth` | Browser sign-in (API token page by default, OAuth optional); credentials kept in the macOS Keychain |
| `jira-api` | Read, search, create, update, transition, comment and link issues; endpoint catalog |
| `jira-groom` | Groom an existing ticket, or create a new one through guided questions |
| `jira-knowledge` | Build and refresh the ticket knowledge base (markdown + SQLite FTS) |
| `jira-dedupe` | Check for similar tickets before creating |
| `jira-structure` | Break a requirement into epic / stories / sub-tasks and create the tree |

## Requirements

- Claude Code and [`uv`](https://docs.astral.sh/uv/) (uv also provides Python 3.11+)
- macOS (tokens use the system Keychain via `keyring`)
- A Jira Cloud site and an Atlassian account that can use it. Nothing to register: the sign-in page
  walks you through creating an API token.

## Install

```bash
claude plugin install jira-agent --marketplace <owner>/<repo>      # or a git URL or folder path
```

Then start Claude and say "Let's work on Jira." Full instructions (team-wide install, updates,
uninstall, troubleshooting): [docs/INSTALL.md](docs/INSTALL.md). To try it from a checkout without
installing: `uv sync && claude --plugin-dir /path/to/jira-agent`.

On first use the agent will:

1. `session check` - find a team preset or create a profile (`jira-setup`)
2. open the sign-in page in your browser, create an Atlassian API token, paste it, Connect (`jira-auth`)
3. backfill the knowledge base (`jira-knowledge`)

After that, a new session picks up your last profile and only syncs what changed.

Or, in any agent that supports skills: `npx skills add <repo-url> --skill jira` (see docs/INSTALL.md).

## CLI

The agent drives this CLI; you can too. Everything prints JSON.

```bash
uv run jira-agent session check            # profile + sign-in + KB status in one call
uv run jira-agent profile list|create|update|clone|use|delete|check|export|import
uv run jira-agent auth login|status|expiry|logout   # advanced OAuth: auth setup, login --method oauth
uv run jira-agent usage [--days N] [--static]     # tokens the CLI has handed to the model
uv run jira-agent jira get|search|types|meta|create|update|transition|comment|link|catalog
uv run jira-agent kb sync|refresh|reconcile|status|search|context|card|epic|epics|show|summary
uv run jira-agent dedupe --summary "..."
uv run jira-agent groom check|template|questions|conventions
uv run jira-agent structure patterns|example|validate|apply
```

Exit codes: `0` ok, `2` bad input, `3` sign-in needed, `4` preview awaiting approval
(re-run with `--confirm`), `5` Jira API error.

## Where things live

| What | Where |
|---|---|
| Profiles | `~/.jira-agent/profiles/<name>.json` (no secrets), `~/.jira-agent/state.json` |
| Team preset (optional) | `jira-agent.preset.json` in the working folder, `~/.jira-agent/preset.json`, or `$JIRA_AGENT_PRESET` |
| API token / OAuth tokens, OAuth client secret | macOS Keychain, service `jira-agent` |
| Audit log of writes | `~/.jira-agent/audit.log` (action, target, field names only) |
| Endpoint catalog | `~/.jira-agent/catalog/jira-v3.json` |
| Custom grooming templates | `~/.jira-agent/templates/<name>/<Type>.md` |
| Knowledge base | each profile's `kb_path` (`index.db`, `tickets/`, `projects/`) |
| Structure plan progress | `<plan>.json.state.json` next to the plan file |

Set `JIRA_AGENT_HOME` to relocate `~/.jira-agent`.

## Safety

- Every write is previewed and needs an explicit yes before it runs (`--confirm`); `dry_run`
  profiles never write. There is no delete.
- Secrets are never printed, logged or put in profiles.
- Text read from Jira is treated as data, never as instructions.
- A knowledge base is bound to one Jira site.

## Docs

- [docs/INSTALL.md](docs/INSTALL.md) - installing, updating and hosting for a team
- [docs/RELEASING.md](docs/RELEASING.md) - how an admin ships a new version
- [docs/PM_GUIDE.md](docs/PM_GUIDE.md) - plain-language guide for product managers
- [docs/TEAM_SETUP.md](docs/TEAM_SETUP.md) - sharing the agent with a team: presets, what is personal, what is shared
- [docs/TOKEN_USAGE.md](docs/TOKEN_USAGE.md) - how many AI tokens the agent uses, measured
- [docs/LIVE_TEST.md](docs/LIVE_TEST.md) - first run against a real Jira site, including the sign-in
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) - symptoms and fixes, exit codes, starting clean
- [docs/SECURITY.md](docs/SECURITY.md) - what is stored where, protections and known limits
- [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md) - how the knowledge base stays correct and cheap, with measurements
- [docs/PLAN.md](docs/PLAN.md) - the original build plan

## Status

Version 0.9.0. Covered by tests against mocked Atlassian responses (`uv run pytest`) and verified against a
real Jira site: sign-in, reads, the knowledge base, and live writes (create, edit, comment, transition,
link, epic plans) including through a real Claude session. Not yet verified: the optional OAuth sign-in,
operating systems other than macOS, and wide use by non-technical teammates. See
[CHANGELOG.md](CHANGELOG.md).

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
claude plugin validate . --profile directory --strict
```
