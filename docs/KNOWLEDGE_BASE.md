# Knowledge base design

How the ticket knowledge base stays correct and cheap as projects grow.

## Layout

```
<kb_path>/
  index.db                      SQLite: tickets table + FTS5 full-text index + meta
  tickets/<PROJECT>/<KEY>.md    one markdown file per ticket
  projects/<PROJECT>.md         generated project profile
```

## Identity: Jira's numeric id, not the key

Every ticket is stored under its permanent numeric Jira id; the key (`PROJ-123`) is just the current
name. This matters because keys change:

- A ticket moved or renamed keeps one row; its old markdown file is deleted and the new one written.
- A reused key (it can happen when issues are moved or keys are reassigned) replaces the stale
  ticket instead of merging two unrelated tickets into one entry.
- Older indexes without ids are upgraded in place; the next sync is one full pass that fills in the
  ids (about 10 s per 600 tickets).

## Keeping it current

| Operation | Cost | What it does |
|---|---|---|
| `kb sync` (incremental) | 1 request when nothing changed (~1 s) | Fetches tickets updated since the stored cursor, re-reading a 2-minute overlap |
| `kb sync` (first, `--full`, or after a format/field change) | ~1 request per 100 tickets | Fetches everything, then removes tickets that no longer exist |
| `kb refresh KEY...` | 1 request per 50 tickets | Re-fetches specific tickets; the cursor is untouched |
| `kb reconcile` | ~1 request per 100 tickets, ~25 KB per 100 | Compares every id and last-updated time with Jira, fetches only what differs, removes what is gone or moved away |
| Auto-refresh after writes | 1 request | Refreshes just the tickets a confirmed write touched |
| Weekly maintenance | as reconcile | `kb sync` runs a reconcile after an incremental sync if none ran in 7 days |

Unchanged tickets cost no index or disk writes: a ticket whose stored data is identical is skipped,
and a markdown file is only rewritten when its content differs.

Safety: a sync or reconcile that would remove over half of a project (20+ tickets) refuses and says
why, because that pattern means a permissions problem or an outage rather than a mass deletion.
`kb reconcile --force` overrides it.

## Measured on a real project (607 tickets, 2026-10)

| Measure | Result |
|---|---|
| Full fetch with comments and status history | 7 requests, 10.3 s, 16.5 MB |
| Incremental sync, nothing new | 1 request, 1.3 s |
| Reconcile of all 607 tickets | 7 requests, 0.15 MB (about 100x lighter than a full fetch) |
| Page of 100 tickets: full / without history / ids and timestamps only | 1.6 s, 2.0 MB / 0.5 s, 0.7 MB / 0.4 s, 24 KB |
| Index size | 4.6 MB (about 7.5 KB per ticket) |
| Files rewritten on a repeated full sync | 0 of 607 |

Extrapolated linearly: 10,000 tickets is roughly 2.7 minutes and 200 MB of transfer for a first
backfill, and a 75 MB index. A cheap reconcile of 10,000 tickets is about 40 seconds and 2.5 MB.

## Reading it cheaply: cards, epic digests, context packs

The agent's context window is the scarce resource, so retrieval goes from small to large and is
computed locally (no Jira calls, no AI summarising):

| Layer | Command | What you get |
|---|---|---|
| Ticket card | `kb card KEY` | key, type, status, done flag, 90-char summary, epic, first readable words of the description (headings and ALL-CAPS section labels dropped) |
| Epic digest | `kb epic KEY` | goal, children and done counts, status mix, common labels and people, open-first child cards; computed from the children, so never stale |
| Context pack | `kb context --summary ...` | duplicate verdict, related cards, suggested parent with the reason, relevant epic digests, well-groomed examples of the same type, project conventions |
| Full ticket | `kb show KEY` | everything, only when needed |

Context packs fit a token budget (`--budget`, default 2500, estimated as characters / 4) by stepping
through seven levels of detail: fewer related tickets, fewer and shorter epic digests, fewer
examples, shorter text, then no convention detail. The pack records what was trimmed and whether
even the leanest level was over budget.

How the pack is chosen: candidates come from an any-word full-text search with common words removed,
then are ranked by word-overlap similarity (the same measure the duplicate check uses). Epics are
ranked by the similarity of their own text plus the similarity of related tickets that sit under
them. A parent is suggested only when that score clears a threshold. Examples are same-type tickets
that score 70+ on the grooming checklist, excluding near-duplicates of the draft; if too few
related tickets qualify, the best-groomed recent tickets of that type fill in.

Measured on the same 607-ticket project: 0.23 s end to end including Python start-up; a default pack is
about 1,700 to 2,300 tokens and trims to roughly 250 at the minimum; 3,000 synthetic tickets stay
under 3 s in the test suite.

"Done" counts use Jira's status category as well as the resolution field: some workflows never
set a resolution, so resolution alone showed finished epics as 0% complete.

## Project habits (for grooming)

Project statistics now include `habits`: for each issue type with enough tickets, the percentage that
have a real description, acceptance criteria, scope boundaries, reproduction wording, a requirements
list, a stated goal, and any way to tell it is done. The grooming checklist scales each check's weight
by these numbers (see the jira-groom skill). They are cached in the index (`stats:<PROJECT>`) and
recomputed on every sync or `kb summary`; cached statistics from an older version are recomputed
automatically. Example from one real project: Stories 48% acceptance criteria, Tasks 6%, Bugs 0%, Epics 1% but
74% with a goal and a requirements list.

## Observability

`kb sync` and `kb reconcile` report seconds, requests, retries, rate-limit hits, megabytes, and
counts of new / updated / unchanged / removed tickets and files written. The latest of each is kept
in `kb status` (`last_sync`, `last_reconcile`), along with `legacy_rows` (should be 0),
`reconciled_at` and `reconcile_due`.

## Known limits

- Bulk fetches return status history and comments that matched Jira's per-ticket data on the
  tickets checked (up to 40 history entries). Tickets with far longer histories were not checked.
- Sync is sequential: Jira's search pages by token, so pages cannot be fetched in parallel. This only
  matters for projects far beyond 50,000 tickets.
- Search is word-based (SQLite FTS5). Meaning-based matching and shared team snapshots are
  separate, later phases.
- The knowledge base holds a copy of ticket text. Treat it as sensitive and see `SECURITY.md`.

## Decisions (0.10)

- **Per person.** Each person syncs their own copy. Nothing is shared between people.
- **Visibility.** The copy contains what Jira shows that person; there is no separate exclusion list.
- **Size.** Designed for up to about 10,000 tickets per project. Synthetic check (index only, no
  markdown rendering or network): 10,000 tickets insert in under a second, 36 MB on disk, a
  duplicate check takes about 15 ms, word weights build in 0.3 s and are cached afterwards.
- **Embeddings.** Decided after the duplicate evaluation set has enough real pairs; see DUPLICATES.md.

