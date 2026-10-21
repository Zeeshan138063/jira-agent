import html
import secrets as _secrets
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import httpx

from jira_agent.auth.credentials import METHOD_TOKEN, basic_header
from jira_agent.auth.keychain import SecretStore, token_key

TOKEN_PAGE_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"
EXPIRY_CHOICES = (
    ("7", "7 days"),
    ("30", "30 days"),
    ("90", "90 days"),
    ("180", "6 months"),
    ("365", "1 year (Atlassian's default)"),
    ("", "I'm not sure"),
)
DEFAULT_EXPIRY_DAYS = "365"
MAX_BODY = 8192
MIN_TOKEN_CHARS = 20

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;"
    "color:#172b4d;line-height:1.5}h1{font-size:1.4rem}ol{padding-left:1.2rem}li{margin:.6rem 0}"
    "input{width:100%;padding:.55rem;margin:.25rem 0 .9rem;font-size:1rem;box-sizing:border-box}"
    "button{background:#0052cc;color:#fff;border:0;padding:.7rem 1.2rem;font-size:1rem;"
    "border-radius:4px;cursor:pointer}.err{background:#ffebe6;padding:.7rem;border-radius:4px}"
    ".ok{background:#e3fcef;padding:.7rem;border-radius:4px}small{color:#5e6c84}"
)
_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}


class TokenLoginError(Exception):
    pass


class TokenRejected(Exception):
    pass


def verify_token(site_url: str, email: str, token: str, http: httpx.Client) -> dict:
    try:
        resp = http.get(
            f"{site_url}/rest/api/3/myself",
            headers={"Authorization": basic_header(email, token), "Accept": "application/json"},
        )
    except httpx.HTTPError:
        raise TokenRejected(
            f"Could not reach {site_url}. Check your connection and try again."
        ) from None
    if resp.status_code in (401, 403):
        raise TokenRejected(
            "Atlassian did not accept that email and token. Use the email you sign in to Jira "
            "with, copy the whole token, and make sure you used the plain "
            "'Create API token' button (not 'Create API token with scopes')."
        )
    if resp.status_code >= 300:
        raise TokenRejected(f"{site_url} answered with an unexpected status ({resp.status_code}).")
    try:
        return resp.json()
    except ValueError:
        raise TokenRejected(f"{site_url} did not answer like a Jira site.") from None


def render_page(
    site_url: str,
    error: str | None = None,
    email: str = "",
    expires: str = DEFAULT_EXPIRY_DAYS,
) -> str:
    site = html.escape(site_url)
    options = "".join(
        f'<option value="{value}"{" selected" if value == expires else ""}>{label}</option>'
        for value, label in EXPIRY_CHOICES
    )
    notice = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><meta charset=utf-8><title>Connect Jira</title>
<style>{_STYLE}</style>
<h1>Connect the Jira Agent to {site}</h1>
{notice}
<ol>
<li><a href="{TOKEN_PAGE_URL}" target="_blank" rel="noopener noreferrer">
 Open Atlassian's API token page</a> (sign in if asked). Click <b>Create API token</b>,
 name it <i>jira-agent</i>, pick an expiry,
 click <b>Create</b>, then <b>Copy</b>.</li>
<li>Come back here, enter the email you use for Jira, and paste the token.</li>
</ol>
<form method="post" action="connect" autocomplete="off">
<label>Jira email<input name="email" type="email" required value="{html.escape(email)}"
 autocomplete="off"></label>
