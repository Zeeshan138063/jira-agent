# JQL cheat sheet

Use with `jira-agent jira search --jql "<query>" --limit 20`. Replace `PROJ` with your project key.

| Goal | JQL |
| --- | --- |
| Open tickets in a project | `project = PROJ AND statusCategory != Done` |
| Assigned to me | `assignee = currentUser() AND statusCategory != Done` |
| Updated recently | `project = PROJ AND updated >= -7d ORDER BY updated DESC` |
| Created this month | `project = PROJ AND created >= startOfMonth()` |
| Words in title or text | `project = PROJ AND text ~ "refund overview"` |
| One issue type | `project = PROJ AND issuetype = Bug` |
| Children of an epic | `parent = PROJ-123` |
| Unassigned | `project = PROJ AND assignee is EMPTY` |
| Label | `project = PROJ AND labels = "my-label"` |
| In a sprint | `project = PROJ AND sprint in openSprints()` |
| Linked to a ticket | `issue in linkedIssues(PROJ-123)` |

Notes:
- Quote values with spaces: `status = "In Progress"`. Status names differ per project; prefer
  `statusCategory` (`"To Do"`, `"In Progress"`, `Done`) for portable queries.
- `ORDER BY` goes last. Dates accept `-7d`, `-2w`, `startOfWeek()`, or `"2026-01-31"`.
- `text ~` searches summary, description and comments; it is word-based and may lag recent edits.
- For "does this already exist" questions, use `jira-agent dedupe` or `kb context` first; they
  search the local knowledge base and cost far fewer tokens than a live search.
