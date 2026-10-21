import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from jira_agent.client.adf import from_adf
from jira_agent.client.issues import SUMMARY_FIELDS, IssueAPI, normalize_issue
from jira_agent.kb import render
from jira_agent.kb.store import KbStore
from jira_agent.kb.summarize import write_project_summary

KB_FIELDS = [*SUMMARY_FIELDS, "comment", "resolution", "resolutiondate"]
OVERLAP = timedelta(minutes=2)
COMMIT_EVERY = 100
MAX_HISTORY = 30
MIN_ROWS_FOR_GUARD = 20
MAX_REMOVAL_SHARE = 0.5
FIELDS_VERSION = hashlib.sha1(json.dumps([KB_FIELDS, "changelog", 3]).encode()).hexdigest()[:12]
_JIRA_TS = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(_JIRA_TS.sub(r"\1:\2", value))


def jql_stamp(value: str) -> str:
    return (parse_ts(value) - OVERLAP).strftime("%Y-%m-%d %H:%M")


def kb_ticket(raw: dict) -> dict:
    t = normalize_issue(raw)
    t["id"] = raw.get("id")
    t["project"] = t["key"].rsplit("-", 1)[0]
    f = raw.get("fields", {})
    t["resolution"] = (f.get("resolution") or {}).get("name")
    t["comments"] = [
        {
            "author": (c.get("author") or {}).get("displayName"),
            "created": c.get("created"),
            "body": from_adf(c.get("body")),
        }
        for c in (f.get("comment") or {}).get("comments", [])
    ]
    history = []
    for h in (raw.get("changelog") or {}).get("histories", []):
        for item in h.get("items", []):
            if item.get("field") == "status":
                history.append(
                    {
                        "date": h.get("created"),
                        "from": item.get("fromString"),
                        "to": item.get("toString"),
                    }
                )
    t["history"] = sorted(history, key=lambda x: x["date"] or "")[-MAX_HISTORY:]
    return t


def build_jql(project: str, since: str | None) -> str:
    clause = f'project = "{project}"'
    if since:
        clause += f' AND updated >= "{jql_stamp(since)}"'
    return f"{clause} ORDER BY updated ASC"


@dataclass
class SyncStats:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    files_written: int = 0
    files_unchanged: int = 0

    def count(self, status: str) -> None:
        setattr(self, status, getattr(self, status) + 1)


def key_project(key: str) -> str:
    return key.rsplit("-", 1)[0]


def fetch_tickets(api: IssueAPI, jql: str) -> Iterator[dict]:
    for raw in api.search(jql, fields=KB_FIELDS, expand="changelog"):
        yield kb_ticket(raw)


def apply_upsert(kb: KbStore, t: dict, site_url: str, stats: SyncStats):
    """Store one ticket and keep its markdown file in step (renames, displaced keys)."""
    result = kb.upsert(t)
    stats.count(result.status)
    for stale_key in (result.old_key, result.displaced_key):
        if stale_key:
            render.remove_ticket(kb.root, key_project(stale_key), stale_key)
    if render.write_ticket(kb.root, t, site_url):
        stats.files_written += 1
    else:
        stats.files_unchanged += 1
    return result


def removal_guard(total: int, removals: int) -> str | None:
    if total >= MIN_ROWS_FOR_GUARD and removals > total * MAX_REMOVAL_SHARE:
        return (
            f"refusing to remove {removals} of {total} tickets at once; this usually means the "
            "search returned too little (permissions or an outage), not a mass deletion"
        )
    return None


def remove_rows(kb: KbStore, rows: list[dict]) -> int:
    removed = 0
    for row in rows:
        key = kb.remove(jira_id=row["jira_id"]) if row["jira_id"] else kb.remove(key=row["key"])
        if key:
            render.remove_ticket(kb.root, key_project(key), key)
            removed += 1
    return removed


def client_counters(api: IssueAPI) -> dict:
    return dict(getattr(api.client, "stats", {}))


def counter_delta(api: IssueAPI, before: dict) -> dict:
    after = client_counters(api)
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    return {
        "requests": delta.get("requests", 0),
        "retries": delta.get("retries", 0),
        "rate_limited": delta.get("rate_limited", 0),
        "megabytes": round(delta.get("bytes", 0) / 1_000_000, 2),
    }


def needs_full(kb: KbStore, project: str) -> str | None:
    if kb.legacy_count(project):
        return "the knowledge base format was upgraded; a full re-sync fills in Jira ids"
    stored = kb.get_meta(f"fields_version:{project}")
    if stored is not None and stored != FIELDS_VERSION:
        return "the set of fetched fields changed; a full re-sync is needed"
    return None


def sync_project(
    api: IssueAPI,
    kb: KbStore,
    project: str,
    site_url: str,
    full: bool = False,
    prune: bool = True,
    progress: Callable[[str], None] = lambda m: None,
) -> dict:
    started = time.monotonic()
    counters = client_counters(api)
    cursor_key = f"cursor:{project}"
    forced = needs_full(kb, project)
    cursor = None if (full or forced) else kb.get_meta(cursor_key)
    mode = "incremental" if cursor else "full"
    stats = SyncStats()
    warnings: list[str] = []
    if forced and not full:
        warnings.append(f"full sync: {forced}")
    local_before = kb.rows(project)
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    newest = cursor
    fetched = 0

    for t in fetch_tickets(api, build_jql(project, cursor)):
        apply_upsert(kb, t, site_url, stats)
        if t.get("id"):
            seen_ids.add(str(t["id"]))
        seen_keys.add(t["key"])
        updated = t.get("updated")
        if updated and (newest is None or parse_ts(updated) > parse_ts(newest)):
            newest = updated
        fetched += 1
        if fetched % COMMIT_EVERY == 0:
            kb.commit()
            if newest:
                kb.set_meta(cursor_key, newest)
            progress(f"{project}: {fetched} tickets")

    if prune and mode == "full":
        stale = [
            r
            for r in kb.rows(project)
            if (str(r["jira_id"]) not in seen_ids if r["jira_id"] else r["key"] not in seen_keys)
        ]
        problem = removal_guard(len(local_before), len(stale))
        if problem:
            warnings.append(f"prune skipped: {problem}")
        else:
            stats.removed = remove_rows(kb, stale)
            kb.set_meta(f"reconciled_at:{project}", str(int(time.time())))
    kb.commit()

    if newest:
        kb.set_meta(cursor_key, newest)
    kb.set_meta(f"synced_at:{project}", str(int(time.time())))
    kb.set_meta(f"fields_version:{project}", FIELDS_VERSION)
    write_project_summary(kb, project)
    result = {
        "project": project,
        "mode": mode,
        "fetched": fetched,
        "new": stats.new,
        "updated": stats.updated,
        "unchanged": stats.unchanged,
        "removed": stats.removed,
        "total": kb.count(project),
        "files_written": stats.files_written,
        "files_unchanged": stats.files_unchanged,
        "seconds": round(time.monotonic() - started, 2),
        **counter_delta(api, counters),
        "warnings": warnings,
    }
    kb.set_meta(f"last_sync:{project}", json.dumps({**result, "at": int(time.time())}))
    return result
