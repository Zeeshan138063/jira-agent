import json
import threading
import time

import httpx
import pytest
import respx

from jira_agent.auth import credentials, oauth, token_login
from jira_agent.auth.credentials import expiry_message, token_expiry
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.auth.token_login import EXPIRY_CHOICES, TokenFormServer
from jira_agent.cli import run
from jira_agent.config import Profile, ProfileStore

SITE = "https://acme.atlassian.net"
MYSELF = f"{SITE}/rest/api/3/myself"
TOKEN = "ATATT3xFfGF0" + "x" * 30
REAL_CLIENT = httpx.Client
DAY = 86400
NOW = 1_800_000_000.0


def rec(**kw):
    return {
        "method": "api_token",
        "email": "user@example.com",
        "token": TOKEN,
        "site_url": SITE,
        **kw,
    }


# --- the expiry rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "days_left,state",
    [(300, "ok"), (8, "ok"), (7, "expiring"), (1, "expiring"), (0, "expiring")],
)
def test_state_by_days_left(days_left, state):
    out = token_expiry(rec(expires_at=NOW + days_left * DAY + 60), NOW)
    assert out["state"] == state and out["days_left"] == days_left and out["assumed"] is False


def test_a_past_expiry_is_expired():
    out = token_expiry(rec(expires_at=NOW - 3 * DAY), NOW)
    assert out["state"] == "expired" and out["days_left"] < 0


def test_without_a_stated_expiry_the_latest_possible_date_is_assumed():
    young = token_expiry(rec(created_at=NOW - 100 * DAY), NOW)
    assert young["state"] == "ok" and young["assumed"] is True
    assert young["days_left"] == 265
    old = token_expiry(rec(created_at=NOW - 360 * DAY), NOW)
    assert old["state"] == "expiring" and old["assumed"] is True
    assert token_expiry(rec(created_at=NOW - 400 * DAY), NOW)["state"] == "expired"


def test_a_token_saved_before_expiry_tracking_is_simply_unknown():
    out = token_expiry(rec(), NOW)
    assert out["state"] == "unknown" and out["days_left"] is None
    assert expiry_message(out) is None


def test_oauth_records_have_no_expiry_to_track():
    oauth_record = {"access_token": "t", "refresh_token": "r", "cloud_id": "c"}
    assert token_expiry(oauth_record, NOW) == {"state": "not_applicable"}
    assert token_expiry(None, NOW) == {"state": "not_applicable"}


def test_messages_are_plain_and_mention_the_date_and_any_guess():
    soon = expiry_message(token_expiry(rec(expires_at=NOW + 3 * DAY + 60), NOW))
    assert "expires in 3 days" in soon and "create a new one" in soon
    one = expiry_message(token_expiry(rec(expires_at=NOW + DAY + 60), NOW))
    assert "in 1 day (" in one
    gone = expiry_message(token_expiry(rec(created_at=NOW - 400 * DAY), NOW))
    assert "should have expired" in gone and "assumed" in gone
    assert expiry_message(token_expiry(rec(expires_at=NOW + 90 * DAY), NOW)) is None


# --- the sign-in form asks, and records it ----------------------------------------------------


def local(url, **kw):
    return REAL_CLIENT(timeout=5).request(url=url, **kw)


def start(verify=lambda e, t: {"accountId": "u1", "displayName": "Pat Example"}):
    server = TokenFormServer(SITE, verify)
    out = {}
    thread = threading.Thread(target=lambda: out.update(server.wait(10)), daemon=True)
    thread.start()
    return server, out, thread


def test_the_form_offers_the_expiry_choices_with_atlassians_default_selected():
    server, out, thread = start()
    page = local(server.url, method="GET").text
    for value, label in EXPIRY_CHOICES:
        assert f'value="{value}"' in page and label in page
    assert '<option value="365" selected>' in page
    server.result = {"stop": True}
    thread.join(5)


@pytest.mark.parametrize("chosen,days", [("30", 30), ("7", 7), ("365", 365)])
def test_a_chosen_expiry_is_stored_with_the_token(chosen, days):
    server, out, thread = start()
    before = int(time.time())
    resp = local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": TOKEN, "expires": chosen},
    )
    thread.join(5)
    assert out["created_at"] >= before
    assert out["expires_at"] == out["created_at"] + days * DAY
    assert "Your token expires on" in resp.text and TOKEN not in resp.text


@pytest.mark.parametrize("chosen", ["", "999", "abc", "-5", "0"])
def test_not_sure_or_invalid_choices_store_no_expiry(chosen):
    server, out, thread = start()
    resp = local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": TOKEN, "expires": chosen},
    )
    thread.join(5)
    assert out["expires_at"] is None and out["created_at"] > 0
    assert "Your token expires on" not in resp.text


def test_a_missing_expiry_field_is_treated_as_not_sure():
    server, out, thread = start()
    local(f"{server.url}connect", method="POST", data={"email": "user@example.com", "token": TOKEN})
    thread.join(5)
    assert out["expires_at"] is None


def test_the_chosen_expiry_survives_a_rejected_attempt():
    def verify(email, token):
        raise token_login.TokenRejected("Atlassian did not accept that email and token.")

    server, out, thread = start(verify)
    bad = local(
        f"{server.url}connect",
        method="POST",
        data={"email": "user@example.com", "token": TOKEN, "expires": "30"},
    )
    assert bad.status_code == 400 and '<option value="30" selected>' in bad.text
    server.result = {"stop": True}
    thread.join(5)


