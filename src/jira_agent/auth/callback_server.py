import html
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

CALLBACK_PATH = "/callback"

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>jira-agent</title>"
    "<body style='font-family:system-ui;margin:3rem'><h2>{title}</h2><p>{body}</p></body>"
)


class CallbackError(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._send(404, "Not found", "")
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if "code" not in params and "error" not in params:
            self._send(400, "Bad request", "Missing authorization response.")
            return
        if params.get("state") != getattr(self.server, "expected_state", None):
            self._send(400, "Ignored", "This response did not match the sign-in in progress.")
            return
        self.server.result = params
        if "error" in params:
            self._send(400, "Sign-in failed", "You can close this tab and return to Claude.")
        else:
            self._send(200, "Signed in", "You can close this tab and return to Claude.")

    def _send(self, status: int, title: str, body: str):
        payload = _PAGE.format(title=html.escape(title), body=html.escape(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class CallbackServer:
    def __init__(self, port: int):
        try:
            self._server = HTTPServer(("127.0.0.1", port), _Handler)
        except OSError as e:
            raise CallbackError(
                f"cannot listen on 127.0.0.1:{port} ({e.strerror}); "
                "free the port or choose another and update the OAuth app's callback URL"
            ) from e
        self._server.result = None
        self._server.expected_state = None
        self._server.timeout = 0.5
        self.port = self._server.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def wait(self, expected_state: str, timeout: float = 300.0) -> str:
        deadline = time.monotonic() + timeout
        self._server.expected_state = expected_state
        try:
            while self._server.result is None:
                if time.monotonic() > deadline:
                    raise CallbackError("timed out waiting for browser sign-in")
                self._server.handle_request()
        finally:
            self._server.server_close()
        result = self._server.result
        if "error" in result:
            detail = result.get("error_description", "")
            raise CallbackError(f"authorization denied: {result['error']} {detail}".strip())
        if result.get("state") != expected_state:
            raise CallbackError("state mismatch; ignoring this response")
        return result["code"]
