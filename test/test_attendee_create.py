from unittest.mock import patch, MagicMock
from src import attendee


def test_create_bot_payload_includes_obf():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"id": "bot_123"}
        resp.raise_for_status.return_value = None
        return resp

    with patch.dict("os.environ", {
        "ATTENDEE_BASE_URL": "https://attendee.example",
        "ATTENDEE_API_KEY": "k",
    }), patch("src.attendee.requests.post", fake_post):
        bot_id = attendee.create_bot(
            meeting_url="https://zoom.us/j/123?pwd=x",
            obf_connection_user_id="u-42",
            bot_name="Get Context",
        )

    assert bot_id == "bot_123"
    assert captured["url"].endswith("/api/v1/bots")
    z = captured["json"]["zoom_settings"]["onbehalf_token"]
    assert z["zoom_oauth_connection_user_id"] == "u-42"
    assert captured["json"]["meeting_url"].startswith("https://zoom.us/j/123")
    assert captured["json"]["bot_name"] == "Get Context"


def test_create_zoom_oauth_connection_payload():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        resp = MagicMock()
        resp.json.return_value = {"id": "conn_1", "user_id": "zu_99", "state": "connected"}
        resp.raise_for_status.return_value = None
        return resp

    with patch.dict("os.environ", {
        "ATTENDEE_BASE_URL": "https://attendee.example",
        "ATTENDEE_API_KEY": "k",
    }), patch("src.attendee.requests.post", fake_post):
        conn = attendee.create_zoom_oauth_connection(
            authorization_code="abc",
            redirect_uri="http://localhost:8799/callback",
        )

    assert conn["user_id"] == "zu_99"
    assert captured["url"].endswith("/api/v1/zoom_oauth_connections")
    body = captured["json"]
    assert body["authorization_code"] == "abc"
    assert body["redirect_uri"] == "http://localhost:8799/callback"
    assert body["is_onbehalf_token_supported"] is True
    # app id omitted when not supplied
    assert "zoom_oauth_app_id" not in body
