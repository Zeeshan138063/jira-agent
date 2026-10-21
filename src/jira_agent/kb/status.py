import json
import time

from jira_agent.config import Profile, SyncPolicy
from jira_agent.kb.store import KbStore
from jira_agent.kb.sync import needs_full

DAY = 24 * 3600


RECONCILE_DAYS = 7


def _json_meta(kb: KbStore, key: str) -> dict | None:
    raw = kb.get_meta(key)
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def project_status(kb: KbStore, project: str, policy: SyncPolicy, now: float | None = None) -> dict:
    now = now or time.time()
    raw = kb.get_meta(f"synced_at:{project}")
    synced_at = int(raw) if raw else None
    forced = needs_full(kb, project) if synced_at else None
    if synced_at is None:
        action, reason = "backfill", "never synced"
    elif forced:
        action, reason = "sync", forced
    elif policy == SyncPolicy.ON_START:
        action, reason = "sync", "sync_policy is on_start"
    elif policy == SyncPolicy.DAILY and now - synced_at > DAY:
        action, reason = "sync", "last sync is over 24h old"
    else:
        action, reason = "none", "up to date for sync_policy"
    reconciled = kb.get_meta(f"reconciled_at:{project}")
    reconciled_at = int(reconciled) if reconciled else None
    return {
        "project": project,
        "tickets": kb.count(project),
        "synced_at": synced_at,
        "age_hours": round((now - synced_at) / 3600, 1) if synced_at else None,
        "recommended_action": action,
        "reason": reason,
        "legacy_rows": kb.legacy_count(project),
        "reconciled_at": reconciled_at,
        "reconcile_due": bool(synced_at)
        and (reconciled_at is None or now - reconciled_at > RECONCILE_DAYS * DAY),
        "last_sync": _json_meta(kb, f"last_sync:{project}"),
        "last_reconcile": _json_meta(kb, f"last_reconcile:{project}"),
    }


def kb_status(kb: KbStore, profile: Profile) -> dict:
    return {
        "kb_path": str(kb.root),
        "site_url": kb.get_meta("site_url"),
        "sync_policy": profile.sync_policy.value,
        "projects": [project_status(kb, p, profile.sync_policy) for p in profile.projects],
    }
