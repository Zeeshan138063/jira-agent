import json
import stat
import threading
import time

import httpx
import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.callback_server import CallbackError, CallbackServer
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.cli import run
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, ProfileStore, WriteMode
from jira_agent.kb.render import ticket_path
from jira_agent.kb.store import KbStore
from jira_agent.kb.sync import sync_project

SITE = "https://acme.atlassian.net"
BASE = f"{oauth.API_BASE}/cloud-1/rest/api/3"
SEARCH = f"{BASE}/search/jql"


def make_secrets():
    s = MemoryStore()
    oauth.save_client(s, "cid", "csecret", 0)
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


def hit(url, **params):
    threading.Thread(target=lambda: httpx.get(url, params=params, timeout=5), daemon=True).start()


def test_callback_ignores_mismatched_state_and_keeps_waiting():
    server = CallbackServer(0)

    def sender():
        httpx.get(server.redirect_uri, params={"code": "evil", "state": "wrong"})
        httpx.get(server.redirect_uri, params={"error": "access_denied", "state": "wrong"})
        httpx.get(server.redirect_uri, params={"code": "good", "state": "right"})

    threading.Thread(target=sender, daemon=True).start()
    assert server.wait("right", timeout=10) == "good"


def test_callback_without_any_matching_response_times_out():
    server = CallbackServer(0)
    hit(server.redirect_uri, code="x", state="wrong")
    with pytest.raises(CallbackError, match="timed out"):
        server.wait("right", timeout=1)


@respx.mock
@pytest.mark.parametrize(
    "path",
    [
        "issue/X-1/../../myself",
        "issue/X-1?expand=all",
        "issue/X-1#frag",
        "issue/%2e%2e/x",
        "issue/a b",
        "../x",
    ],
)
def test_unsafe_api_paths_are_refused_before_any_request(path):
    route = respx.get(url__regex=r".*")
    with pytest.raises(ValueError, match="unsafe"):
        JiraClient(SITE, make_secrets()).get(path)
    assert not route.called


@respx.mock
def test_redirects_are_not_followed_and_do_not_leak_the_token():
    respx.get(f"{BASE}/myself").respond(302, headers={"Location": "https://evil.example/steal"})
    evil = respx.get("https://evil.example/steal")
    with pytest.raises(JiraError, match="redirect"):
        JiraClient(SITE, make_secrets()).get("myself")
    assert not evil.called


@pytest.mark.parametrize("key", ["../../etc/passwd", "proj-1", "PROJ-1/../x", "PROJ", ""])
def test_ticket_path_rejects_bad_keys(tmp_path, key):
    with pytest.raises(ValueError):
        ticket_path(tmp_path, "PROJ", key)


def test_ticket_path_rejects_project_mismatch(tmp_path):
    with pytest.raises(ValueError):
        ticket_path(tmp_path, "PROJ", "OPS-1")
    assert ticket_path(tmp_path, "PROJ", "PROJ-1").name == "PROJ-1.md"


def test_kb_show_with_traversal_key_exits_2(tmp_path):
    store = ProfileStore(tmp_path / "home")
    store.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "kb")))
    store.set_last_used("p")
    assert run(["kb", "show", "../../etc/passwd"], store, make_secrets()) == 2


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_private_directories_and_files(tmp_path):
    home = tmp_path / "home"
    store = ProfileStore(home)
    store.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "kb")))
    assert mode(home) == 0o700 and mode(home / "profiles") == 0o700
    with KbStore(tmp_path / "kb") as kb:
        assert mode(kb.root) == 0o700 and mode(kb.root / "index.db") == 0o600
    audit = home / "audit.log"
    WriteGate(WriteMode.CONFIRM_EACH, True, SITE, audit).run(
        "x", {"target": "T"}, lambda: {"key": "K-1"}
    )
    assert mode(audit) == 0o600


def test_existing_directories_keep_their_permissions(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    ProfileStore(shared).save(
        Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "kb"))
    )
    assert mode(shared) == 0o755


def raw(i):
    return {
        "id": str(1000 + i),
        "key": f"PROJ-{i}",
        "fields": {
            "summary": f"Ticket {i}",
            "updated": f"2026-10-01T10:{i % 60:02d}:00.000+0500",
            "issuetype": {"name": "Task"},
        },
    }


@respx.mock
def test_interrupted_sync_keeps_progress_and_resumes_incrementally(tmp_path):
    api = IssueAPI(
        JiraClient(SITE, make_secrets(), sleep=lambda s: None, max_retries=0),
        WriteGate(WriteMode.DRY_RUN),
    )
    pages = iter(
        [
            httpx.Response(
                200, json={"issues": [raw(i) for i in range(100)], "nextPageToken": "t"}
            ),
            httpx.Response(500, json={"errorMessages": ["down"]}),
        ]
    )
    route = respx.post(SEARCH).mock(side_effect=lambda request: next(pages))
    with KbStore(tmp_path / "kb") as kb:
        with pytest.raises(JiraError):
            sync_project(api, kb, "PROJ", SITE)
        assert kb.count("PROJ") == 100
        cursor = kb.get_meta("cursor:PROJ")
        assert cursor == "2026-10-01T10:59:00.000+0500"

        route.mock(return_value=httpx.Response(200, json={"issues": [], "isLast": True}))
        result = sync_project(api, kb, "PROJ", SITE)
        assert result["mode"] == "incremental"
        assert 'updated >= "2026-10-01 10:57"' in json.loads(route.calls[-1].request.content)["jql"]


@respx.mock
def test_network_failure_is_a_clean_error_not_a_traceback(tmp_path, capsys):
    store = ProfileStore(tmp_path / "home")
    store.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "kb")))
    store.set_last_used("p")
    respx.get(f"{BASE}/issue/PROJ-1").mock(side_effect=httpx.ConnectError("no route"))
    code = run(["jira", "get", "PROJ-1", "--no-comments"], store, make_secrets())
    assert code == 5 and "network error: ConnectError" in capsys.readouterr().err


def test_file_secret_store_roundtrip_and_privacy(tmp_path, monkeypatch):
    import json
    import os
    import stat

    from jira_agent.auth.keychain import FileStore, KeychainStore, default_store

    store = FileStore(tmp_path / "home" / "secrets.json")
    assert store.get("k") is None
    store.set("k", {"token": "x", "name": "café"})
    assert store.get("k") == {"token": "x", "name": "café"}
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.delete("k")
    store.delete("missing")
    assert json.loads(store.path.read_text(encoding="utf-8")) == {}

    monkeypatch.delenv("JIRA_AGENT_SECRETS", raising=False)
    assert isinstance(default_store(), KeychainStore)
    monkeypatch.setenv("JIRA_AGENT_SECRETS", "file")
    monkeypatch.setenv("JIRA_AGENT_HOME", str(tmp_path / "h"))
    assert isinstance(default_store(), FileStore)
