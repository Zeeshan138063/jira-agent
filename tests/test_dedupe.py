import json
import time

import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.cli import run
from jira_agent.config import Profile, ProfileStore
from jira_agent.dedupe import find_duplicates, live_jql, similarity, stem, term_set
from jira_agent.kb.store import KbStore

SITE = "https://acme.atlassian.net"
SEARCH = f"{oauth.API_BASE}/cloud-1/rest/api/3/search/jql"


def ticket(key, summary, description="", **kw):
    return {
        "key": key,
        "project": key.split("-")[0],
        "summary": summary,
        "description": description,
        "status": "To Do",
        "type": "Bug",
        "labels": [],
        "components": [],
        "comments": [],
        **kw,
    }


@pytest.fixture
def kb(tmp_path):
    with KbStore(tmp_path / "kb") as store:
        store.upsert(
            ticket("PROJ-1", "Login fails on iOS after update", "Users cannot log in on iOS")
        )
        store.upsert(ticket("PROJ-2", "Invoice export times out", "CSV export of invoices hangs"))
        store.upsert(
            ticket(
                "PROJ-3",
                "Appointment reminder email wording",
                "Reword the reminder",
                status="Done",
                resolution="Done",
                components=["Email"],
            )
        )
        store.commit()
        yield store


def test_stemming_and_term_sets():
    assert stem("failing") == "fail" and stem("invoices") == "invoic" and stem("ios") == "ios"
    assert term_set("The Login fails") == term_set("login failing")


def test_identical_summaries_score_one():
    assert similarity({"summary": "Login fails"}, {"summary": "login failing!"}) == 1.0


def test_unrelated_scores_low():
    s = similarity({"summary": "Add dark mode"}, {"summary": "Invoice export times out"})
    assert s < 0.2


def test_near_duplicate_flagged(kb):
    result = find_duplicates(kb, {"summary": "iOS login failing after the update"}, ["PROJ"], 0.5)
    assert result["verdict"] == "likely_duplicate"
    top = result["matches"][0]
    assert top["key"] == "PROJ-1" and top["source"] == "kb"
    assert any("shared summary terms" in r for r in top["reasons"])


def test_related_band_between_factor_and_threshold(kb):
    draft = {"summary": "Login screen redesign for iOS"}
    result = find_duplicates(kb, draft, ["PROJ"], 0.6)
    assert result["verdict"] == "related"
    assert result["matches"][0]["verdict"] == "related"


def test_no_match_returns_none(kb):
    result = find_duplicates(kb, {"summary": "Add dark mode toggle"}, ["PROJ"], 0.6)
    assert result == {"verdict": "none", "threshold": 0.6, "matches": []}


def test_resolved_candidates_are_marked(kb):
    result = find_duplicates(kb, {"summary": "Appointment reminder email wording"}, ["PROJ"], 0.6)
    match = result["matches"][0]
    assert match["key"] == "PROJ-3" and match["resolved"] is True


def test_shared_components_are_reported(kb):
    draft = {"summary": "Appointment reminder email wording", "components": ["Email"]}
    reasons = find_duplicates(kb, draft, ["PROJ"], 0.6)["matches"][0]["reasons"]
    assert "same components: Email" in reasons


def test_exclude_self(kb):
    draft = {"summary": "Login fails on iOS after update"}
    result = find_duplicates(kb, draft, ["PROJ"], 0.5, exclude="PROJ-1")
    assert all(m["key"] != "PROJ-1" for m in result["matches"])


def test_live_tickets_merge_without_overriding_kb(kb):
    live = [
        ticket("PROJ-1", "totally different"),
        ticket("PROJ-9", "Login failing on iOS after update"),
    ]
    result = find_duplicates(
        kb, {"summary": "Login fails on iOS after update"}, ["PROJ"], 0.5, live_tickets=live
    )
    sources = {m["key"]: m["source"] for m in result["matches"]}
    assert sources["PROJ-1"] == "kb" and sources["PROJ-9"] == "live"


def test_live_jql_uses_longest_terms_and_quotes_projects():
    jql = live_jql({"summary": "Login fails on iOS"}, ["PROJ", "OPS"])
    assert jql.startswith('project in ("PROJ", "OPS") AND text ~ "')
    assert live_jql({"summary": "the a of"}, ["PROJ"]) is None


@pytest.fixture
def cli_env(tmp_path, kb):
    store = ProfileStore(tmp_path / "home")
    store.save(
        Profile(
            name="p", site_url=SITE, projects=["PROJ"], kb_path=str(kb.root), dedupe_threshold=0.5
        )
    )
    store.set_last_used("p")
    kb.bind_site(SITE)
    secrets = MemoryStore()
    oauth.save_client(secrets, "cid", "csecret", 0)
    secrets.set(
        token_key(SITE),
        {
            "access_token": "t",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )
    return store, secrets


def test_cli_offline_check(cli_env, capsys):
    store, secrets = cli_env
    code = run(
        ["dedupe", "--summary", "iOS login failing after update", "--no-live"], store, secrets
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["verdict"] == "likely_duplicate" and out["warnings"] == []


@respx.mock
def test_cli_live_search_adds_unsynced_ticket(cli_env, capsys):
    store, secrets = cli_env
    respx.post(SEARCH).respond(
        json={
            "issues": [
                {
                    "key": "PROJ-77",
                    "fields": {"summary": "Dark mode toggle missing", "status": {"name": "To Do"}},
                }
            ],
            "isLast": True,
        }
    )
    run(["dedupe", "--summary", "Add dark mode toggle"], store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert out["matches"][0]["key"] == "PROJ-77" and out["matches"][0]["source"] == "live"


@respx.mock
def test_cli_live_failure_degrades_to_kb(cli_env, capsys):
    store, secrets = cli_env
    respx.post(SEARCH).respond(403, json={"errorMessages": ["nope"]})
    code = run(["dedupe", "--summary", "iOS login failing after update"], store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["verdict"] == "likely_duplicate"
    assert "live search skipped" in out["warnings"][0]


def test_cli_checks_existing_ticket_excluding_itself(cli_env, capsys):
    store, secrets = cli_env
    run(["dedupe", "--key", "PROJ-1", "--no-live", "--threshold", "0.1"], store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert all(m["key"] != "PROJ-1" for m in out["matches"])


def test_cli_requires_summary(cli_env, capsys):
    store, secrets = cli_env
    assert run(["dedupe", "--no-live"], store, secrets) == 2
