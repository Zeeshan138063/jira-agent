import json
import sqlite3
import time

import httpx
import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.cli import run
from jira_agent.client.adf import to_adf
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, ProfileStore, SyncPolicy, WriteMode
from jira_agent.kb import render
from jira_agent.kb.reconcile import plan, reconcile_project
from jira_agent.kb.refresh import normalize_keys, refresh_after_write, refresh_keys
from jira_agent.kb.render import ticket_path
from jira_agent.kb.status import project_status
from jira_agent.kb.store import KbError, KbStore
from jira_agent.kb.sync import FIELDS_VERSION, sync_project

SITE = "https://acme.atlassian.net"
BASE = f"{oauth.API_BASE}/cloud-1/rest/api/3"
SEARCH = f"{BASE}/search/jql"


def make_secrets():
    s = MemoryStore()
    s.set(
        token_key(SITE),
        {
            "access_token": "t",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )
    return s


@pytest.fixture
def api():
    client = JiraClient(SITE, make_secrets(), sleep=lambda s: None)
    return IssueAPI(client, WriteGate(WriteMode.DRY_RUN))


@pytest.fixture
def kb(tmp_path):
    with KbStore(tmp_path / "kb") as store:
        yield store


def tk(jira_id, key, summary="Some ticket", updated="2026-10-01T10:00:00.000+0500", **kw):
    return {
        "id": jira_id,
        "key": key,
        "project": key.rsplit("-", 1)[0],
        "summary": summary,
        "description": "body",
        "type": "Task",
        "labels": [],
        "components": [],
        "comments": [],
        "history": [],
        "updated": updated,
        **kw,
    }


def issue(jira_id, key, summary="Some ticket", updated="2026-10-01T10:00:00.000+0500"):
    return {
        "id": jira_id,
        "key": key,
        "fields": {
            "summary": summary,
            "issuetype": {"name": "Task"},
            "description": to_adf("body"),
            "updated": updated,
        },
    }


def index_issue(jira_id, key, updated="2026-10-01T10:00:00.000+0500"):
    return {"id": jira_id, "key": key, "fields": {"updated": updated}}


def route_search(mock, handler):
    def respond(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"issues": handler(body["jql"], body), "isLast": True})

    return mock.post(SEARCH).mock(side_effect=respond)


def sync_state(kb, project="PROJ"):
    kb.set_meta(f"cursor:{project}", "2026-10-01T10:00:00.000+0500")
    kb.set_meta(f"fields_version:{project}", FIELDS_VERSION)
    kb.set_meta(f"synced_at:{project}", str(int(time.time())))


# --- store: ids, renames, reuse, legacy rows, migration ---------------------------------------


def test_upsert_reports_new_unchanged_updated(kb):
    assert kb.upsert(tk("1", "PROJ-1")).status == "new"
    assert kb.upsert(tk("1", "PROJ-1")).status == "unchanged"
    assert kb.upsert(tk("1", "PROJ-1", summary="Changed")).status == "updated"
    assert kb.count() == 1


def test_a_moved_or_renamed_ticket_keeps_one_row_and_reports_the_old_key(kb):
    kb.upsert(tk("1", "PROJ-5", summary="Quartz scheduler"))
    result = kb.upsert(tk("1", "OPS-9", summary="Quartz scheduler"))
    assert (result.status, result.old_key) == ("updated", "PROJ-5")
    assert kb.count() == 1 and kb.get("PROJ-5") is None and kb.get("OPS-9")["id"] == "1"
    assert [h["key"] for h in kb.search("quartz")] == ["OPS-9"]


def test_key_reuse_replaces_the_stale_row_instead_of_merging(kb):
    kb.upsert(tk("1", "PROJ-178", summary="Original auto-refund"))
    result = kb.upsert(tk("2", "PROJ-178", summary="Unrelated ticket"))
    assert result.status == "new" and result.displaced_key == "PROJ-178"
    assert kb.count() == 1 and kb.get("PROJ-178")["id"] == "2"
    assert kb.search("auto refund") == []


