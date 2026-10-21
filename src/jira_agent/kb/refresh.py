import re
from pathlib import Path

from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, WriteMode
from jira_agent.kb.store import DB_NAME, KbStore
from jira_agent.kb.sync import (
    KB_FIELDS,
    SyncStats,
    apply_upsert,
    fetch_tickets,
    kb_ticket,
    key_project,
    remove_rows,
)

KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
BATCH = 50


def normalize_keys(keys: list[str]) -> list[str]:
    cleaned = []
    for key in keys:
        key = key.strip().upper()
        if not KEY.match(key):
            raise ValueError(f"invalid issue key: {key!r}")
        if key not in cleaned:
            cleaned.append(key)
    return cleaned


def fetch_direct(api: IssueAPI, key: str) -> dict | None:
    """One ticket by key. Unlike search, a read straight after a write always sees the write."""
    try:
        raw = api.client.get(f"issue/{key}", fields=",".join(KB_FIELDS), expand="changelog")
    except JiraError as e:
        if e.status == 404:
            return None
        raise
    return kb_ticket(raw) if raw else None


def refresh_keys(
    api: IssueAPI,
    kb: KbStore,
    keys: list[str],
    site_url: str,
    drop_missing: bool = False,
    direct: bool = False,
) -> dict:
    """Re-fetch specific tickets without touching the project cursor.

    Search is cheap for many keys (one request per 50) but Jira's search index lags a few
    seconds behind writes, so keys it does not return are read directly before being called
    missing. `direct=True` skips search, for tickets that were just written.
    """
    wanted = normalize_keys(keys)
    stats = SyncStats()
    returned: set[str] = set()
    moved: list[dict] = []

    def store(t: dict) -> None:
        result = apply_upsert(kb, t, site_url, stats)
        returned.add(t["key"])
        if result.old_key:
            returned.add(result.old_key)
            moved.append({"from": result.old_key, "to": t["key"]})

    if not direct:
        for start in range(0, len(wanted), BATCH):
            quoted = ",".join(f'"{k}"' for k in wanted[start : start + BATCH])
            for t in fetch_tickets(api, f"key in ({quoted}) ORDER BY key ASC"):
                store(t)
    for key in [k for k in wanted if k not in returned]:
        if (t := fetch_direct(api, key)) is not None:
            store(t)
            returned.add(key)
    missing = [k for k in wanted if k not in returned]
    removed = 0
    if drop_missing and missing:
        rows = [{"jira_id": None, "key": k} for k in missing]
        removed = remove_rows(kb, rows)
    kb.commit()
    return {
        "requested": wanted,
        "new": stats.new,
        "updated": stats.updated,
        "unchanged": stats.unchanged,
        "moved": moved,
        "missing": missing,
        "removed": removed,
        "projects": sorted({key_project(k) for k in wanted}),
    }


def refresh_after_write(profile: Profile, secrets, http, keys: list[str]) -> str | None:
    """Best-effort KB refresh after a Jira write; returns a warning instead of raising."""
    if not (Path(profile.kb_path).expanduser() / DB_NAME).exists():
        return None
    try:
        wanted = normalize_keys(keys)
        with KbStore(profile.kb_path) as kb:
            kb.bind_site(profile.site_url)
            if not any(kb.get_meta(f"synced_at:{key_project(k)}") for k in wanted):
                return None
            api = IssueAPI(
                JiraClient(profile.site_url, secrets, http), WriteGate(WriteMode.DRY_RUN)
            )
            result = refresh_keys(api, kb, wanted, profile.site_url, direct=True)
            if result["missing"]:
                return (
                    f"knowledge base could not find {', '.join(result['missing'])} yet; "
                    f"run `jira-agent kb refresh {' '.join(result['missing'])}`"
                )
    except Exception as e:  # noqa: BLE001 - the write already succeeded; never fail it here
        return f"knowledge base not refreshed ({e}); run `jira-agent kb refresh {' '.join(keys)}`"
    return None
