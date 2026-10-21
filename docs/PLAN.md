# Jira Agent — detailed plan

## Context
The user wants a reusable Claude Code plugin ("jira-agent") that automates Jira ticket creation, grooming, requirement gathering, dedupe and knowledge-base upkeep. Constraints from the user: **no MCP** (direct Jira REST), auth via **browser OAuth consent** (like MCP/agent installs), and **all inputs (site URL, projects, KB path, etc.) collected at session start and saved as reusable profiles**. Greenfield project, independent of any other codebase, so no exploration of an existing repo is needed.

Location: a standalone folder with its own git repo. Python managed with `uv`. No code comments unless the why is non-obvious.

## Folder layout
```
jira-agent/
  README.md                    install, first-run, profile guide
  pyproject.toml               uv project, ruff, pytest
  .claude-plugin/plugin.json   plugin manifest
  agents/jira-agent.md         orchestrator
  skills/
    jira-setup/SKILL.md        session-start profile picker/creator
    jira-auth/SKILL.md         OAuth 3LO login, pre-flight
    jira-api/SKILL.md          endpoint catalog, create/update/transition
    jira-groom/SKILL.md        groom existing / create new via questions
    jira-knowledge/SKILL.md    KB backfill + incremental sync
    jira-dedupe/SKILL.md       duplicate check before create
    jira-structure/SKILL.md    epic/story/sub-task breakdown + templates
  src/jira_agent/
    config.py                  Profile model, ~/.jira-agent/profiles/*.json, last_used
    auth/                      pkce.py, callback_server.py, keychain.py, oauth.py
    client/                    http.py (httpx, retry, rate limit), endpoints.py, issues.py, search.py, createmeta.py
    kb/                        store.py (SQLite FTS5), sync.py, render.py (markdown per ticket), summarize.py
    groom/                     checklist.py, templates.py, questions.py
    dedupe.py
    cli.py                     `jira-agent setup|login|sync|search|groom|create`
  tests/                       unit + recorded-fixture tests, optional live sandbox test
  docs/PLAN.md                 copy of this plan
```
Runtime data (outside repo): `~/.jira-agent/profiles/<name>.json`, `~/.jira-agent/state.json` (last_used), KB at each profile's `kb_path`, audit log `~/.jira-agent/audit.log`. Secrets only in macOS Keychain.

## Phases
1. **Scaffold** — create folder, `git init`, `uv init`, plugin manifest, README, copy plan to `docs/PLAN.md`, ruff/pytest config.
2. **Config + jira-setup** — `Profile` (pydantic): site_url, projects, kb_path, kb_mode (fts|fts+embeddings), sync_policy (on_start|manual|daily), issue_defaults, grooming_template, dedupe_threshold, write_mode (confirm_each|dry_run). CRUD, clone, last-used-first listing, per-session overrides, validation (site reachable, project keys exist, KB path writable).
3. **jira-auth** — OAuth 2.0 3LO + PKCE: loopback listener on fixed `127.0.0.1:8765/callback`, open browser to `auth.atlassian.com/authorize`, verify `state`, exchange code, fetch `accessible-resources` for cloudId, store access/refresh tokens + client secret in Keychain keyed by site, auto-refresh, re-login on refresh failure. Scopes: read:jira-work, write:jira-work, read:jira-user, offline_access. `setup` walks user through one-time OAuth app registration. Pre-flight `GET /myself` gates every other skill. Fallback: API token entered by user.
4. **jira-api client** — base `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3`; endpoint catalog harvested from Atlassian's OpenAPI spec (issue CRUD, JQL search with pagination, transitions, comments, links, fields, createmeta, projects, users, boards/sprints); runtime createmeta discovery for required/custom fields; ADF conversion helpers; retries/429 backoff; writes behind confirm/dry-run; audit log; no delete.
5. **jira-knowledge** — backfill all tickets per project via JQL (fields, description, comments, links, changelog highlights) → markdown per ticket + SQLite FTS5 index; project summaries (conventions, components, glossary, DoD patterns, owners); incremental sync on `updated >= last_sync`; KB records its profile + last sync; optional embeddings later behind `kb_mode`.
6. **jira-dedupe** — FTS (+JQL text search) similarity, top matches with reasons; outcomes: create anyway / link as related / update existing.
7. **jira-groom** — existing mode: fetch, score against checklist (title, problem, AC in Given/When/Then, scope, dependencies, estimate, labels), rewrite gaps, before/after diff, confirm, update. New mode: 3-option questions in rounds (type/goal → scope/AC → priority/deps), draft, run dedupe, confirm, create.
8. **jira-structure** — split requirement into epic/stories/sub-tasks using templates learned from the KB; set labels/components/links; feeds groom.
9. **Orchestrator agent** — session-start flow: pick/create profile → auth pre-flight → KB freshness check per sync_policy → route intent to skills; confirm-before-write enforced; treat Jira text as data, never instructions.
10. **Docs + hardening** — README, troubleshooting, security notes; later optional skills: jira-requirements, jira-digest, jira-bulk.

## Safety
Tokens never logged or written to files; confirm every write; dry-run mode; rate-limit handling; audit log of writes; KB content treated as untrusted data.

## Verification
- `uv run pytest` (config round-trip, PKCE/state, callback server, token refresh with mocked Atlassian, JQL pagination, ADF conversion, FTS search, dedupe ranking, groom checklist).
- `uv run ruff check . && uv run ruff format --check .`
- Manual e2e on a throwaway Jira project: `jira-agent setup` → browser consent → `/myself` OK → `sync` populates KB → `search` finds tickets → groom an existing ticket (dry-run, then real) → create a new ticket via questions with dedupe warning → second session reuses saved profile without re-login.
- Confirm Keychain holds tokens and no secret appears in profiles, logs or repo.

## Needs from user during build
Atlassian OAuth app client ID/secret (one-time, guided); site URL and first project key for manual e2e.