def test_legacy_rows_without_an_id_are_adopted_by_key(kb):
    kb.upsert(tk(None, "PROJ-1"))
    assert kb.legacy_count() == 1
    result = kb.upsert(tk("77", "PROJ-1"))
    assert result.status == "updated" and kb.count() == 1 and kb.legacy_count() == 0
    assert kb.rows("PROJ")[0]["jira_id"] == "77"


def test_an_upsert_without_an_id_does_not_wipe_a_stored_id(kb):
    kb.upsert(tk("5", "PROJ-1"))
    kb.upsert(tk(None, "PROJ-1", summary="edited"))
    assert kb.rows("PROJ")[0]["jira_id"] == "5"


def test_remove_by_id_or_key_and_rows_listing(kb):
    kb.upsert(tk("1", "PROJ-1"))
    kb.upsert(tk("2", "PROJ-2"))
    assert kb.remove(jira_id="1") == "PROJ-1" and kb.remove(key="PROJ-2") == "PROJ-2"
    assert kb.remove(key="PROJ-9") is None and kb.rows("PROJ") == []
    assert kb.search("ticket") == []


def test_a_v1_index_is_migrated_in_place(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    old = sqlite3.connect(root / "index.db")
    old.executescript(
        """
        CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
          project TEXT NOT NULL, summary TEXT, type TEXT, status TEXT, priority TEXT,
          assignee TEXT, reporter TEXT, parent TEXT, labels TEXT, components TEXT,
          description TEXT, created TEXT, updated TEXT, data TEXT NOT NULL);
        CREATE VIRTUAL TABLE tickets_fts USING fts5(key UNINDEXED, summary, description,
          comments, labels, components, tokenize = 'porter unicode61');
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO tickets(key, project, summary, data) VALUES
          ('PROJ-1', 'PROJ', 'Legacy login ticket', '{"key": "PROJ-1", "project": "PROJ"}');
        INSERT INTO tickets_fts(rowid, key, summary) VALUES (1, 'PROJ-1', 'Legacy login ticket');
        INSERT INTO meta VALUES ('cursor:PROJ', '2026-10-01T10:00:00.000+0500');
        INSERT INTO meta VALUES ('synced_at:PROJ', '123');
        PRAGMA user_version = 1;
        """
    )
    old.commit()
    old.close()

    with KbStore(root) as kb:
        assert kb.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert kb.legacy_count("PROJ") == 1
        assert kb.get_meta("cursor:PROJ") is None and kb.get_meta("synced_at:PROJ") == "123"
        assert [h["key"] for h in kb.search("legacy")] == ["PROJ-1"]
        assert kb.upsert(tk("9", "PROJ-1", summary="Legacy login ticket")).status == "updated"
        assert kb.legacy_count() == 0


# --- render: skip unchanged files -------------------------------------------------------------


def test_write_ticket_skips_identical_content(tmp_path):
    t = tk("1", "PROJ-1")
    assert render.write_ticket(tmp_path, t, SITE) is True
    assert render.write_ticket(tmp_path, t, SITE) is False
    assert render.write_ticket(tmp_path, {**t, "summary": "new"}, SITE) is True


# --- sync: stats, skipping, forced full, prune guard ------------------------------------------


@respx.mock
def test_repeating_a_full_sync_rewrites_nothing(api, kb):
    issues = [issue(str(i), f"PROJ-{i}") for i in range(1, 6)]
    route_search(respx, lambda jql, body: issues)
    first = sync_project(api, kb, "PROJ", SITE, full=True)
    again = sync_project(api, kb, "PROJ", SITE, full=True)
    assert (first["new"], first["files_written"]) == (5, 5)
    assert (again["new"], again["updated"], again["unchanged"]) == (0, 0, 5)
    assert (again["files_written"], again["files_unchanged"]) == (0, 5)


@respx.mock
def test_sync_reports_request_counters_and_stores_them(api, kb):
    calls = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"issues": [issue("1", "PROJ-1")], "isLast": True}),
        ]
    )
    respx.post(SEARCH).mock(side_effect=lambda request: next(calls))
    result = sync_project(api, kb, "PROJ", SITE)
    assert (result["requests"], result["retries"], result["rate_limited"]) == (2, 1, 1)
    assert result["megabytes"] >= 0 and result["seconds"] >= 0
    stored = json.loads(kb.get_meta("last_sync:PROJ"))
    assert stored["fetched"] == 1 and stored["at"] > 0


