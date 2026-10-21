# Security notes

## What the plugin handles

| Asset | Where it lives | Protection |
|---|---|---|
| API token, or OAuth access and refresh tokens | macOS Keychain, service `jira-agent`, one entry per site | Never written to files, logs, profiles or command output |
| OAuth client secret (only if you use your own OAuth app) | Keychain, entered at a hidden terminal prompt (or `JIRA_AGENT_CLIENT_SECRET`) | Never accepted as a command-line argument |
| Profiles | `~/.jira-agent/profiles/*.json` | No secrets; files `0600`, new directories `0700` |
| Ticket knowledge base | the profile's `kb_path` | Contains ticket text; new directory `0700`, `index.db` `0600`. Treat it like the Jira data it copies |
| Usage log | `~/.jira-agent/usage.jsonl` (`0600`) | Command name (never arguments or output), estimated token count, seconds and exit code; `JIRA_AGENT_NO_USAGE=1` disables it |
| Audit log | `~/.jira-agent/audit.log` (`0600`) | Records action, target, changed field names and result key; never descriptions, comments or tokens |

## Sign-in design

### Default: API token pasted into a local page

- `auth login` starts a one-shot web server on `127.0.0.1` (a random port) and opens it in the
  user's browser. The page links to Atlassian's token page, then collects the user's email and
  token.
- The page address contains a random 256-bit path. Requests to any other path, with a `Host` other
  than `127.0.0.1:<port>`, or with a foreign `Origin` are refused, so another website or local
  process cannot submit to or probe the form. Bodies over 8 KB are rejected.
- The token is checked against the site (`GET /rest/api/3/myself` with HTTP Basic auth), then saved
  to the Keychain. It is never echoed back in a page, printed, logged or written to a file. The page
  sends `Cache-Control: no-store`, a strict Content-Security-Policy and
  `Referrer-Policy: same-origin`, and the token field is a password input with autocomplete off.
- The token never passes through the chat or the model: it goes from the user's browser to the
  local server to the Keychain.
- **Limit:** an Atlassian API token carries all the permissions of the user's account and cannot be
  made read-only. Scoped tokens exist but use a different endpoint and are not supported yet. The
  user chooses the expiry (shorter is safer) and can revoke the token at any time on Atlassian's
  token page. Treat the Keychain entry like the password it effectively is.

### Optional: OAuth 2.0 (3LO) with your own app

- OAuth 2.0 authorization code flow with PKCE (S256) and a random `state`.
- The redirect listener binds to `127.0.0.1` only, accepts one matching response, and ignores
  responses whose `state` does not match, so a stray local request cannot cancel or hijack a login.
- Requested scopes: `read:jira-work`, `write:jira-work`, `read:jira-user`, `offline_access`. No
  admin or delete scopes, so this is the narrower option.
- Refresh tokens rotate; the new one is stored on every refresh. A rejected refresh deletes the
  stored session.

### Both

- HTTP redirects are never followed, so credentials cannot be forwarded to another host.
  Requests use a 30 s timeout and retry only 429 and transient 5xx responses.

## Write safety

- Every create, update, transition, comment, link and structure apply is previewed first and only
  runs with an explicit `--confirm`. `dry_run` profiles never write, even with `--confirm`.
- There is no delete operation anywhere.
- Issue keys and project keys are validated, and API paths containing `..`, `?`, `#`, `%`,
  whitespace or backslashes are refused before any request, so a crafted key cannot redirect a call
  to another endpoint. Knowledge-base file paths are built only from validated keys.
- Structure plans save progress after each ticket, so a failure never causes duplicate creation on
  retry.

## Untrusted content

Jira summaries, descriptions, comments and linked text can contain instructions aimed at an AI
assistant. The agent and every skill treat that text as data. The preview-and-confirm step is the
backstop: nothing a ticket says can make a write happen without the user approving the exact
change.

## Known limits

- The plugin does not sandbox the agent. The agent can read local files you point it at (for
  example `--description-file`), as any Claude Code session can.
- Anyone with access to your user account on the machine can read the knowledge base and use the
  Keychain items as you can. Use full-disk encryption and a locked screen.
- With your own OAuth app, the client secret belongs to the Atlassian app you create; each user keeps
  their own copy in their own Keychain. Rotate it in the Atlassian developer console if it is exposed, then re-run
  `auth setup`.
- `write:jira-work` allows edits Jira's own permissions allow for your account. The plugin adds
  previews and an audit log; it does not narrow what Jira would let you do.
- Duplicate detection and grooming scores are heuristics, not guarantees.

## If something leaks

1. Revoke the API token on https://id.atlassian.com/manage-profile/security/api-tokens (and, for
   OAuth, the app's access under Connected apps, then rotate the client secret).
2. `jira-agent auth logout` for each site, then sign in again.
3. Delete knowledge bases that should not persist.

Report problems to the repository owner privately rather than in a public issue.