# --- session check and status -----------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    store = ProfileStore(tmp_path / "home")
    store.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(tmp_path / "kb")))
    store.set_last_used("p")
    return store, MemoryStore()


def save_token(secrets, **kw):
    secrets.set(token_key(SITE), rec(**kw))


def check(env, capsys, *extra):
    store, secrets = env
    assert run(["session", "check", *extra], store, secrets) == 0
    return json.loads(capsys.readouterr().out)


def steps(out):
    return {s["step"]: s for s in out["next"]}


@respx.mock
def test_session_check_warns_without_blocking_when_the_token_is_about_to_expire(env, capsys):
    save_token(env[1], expires_at=time.time() + 3 * DAY + 60)
    respx.get(MYSELF).respond(json={"accountId": "u1", "displayName": "Pat"})
    out = check(env, capsys)
    renew = steps(out)["auth-renew"]
    assert renew["blocking"] is False and "expires in 3 days" in renew["reason"]
    assert out["ready"] is True and out["auth"]["expiry"]["state"] == "expiring"


@respx.mock
def test_session_check_is_quiet_for_a_healthy_token(env, capsys):
    save_token(env[1], expires_at=time.time() + 90 * DAY)
    respx.get(MYSELF).respond(json={"accountId": "u1"})
    out = check(env, capsys)
    assert "auth-renew" not in steps(out)
    assert out["auth"]["expiry"]["state"] == "ok" and out["auth"]["expiry"]["days_left"] >= 89


def test_the_warning_also_appears_offline(env, capsys):
    save_token(env[1], expires_at=time.time() + 2 * DAY + 60)
    out = check(env, capsys, "--offline")
    assert "auth-renew" in steps(out) and out["ready"] is True


@respx.mock
def test_an_expired_by_date_token_that_jira_still_accepts_is_a_warning_not_a_block(env, capsys):
    save_token(env[1], expires_at=time.time() - 2 * DAY)
    respx.get(MYSELF).respond(json={"accountId": "u1"})
    out = check(env, capsys)
    assert out["ready"] is True and "should have expired" in steps(out)["auth-renew"]["reason"]


@respx.mock
def test_a_token_jira_rejects_still_blocks_with_a_sign_in_step(env, capsys):
    save_token(env[1], expires_at=time.time() - 2 * DAY)
    respx.get(MYSELF).respond(401)
    out = check(env, capsys)
    assert out["ready"] is False and steps(out)["auth-login"]["blocking"] is True


@respx.mock
def test_a_token_saved_before_tracking_gives_no_warning_and_no_crash(env, capsys):
    save_token(env[1])
    respx.get(MYSELF).respond(json={"accountId": "u1"})
    out = check(env, capsys)
    assert out["auth"]["expiry"]["state"] == "unknown" and "auth-renew" not in steps(out)


@respx.mock
def test_auth_status_reports_the_expiry(env, capsys):
    save_token(env[1], expires_at=time.time() + 40 * DAY)
    respx.get(MYSELF).respond(json={"accountId": "u1", "displayName": "Pat"})
    assert run(["auth", "status"], *env) == 0
    assert json.loads(capsys.readouterr().out)["expiry"]["state"] == "ok"


# --- recording an expiry on a token that was saved earlier ------------------------------------


def test_auth_expiry_sets_a_date_on_an_existing_token_without_touching_the_token(env, capsys):
    save_token(env[1])
    assert run(["auth", "expiry", "--date", "2099-01-31"], *env) == 0
    out = json.loads(capsys.readouterr().out)["expiry"]
    assert out["state"] == "ok" and out["days_left"] > 365
    saved = env[1].get(token_key(SITE))
    assert saved["token"] == TOKEN and saved["created_at"] > 0 and saved["expires_at"] > 0


def test_auth_expiry_days_left_and_unknown(env, capsys):
    save_token(env[1])
    assert run(["auth", "expiry", "--days-left", "5"], *env) == 0
    assert json.loads(capsys.readouterr().out)["expiry"]["state"] == "expiring"
    assert run(["auth", "expiry", "--unknown"], *env) == 0
    assert env[1].get(token_key(SITE))["expires_at"] is None


def test_auth_expiry_needs_a_saved_api_token_and_a_valid_date(env, capsys):
    assert run(["auth", "expiry", "--days-left", "5"], *env) == 2
    save_token(env[1])
    with pytest.raises(SystemExit):
        run(["auth", "expiry"], *env)
    assert run(["auth", "expiry", "--date", "next tuesday"], *env) == 2


def test_oauth_sessions_do_not_get_expiry_steps(env, capsys):
    secrets = env[1]
    oauth.save_client(secrets, "cid", "csecret", 0)
    secrets.set(
        token_key(SITE),
        {
            "access_token": "t",
            "refresh_token": "r",
            "expires_at": time.time() - 10,
            "cloud_id": "c",
            "site_url": SITE,
        },
    )
    out = check(env, capsys, "--offline")
    assert "auth-renew" not in steps(out) and out["auth"]["expiry"] == {"state": "not_applicable"}
    assert credentials.method_of(secrets.get(token_key(SITE))) == "oauth"
