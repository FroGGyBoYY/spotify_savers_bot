from __future__ import annotations

import base64
import json
import os
import secrets
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv


DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"


class CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    expected_state: str = ""

    def log_message(self, *_: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        state = query.get("state", [""])[0]
        if state != self.expected_state:
            self.error = "Invalid state in Spotify callback"
        elif "error" in query:
            self.error = query["error"][0]
        else:
            self.code = query.get("code", [None])[0]

        body = (
            "<html><body><h2>Spotify token received.</h2>"
            "<p>You can close this tab and return to the terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


def main() -> None:
    load_dotenv(override=True, encoding="utf-8-sig")
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip() or DEFAULT_REDIRECT_URI

    if not client_id or not client_secret:
        raise SystemExit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env first.")

    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    if parsed_redirect.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Use a local redirect URI, for example http://127.0.0.1:8080/callback")

    state = secrets.token_urlsafe(24)
    CallbackHandler.expected_state = state
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "show_dialog": "true",
        }
    )
    auth_url = f"https://accounts.spotify.com/authorize?{query}"

    server = HTTPServer((parsed_redirect.hostname, parsed_redirect.port or 80), CallbackHandler)
    print("Opening Spotify authorization page...")
    print(auth_url)
    webbrowser.open(auth_url)
    server.handle_request()

    if CallbackHandler.error:
        raise SystemExit(f"Spotify authorization failed: {CallbackHandler.error}")
    if not CallbackHandler.code:
        raise SystemExit("Spotify did not return an authorization code.")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": CallbackHandler.code,
            "redirect_uri": redirect_uri,
        }
    ).encode()
    request = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=data,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise SystemExit(f"Spotify did not return refresh_token: {payload}")

    print("\nAdd this line to .env on the bot server:")
    print(f"SPOTIFY_USER_REFRESH_TOKEN={refresh_token}")


if __name__ == "__main__":
    main()
