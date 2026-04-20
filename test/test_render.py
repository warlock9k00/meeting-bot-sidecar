from src.render import render_source, format_ts, source_filename, detect_platform


def test_format_ts():
    assert format_ts(0) == "00:00:00"
    assert format_ts(65) == "00:01:05"
    assert format_ts(3725) == "01:02:05"


def test_detect_platform():
    assert detect_platform("https://us02web.zoom.us/j/123") == "zoom"
    assert detect_platform("https://meet.google.com/abc-defg-hij") == "google-meet"
    assert detect_platform("https://teams.microsoft.com/foo") == "other"


def test_source_filename_zoom():
    bot = {"id": "bot_X", "meeting_url": "https://us02web.zoom.us/j/89948953550?pwd=foo"}
    assert source_filename("2026-04-19", bot) == "2026-04-19-zoom-89948953550.md"


def test_source_filename_fallback():
    bot = {"id": "bot_ABC123", "meeting_url": "https://other.example.com/foo"}
    assert source_filename("2026-04-19", bot) == "2026-04-19-other-bot_ABC123.md"


def test_render_source_basic():
    bot = {
        "id": "bot_X",
        "meeting_url": "https://us02web.zoom.us/j/123?pwd=x",
        "meeting_title": None,
    }
    participants = [{"name": "Aleksandr Khomich"}]
    segments = [
        {"start": 0.0, "text": "Привет"},
        {"start": 30.5, "text": "Как дела"},
    ]
    started_at = "2026-04-20T13:00:48Z"
    ended_at = "2026-04-20T13:37:08Z"

    md = render_source(bot, participants, segments, started_at, ended_at)

    assert "type: source" in md
    assert "kind: zoom_transcript" in md
    assert "date: 2026-04-20" in md
    assert "duration_min: 36" in md  # 36:20 → round → 36
    assert "platform: zoom" in md
    assert "bot_id: bot_X" in md
    assert "meeting_title:" not in md  # None → omit
    assert "# Meeting" in md
    assert "**Участники:** Aleksandr Khomich" in md
    assert "## Транскрипт" in md
    assert "[00:00:00] **Участник:** Привет" in md
    assert "[00:00:30] **Участник:** Как дела" in md


def test_render_source_with_title_and_no_participants():
    bot = {
        "id": "bot_Y",
        "meeting_url": "https://us02web.zoom.us/j/9?pwd=y",
        "meeting_title": "Q2 sync",
    }
    md = render_source(bot, [], [{"start": 0.0, "text": "hi"}],
                       "2026-04-20T13:00:00Z", "2026-04-20T13:05:00Z")
    assert 'meeting_title: "Q2 sync"' in md
    assert "# Q2 sync" in md
    assert "**Участники:** —" in md
    assert "duration_min: 5" in md


def test_render_source_yaml_escape_quotes():
    bot = {
        "id": "bot_Z",
        "meeting_url": "https://us02web.zoom.us/j/9",
        "meeting_title": 'Talk with "Misha" today',
    }
    md = render_source(bot, [], [{"start": 0, "text": "x"}],
                       "2026-04-20T10:00:00Z", "2026-04-20T10:01:00Z")
    assert 'meeting_title: "Talk with \\"Misha\\" today"' in md
