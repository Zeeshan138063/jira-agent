# Changelog

All notable changes. Versions follow semantic versioning; the plugin version lives in
`.claude-plugin/plugin.json` and must match `pyproject.toml`, `.claude-plugin/marketplace.json` and
`src/jira_agent/__init__.py` (a test enforces it).

## 0.10 - unreleased

- Duplicate check learns word weights from each project: words shared by many tickets count little,
  rare words count a lot, and a title repeated across the project (a template) is no longer an
  automatic duplicate. On a real 600-ticket project, precision at the default threshold went from
  0.46 to 0.70 with recall unchanged (about 0.75); candidate retrieval was unaffected.
- `jira-agent eval dedupe` and `eval candidates`: measure precision, recall and retrieval from
  labelled pairs, Jira clone/duplicate links and generated reworded duplicates. See docs/DUPLICATES.md.
- `jira` entry skill: `npx skills add <repo-url> --skill jira` installs it in any skills-aware agent;
  it installs the CLI with `uv tool install` on first use. `jira-agent --version` added.
- Cross-platform: UTF-8 file handling everywhere, `JIRA_AGENT_SECRETS=file` fallback for machines
  without a keyring, Windows launcher, exclusive loopback port on Windows, CI on Linux/macOS/Windows.
- Decisions recorded: each person syncs their own knowledge base, visibility is whatever Jira shows
  that person, sized for about 10,000 tickets per project. Embeddings wait for the evaluation data.

## 0.9.0 - first team release

- Agent and seven skills: setup, auth, api, groom, knowledge, dedupe, structure.
- Direct Jira Cloud REST client (no MCP). Sign-in with an Atlassian API token on a local browser page;
  OAuth 2.0 (3LO) with your own app is optional. Token expiry tracking and renewal reminders.
- Knowledge base of existing tickets: full-text search, reconcile for deletions and moves,
  single-ticket refresh, cards, epic digests, and a token-budgeted context pack for drafts.
- Grooming checklist with per-type, per-project weights; epics graded on goal and success measures.
- Duplicate check, epic/story/sub-task plans with a resumable apply, preview-and-confirm for every write.
- Team presets (`profile export` / `profile import`), token usage meter (`jira-agent usage`), PM guide.
- Verified against a real Jira site, including live writes and a real headless Claude session.