<label>API token<input name="token" type="password" required autocomplete="off"></label>
<label>Expiry you chose on Atlassian's page
<select name="expires" style="width:100%;padding:.55rem;margin:.25rem 0 .9rem">{options}</select>
</label>
<p><small>The agent uses this to remind you to renew before the token stops working.</small></p>
<button type="submit">Connect</button>
</form>
<p><small>The token is checked against {site} and saved in this computer's Keychain.
It is never shown again or sent anywhere else. You can revoke it any time on Atlassian's token
page.</small></p>"""


def render_done(name: str, site_url: str, expires_at: int | None = None) -> str:
    until = ""
    if expires_at:
        when = time.strftime("%d %b %Y", time.localtime(expires_at))
        until = f" Your token expires on <b>{when}</b>; the agent will remind you before then."
    return (
        f"<!doctype html><meta charset=utf-8><title>Connected</title><style>{_STYLE}</style>"
        f'<h1>Connected</h1><p class="ok">Signed in to {html.escape(site_url)} as '
        f"<b>{html.escape(name)}</b>.{until} You can close this tab and return to Claude.</p>"
    )


class _Handler(BaseHTTPRequestHandler):
    server: "TokenFormServer"

    def _reply(self, status: int, body: str):
        payload = body.encode()
        self.send_response(status)
        for key, value in _HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _trusted(self) -> bool:
        origin = self.headers.get("Origin")
        return self.headers.get("Host") == f"127.0.0.1:{self.server.port}" and origin in (
            None,
            f"http://127.0.0.1:{self.server.port}",
        )

    def do_GET(self):
        if not self._trusted() or self.path.split("?")[0].rstrip("/") != self.server.base:
            self._reply(404, "Not found")
            return
        self._reply(200, render_page(self.server.site_url))

    def do_POST(self):
        if not self._trusted() or self.path != f"{self.server.base}/connect":
            self._reply(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._reply(400, render_page(self.server.site_url, "That request was not valid."))
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        chosen = (form.get("expires") or [""])[0]
        days = int(chosen) if chosen in {v for v, _ in EXPIRY_CHOICES if v} else None
        email = (form.get("email") or [""])[0].strip()
        token = "".join((form.get("token") or [""])[0].split())
        problem = None
        if "@" not in email:
            problem = "Enter the email address you use for Jira."
        elif len(token) < MIN_TOKEN_CHARS:
            problem = "That token looks too short. Copy the whole token from Atlassian."
        if problem:
            self._reply(400, render_page(self.server.site_url, problem, email, chosen))
            return
        try:
            me = self.server.verify(email, token)
        except TokenRejected as e:
            self._reply(400, render_page(self.server.site_url, str(e), email, chosen))
            return
        name = me.get("displayName") or email
        created = int(time.time())
        expires_at = created + days * 86400 if days else None
        self.server.result = {
            "method": METHOD_TOKEN,
            "email": email,
            "token": token,
            "site_url": self.server.site_url,
            "account_id": me.get("accountId"),
            "display_name": me.get("displayName"),
            "created_at": created,
            "expires_at": expires_at,
        }
        self._reply(200, render_done(name, self.server.site_url, expires_at))

    def log_message(self, *args):
        pass


class TokenFormServer(HTTPServer):
    def __init__(self, site_url: str, verify: Callable[[str, str], dict], port: int = 0):
        super().__init__(("127.0.0.1", port), _Handler)
        self.site_url = site_url
        self.verify = verify
        self.port = self.server_address[1]
        self.secret = _secrets.token_urlsafe(32)
        self.base = f"/{self.secret}"
        self.result: dict | None = None
        self.timeout = 0.5

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.base}/"

    def wait(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        try:
            while self.result is None:
                if time.monotonic() > deadline:
                    raise TokenLoginError("timed out waiting for the sign-in form to be completed")
                self.handle_request()
        finally:
            self.server_close()
        return self.result


def login_with_token(
    site_url: str,
    store: SecretStore,
    http: httpx.Client | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    announce: Callable[[str], None] = print,
    timeout: float = 600.0,
) -> dict:
    http = http or httpx.Client(timeout=30)
    server = TokenFormServer(
        site_url, lambda email, token: verify_token(site_url, email, token, http)
    )
    announce(f"Opening your browser to connect Jira. If nothing opens, visit:\n{server.url}")
    open_browser(server.url)
    record = server.wait(timeout)
    store.set(token_key(site_url), record)
    return {k: v for k, v in record.items() if k != "token"}
