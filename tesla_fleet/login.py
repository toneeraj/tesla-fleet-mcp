"""One-time OAuth login. Opens a browser, catches the redirect on localhost:8080, saves tokens."""

from __future__ import annotations

import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import AUTH_BASE, AUTHORIZE_URL, REDIRECT_URI, USER_SCOPES, load_settings
from .core import TokenStore


def authorize_url(client_id: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": USER_SCOPES,
            "state": state,
        }
    )


def wait_for_code(expected_state: str) -> str:
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            result["code"] = (query.get("code") or [""])[0]
            result["state"] = (query.get("state") or [""])[0]
            result["error"] = (query.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Tesla login received. You can close this tab.")

        def log_message(self, *args: object) -> None:
            pass

    port = urlparse(REDIRECT_URI).port or 8080
    with HTTPServer(("localhost", port), Handler) as server:
        while "code" not in result:
            server.handle_request()
    if result.get("error"):
        sys.exit(f"Tesla returned an error: {result['error']}")
    if result.get("state") != expected_state:
        sys.exit("State mismatch; login aborted.")
    if not result.get("code"):
        sys.exit("No authorization code received.")
    return result["code"]


def main() -> None:
    settings = load_settings()
    state = secrets.token_urlsafe(16)
    url = authorize_url(settings.client_id, state)
    print("Opening Tesla sign-in. If the browser doesn't open, visit:\n" + url)
    webbrowser.open(url)
    code = wait_for_code(state)
    with httpx.Client(timeout=30) as http:
        response = http.post(
            f"{AUTH_BASE}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "code": code,
                "audience": settings.api_base,
                "redirect_uri": REDIRECT_URI,
            },
        )
        response.raise_for_status()
        TokenStore(settings, http).save(response.json())
    print(f"Tokens saved to {settings.token_path}")


if __name__ == "__main__":
    main()
