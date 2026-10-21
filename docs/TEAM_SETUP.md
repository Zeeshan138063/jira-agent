# Sharing the Jira Agent with your team

The plugin contains no site, project, email, name or path of its own. Everything that differs between
people or teams is configuration that each person (or an admin, once) supplies.

## What is shared and what is personal

| Setting | Where it comes from | Shared? |
|---|---|---|
| Jira site URL, project keys | the **team preset** (or typed once per person) | shared |
| Sync policy, write mode, duplicate threshold, default issue type/labels, grooming template name | the team preset | shared |
| Profile name | the preset's suggestion, or each person's choice | either |
| Folder for the ticket copy (`kb_path`) | each person (default `~/jira-kb/<name>`) | personal |
| Jira email and API token | each person, on first sign-in; kept in their Keychain | personal, never in a file |
| Token expiry date | each person, on first sign-in | personal |
| OAuth app id and secret (only if you use OAuth) | each person's Keychain | personal |

A preset never contains secrets, emails, names or folders.

## Admin: set up once

1. **Host and install the plugin.** Put the repository on a git host your team can reach and share the
   marketplace source; everyone runs one install command. Details, including installing for everyone who
   uses a repository: [INSTALL.md](INSTALL.md).
2. **Create a preset** from a profile that has the settings you want everyone to share:

   ```bash
   uv run jira-agent profile create team --site-url https://<yoursite>.atlassian.net \
     --projects KEY1,KEY2 --kb-path ~/jira-kb/team --sync-policy daily
   uv run jira-agent profile export team --output jira-agent.preset.json
   ```

3. **Hand it out.** Any one of these works; the agent finds the file by itself on first run:
   - commit `jira-agent.preset.json` in the team's shared repo (it is picked up when Claude starts in
     that folder),
   - put the file at `~/.jira-agent/preset.json` on each machine,
   - or set `JIRA_AGENT_PRESET=/path/to/jira-agent.preset.json`.
4. **Allow the agent's commands** so people are not asked on every step. In each person's Claude
   settings (or the team's managed settings), allow `Bash(jira-agent:*)`. The agent never needs
   to write files.

## Each person: first run

Start Claude with the Jira Agent and say "Let's work on Jira." If a preset is found, the agent skips
the questions about site and projects and only asks where to keep your ticket copy. Then the browser
page opens to create and paste your own API token. See [PM_GUIDE.md](PM_GUIDE.md).

Manual alternative: `uv run jira-agent profile import jira-agent.preset.json --kb-path ~/jira-kb/me`.

## Good to know

- Everyone signs in with **their own** token, so Jira shows their name on what they create and their own
  permissions apply. There is no shared service login.
- Each person keeps their own ticket copy. That respects per-person visibility in Jira (restricted
  tickets stay restricted). A shared snapshot is possible later but needs a decision on restricted tickets.
- Team-specific grooming templates: put `<Type>.md` files in `~/.jira-agent/templates/<name>/` on each
  machine (distribute the folder with the preset) and set `grooming_template` to `<name>` in the preset.
- Offboarding: the person deletes their token at
  https://id.atlassian.com/manage-profile/security/api-tokens (an admin can also remove the account in
  Jira). Nothing else is shared between machines.

## Before you share the repository

- Run `uv run pytest`. It includes guards that fail if an email address, a home-directory path, a real
  Atlassian site or a token-shaped string ends up in the repository.
- Check the git author metadata (`git log --format='%an <%ae>'`). Commits carry whoever made them; use a
  team identity for a release commit if that matters to you.
- Fill in the author, license and repository fields in `.claude-plugin/plugin.json` with your team's details
  when you package it.
