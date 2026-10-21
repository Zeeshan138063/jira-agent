import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from jira_agent.auth import oauth, pkce
from jira_agent.auth.callback_server import CallbackError, CallbackServer
from jira_agent.auth.keychain import MemoryStore, token_key

SITE = "https://acme.atlassian.net"


@pytest.fixture
def store():
    s = MemoryStore()
    oauth.save_client(s, "cid", "csecret", 0)
    return s


def browser_returning(**override):
    def open_browser(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        params = {"code": "abc", "state": q["state"], **override}
        threading.Thread(
            target=lambda: httpx.get(q["redirect_uri"], params=params, timeout=5), daemon=True
        ).start()

    return open_browser


def mock_token_endpoints(mock, refresh_token="r1"):
    mock.post(oauth.TOKEN_URL).respond(
        json={"access_token": "a1", "refresh_token": refresh_token, "expires_in": 3600}
    )
    mock.get(oauth.RESOURCES_URL).respond(
        json=[
            {"id": "cloud-1", "url": SITE},
            {"id": "cloud-2", "url": "https://other.atlassian.net"},
        ]
    )


def test_pkce_challenge_matches_rfc7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce.challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_authorize_url_has_required_params():
    url = oauth.build_authorize_url("cid", "http://127.0.0.1:1/callback", "st", "ch")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["audience"] == "api.atlassian.com" and q["code_challenge_method"] == "S256"
    assert set(q["scope"].split()) == set(oauth.SCOPES)


def test_callback_server_times_out():
    with pytest.raises(CallbackError, match="timed out"):
        CallbackServer(0).wait("s", timeout=0.2)


def test_callback_server_port_in_use():
    first = CallbackServer(0)
    try:
        with pytest.raises(CallbackError, match="cannot listen"):
            CallbackServer(first.port)
    finally:
        first._server.server_close()


@respx.mock(assert_all_called=False)
def test_login_stores_tokens_for_matching_site(store, respx_mock):
    respx_mock.route(host="127.0.0.1").pass_through()
    mock_token_endpoints(respx_mock)
    record = oauth.login(
        SITE, store, open_browser=browser_returning(), announce=lambda m: None, timeout=10
    )
    assert record["cloud_id"] == "cloud-1"
    saved = store.get(token_key(SITE))
    assert saved["refresh_token"] == "r1"
    token_call = next(c for c in respx_mock.calls if str(c.request.url) == oauth.TOKEN_URL)
    body = token_call.request.content.decode()
    assert "code_verifier" in body and "csecret" in body


@respx.mock(assert_all_called=False)
def test_login_fails_when_account_lacks_site(store, respx_mock):
    respx_mock.route(host="127.0.0.1").pass_through()
    mock_token_endpoints(respx_mock)
    with pytest.raises(oauth.AuthError, match="no access"):
        oauth.login(
            "https://nope.atlassian.net",
            store,
            open_browser=browser_returning(),
            announce=lambda m: None,
            timeout=10,
        )
    assert store.get(token_key("https://nope.atlassian.net")) is None


@respx.mock(assert_all_called=False)
def test_login_reports_user_denial(store, respx_mock):
    respx_mock.route(host="127.0.0.1").pass_through()
    with pytest.raises(CallbackError, match="denied"):
        oauth.login(
            SITE,
            store,
            open_browser=browser_returning(error="access_denied", code=None),
            announce=lambda m: None,
            timeout=10,
        )


def seed(store, expires_in, refresh_token="r1"):
    store.set(
        token_key(SITE),
        {
            "access_token": "old",
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )


@respx.mock
def test_valid_token_returned_without_network(store):
    seed(store, 3600)
    assert oauth.get_valid_token(SITE, store)["access_token"] == "old"


@respx.mock
def test_expired_token_refreshes_and_stores_rotated_refresh_token(store):
    seed(store, -10)
    route = respx.post(oauth.TOKEN_URL).respond(
        json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
    )
    record = oauth.get_valid_token(SITE, store)
    assert record["access_token"] == "new"
    assert store.get(token_key(SITE))["refresh_token"] == "r2"
    assert b"r1" in route.calls[0].request.content


@respx.mock
def test_rejected_refresh_clears_session(store):
    seed(store, -10)
    respx.post(oauth.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(oauth.AuthRequired):
        oauth.get_valid_token(SITE, store)
    assert store.get(token_key(SITE)) is None


def test_not_signed_in(store):
    with pytest.raises(oauth.AuthRequired, match="not signed in"):
        oauth.get_valid_token(SITE, store)


@respx.mock
def test_preflight_returns_identity(store):
    seed(store, 3600)
    respx.get(f"{oauth.API_BASE}/cloud-1/rest/api/3/myself").respond(
        json={"accountId": "u1", "displayName": "Pat", "emailAddress": "user@example.com"}
    )
    me = oauth.preflight(SITE, store)
    assert me["display_name"] == "Pat" and me["cloud_id"] == "cloud-1"


@respx.mock
def test_preflight_refreshes_once_on_401(store):
    seed(store, 3600)
    respx.post(oauth.TOKEN_URL).respond(
        json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
    )
    myself = respx.get(f"{oauth.API_BASE}/cloud-1/rest/api/3/myself").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"accountId": "u1"})]
    )
    assert oauth.preflight(SITE, store)["account_id"] == "u1"
    assert myself.calls[1].request.headers["Authorization"] == "Bearer new"


@respx.mock
def test_preflight_persistent_401_requires_login(store):
    seed(store, 3600)
    respx.post(oauth.TOKEN_URL).respond(
        json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
    )
    respx.get(f"{oauth.API_BASE}/cloud-1/rest/api/3/myself").respond(401)
    with pytest.raises(oauth.AuthRequired):
        oauth.preflight(SITE, store)


def test_logout_removes_tokens_only(store):
    seed(store, 3600)
    oauth.logout(SITE, store)
    assert store.get(token_key(SITE)) is None
    assert oauth.load_client(store)["client_id"] == "cid"
