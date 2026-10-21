import json
import time

import httpx
import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.client import endpoints
from jira_agent.client.adf import from_adf, to_adf
from jira_agent.client.gate import ConfirmationRequired, WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI, IssueError, coerce_value
from jira_agent.config import WriteMode

SITE = "https://acme.atlassian.net"
BASE = f"{oauth.API_BASE}/cloud-1/rest/api/3"


@pytest.fixture
def store():
    s = MemoryStore()
    oauth.save_client(s, "cid", "csecret", 0)
    s.set(
        token_key(SITE),
        {
            "access_token": "tok",
            "refresh_token": "r1",
            "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )
    return s


def make_api(store, tmp_path, mode=WriteMode.CONFIRM_EACH, confirmed=False):
    client = JiraClient(SITE, store, sleep=lambda s: None)
    gate = WriteGate(mode, confirmed, SITE, tmp_path / "audit.log")
    return IssueAPI(client, gate)


META_FIELDS = [
    {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string"}},
    {"fieldId": "project", "name": "Project", "required": True, "schema": {"type": "project"}},
    {
        "fieldId": "issuetype",
        "name": "Issue Type",
        "required": True,
        "schema": {"type": "issuetype"},
    },
    {"fieldId": "description", "name": "Description", "required": False, "schema": {"type": "doc"}},
    {
        "fieldId": "labels",
        "name": "Labels",
        "required": False,
        "schema": {"type": "array", "items": "string"},
    },
    {
        "fieldId": "customfield_10001",
        "name": "Team",
        "required": True,
        "schema": {"type": "option"},
        "allowedValues": [{"value": "Core"}, {"value": "Web"}],
    },
]


def mock_meta(mock):
    mock.get(f"{BASE}/issue/createmeta/PROJ/issuetypes").respond(
        json={
            "issueTypes": [{"id": "10", "name": "Story"}, {"id": "11", "name": "Bug"}],
            "total": 2,
        }
    )
    mock.get(f"{BASE}/issue/createmeta/PROJ/issuetypes/10").respond(
        json={"fields": META_FIELDS, "total": len(META_FIELDS)}
    )


def test_adf_round_trip():
    text = "## Goal\n\nShip it.\nSecond line.\n\n- one\n- two\n\n1. a\n2. b"
    adf = to_adf(text)
    assert adf["type"] == "doc" and adf["version"] == 1
    assert from_adf(adf) == text


def test_from_adf_handles_empty_and_plain():
    assert from_adf(None) == "" and from_adf("x") == "x"


@respx.mock
def test_retries_429_then_succeeds(store):
    route = respx.get(f"{BASE}/myself").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    assert JiraClient(SITE, store, sleep=lambda s: None).get("myself") == {"ok": 1}
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_max_retries(store):
    respx.get(f"{BASE}/myself").respond(503)
    with pytest.raises(JiraError) as e:
        JiraClient(SITE, store, sleep=lambda s: None, max_retries=2).get("myself")
    assert e.value.status == 503


@respx.mock
def test_refreshes_on_401_then_retries(store):
    respx.post(oauth.TOKEN_URL).respond(
        json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
    )
    route = respx.get(f"{BASE}/myself").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": 1})]
    )
    JiraClient(SITE, store).get("myself")
    assert route.calls[1].request.headers["Authorization"] == "Bearer new"


@respx.mock
def test_error_messages_surface(store):
    respx.get(f"{BASE}/issue/X-1").respond(404, json={"errorMessages": ["Issue does not exist"]})
    with pytest.raises(JiraError, match="does not exist"):
        JiraClient(SITE, store).get("issue/X-1")


@respx.mock
def test_search_follows_next_page_token(store, tmp_path):
    route = respx.post(f"{BASE}/search/jql").mock(
        side_effect=[
            httpx.Response(
                200, json={"issues": [{"key": "A-1"}, {"key": "A-2"}], "nextPageToken": "t2"}
            ),
            httpx.Response(200, json={"issues": [{"key": "A-3"}], "isLast": True}),
        ]
    )
    keys = [i["key"] for i in make_api(store, tmp_path).search("project = A")]
    assert keys == ["A-1", "A-2", "A-3"]
    assert json.loads(route.calls[1].request.content)["nextPageToken"] == "t2"


@respx.mock
def test_search_respects_limit(store, tmp_path):
    respx.post(f"{BASE}/search/jql").respond(
        json={"issues": [{"key": "A-1"}, {"key": "A-2"}], "nextPageToken": "t"}
    )
    assert len(list(make_api(store, tmp_path).search("x", limit=1))) == 1


@respx.mock
def test_meta_lists_required_fields_excluding_auto(store, tmp_path):
    mock_meta(respx)
    meta = make_api(store, tmp_path).create_meta("PROJ", "story")
    assert [r["key"] for r in meta["required"]] == ["summary", "customfield_10001"]
    assert meta["required"][1]["allowed"] == ["Core", "Web"]


@respx.mock
def test_unknown_issue_type_lists_available(store, tmp_path):
    mock_meta(respx)
    with pytest.raises(IssueError, match="Story, Bug"):
        make_api(store, tmp_path).create_meta("PROJ", "Epic")


@respx.mock
def test_create_without_confirm_returns_preview_and_does_not_post(store, tmp_path):
    mock_meta(respx)
    post = respx.post(f"{BASE}/issue")
    with pytest.raises(ConfirmationRequired) as e:
        make_api(store, tmp_path).create_issue(
            "PROJ", "Story", "Do thing", "desc", {"customfield_10001": "Core"}
        )
    assert e.value.preview["fields"]["summary"] == "Do thing"
    assert not post.called


@respx.mock
def test_create_missing_required_field_fails_before_gate(store, tmp_path):
    mock_meta(respx)
    with pytest.raises(IssueError, match="customfield_10001"):
        make_api(store, tmp_path, confirmed=True).create_issue("PROJ", "Story", "x")


@respx.mock
def test_create_confirmed_posts_coerced_fields_and_audits(store, tmp_path):
    mock_meta(respx)
    post = respx.post(f"{BASE}/issue").respond(json={"key": "PROJ-9"})
    api = make_api(store, tmp_path, confirmed=True)
    result = api.create_issue(
        "PROJ",
        "Story",
        "Do thing",
        "desc",
        {"customfield_10001": "Core"},
        defaults={"labels": "a,b", "priority": "High"},
    )
    assert result == {"key": "PROJ-9"}
    sent = json.loads(post.calls[0].request.content)["fields"]
    assert sent["customfield_10001"] == {"value": "Core"}
    assert sent["labels"] == ["a", "b"] and "priority" not in sent
    assert sent["description"]["type"] == "doc"
    entry = json.loads((tmp_path / "audit.log").read_text().splitlines()[0])
    assert entry["result_key"] == "PROJ-9" and "description" not in entry


@respx.mock
def test_dry_run_never_posts_even_when_confirmed(store, tmp_path):
    mock_meta(respx)
    post = respx.post(f"{BASE}/issue")
    out = make_api(store, tmp_path, WriteMode.DRY_RUN, confirmed=True).create_issue(
        "PROJ", "Story", "x", extra={"customfield_10001": "Core"}
    )
    assert out["dry_run"] and not post.called


@respx.mock
def test_update_previews_before_after(store, tmp_path):
    respx.get(f"{BASE}/issue/PROJ-1/editmeta").respond(
        json={"fields": {"summary": {"schema": {"type": "string"}}, "description": {}}}
    )
    respx.get(f"{BASE}/issue/PROJ-1").respond(
        json={"fields": {"summary": "Old", "description": to_adf("old text")}}
    )
    with pytest.raises(ConfirmationRequired) as e:
        make_api(store, tmp_path).update_issue("PROJ-1", summary="New", description="new text")
    changes = e.value.preview["fields"]
    assert changes["summary"] == {"before": "Old", "after": "New"}
    assert changes["description"]["before"] == "old text"


@respx.mock
def test_update_rejects_non_editable_field(store, tmp_path):
    respx.get(f"{BASE}/issue/PROJ-1/editmeta").respond(json={"fields": {}})
    respx.get(f"{BASE}/issue/PROJ-1").respond(json={"fields": {}})
    with pytest.raises(IssueError, match="not editable"):
        make_api(store, tmp_path).update_issue("PROJ-1", summary="x")


@respx.mock
def test_transition_matches_by_name_or_target_status(store, tmp_path):
    respx.get(f"{BASE}/issue/PROJ-1/transitions").respond(
        json={"transitions": [{"id": "31", "name": "Start", "to": {"name": "In Progress"}}]}
    )
    post = respx.post(f"{BASE}/issue/PROJ-1/transitions").respond(204)
    make_api(store, tmp_path, confirmed=True).transition("PROJ-1", "in progress")
    assert json.loads(post.calls[0].request.content) == {"transition": {"id": "31"}}
    with pytest.raises(IssueError, match="Start -> In Progress"):
        make_api(store, tmp_path, confirmed=True).transition("PROJ-1", "Done")


@respx.mock
def test_get_issue_normalizes(store, tmp_path):
    respx.get(f"{BASE}/issue/PROJ-1").respond(
        json={
            "key": "PROJ-1",
            "fields": {
                "summary": "S",
                "status": {"name": "To Do"},
                "assignee": {"displayName": "Pat"},
                "description": to_adf("hello"),
                "components": [{"name": "API"}],
            },
        }
    )
    respx.get(f"{BASE}/issue/PROJ-1/comment").respond(
        json={"comments": [{"author": {"displayName": "A"}, "body": to_adf("hi")}]}
    )
    issue = make_api(store, tmp_path).get_issue("PROJ-1")
    assert issue["status"] == "To Do" and issue["assignee"] == "Pat"
    assert issue["description"] == "hello" and issue["comments"][0]["body"] == "hi"


def test_coerce_value_by_schema():
    assert coerce_value({"schema": {"type": "user"}}, "abc") == {"accountId": "abc"}
    assert coerce_value({"schema": {"type": "array", "items": "component"}}, "A, B") == [
        {"name": "A"},
        {"name": "B"},
    ]
    assert coerce_value({"schema": {"type": "number"}}, "3") == 3.0


def test_gate_requires_confirmation_for_writes(tmp_path):
    gate = WriteGate(WriteMode.CONFIRM_EACH, False, SITE, tmp_path / "a.log")
    with pytest.raises(ConfirmationRequired):
        gate.run("x", {}, lambda: 1)


def test_catalog_parse_and_search():
    spec = {
        "paths": {
            "/rest/api/3/issue": {
                "post": {"operationId": "createIssue", "summary": "Create issue"}
            },
            "/rest/api/3/dashboard": {"get": {"summary": "nope"}},
            "/rest/api/3/search/jql": {"post": {"summary": "Search"}},
        }
    }
    entries = endpoints.parse_spec(spec)
    assert [e["path"] for e in entries] == ["/rest/api/3/issue", "/rest/api/3/search/jql"]
    assert entries[0]["write"] and not entries[1]["write"]
    assert endpoints.search_catalog(entries, "create")[0]["operation_id"] == "createIssue"


def test_catalog_falls_back_to_builtin(tmp_path):
    assert endpoints.load_catalog(tmp_path)[0]["source"] == "builtin"


@respx.mock
def test_link_makes_the_first_issue_act_on_the_second(store, tmp_path):
    """`link Blocks A B` must make A block B; Jira's inward/outward naming is easy to invert."""
    respx.get(f"{BASE}/issueLinkType").respond(
        json={
            "issueLinkTypes": [{"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}]
        }
    )
    post = respx.post(f"{BASE}/issueLink").respond(201)
    api = make_api(store, tmp_path, confirmed=True)
    api.link("blocks", "PROJ-1", "PROJ-2")
    sent = json.loads(post.calls[0].request.content)
    assert sent["inwardIssue"] == {"key": "PROJ-1"} and sent["outwardIssue"] == {"key": "PROJ-2"}
    assert sent["type"] == {"name": "Blocks"}


@respx.mock
def test_link_preview_reads_in_plain_words(store, tmp_path):
    respx.get(f"{BASE}/issueLinkType").respond(
        json={
            "issueLinkTypes": [{"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}]
        }
    )
    with pytest.raises(ConfirmationRequired) as e:
        make_api(store, tmp_path).link("Blocks", "PROJ-1", "PROJ-2")
    assert e.value.action == "link PROJ-1 blocks PROJ-2 (Blocks)"