@respx.mock
def test_a_ticket_that_changed_key_moves_its_markdown_file(api, kb):
    route_search(respx, lambda jql, body: [issue("1", "PROJ-5", "Scheduler")])
    sync_project(api, kb, "PROJ", SITE, full=True)
    old_path = ticket_path(kb.root, "PROJ", "PROJ-5")
    assert old_path.exists()
    route_search(
        respx,
        lambda jql, body: [issue("1", "PROJ-50", "Scheduler", "2026-10-02T10:00:00.000+0500")],
    )
    result = sync_project(api, kb, "PROJ", SITE)
    assert result["updated"] == 1 and not old_path.exists()
    assert ticket_path(kb.root, "PROJ", "PROJ-50").exists()


@respx.mock
def test_legacy_rows_force_a_full_sync_even_with_a_cursor(api, kb):
    kb.upsert(tk(None, "PROJ-1", summary="Some ticket"))
    kb.set_meta("cursor:PROJ", "2026-10-03T12:30:00.000+0500")
    route = route_search(respx, lambda jql, body: [issue("1", "PROJ-1")])
    result = sync_project(api, kb, "PROJ", SITE)
    assert result["mode"] == "full" and "format was upgraded" in result["warnings"][0]
    assert "updated >=" not in json.loads(route.calls[0].request.content)["jql"]
    assert kb.legacy_count() == 0


@respx.mock
def test_changing_the_fetched_fields_forces_a_full_sync(api, kb):
    kb.upsert(tk("1", "PROJ-1"))
    kb.set_meta("cursor:PROJ", "2026-10-03T12:30:00.000+0500")
    kb.set_meta("fields_version:PROJ", "outdated")
    route_search(respx, lambda jql, body: [issue("1", "PROJ-1")])
    result = sync_project(api, kb, "PROJ", SITE)
    assert result["mode"] == "full" and "fields changed" in result["warnings"][0]
    assert kb.get_meta("fields_version:PROJ") == FIELDS_VERSION


@respx.mock
def test_full_sync_prunes_by_jira_id_so_a_renamed_ticket_is_not_removed(api, kb):
    kb.upsert(tk("1", "PROJ-1"))
    kb.upsert(tk("2", "PROJ-2"))
    route_search(respx, lambda jql, body: [issue("1", "PROJ-10")])
    result = sync_project(api, kb, "PROJ", SITE, full=True)
    assert result["removed"] == 1 and kb.get("PROJ-2") is None
    assert kb.get("PROJ-10")["id"] == "1"


@respx.mock
def test_prune_is_skipped_when_it_would_remove_most_of_the_project(api, kb):
    for i in range(1, 31):
        kb.upsert(tk(str(i), f"PROJ-{i}"))
    route_search(respx, lambda jql, body: [issue(str(i), f"PROJ-{i}") for i in range(1, 6)])
    result = sync_project(api, kb, "PROJ", SITE, full=True)
    assert result["removed"] == 0 and kb.count("PROJ") == 30
    assert "prune skipped" in result["warnings"][0]


@respx.mock
def test_prune_can_be_disabled(api, kb):
    kb.upsert(tk("2", "PROJ-2"))
    route_search(respx, lambda jql, body: [issue("1", "PROJ-1")])
    assert sync_project(api, kb, "PROJ", SITE, full=True, prune=False)["removed"] == 0
    assert kb.get("PROJ-2")


# --- reconcile --------------------------------------------------------------------------------


def row(jira_id, key, updated="2026-10-01T10:00:00.000+0500"):
    return {"jira_id": jira_id, "key": key, "updated": updated}


