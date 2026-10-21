import threading
import time

import httpx
import pytest
import respx

from jira_agent.auth import credentials, oauth, token_login
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.auth.token_login import TokenFormServer, TokenLoginError, TokenRejected
from jira_agent.cli import run
from jira_agent.client.http import JiraClient
from jira_agent.config import Profile, ProfileStore

SITE = "https://acme.atlassian.net"
MYSELF = f"{SITE}/rest/api/3/myself"
TOKEN = "ATATT3xFfGF0" + "x" * 30
REAL_CLIENT = httpx.Client


def local(url, **kw):
    return REAL_CLIENT(timeout=5).request(url=url, **kw)


def start(verify):
    server = TokenFormServer(SITE, verify)
    out = {}
    thread = threading.Thread(target=lambda: out.update(server.wait(10)), daemon=True)
    thread.start()
    return server, out, thread


def ok_verify(email, token):
    return {"accountId": "u1", "displayName": "Pat Example"}


def test_form_page_shows_site_and_atlassian_link_but_no_secret():
    server, out, thread = start(ok_verify)
    page = local(server.url, method="GET")
    assert page.status_code == 200
    assert SITE in page.text and token_login.TOKEN_PAGE_URL in page.text
    assert page.headers["Cache-Control"] == "no-store"
    assert "form-action 'self'" in page.headers["Content-Security-Policy"]
    local(f"{server.url}connect", method="POST", data={"email": "user@example.com", "token": TOKEN})
    thread.join(5)
    assert out["token"] == TOKEN


def test_successful_connect_returns_record_and_confirmation_page():
    server, out, thread = start(ok_verify)
    resp = local(
        f"{server.url}connect", method="POST", data={"email": " user@example.com ", "token": TOKEN}
    )
    thread.join(5)
    assert resp.status_code == 200 and "Pat Example" in resp.text and TOKEN not in resp.text
    assert (
        out["email"] == "user@example.com"
        and out["method"] == "api_token"
        and out["site_url"] == SITE
    )


def test_same_origin_browser_post_is_accepted_and_policy_keeps_origin_header():
    server, out, thread = start(ok_verify)
    page = local(server.url, method="GET")
    assert page.headers["Referrer-Policy"] == "same-origin"
    resp = local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": TOKEN},
        headers={"Origin": f"http://127.0.0.1:{server.port}"},
    )
    thread.join(5)
    assert resp.status_code == 200 and out["token"] == TOKEN


def test_pasted_token_whitespace_and_line_breaks_are_removed():
    server, out, thread = start(ok_verify)
    local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": f"{TOKEN[:15]}\n {TOKEN[15:]} "},
    )
    thread.join(5)
    assert out["token"] == TOKEN


def test_rejected_credentials_show_a_message_and_allow_retry():
    calls = []

    def verify(email, token):
        calls.append(token)
        if token == TOKEN:
            raise TokenRejected("Atlassian did not accept that email and token.")
        return {"displayName": "Pat"}

    server, out, thread = start(verify)
    bad = local(
        f"{server.url}connect", method="POST", data={"email": "user@example.com", "token": TOKEN}
    )
    assert (
        bad.status_code == 400
        and "did not accept" in bad.text
        and 'value="user@example.com"' in bad.text
    )
    assert TOKEN not in bad.text
    good_token = "y" * 30
    local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": good_token},
    )
    thread.join(5)
    assert out["token"] == good_token and len(calls) == 2


@pytest.mark.parametrize(
    "data,message",
    [
        ({"email": "nope", "token": TOKEN}, "email address"),
        ({"email": "user@example.com", "token": "short"}, "too short"),
        ({"email": "user@example.com"}, "too short"),
    ],
)
def test_invalid_input_is_explained_without_calling_jira(data, message):
    called = []
    server, out, thread = start(lambda e, t: called.append(1))
    resp = local(f"{server.url}connect", method="POST", data=data)
    assert resp.status_code == 400 and message in resp.text and not called
    server.result = {"stop": True}
    thread.join(5)


def test_wrong_secret_path_and_foreign_host_and_foreign_origin_are_refused():
    called = []
    server, out, thread = start(lambda e, t: called.append(1) or {})
    assert local(f"http://127.0.0.1:{server.port}/guess/", method="GET").status_code == 404
    assert local(f"http://127.0.0.1:{server.port}/", method="GET").status_code == 404
    assert local(server.url, method="GET", headers={"Host": "evil.example"}).status_code == 404
    forged = local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": TOKEN},
        headers={"Origin": "https://evil.example"},
    )
    assert forged.status_code == 404
    null_origin = local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": TOKEN},
        headers={"Origin": "null"},
    )
    assert null_origin.status_code == 404 and not called
    server.result = {"stop": True}
    thread.join(5)


