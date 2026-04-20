"""Attendee API client — bot meta, recording presigned URL, participants."""
import os
import requests


def _headers():
    return {"Authorization": f"Token {os.environ['ATTENDEE_API_KEY']}"}


def _base():
    return os.environ["ATTENDEE_BASE_URL"].rstrip("/")


def get_bot(bot_id: str) -> dict:
    r = requests.get(f"{_base()}/api/v1/bots/{bot_id}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def get_recording_url(bot_id: str) -> str:
    r = requests.get(
        f"{_base()}/api/v1/bots/{bot_id}/recording",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["url"]


def get_participants(bot_id: str) -> list[dict]:
    """DRF-paginated. Filter out the bot itself."""
    url = f"{_base()}/api/v1/bots/{bot_id}/participants"
    out = []
    while url:
        r = requests.get(url, headers=_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        for p in data.get("results", []):
            if p.get("is_the_bot"):
                continue
            out.append(p)
        url = data.get("next")
    return out


def is_final_state(bot: dict) -> bool:
    state = bot.get("state", "")
    if state in ("ended", "completed"):
        return True
    events = bot.get("events", [])
    return any(e.get("type") == "post_processing_completed" for e in events)


def event_timestamps(bot: dict) -> tuple[str, str]:
    """Return (started_at, ended_at) ISO strings."""
    events = bot.get("events", [])
    started = next((e["created_at"] for e in events if e["type"] == "joined_meeting"), None)
    ended = next((e["created_at"] for e in reversed(events) if e["type"] in (
        "left_meeting", "meeting_ended", "post_processing_completed"
    )), None)
    if not started:
        started = events[0]["created_at"]
    if not ended:
        ended = events[-1]["created_at"]
    return started, ended