def test_plan_finds_removed_added_changed_and_legacy():
    local = [row("1", "PROJ-1"), row("2", "PROJ-2"), row("3", "PROJ-3"), row(None, "PROJ-4")]
    remote = {
        "1": ("PROJ-1", "2026-10-01T10:00:00.000+0500"),
        "3": ("PROJ-33", "2026-10-01T10:00:00.000+0500"),
        "4": ("PROJ-4", "2026-10-01T10:00:00.000+0500"),
        "9": ("PROJ-9", "2026-10-01T10:00:00.000+0500"),
    }
    found = plan(local, remote)
    assert [r["key"] for r in found["remove"]] == ["PROJ-2"]
    assert sorted(found["changed"]) == ["3", "4"] and found["added"] == ["9"]


def reconcile_handler(remote, full):
    def handler(jql, body):
        if jql.startswith("id in"):
            wanted = jql[jql.index("(") + 1 : jql.index(")")].split(",")
            return [full[i] for i in wanted]
        return remote

    return handler


@respx.mock
def test_reconcile_removes_deleted_fetches_changed_and_added(api, kb):
    for i, key in (("1", "PROJ-1"), ("2", "PROJ-2"), ("3", "PROJ-3")):
        kb.upsert(tk(i, key, summary=f"Ticket {key}"))
        render.write_ticket(kb.root, kb.get(key), SITE)
    new_time = "2026-10-05T09:00:00.000+0500"
    remote = [
        index_issue("1", "PROJ-1", new_time),
        index_issue("3", "PROJ-3"),
        index_issue("4", "PROJ-4"),
    ]
    full = {
        "1": issue("1", "PROJ-1", "Ticket PROJ-1 edited", new_time),
        "4": issue("4", "PROJ-4", "Brand new"),
    }
    route = route_search(respx, reconcile_handler(remote, full))
    result = reconcile_project(api, kb, "PROJ", SITE)
    assert (result["added"], result["changed"], result["removed"]) == (1, 1, 1)
    assert kb.get("PROJ-2") is None and not ticket_path(kb.root, "PROJ", "PROJ-2").exists()
    assert kb.get("PROJ-1")["summary"] == "Ticket PROJ-1 edited" and kb.get("PROJ-4")
    assert result["total"] == 3 and result["requests"] == 2
    assert "id in (4,1)" in json.loads(route.calls[1].request.content)["jql"]
    assert kb.get_meta("reconciled_at:PROJ") and kb.get_meta("last_reconcile:PROJ")


@respx.mock
def test_reconcile_follows_a_renamed_key(api, kb):
    kb.upsert(tk("1", "PROJ-5"))
    render.write_ticket(kb.root, kb.get("PROJ-5"), SITE)
    remote = [index_issue("1", "PROJ-50")]
    route_search(respx, reconcile_handler(remote, {"1": issue("1", "PROJ-50")}))
    result = reconcile_project(api, kb, "PROJ", SITE)
    assert result["changed"] == 1 and result["removed"] == 0
    assert not ticket_path(kb.root, "PROJ", "PROJ-5").exists()
    assert ticket_path(kb.root, "PROJ", "PROJ-50").exists()


@respx.mock
def test_reconcile_adopts_legacy_rows_without_removing_them(api, kb):
    kb.upsert(tk(None, "PROJ-1", summary="Some ticket"))
    remote = [index_issue("11", "PROJ-1")]
    route_search(respx, reconcile_handler(remote, {"11": issue("11", "PROJ-1")}))
    result = reconcile_project(api, kb, "PROJ", SITE)
    assert result["removed"] == 0 and kb.legacy_count() == 0 and kb.count() == 1


@respx.mock
def test_reconcile_refuses_mass_removal_unless_forced(api, kb):
    for i in range(1, 31):
        kb.upsert(tk(str(i), f"PROJ-{i}"))
    remote = [index_issue(str(i), f"PROJ-{i}") for i in range(1, 6)]
    route_search(respx, reconcile_handler(remote, {}))
    with pytest.raises(KbError, match="refusing to remove 25 of 30"):
        reconcile_project(api, kb, "PROJ", SITE)
    assert kb.count() == 30
    result = reconcile_project(api, kb, "PROJ", SITE, force=True)
    assert result["removed"] == 25 and kb.count() == 5


