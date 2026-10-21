import json
import time

import httpx
import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.client.adf import to_adf
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient
from jira_agent.client.issues import IssueAPI
from jira_agent.config import SyncPolicy, WriteMode
from jira_agent.kb.render import ticket_path
from jira_agent.kb.status import project_status
from jira_agent.kb.store import KbError, KbStore, fts_query
from jira_agent.kb.sync import build_jql, jql_stamp, kb_ticket, sync_project

SITE = "https://acme.atlassian.net"
SEARCH = f"{oauth.API_BASE}/cloud-1/rest/api/3/search/jql"


@pytest.fixture
def api():
    s = MemoryStore()
    oauth.save_client(s, "cid", "csecret", 0)
    s.set(
        token_key(SITE),
        {
            "access_token": "tok",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )
    return IssueAPI(JiraClient(SITE, s, sleep=lambda x: None), WriteGate(WriteMode.DRY_RUN))


@pytest.fixture
def kb(tmp_path):
    with KbStore(tmp_path / "kb") as store:
        yield store


def raw_issue(key, summary="Fix login", updated="2026-10-01T10:00:00.000+0500", **fields):
    return {
        "id": str(10000 + int(key.rsplit("-", 1)[1])),
        "key": key,
        "fields": {
            "summary": summary,
            "issuetype": {"name": "Bug"},
            "status": {"name": "Done"},
            "assignee": {"displayName": "Pat"},
            "labels": ["auth"],
            "components": [{"name": "API"}],
            "description": to_adf("Users cannot log in.\n\nAcceptance criteria:\n- works"),
            "updated": updated,
            "created": "2026-09-01T10:00:00.000+0500",
            "comment": {
                "comments": [{"author": {"displayName": "Q"}, "body": to_adf("seen on iOS")}]
            },
            **fields,
        },
        "changelog": {
            "histories": [
                {
                    "created": "2026-09-02T10:00:00.000+0500",
                    "items": [
                        {"field": "status", "fromString": "To Do", "toString": "Done"},
                        {"field": "assignee", "fromString": None, "toString": "Pat"},
                    ],
                }
            ]
        },
    }


def test_kb_ticket_extracts_comments_and_status_history():
    t = kb_ticket(raw_issue("PROJ-1"))
    assert t["project"] == "PROJ"
    assert t["comments"][0]["body"] == "seen on iOS"
    assert t["history"] == [{"date": "2026-09-02T10:00:00.000+0500", "from": "To Do", "to": "Done"}]


def test_jql_stamp_keeps_jira_offset_and_overlaps():
    assert jql_stamp("2026-10-01T10:00:00.000+0500") == "2026-10-01 09:58"
    assert build_jql("PROJ", None) == 'project = "PROJ" ORDER BY updated ASC'
    assert 'updated >= "2026-10-01 09:58"' in build_jql("PROJ", "2026-10-01T10:00:00.000+0500")


def test_fts_query_is_injection_safe():
    assert fts_query('login" OR key:* NEAR(', "all") == '"login" AND "or" AND "key" AND "near"'
    assert fts_query("!!", "any") is None


def test_upsert_search_update_and_delete(kb):
    kb.upsert(kb_ticket(raw_issue("PROJ-1", "Login fails on iOS")))
    kb.upsert(kb_ticket(raw_issue("PROJ-2", "Invoice export timeout", description=None)))
    kb.commit()
    assert [h["key"] for h in kb.search("login ios")][:1] == ["PROJ-1"]
    assert kb.search("logins")[0]["key"] == "PROJ-1"
    changed = kb_ticket(raw_issue("PROJ-1", "Payment refund broken"))
    kb.upsert(changed)
    assert kb.search("payment")[0]["key"] == "PROJ-1"
    assert kb.search("payment", project="PROJ")[0]["summary"] == "Payment refund broken"
    assert kb.count() == 2
    kb.delete("PROJ-2")
    assert kb.get("PROJ-2") is None and kb.search("invoice") == []


def test_search_falls_back_to_any_term(kb):
    kb.upsert(kb_ticket(raw_issue("PROJ-1", "Login fails")))
    assert kb.search("login nonexistentword")[0]["key"] == "PROJ-1"


def test_search_filters_by_project(kb):
    kb.upsert(kb_ticket(raw_issue("PROJ-1", "Login fails")))
    kb.upsert(kb_ticket(raw_issue("OPS-1", "Login fails")))
    assert [h["key"] for h in kb.search("login", project="OPS")] == ["OPS-1"]


def test_kb_refuses_other_site(kb):
    kb.bind_site(SITE)
    kb.bind_site(SITE)
    with pytest.raises(KbError, match="belongs to"):
        kb.bind_site("https://other.atlassian.net")


@respx.mock
def test_full_sync_writes_markdown_summary_and_cursor(api, kb):
    route = respx.post(SEARCH).respond(
        json={
            "issues": [
                raw_issue("PROJ-1", updated="2026-10-01T10:00:00.000+0500"),
                raw_issue("PROJ-2", "Other", updated="2026-10-03T12:30:00.000+0500"),
            ],
            "isLast": True,
        }
    )
    result = sync_project(api, kb, "PROJ", SITE)
    assert {k: result[k] for k in ("project", "mode", "fetched", "new", "removed", "total")} == {
        "project": "PROJ",
        "mode": "full",
        "fetched": 2,
        "new": 2,
        "removed": 0,
        "total": 2,
    }
    assert result["files_written"] == 2 and result["requests"] == 1 and result["warnings"] == []
    sent = json.loads(route.calls[0].request.content)
    assert sent["expand"] == "changelog" and "comment" in sent["fields"]
    md = ticket_path(kb.root, "PROJ", "PROJ-1").read_text()
    assert "# PROJ-1: Fix login" in md and "seen on iOS" in md and "To Do -> Done" in md
    assert f"{SITE}/browse/PROJ-1" in md
    assert "Tickets: 2" in (kb.root / "projects" / "PROJ.md").read_text()
    assert kb.get_meta("cursor:PROJ") == "2026-10-03T12:30:00.000+0500"


@respx.mock
def test_incremental_sync_uses_cursor_and_is_idempotent(api, kb):
    kb.set_meta("cursor:PROJ", "2026-10-03T12:30:00.000+0500")
    route = respx.post(SEARCH).respond(
        json={
            "issues": [raw_issue("PROJ-2", updated="2026-10-04T08:00:00.000+0500")],
            "isLast": True,
        }
    )
    first = sync_project(api, kb, "PROJ", SITE)
    again = sync_project(api, kb, "PROJ", SITE)
    assert first["mode"] == "incremental" and first["new"] == 1 and again["new"] == 0
    assert 'updated >= "2026-10-04 07:58"' in json.loads(route.calls[1].request.content)["jql"]
    assert kb.count("PROJ") == 1


@respx.mock
def test_full_sync_prune_removes_deleted_tickets(api, kb):
    respx.post(SEARCH).mock(
        side_effect=[
            httpx.Response(
                200, json={"issues": [raw_issue("PROJ-1"), raw_issue("PROJ-2")], "isLast": True}
            ),
            httpx.Response(200, json={"issues": [raw_issue("PROJ-1")], "isLast": True}),
        ]
    )
    sync_project(api, kb, "PROJ", SITE)
    result = sync_project(api, kb, "PROJ", SITE, full=True)
    assert result["removed"] == 1 and kb.get("PROJ-2") is None
    assert not ticket_path(kb.root, "PROJ", "PROJ-2").exists()


@respx.mock
def test_prune_is_ignored_for_incremental(api, kb):
    kb.upsert(kb_ticket(raw_issue("PROJ-9")))
    kb.set_meta("cursor:PROJ", "2026-10-03T12:30:00.000+0500")
    respx.post(SEARCH).respond(json={"issues": [], "isLast": True})
    assert sync_project(api, kb, "PROJ", SITE)["removed"] == 0
    assert kb.get("PROJ-9")


def test_project_status_by_policy(kb):
    now = 1_000_000.0
    assert project_status(kb, "PROJ", SyncPolicy.MANUAL, now)["recommended_action"] == "backfill"
    kb.set_meta("synced_at:PROJ", str(int(now - 3600)))
    assert project_status(kb, "PROJ", SyncPolicy.MANUAL, now)["recommended_action"] == "none"
    assert project_status(kb, "PROJ", SyncPolicy.ON_START, now)["recommended_action"] == "sync"
    assert project_status(kb, "PROJ", SyncPolicy.DAILY, now)["recommended_action"] == "none"
    assert (
        project_status(kb, "PROJ", SyncPolicy.DAILY, now + 90_000)["recommended_action"] == "sync"
    )
