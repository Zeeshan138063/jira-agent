# Troubleshooting

Run `uv run jira-agent session check` first. It reports the active profile, sign-in state and
knowledge-base freshness, and its `next` list says what to fix.

Exit codes: `0` ok, `2` bad input, `3` sign-in needed, `4` preview awaiting approval, `5` Jira or
network error.

## Sign-in

`jira-agent auth login` opens a page on your own computer (`http://127.0.0.1:<port>/...`). If the
browser does not open, copy the address printed in the terminal.

| Symptom | Cause and fix |
|---|---|
| "Atlassian did not accept that email and token" | Use the email you sign in to Jira with. Copy the whole token (the page removes spaces and line breaks). Create it with the plain **Create API token** button, not "with scopes". Tokens that were revoked or have expired also fail: create a new one. |
| "Could not reach https://..." | No connection, or the profile's `site_url` is wrong. Check it with `profile show NAME`. |
| The page says "Not found" | The address is single-use and tied to one `auth login` run. Start `auth login` again and use the new address. |
| `timed out waiting for the sign-in form` | The form was not completed within 10 minutes. Run `auth login` again. |
| Calls start failing with `auth required` later | The API token expired or was revoked. Run `auth login` and create a new one. |
| `auth-renew` reminder in `session check` | The token expires within 7 days (or its recorded date has passed). Create a new token and run `auth login`. If you know the real date, `auth expiry --date YYYY-MM-DD` corrects it without re-entering the token. |
| Claude asks permission for every command | Choose "Always allow" for `jira-agent`, or have an admin allow `Bash(jira-agent:*)` in settings. The agent never needs write access to files. |
| `jira-agent: command not found` inside Claude | The plugin is not enabled in this session. Run `claude plugin list`, enable `jira-agent`, and restart Claude. |
| `signed-in account has no access to <site>` (OAuth) | You signed in with an account that cannot reach the profile's site. `auth logout`, sign in as the right account, or fix the profile's `site_url`. |
| `keychain unavailable` | The macOS Keychain is locked or denied access. Unlock it and retry. |
| 403 on a specific issue or project | The account lacks permission in Jira itself; sign-in is fine. |

### OAuth (advanced)

| Symptom | Cause and fix |
|---|---|
| `OAuth app not configured` | Register the OAuth app once (steps in the jira-auth skill), then `auth setup --client-id <ID>` and type the secret at the hidden prompt. |
| `cannot listen on 127.0.0.1:8765` | Another process holds the port. Free it, or pick another with `auth setup --port N` and change the app's callback URL to match. |
| Atlassian says the callback URL does not match | The callback must be exactly `http://127.0.0.1:<port>/callback` (no `localhost`, no trailing slash). |
| `timed out waiting for browser sign-in` | Consent was not completed within 5 minutes. Run `auth login --method oauth` again. Responses with the wrong `state` are ignored on purpose. |
| `authorization denied: access_denied` | Consent was declined, or the org restricts third-party OAuth apps. Ask an Atlassian admin to approve the app. |
| `session ... expired` | The refresh token was revoked. Run `auth login --method oauth` again. |

## Profiles and knowledge base

| Symptom | Cause and fix |
|---|---|
| `this knowledge base belongs to <site>` | A KB folder is bound to one Jira site. Give the profile a different `kb_path` (`profile update NAME --kb-path ...`). |
| `kb_path is not writable` | Choose a folder you can write to. |
| First sync is slow | It pulls every ticket in the project, including comments and history. Progress prints every 100 tickets; an interrupted sync keeps what it fetched and resumes from there. |
| Tickets deleted or moved in Jira still appear | Incremental sync cannot see them. Run `kb reconcile` (add `--dry-run` first to see what it would do). A full sync also removes them. |
| `refusing to remove N of M tickets` | Safety stop: removing over half a project usually means a permissions problem or outage. Check access, then `kb reconcile --force` only if the removals are real. |
| `kb status` asks for a full re-sync | The stored format or fetched fields changed; `kb sync` does it once (about 10 s per 600 tickets). |
| A warning says the knowledge base was not refreshed after a write | The Jira write succeeded; the follow-up refresh failed. Run `kb refresh KEY`. |
| Search misses something you know exists | Run `kb sync` first. Search matches words, not meaning, so try other terms from the ticket. |
| Wrong project conventions | `kb summary` is computed from synced tickets; re-sync, and check the profile lists the right projects. |

## Creating and editing tickets

| Symptom | Cause and fix |
|---|---|
| Exit `4` with `confirmation_required` | Working as intended: show the preview, get a yes, re-run with `--confirm`. |
| `missing required field(s): customfield_...` | The project requires a field the draft lacks. `jira meta PROJECT TYPE` lists them; pass `--field key=value`. |
| `field 'parent' is not available on this issue type` | That project does not allow that parent relationship for the type. Adjust the plan, or link instead. |
| `issue type '...' not in PROJECT` | The message lists the available types. Plans can set `story_type`, `epic_type`, `subtask_type`. |
| `field ... is not editable` | The ticket's screen or workflow hides it. Edit it in Jira, or pick another field. |
| `no transition '...'` | The message lists the transitions available from the current status. |
| `refusing to call an unsafe API path` | An issue key or project contained characters such as `..`, `?`, `#`, `%` or spaces. Use the plain key (`PROJ-123`). |
| `network error: ConnectError` | No route to Atlassian. Check the connection and retry; the command did not complete. |
| Jira errors with 429 | Rate limit. The client retries with backoff; if it still fails, wait a minute. |

## Structure plans

| Symptom | Cause and fix |
|---|---|
| A plan failed midway | The output lists what was created and the failure. Fix the cause and re-run the same `structure apply ... --confirm`; created tickets are skipped. The progress file is `<plan>.json.state.json`. |
| Start over with the same plan | Only after deleting the created tickets in Jira, remove the `.state.json` file. The tool never deletes tickets. |
| Plan rejected as invalid | The message names the field path (for example `stories.0.id`). IDs allow letters, digits, `_` and `-`. |

## Duplicate check

- Too many false alarms: raise `dedupe_threshold` (`profile update NAME --dedupe-threshold 0.7`).
- Missing real duplicates: lower it, run `kb sync`, and remember matching is word-based.

## Starting clean

```bash
uv run jira-agent auth logout                     # forget tokens for the active site
uv run jira-agent profile delete NAME             # remove a saved profile
rm -rf ~/.jira-agent/catalog                      # drop the cached endpoint catalog
rm -rf <kb_path>                                  # drop a knowledge base (re-sync rebuilds it)
```

Credentials live in the Keychain (service `jira-agent`): one `tokens:<site>` entry per site, plus
`oauth-client` if you use your own OAuth app. Revoke API tokens on Atlassian's token page.