@respx.mock
def test_reconcile_dry_run_reports_without_changing_anything(api, kb):
    kb.upsert(tk("1", "PROJ-1"))
    kb.upsert(tk("2", "PROJ-2"))
    remote = [index_issue("1", "PROJ-1")]
    route = route_search(respx, reconcile_handler(remote, {}))
    result = reconcile_project(api, kb, "PROJ", SITE, dry_run=True)
    assert result["dry_run"] and result["removed"] == 1 and result["remove_keys"] == ["PROJ-2"]
    assert kb.count() == 2 and route.call_count == 1


# --- refresh ----------------------------------------------------------------------------------


def test_normalize_keys_upcases_dedupes_and_validates():
    assert normalize_keys(["proj-1", "PROJ-1", " ops-2 "]) == ["PROJ-1", "OPS-2"]
    for bad in ("PROJ", "proj-x", 'PROJ-1") OR ("X-1', "../a-1"):
        with pytest.raises(ValueError):
            normalize_keys([bad])


def mock_direct(mock, key, body=None, status=200):
    """GET /issue/<key> as used for a direct read (has expand=changelog)."""
    if body is None:
        return mock.get(f"{BASE}/issue/{key}").respond(status, json={"errorMessages": ["gone"]})
    return mock.get(f"{BASE}/issue/{key}").respond(json=body)


@respx.mock
def test_refresh_updates_reports_missing_and_moved_without_touching_the_cursor(api, kb):
    kb.upsert(tk("1", "PROJ-1", summary="Old title"))
    kb.upsert(tk("2", "PROJ-2"))
    kb.set_meta("cursor:PROJ", "2026-10-01T10:00:00.000+0500")
    route = route_search(
        respx,
        lambda jql, body: [issue("1", "PROJ-1", "New title"), issue("2", "PROJ-20")],
    )
    mock_direct(respx, "PROJ-3", status=404)
    result = refresh_keys(api, kb, ["proj-1", "PROJ-2", "PROJ-3"], SITE)
    assert (
        'key in ("PROJ-1","PROJ-2","PROJ-3")' in json.loads(route.calls[0].request.content)["jql"]
    )
    assert result["updated"] == 2 and result["missing"] == ["PROJ-3"]
    assert result["moved"] == [{"from": "PROJ-2", "to": "PROJ-20"}]
    assert kb.get("PROJ-1")["summary"] == "New title" and kb.get("PROJ-20")
    assert kb.get_meta("cursor:PROJ") == "2026-10-01T10:00:00.000+0500"


@respx.mock
def test_a_ticket_search_has_not_indexed_yet_is_found_by_a_direct_read(api, kb):
    """Jira's search index lags a few seconds behind writes; this was a real bug."""
    route_search(respx, lambda jql, body: [])
    direct = mock_direct(respx, "PROJ-9", issue("99", "PROJ-9", "Just created"))
    result = refresh_keys(api, kb, ["PROJ-9"], SITE)
    assert result["missing"] == [] and result["new"] == 1 and direct.called
    assert kb.get("PROJ-9")["summary"] == "Just created"


@respx.mock
def test_direct_mode_skips_search_entirely(api, kb):
    search = respx.post(SEARCH)
    mock_direct(respx, "PROJ-9", issue("99", "PROJ-9", "Just created"))
    result = refresh_keys(api, kb, ["PROJ-9"], SITE, direct=True)
    assert result["new"] == 1 and not search.called


