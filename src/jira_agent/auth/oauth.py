import time
import webbrowser
from collections.abc import Callable
from urllib.parse import urlencode

import httpx

from jira_agent.auth import pkce
from jira_agent.auth.callback_server import CallbackServer
from jira_agent.auth.keychain import CLIENT_KEY, SecretStore, token_key

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
API_BASE = "https://api.atlassian.com/ex/jira"
SCOPES = ("read:jira-work", "write:jira-work", "read:jira-user", "offline_access")
DEFAULT_PORT = 8765
EXPIRY_SKEW = 60


class AuthError(Exception):
    pass


class AuthRequired(AuthError):
    pass


def _check(resp: httpx.Response, what: str) -> dict:
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            pass
        raise AuthError(f"{what} failed ({resp.status_code}) {detail}".strip())
    return resp.json()


def save_client(store: SecretStore, client_id: str, client_secret: str, port: int) -> None:
    store.set(CLIENT_KEY, {"client_id": client_id, "client_secret": client_secret, "port": port})


def load_client(store: SecretStore) -> dict:
    client = store.get(CLIENT_KEY)
    if not client:
        raise AuthRequired("OAuth app not configured; run `jira-agent auth setup`")
    return client


def build_authorize_url(
    client_id: str, redirect_uri: str, state: str, challenge: str, scopes=SCOPES
) -> str:
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": " ".join(scopes),
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _tokens_from(body: dict, previous: dict | None = None) -> dict:
    refresh = body.get("refresh_token") or (previous or {}).get("refresh_token")
    if not refresh:
        raise AuthError("no refresh token returned; the app needs the offline_access scope")
    return {
        "access_token": body["access_token"],
        "refresh_token": refresh,
        "expires_at": time.time() + int(body.get("expires_in", 3600)),
        "scope": body.get("scope", ""),
    }


def find_site(resources: list[dict], site_url: str) -> dict:
    for r in resources:
        if r.get("url", "").rstrip("/").lower() == site_url.rstrip("/").lower():
            return r
    known = ", ".join(r.get("url", "?") for r in resources) or "none"
    raise AuthError(f"signed-in account has no access to {site_url} (accessible sites: {known})")


def login(
    site_url: str,
    store: SecretStore,
    http: httpx.Client | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    announce: Callable[[str], None] = print,
    timeout: float = 300.0,
    port: int | None = None,
) -> dict:
    client = load_client(store)
    http = http or httpx.Client(timeout=30)
    server = CallbackServer(client["port"] if port is None else port)
    verifier, state = pkce.generate_verifier(), pkce.generate_state()
    url = build_authorize_url(
        client["client_id"], server.redirect_uri, state, pkce.challenge_for(verifier)
    )
    announce(f"Opening your browser to sign in to Atlassian.\nIf nothing opens, visit:\n{url}")
    open_browser(url)
    code = server.wait(state, timeout)

    body = _check(
        http.post(
            TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "code": code,
                "redirect_uri": server.redirect_uri,
                "code_verifier": verifier,
            },
        ),
        "token exchange",
    )
    tokens = _tokens_from(body)
    resources = _check_list(
        http.get(RESOURCES_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    )
    site = find_site(resources, site_url)
    record = {**tokens, "cloud_id": site["id"], "site_url": site["url"].rstrip("/").lower()}
    store.set(token_key(site_url), record)
    return record


def _check_list(resp: httpx.Response) -> list[dict]:
    if resp.status_code >= 400:
        raise AuthError(f"listing accessible sites failed ({resp.status_code})")
    return resp.json()


def refresh(site_url: str, store: SecretStore, http: httpx.Client) -> dict:
    record = store.get(token_key(site_url))
    if not record:
        raise AuthRequired(f"not signed in to {site_url}; run `jira-agent auth login`")
    client = load_client(store)
    resp = http.post(
        TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": record["refresh_token"],
        },
    )
    if resp.status_code in (400, 401, 403):
        store.delete(token_key(site_url))
        raise AuthRequired(f"session for {site_url} expired; run `jira-agent auth login`")
    record = {**record, **_tokens_from(_check(resp, "token refresh"), record)}
    store.set(token_key(site_url), record)
    return record


def get_valid_token(site_url: str, store: SecretStore, http: httpx.Client | None = None) -> dict:
    http = http or httpx.Client(timeout=30)
    record = store.get(token_key(site_url))
    if not record:
        raise AuthRequired(f"not signed in to {site_url}; run `jira-agent auth login`")
    if record["expires_at"] - EXPIRY_SKEW <= time.time():
        record = refresh(site_url, store, http)
    return record


def preflight(site_url: str, store: SecretStore, http: httpx.Client | None = None) -> dict:
    from jira_agent.auth import credentials

    return credentials.preflight(site_url, store, http)


def logout(site_url: str, store: SecretStore) -> None:
    store.delete(token_key(site_url))
