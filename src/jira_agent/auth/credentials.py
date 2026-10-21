import base64
import math
import time
from dataclasses import dataclass

import httpx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import SecretStore, token_key

METHOD_OAUTH = "oauth"
METHOD_TOKEN = "api_token"
EXPIRY_WARN_DAYS = 7
MAX_TOKEN_DAYS = 365  # Atlassian API tokens last 1 to 365 days


@dataclass
class Credentials:
    method: str
    base_url: str
    headers: dict


def basic_header(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def method_of(record: dict | None) -> str | None:
    if not record:
        return None
    return record.get("method", METHOD_OAUTH)


def token_expiry(record: dict | None, now: float | None = None) -> dict:
    """When an API token stops working, and whether it is time to renew it.

    states: ok, expiring (7 days or fewer), expired, unknown, not_applicable (OAuth).
    If the user did not say when it expires, the latest possible date (created + 365 days) is
    assumed, since Atlassian tokens never last longer.
    """
    if method_of(record) != METHOD_TOKEN:
        return {"state": "not_applicable"}
    now = now if now is not None else time.time()
    expires_at = record.get("expires_at")
    assumed = False
    if expires_at is None and record.get("created_at"):
        expires_at, assumed = record["created_at"] + MAX_TOKEN_DAYS * 86400, True
    if expires_at is None:
        return {"state": "unknown", "expires_at": None, "days_left": None, "assumed": False}
    days_left = math.floor((expires_at - now) / 86400)
    if expires_at <= now:
        state = "expired"
    elif days_left <= EXPIRY_WARN_DAYS:
        state = "expiring"
    else:
        state = "ok"
    return {
        "state": state,
        "expires_at": int(expires_at),
        "days_left": days_left,
        "assumed": assumed,
    }


def expiry_message(expiry: dict) -> str | None:
    if expiry.get("state") not in ("expiring", "expired"):
        return None
    when = time.strftime("%d %b %Y", time.localtime(expiry["expires_at"]))
    guess = " (assumed: the latest an Atlassian token can last)" if expiry.get("assumed") else ""
    if expiry["state"] == "expired":
        return (
            f"your Jira token should have expired on {when}{guess}; if Jira calls fail, "
            "sign in again with a new token"
        )
    days = expiry["days_left"]
    unit = "day" if days == 1 else "days"
    return (
        f"your Jira token expires in {days} {unit} (on {when}){guess}; "
        "create a new one before then and sign in again"
    )


def load(site_url: str, store: SecretStore, http: httpx.Client | None = None) -> Credentials:
    record = store.get(token_key(site_url))
    if not record:
        raise oauth.AuthRequired(f"not signed in to {site_url}; run `jira-agent auth login`")
    if method_of(record) == METHOD_TOKEN:
        return Credentials(
            METHOD_TOKEN,
            f"{record['site_url']}/rest/api/3/",
            {
                "Authorization": basic_header(record["email"], record["token"]),
                "Accept": "application/json",
            },
        )
    record = oauth.get_valid_token(site_url, store, http)
    return Credentials(
        METHOD_OAUTH,
        f"{oauth.API_BASE}/{record['cloud_id']}/rest/api/3/",
        {"Authorization": f"Bearer {record['access_token']}", "Accept": "application/json"},
    )


def on_unauthorized(site_url: str, store: SecretStore, http: httpx.Client) -> bool:
    if method_of(store.get(token_key(site_url))) == METHOD_OAUTH:
        oauth.refresh(site_url, store, http)
        return True
    return False


def preflight(site_url: str, store: SecretStore, http: httpx.Client | None = None) -> dict:
    http = http or httpx.Client(timeout=30)
    for attempt in (1, 2):
        creds = load(site_url, store, http)
        resp = http.get(f"{creds.base_url}myself", headers=creds.headers)
        if resp.status_code == 401 and attempt == 1 and on_unauthorized(site_url, store, http):
            continue
        if resp.status_code in (401, 403):
            raise oauth.AuthRequired(
                f"Jira rejected the credentials ({resp.status_code}); run `jira-agent auth login`"
            )
        me = oauth._check(resp, "identity check")
        record = store.get(token_key(site_url)) or {}
        return {
            "method": creds.method,
            "account_id": me.get("accountId"),
            "display_name": me.get("displayName"),
            "email": me.get("emailAddress"),
            "cloud_id": record.get("cloud_id"),
            "site_url": record.get("site_url", site_url),
            "expiry": token_expiry(record),
        }
    raise oauth.AuthRequired("could not authenticate")