@respx.mock
def test_a_direct_read_that_follows_a_moved_key_is_recorded_as_a_move(api, kb):
    kb.upsert(tk("5", "PROJ-5", summary="Scheduler"))
    route_search(respx, lambda jql, body: [])
    mock_direct(respx, "PROJ-5", issue("5", "OPS-1", "Scheduler"))
    result = refresh_keys(api, kb, ["PROJ-5"], SITE)
    assert result["moved"] == [{"from": "PROJ-5", "to": "OPS-1"}] and result["missing"] == []
    assert kb.get("PROJ-5") is None and kb.get("OPS-1")["id"] == "5"


@respx.mock
def test_a_direct_read_error_other_than_not_found_is_surfaced(api, kb):
    mock_direct(respx, "PROJ-9", status=500)
    with pytest.raises(JiraError):
        refresh_keys(api, kb, ["PROJ-9"], SITE, direct=True)


@respx.mock
def test_refresh_can_drop_keys_jira_no_longer_returns(api, kb):
    kb.upsert(tk("3", "PROJ-3"))
    route_search(respx, lambda jql, body: [])
    mock_direct(respx, "PROJ-3", status=404)
    kept = refresh_keys(api, kb, ["PROJ-3"], SITE)
    assert kept["missing"] == ["PROJ-3"] and kb.get("PROJ-3")
    dropped = refresh_keys(api, kb, ["PROJ-3"], SITE, drop_missing=True)
    assert dropped["removed"] == 1 and kb.get("PROJ-3") is None


# --- automatic refresh after writes -----------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    kb_dir = tmp_path / "kb"
    with KbStore(kb_dir) as kb:
        kb.bind_site(SITE)
        kb.upsert(tk("1", "PROJ-1", summary="Before the edit"))
        sync_state(kb)
    store = ProfileStore(tmp_path / "home")
    store.save(
        Profile(
            name="p", site_url=SITE, projects=["PROJ"], kb_path=str(kb_dir), sync_policy="manual"
        )
    )
    store.set_last_used("p")
    return store, make_secrets(), kb_dir


def mock_edit(mock, after="After the edit"):
    """PROJ-1 as `jira update` sees it, and (with expand=changelog) as the refresh reads it."""

    def read(request):
        if "expand" in request.url.params:
            return httpx.Response(200, json=issue("1", "PROJ-1", after))
        return httpx.Response(200, json={"fields": {"summary": "Before the edit"}})

    mock.get(f"{BASE}/issue/PROJ-1/editmeta").respond(json={"fields": {"summary": {}}})
    mock.get(f"{BASE}/issue/PROJ-1").mock(side_effect=read)
    return mock.put(f"{BASE}/issue/PROJ-1").respond(204)


@respx.mock
def test_a_confirmed_write_refreshes_the_knowledge_base_by_reading_the_ticket(env, capsys):
    store, secrets, kb_dir = env
    mock_edit(respx)
    search = respx.post(SEARCH)
    code = run(
        ["jira", "update", "PROJ-1", "--summary", "After the edit", "--confirm"], store, secrets
    )
    assert code == 0 and not search.called  # search lags writes, so it is never used here
    assert capsys.readouterr().err == ""
    with KbStore(kb_dir) as kb:
        assert kb.get("PROJ-1")["summary"] == "After the edit"


@respx.mock
def test_a_write_whose_ticket_cannot_be_read_back_warns_instead_of_staying_silent(env, capsys):
    store, secrets, _ = env
    respx.get(f"{BASE}/issue/PROJ-1/editmeta").respond(json={"fields": {"summary": {}}})

    def read(request):
        if "expand" in request.url.params:
            return httpx.Response(404, json={"errorMessages": ["not yet"]})
        return httpx.Response(200, json={"fields": {"summary": "Before"}})

    respx.get(f"{BASE}/issue/PROJ-1").mock(side_effect=read)
    respx.put(f"{BASE}/issue/PROJ-1").respond(204)
    code = run(["jira", "update", "PROJ-1", "--summary", "After", "--confirm"], store, secrets)
    assert code == 0
    assert "could not find PROJ-1 yet" in capsys.readouterr().err


