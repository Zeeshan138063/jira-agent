import re
import time
from collections.abc import Callable

import httpx

from jira_agent.auth import credentials, oauth
from jira_agent.auth.keychain import SecretStore

RETRY_STATUS = {429, 502, 503, 504}
_UNSAFE_PATH = re.compile(r"(^|/)\.\.?(/|$)|[?#%\s\\]")


class JiraError(Exception):
    def __init__(self, status: int, messages: list[str]):
        self.status = status
        self.messages = messages
        super().__init__(f"Jira API error {status}: {'; '.join(messages) or 'no details'}")


def _error_messages(resp: httpx.Response) -> list[str]:
    try:
        body = resp.json()
    except ValueError:
        return [resp.text[:200]] if resp.text else []
    if not isinstance(body, dict):
        return []
    msgs = list(body.get("errorMessages") or [])
    msgs += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
    return msgs


class JiraClient:
    def __init__(
        self,
        site_url: str,
        store: SecretStore,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
    ):
        self.site_url = site_url
        self.store = store
        self.http = http or httpx.Client(timeout=30)
        self.sleep = sleep
        self.max_retries = max_retries
        self.stats = {"requests": 0, "retries": 0, "rate_limited": 0, "bytes": 0}

    @staticmethod
    def _url(base_url: str, path: str) -> str:
        if _UNSAFE_PATH.search(path):
            raise ValueError(f"refusing to call an unsafe API path: {path!r}")
        return f"{base_url}{path.lstrip('/')}"

    def request(self, method: str, path: str, *, params=None, json=None):
        refreshed = False
        attempt = 0
        while True:
            creds = credentials.load(self.site_url, self.store, self.http)
            resp = self.http.request(
                method,
                self._url(creds.base_url, path),
                params=params,
                json=json,
                headers=creds.headers,
            )
            self.stats["requests"] += 1
            self.stats["bytes"] += len(resp.content)
            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                if credentials.on_unauthorized(self.site_url, self.store, self.http):
                    continue
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                self.sleep(self._delay(resp, attempt))
                self.stats["retries"] += 1
                attempt += 1
                continue
            if resp.status_code == 401:
                raise oauth.AuthRequired(
                    "Jira rejected the credentials; run `jira-agent auth login`"
                )
            if 300 <= resp.status_code < 400:
                raise JiraError(resp.status_code, ["unexpected redirect; not following it"])
            if resp.status_code >= 400:
                raise JiraError(resp.status_code, _error_messages(resp))
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    @staticmethod
    def _delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(2.0**attempt, 30.0)

    def get(self, path: str, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: dict):
        return self.request("POST", path, json=body)

    def put(self, path: str, body: dict):
        return self.request("PUT", path, json=body)
