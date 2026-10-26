# Installing the Jira Agent

## What you need

- **Claude Code** (a recent version; the one-step `--marketplace` install needs 2.1.275 or later).
- **[`uv`](https://docs.astral.sh/uv/)**, which also fetches Python 3.11+ if needed. The plugin's launcher
  runs through it.
- **macOS** (sign-in tokens use the system Keychain). Other systems are not tested.
- A Jira Cloud account. Network access once, to download the plugin's Python packages on first use.

## Install with one line (non-technical teammates)

`install/install.sh` (macOS, Linux) and `install/install.ps1` (Windows) do everything: install Claude
Code and `uv` if missing (and Git on Windows), install or update the plugin, save the team's Jira site
and projects as a preset, allow `jira-agent` commands, and put a **Jira Agent** shortcut on the Desktop.
Running it again updates the plugin. Replace the site and project keys before sending.

macOS / Linux (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/Zeeshan138063/jira-agent/main/install/install.sh | bash -s -- --site https://yoursite.atlassian.net --projects KEY1,KEY2
```

Windows (PowerShell):

```powershell
$env:JIRA_SITE="https://yoursite.atlassian.net"; $env:JIRA_PROJECTS="KEY1,KEY2"; irm https://raw.githubusercontent.com/Zeeshan138063/jira-agent/main/install/install.ps1 | iex
```

Then double-click **Jira Agent** on the Desktop. First run: sign in to Claude, then connect Jira.

## Install (teammates)

Ask your admin for the **marketplace source** (see [Hosting](#hosting-admin) below), then:

```bash
claude plugin install jira-agent --marketplace <source>
```

Or in two steps (the marketplace name is `team-plugins`):

```bash
claude plugin marketplace add <source>
claude plugin install jira-agent@team-plugins
```

Restart Claude if it was already open. Then check:

```bash
claude plugin list                  # jira-agent@team-plugins, enabled
claude plugin details jira-agent    # components and the plugin's projected token cost
```

Start Claude and say **"Let's work on Jira."** The first run builds the plugin's environment (about 8
seconds once, with network access) and then walks you through connecting Jira
([PM_GUIDE.md](PM_GUIDE.md)).

## Install with one command (any agent that supports skills)

```
npx skills add https://github.com/Zeeshan138063/jira-agent --skill jira
```

This copies the `jira` skill into your agent (Claude Code, Cursor, Codex and others). On first use the
skill checks for the `jira-agent` command and, with your OK, installs it with
`uv tool install git+<repo-url>` (needs [uv](https://docs.astral.sh/uv/)). Add the other skills the
same way (`--skill jira-groom`, and so on) or leave `--skill` off to install them all. The Claude Code
plugin above is still the fuller route: it also gives you the dedicated agent and puts the command on
the PATH for you. Update the command with `uv tool upgrade jira-agent`.

## Operating systems

Tested on every change (GitHub Actions) on Linux, macOS and Windows, Python 3.11 and 3.13.

| | macOS | Windows | Linux |
|---|---|---|---|
| Needs | `uv`, Python 3.11+ | `uv`, Python 3.11+ | `uv`, Python 3.11+ |
| Token storage | Keychain | Credential Manager | Secret Service (GNOME Keyring, KWallet) |
| Launcher | `bin/jira-agent` | `bin/jira-agent.cmd` | `bin/jira-agent` |

- **No desktop keyring** (headless Linux, containers, some WSL setups): set `JIRA_AGENT_SECRETS=file`
  to keep the token in `~/.jira-agent/secrets.json` (owner-only on Linux and macOS; on Windows the
  user profile's access rules apply). It is weaker than an OS keyring, so use a short-lived token.
- **Windows:** file permission modes are not enforced the way they are on Linux and macOS; the
  files live in your user profile, which other users cannot read by default. Use a terminal where
  `uv` is on the PATH.
- All files are read and written as UTF-8, so ticket text in any language is safe.

## Install for everyone who uses a repository

Declare the marketplace and plugin in the repository's Claude settings so teammates are offered it
automatically:

```bash
cd <your-team-repo>
claude plugin marketplace add <source> --scope project
claude plugin install jira-agent@team-plugins --scope project
git add .claude/settings.json && git commit -m "Enable the Jira Agent"
```

The generated settings contain `extraKnownMarketplaces` and `enabledPlugins`. Add the team preset
(`jira-agent.preset.json`, see [TEAM_SETUP.md](TEAM_SETUP.md)) to the same repository and a new person only
has to sign in.

## Hosting (admin)

The repository is both the plugin and a one-plugin marketplace (`.claude-plugin/marketplace.json`). Put
it on any git host your team can reach and give people one of these as the `<source>`:

| Source | Example |
|---|---|
| GitHub shorthand | `owner/repo` |
| HTTPS or HTTP git URL | `https://git.example.com/team/jira-agent.git` |
| SSH git URL | `git@git.example.com:team/jira-agent.git` |
| A local folder (evaluation, offline) | `/path/to/jira-agent` |

Private hosts work the same way as any git clone: people need access with their normal git credentials
(SSH key or token). To evaluate without installing, run `claude --plugin-dir /path/to/jira-agent`.

To rename the marketplace (the `@team-plugins` part), change `name` in `.claude-plugin/marketplace.json`.

## Update

```bash
claude plugin marketplace update team-plugins
claude plugin update jira-agent@team-plugins        # restart Claude to apply
```

Settings, profiles, tokens and ticket copies live outside the plugin (`~/.jira-agent`, the Keychain) and
survive updates. Release steps for admins: [RELEASING.md](RELEASING.md).

## Uninstall

```bash
claude plugin uninstall jira-agent
```

This leaves your data. To remove it too: delete `~/.jira-agent` and your ticket-copy folders, run
`jira-agent auth logout` first (or delete the Keychain entries named `jira-agent`), and revoke the token at
https://id.atlassian.com/manage-profile/security/api-tokens .

## What the plugin costs per session

`claude plugin details jira-agent` reports about **940 tokens always added to every session** (the skill
descriptions) and 1 to 2.2k tokens each time a skill is used. The agent file adds about 1.5k when the
agent is used. See [TOKEN_USAGE.md](TOKEN_USAGE.md). Note: that command may show "Agents (0)"; the agent is
still registered (`jira-agent:jira-agent`) and works.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Plugin "jira-agent" not found in marketplace` | Run `claude plugin marketplace update team-plugins`, or check the source you were given. |
| `Invalid marketplace source format` | Use `owner/repo`, an `https://` or `http://` URL, an SSH git URL, or a folder path. `file://` URLs are not accepted. |
| `uv: command not found` when the agent runs | Install uv (https://docs.astral.sh/uv/), then restart Claude. |
| `jira-agent: command not found` inside Claude | The plugin is not enabled: `claude plugin list`, enable it, restart Claude. |
| First run seems stuck for several seconds | It is downloading the plugin's packages once. Later runs start in under a second. |
| Claude asks permission for every command | Choose "Always allow" for `jira-agent`, or have an admin allow `Bash(jira-agent:*)`. |
| Behind a proxy or offline | `uv` needs access to a Python package index on first use; set the usual proxy variables or a mirror (`UV_INDEX_URL`). |