@respx.mock
def test_a_failed_refresh_never_fails_the_write(env, capsys):
    store, secrets, _ = env
    respx.get(f"{BASE}/issue/PROJ-1/editmeta").respond(json={"fields": {"summary": {}}})

    def read(request):
        if "expand" in request.url.params:
            return httpx.Response(500, json={"errorMessages": ["down"]})
        return httpx.Response(200, json={"fields": {"summary": "Before"}})

    respx.get(f"{BASE}/issue/PROJ-1").mock(side_effect=read)
    respx.put(f"{BASE}/issue/PROJ-1").respond(204)
    code = run(["jira", "update", "PROJ-1", "--summary", "After", "--confirm"], store, secrets)
    assert code == 0
    assert "knowledge base not refreshed" in capsys.readouterr().err


@respx.mock
def test_refresh_is_skipped_for_previews_dry_runs_and_the_opt_out_flag(env):
    store, secrets, _ = env
    mock_edit(respx)
    search = respx.post(SEARCH)
    assert run(["jira", "update", "PROJ-1", "--summary", "x"], store, secrets) == 4
    assert (
        run(
            ["jira", "update", "PROJ-1", "--summary", "x", "--confirm", "--dry-run"], store, secrets
        )
        == 0
    )
    assert (
        run(
            ["jira", "update", "PROJ-1", "--summary", "x", "--confirm", "--no-kb-refresh"],
            store,
            secrets,
        )
        == 0
    )
    assert not search.called


def test_refresh_after_write_does_nothing_for_a_project_that_was_never_synced(tmp_path):
    profile = Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "none"))
    assert refresh_after_write(profile, make_secrets(), None, ["PROJ-1"]) is None
    with KbStore(tmp_path / "kb") as kb:
        kb.bind_site(SITE)
    profile = Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "kb"))
    assert refresh_after_write(profile, make_secrets(), None, ["PROJ-1"]) is None


# --- status and CLI ---------------------------------------------------------------------------


def test_status_flags_an_upgraded_index_and_reconcile_due(kb):
    kb.upsert(tk(None, "PROJ-1"))
    kb.set_meta("synced_at:PROJ", str(int(time.time())))
    status = project_status(kb, "PROJ", SyncPolicy.MANUAL)
    assert status["recommended_action"] == "sync" and "upgraded" in status["reason"]
    assert status["legacy_rows"] == 1 and status["reconcile_due"] is True


def test_status_is_quiet_when_everything_is_current(kb):
    kb.upsert(tk("1", "PROJ-1"))
    sync_state(kb)
    kb.set_meta("reconciled_at:PROJ", str(int(time.time())))
    status = project_status(kb, "PROJ", SyncPolicy.MANUAL)
    assert status["recommended_action"] == "none" and status["reconcile_due"] is False


@respx.mock
def test_kb_sync_runs_the_weekly_reconcile_after_an_incremental_sync(env, capsys):
    store, secrets, kb_dir = env
    with KbStore(kb_dir) as kb:
        kb.set_meta("reconciled_at:PROJ", str(int(time.time()) - 8 * 86400))

    def handler(jql, body):
        if jql.startswith("project") and "updated >=" in jql:
            return []
        return [index_issue("1", "PROJ-1")]

    route_search(respx, handler)
    assert run(["kb", "sync"], store, secrets) == 0
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["mode"] == "incremental" and result["maintenance"]["removed"] == 0
    assert run(["kb", "sync"], store, secrets) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["maintenance"] is None


@respx.mock
def test_kb_refresh_and_reconcile_commands(env, capsys):
    store, secrets, _ = env
    route_search(respx, lambda jql, body: [issue("1", "PROJ-1", "Fresh title")])
    assert run(["kb", "refresh", "proj-1"], store, secrets) == 0
    assert json.loads(capsys.readouterr().out)["updated"] == 1
    route_search(respx, lambda jql, body: [index_issue("1", "PROJ-1")])
    assert run(["kb", "reconcile", "--dry-run"], store, secrets) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["dry_run"] is True


def test_kb_refresh_rejects_bad_keys(env):
    store, secrets, _ = env
    assert run(["kb", "refresh", "not-a-key"], store, secrets) == 2
