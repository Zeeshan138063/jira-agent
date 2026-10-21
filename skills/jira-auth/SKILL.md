---
name: jira-auth
description: Sign in to Jira Cloud and verify the session before any other jira-* skill runs. The default is a browser page where the user pastes an Atlassian API token (no app registration, no admin); browser OAuth consent with your own OAuth app is the advanced option. Use at session start after jira-setup, whenever a Jira call fails with 401/403, or when the user wants to sign in, check, or sign out.
---

# jira-auth

Gate for every Jira call. No other jira-* skill runs until `auth status` succeeds for the active
profile's site.

```bash
jira-agent auth <login|status|logout> [--profile NAME]
jira-agent auth login --method oauth      # advanced
```

`--profile` defaults to the last-used profile. Output is JSON on stdout; progress goes to stderr.
Exit code `3` means sign-in is needed.

## Default flow: API token (for everyone, including non-technical users)

1. Run `auth status`. If it succeeds, continue to the next skill.
2. Otherwise run `auth login`. Tell the user what will happen first, in plain words: "A page will
   open in your browser. It has a link to Atlassian's token page; create a token there, copy it,
   paste it into the page together with your Jira email, and click Connect."
3. The CLI opens a page on this computer (`http://127.0.0.1:...`; the address is also printed to
   stderr if the browser does not open). The page walks the user through:
   - opening https://id.atlassian.com/manage-profile/security/api-tokens (signing in if asked),
   - clicking **Create API token** (not "with scopes"), naming it `jira-agent`, picking an
     expiry, clicking Create, then Copy,
   - entering their Jira email and pasting the token, then **Connect**.
4. The page checks the token against the site, shows a clear message if Atlassian rejects it (the
   user can simply retry), and on success saves it to the macOS Keychain. The command then prints
   who is signed in.
5. Run `auth status` to confirm, then hand back to the caller.

The token goes straight from the user's browser to the Keychain. **Never ask the user to paste the
token into chat, and never read it from a page, a file or the Keychain yourself.** If they paste
it into chat anyway, tell them to revoke it on the token page and start again.

Tokens expire (the user picks the expiry). When one does, calls fail with `auth required`; run
`auth login` again. `auth logout` removes this site's saved sign-in; the user can also revoke the
token on Atlassian's token page at any time.

## Token expiry and renewing

Atlassian API tokens last 1 to 365 days (default and maximum: one year). The sign-in page asks which
expiry the user chose on Atlassian's page, and the CLI stores it with the token (the date is not secret).

- `session check` and `auth status` report `expiry`: `ok`, `expiring` (7 days or fewer), `expired`,
  `unknown`, or `not_applicable` (OAuth). `expiring` and `expired` also add a non-blocking
  `auth-renew` step. If the user did not say when theirs expires, the latest possible date (created
  plus 365 days) is assumed and the message says so.
- Mention the reminder in one plain sentence with the date, offer to renew, and carry on with their
  request. Renewing is just `auth login` again: the user creates a new token on Atlassian's page and
  pastes it into the local page. They can say "renew my Jira token" at any time.
- A token saved before expiry tracking, or one whose expiry the user now remembers: record it without
  re-entering the token using `auth expiry --date YYYY-MM-DD` (the date shown on Atlassian's token page),
  `auth expiry --days-left N`, or `auth expiry --unknown`.
- If Jira still accepts a token past its recorded date, it is only a warning; if Jira rejects it,
  sign-in is required (exit code 3).

## Advanced flow: OAuth consent (own OAuth app)

For teams that prefer scoped permissions and a consent screen, and have someone who can register an
Atlassian OAuth 2.0 (3LO) app once:

1. `auth setup --client-id <ID>` stores the app's client ID and secret (the secret is entered at a
   hidden terminal prompt or via `JIRA_AGENT_CLIENT_SECRET`; never in chat or on a command line).
   App registration: https://developer.atlassian.com/console/myapps/ > Create > OAuth 2.0
   integration; Permissions > Jira API > classic scopes `read:jira-work`, `write:jira-work`,
   `read:jira-user`; Authorization > OAuth 2.0 (3LO) callback URL
   `http://127.0.0.1:8765/callback`.
2. `auth login --method oauth` opens Atlassian's consent screen; the user clicks Accept. Tokens
   refresh automatically; a rejected refresh clears the session.

## Rules

- Secrets (API tokens, OAuth tokens, the client secret, authorization codes) are never printed,
  logged, committed, written to profiles or pasted into chat.
- A token tied to the wrong account or site fails with a clear message; fix the profile's site with
  `jira-setup` or sign in as the right account.
- Never complete the sign-in or consent steps on the user's behalf; they do them in their own
  browser.
- An API token carries all the permissions the user's Jira account has (it cannot be narrowed to
  read-only). The agent's own safeguards are the preview-and-confirm step for every write and the
  audit log. Mention this if the user asks about risk, and point to docs/SECURITY.md.
