import json
import time
from collections.abc import Callable

from jira_agent.client.issues import IssueAPI
from jira_agent.kb.store import KbError, KbStore
from jira_agent.kb.summarize import write_project_summary
from jira_agent.kb.sync import (
    SyncStats,
    apply_upsert,
    client_counters,
    counter_delta,
    fetch_tickets,
    removal_guard,
    remove_rows,
)

FETCH_BATCH = 100
SAMPLE = 20


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def remote_index(api: IssueAPI, project: str) -> dict[str, tuple[str, str | None]]:
    """Jira id -> (key, updated) for every ticket in the project, fetched as cheaply as possible."""
    index = {}
    for raw in api.search(f'project = "{project}" ORDER BY created ASC', fields=["updated"]):
        index[str(raw["id"])] = (raw["key"], (raw.get("fields") or {}).get("updated"))
    return index


def plan(local: list[dict], remote: dict[str, tuple[str, str | None]]) -> dict:
    remote_keys = {key for key, _ in remote.values()}
    by_id = {str(r["jira_id"]): r for r in local if r["jira_id"]}
    legacy_by_key = {r["key"]: r for r in local if not r["jira_id"]}

    remove = [
        r
        for r in local
        if (str(r["jira_id"]) not in remote if r["jira_id"] else r["key"] not in remote_keys)
    ]
    added, changed = [], []
    for jira_id, (key, updated) in remote.items():
        row = by_id.get(jira_id) or legacy_by_key.get(key)
        if row is None:
            added.append(jira_id)
        elif row["updated"] != updated or row["key"] != key or not row["jira_id"]:
            changed.append(jira_id)
    return {"remove": remove, "added": added, "changed": changed}


def reconcile_project(
    api: IssueAPI,
    kb: KbStore,
    project: str,
    site_url: str,
    force: bool = False,
    dry_run: bool = False,
    progress: Callable[[str], None] = lambda m: None,
) -> dict:
    started = time.monotonic()
    counters = client_counters(api)
    local = kb.rows(project)
    remote = remote_index(api, project)
    progress(f"{project}: {len(remote)} tickets in Jira, {len(local)} in the knowledge base")
    found = plan(local, remote)

    problem = removal_guard(len(local), len(found["remove"]))
    if problem and not force:
        raise KbError(f"{problem}. Re-run with --force if the removals are real.")

    summary = {
        "project": project,
        "jira_total": len(remote),
        "kb_total": len(local),
        "added": len(found["added"]),
        "changed": len(found["changed"]),
        "removed": len(found["remove"]),
        "remove_keys": [r["key"] for r in found["remove"]][:SAMPLE],
    }
    if dry_run:
        return {**summary, "dry_run": True, **counter_delta(api, counters)}

    stats = SyncStats()
    todo = found["added"] + found["changed"]
    for batch in _chunks(todo, FETCH_BATCH):
        ids = ",".join(i for i in batch if i.isdigit())
        for t in fetch_tickets(api, f"id in ({ids}) ORDER BY id ASC"):
            apply_upsert(kb, t, site_url, stats)
    stats.removed = remove_rows(kb, found["remove"])
    kb.commit()
    write_project_summary(kb, project)
    result = {
        **summary,
        "removed": stats.removed,
        "files_written": stats.files_written,
        "files_unchanged": stats.files_unchanged,
        "total": kb.count(project),
        "seconds": round(time.monotonic() - started, 2),
        **counter_delta(api, counters),
    }
    kb.set_meta(f"last_reconcile:{project}", json.dumps({**result, "at": int(time.time())}))
    kb.set_meta(f"reconciled_at:{project}", str(int(time.time())))
    return result