def test_oversized_body_is_rejected():
    server, out, thread = start(ok_verify)
    try:
        resp = local(
            f"{server.url}connect",
            method="POST",
            data={"email": "user@example.com", "token": "a" * 20000},
        )
        assert resp.status_code == 400
    except httpx.TransportError:
        pass  # the server may close the connection before the client finishes sending (Windows)
    server.result = {"stop": True}
    thread.join(5)


def test_form_times_out():
    server = TokenFormServer(SITE, ok_verify)
    with pytest.raises(TokenLoginError, match="timed out"):
        server.wait(0.3)


@respx.mock
def test_verify_token_uses_basic_auth_against_the_site():
    route = respx.get(MYSELF).respond(json={"accountId": "u1"})
    token_login.verify_token(SITE, "user@example.com", TOKEN, httpx.Client())
    assert route.calls[0].request.headers["Authorization"] == credentials.basic_header(
        "user@example.com", TOKEN
    )


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_verify_token_rejects_bad_credentials(status):
    respx.get(MYSELF).respond(status)
    with pytest.raises(TokenRejected, match="plain 'Create API token'"):
        token_login.verify_token(SITE, "user@example.com", TOKEN, httpx.Client())


@respx.mock
def test_verify_token_reports_unreachable_site():
    respx.get(MYSELF).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TokenRejected, match="Could not reach"):
        token_login.verify_token(SITE, "user@example.com", TOKEN, httpx.Client())


def token_store():
    s = MemoryStore()
    s.set(
        token_key(SITE),
        {"method": "api_token", "email": "user@example.com", "token": TOKEN, "site_url": SITE},
    )
    return s


@respx.mock
def test_jira_client_uses_site_url_and_basic_auth_for_api_tokens():
    route = respx.get(f"{SITE}/rest/api/3/issue/PROJ-1").respond(json={"key": "PROJ-1"})
    assert JiraClient(SITE, token_store()).get("issue/PROJ-1") == {"key": "PROJ-1"}
    assert route.calls[0].request.headers["Authorization"].startswith("Basic ")


@respx.mock
def test_api_token_401_is_not_refreshed_and_asks_for_login():
    respx.get(f"{SITE}/rest/api/3/issue/PROJ-1").respond(401)
    refresh = respx.post(oauth.TOKEN_URL)
    with pytest.raises(oauth.AuthRequired):
        JiraClient(SITE, token_store()).get("issue/PROJ-1")
    assert not refresh.called


@respx.mock
def test_preflight_for_api_token_reports_identity_and_method():
    respx.get(MYSELF).respond(
        json={"accountId": "u1", "displayName": "Pat", "emailAddress": "user@example.com"}
    )
    me = credentials.preflight(SITE, token_store())
    assert me["method"] == "api_token" and me["display_name"] == "Pat" and me["cloud_id"] is None


@respx.mock
def test_oauth_records_without_a_method_field_still_work():
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
    assert credentials.method_of(s.get(token_key(SITE))) == "oauth"
    assert credentials.load(SITE, s).headers["Authorization"] == "Bearer t"


@pytest.fixture
def cli_env(tmp_path):
    store = ProfileStore(tmp_path / "home")
    store.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "kb")))
    store.set_last_used("p")
    return store, MemoryStore()


@respx.mock
def test_cli_login_defaults_to_the_token_form_and_never_prints_the_token(
    cli_env, monkeypatch, capsys
):
    store, secrets = cli_env
    respx.route(host="127.0.0.1").pass_through()
    respx.get(MYSELF).respond(json={"accountId": "u1", "displayName": "Pat Example"})

    def fake_browser(url):
        threading.Thread(
            target=lambda: REAL_CLIENT(timeout=5).post(
                f"{url}connect", data={"email": "user@example.com", "token": TOKEN}
            ),
            daemon=True,
        ).start()

    monkeypatch.setattr("jira_agent.cli.OPEN_BROWSER", fake_browser)
    assert run(["auth", "login"], store, secrets) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err
    assert '"method": "api_token"' in captured.out and "Pat Example" in captured.out
    assert secrets.get(token_key(SITE))["token"] == TOKEN
    assert run(["auth", "status"], store, secrets) == 0
    assert '"display_name": "Pat Example"' in capsys.readouterr().out
    assert run(["auth", "logout"], store, secrets) == 0
    assert secrets.get(token_key(SITE)) is None
