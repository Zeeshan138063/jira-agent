---
name: jira-setup
description: Start every Jira Agent session by choosing, creating, cloning or adjusting a saved profile (Jira site URL, project keys, knowledge-base path, sync and write settings). Use before any other jira-* skill, and whenever the user wants to switch site, project or KB.
---

# jira-setup

Collect the session inputs once, save them as a profile, and reuse them next time. Every other
jira-* skill needs an active profile, so run this first.

All profile operations go through the CLI, which prints JSON:

```bash
jira-agent profile <list|show|create|update|clone|use|delete|check|export|import> ...
```

## Flow

1. Run `profile list`. If there is no saved profile and `session check` reported a `preset`, a team
   preset (site, projects and defaults chosen by the team) is available: offer to use it. Ask only
   for the person's name for the profile (default suggested) and where to keep their ticket copy
   (default `~/jira-kb/<name>`), then run `profile import <preset-file> [--name N] [--kb-path P]`.
   Skip the questions below for anything the preset already answers. Nothing about the site, the
   projects, the person or the machine is built in; it all comes from a preset or from the user.
2. **Saved profiles exist:** ask one question, last-used profile first:
   - Use `<last_used>` (show its site URL and projects)
   - Pick another saved profile
   - Create a new profile
   Offer "use it but change something for this session" as an option on the chosen profile.
3. **Create:** ask only for what is missing, in rounds of up to three questions with a
   recommended default marked:
   - Round 1: Jira site URL (`https://<site>.atlassian.net`), project key(s), profile name.
   - Round 2: knowledge-base folder (default `~/jira-kb/<name>`), sync policy
     (`on_start` / `manual` / `daily`), KB mode (`fts` recommended, `fts+embeddings` optional).
   - Round 3: write mode (`confirm_each` recommended, or `dry_run`), default issue type,
     default labels/components.
4. Save with `profile create` (it also marks the profile as last used). Use `profile clone` to
   start from an existing profile on the same site, and `profile update` to change saved values.
5. **Session-only overrides:** apply them in the conversation without saving; never pass them to
   `profile update` unless the user asks to persist them.
6. Run `profile check <name>`. If it reports problems (for example an unwritable KB path), fix
   them with the user before continuing.
7. Hand off: confirm the active profile in one line, then run `jira-auth` (pre-flight), and
   afterwards `jira-knowledge` if the KB is missing or stale for the profile's `sync_policy`.

## Rules

- Profiles never contain secrets. Tokens and the OAuth client secret belong to `jira-auth` and
  the macOS Keychain.
- Validate with the CLI, not by guessing: it rejects non-https site URLs and malformed project
  keys.
- Remote checks (site reachable, project keys exist) happen in `jira-auth` / `jira-api` once a
  token exists. Report any mismatch and offer `profile update`.
- Never delete a profile without an explicit yes from the user.
