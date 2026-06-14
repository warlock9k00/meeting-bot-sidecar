"""One-shot: create a managed Zoom OAuth connection in Attendee (for OBF).

Runs a tiny local web server to catch the Zoom OAuth redirect, hands the code
to Attendee (which exchanges it for tokens server-side and fetches the Zoom
user), and prints the connection's user_id — the value to use as
OBF_CONNECTION_USER_ID for scripts/poc_join.py.

Env:
  ZOOM_CLIENT_ID     Zoom OAuth app client id (for the authorize URL)
  ATTENDEE_BASE_URL  e.g. https://attendee.context.select
  ATTENDEE_API_KEY   Attendee project API key
  REDIRECT_URI       must be registered on the Zoom app AND reachable by your
                     browser. Default http://localhost:8799/callback
  ZOOM_OAUTH_APP_ID  optional; only if >1 Zoom OAuth app in the project

Usage: python scripts/obf_connect.py
Open the printed URL, authorize with the Zoom account the bot should act on
behalf of, then copy the printed user_id.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

# Allow running as a plain script from the repo root (see poc_join.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import attendee  # noqa: E402

DEFAULT_REDIRECT = "http://localhost:8799/callback"
ZOOM_AUTHORIZE = "https://zoom.us/oauth/authorize"


def _authorize_url(client_id: str, redirect_uri: str) -> str:
    q = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    })
    return f"{ZOOM_AUTHORIZE}?{q}"


def _capture_code(redirect_uri: str) -> str:
    """Block on a one-request local server until Zoom redirects back with ?code."""
    parsed = urlparse(redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 80
    box = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            code = parse_qs(urlparse(self.path).query).get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if code:
                box["code"] = code
                self.wfile.write(b"<h3>Authorized. You can close this tab.</h3>")
            else:
                self.wfile.write(b"<h3>No code in callback.</h3>")

        def log_message(self, *args):
            pass  # keep the console quiet

    server = HTTPServer((host, port), Handler)
    while "code" not in box:
        server.handle_request()
    server.server_close()
    return box["code"]


def main() -> None:
    client_id = os.environ["ZOOM_CLIENT_ID"]
    redirect_uri = os.environ.get("REDIRECT_URI", DEFAULT_REDIRECT)
    app_id = os.environ.get("ZOOM_OAUTH_APP_ID") or None

    print("1) Open this URL and authorize with the Zoom account the bot acts on behalf of:\n")
    print("   " + _authorize_url(client_id, redirect_uri) + "\n")
    print(f"2) Waiting for the OAuth redirect on {redirect_uri} ...")

    code = _capture_code(redirect_uri)
    print("   got code, exchanging with Attendee ...")

    conn = attendee.create_zoom_oauth_connection(code, redirect_uri, app_id)
    uid = conn.get("user_id")
    print("\n=== Zoom OAuth connection created ===")
    print(f"   state:   {conn.get('state')}")
    print(f"   user_id: {uid}")
    print(f'\nNext:  OBF_CONNECTION_USER_ID={uid} python scripts/poc_join.py "<zoom_join_url>"')


if __name__ == "__main__":
    main()
