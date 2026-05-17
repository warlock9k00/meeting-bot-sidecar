"""Markdown source rendering — точная копия contract'а Worker'а.

Output идентичен sources/*.md что писал Worker раньше: тот же frontmatter,
тот же body shape, та же конвенция `## Транскрипт` и `[HH:MM:SS] **Имя:**`.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Iterable
import re


def format_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def detect_platform(url: str) -> str:
    u = url.lower()
    if "zoom.us" in u or "zoom.com" in u:
        return "zoom"
    if "meet.google.com" in u:
        return "google-meet"
    return "other"


def source_filename(date_str: str, bot: dict) -> str:
    url = bot.get("meeting_url", "")
    platform = detect_platform(url)
    if platform == "zoom":
        m = re.search(r"/j/(\d+)", url)
        meeting_id = m.group(1) if m else bot["id"]
    elif platform == "google-meet":
        m = re.search(r"meet\.google\.com/([\w-]+)", url)
        meeting_id = m.group(1) if m else bot["id"]
    else:
        meeting_id = bot["id"]
    return f"{date_str}-{platform}-{meeting_id}.md"


def yaml_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_source(
    bot: dict,
    participants: list,
    segments: Iterable[dict],
    started_at: str,
    ended_at: str,
) -> str:
    """Сборка markdown source identical to Worker's output."""
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    duration_min = max(1, round((ended - started).total_seconds() / 60))

    date_part = started.astimezone(timezone.utc).strftime("%Y-%m-%d")
    time_human = started.astimezone(timezone.utc).strftime("%H:%M")
    platform = detect_platform(bot.get("meeting_url", ""))
    title = bot.get("meeting_title") or "Meeting"
    names = ", ".join(p["name"] for p in participants) if participants else "—"

    fm_lines = [
        "---",
        "type: source",
        "kind: zoom_transcript",
        f"date: {date_part}",
        "project: null",
        "people: []",
        f"duration_min: {duration_min}",
        f"platform: {platform}",
        f"meeting_url: {bot['meeting_url']}",
        f"bot_id: {bot['id']}",
    ]
    if bot.get("meeting_title"):
        fm_lines.append(f"meeting_title: {yaml_string(bot['meeting_title'])}")
    fm_lines.append("---")
    frontmatter = "\n".join(fm_lines) + "\n\n"

    transcript_lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        transcript_lines.append(
            f"[{format_ts(seg['start'])}] **Участник:** {text}"
        )

    body = (
        f"# {title}\n\n"
        f"**Когда:** {date_part} {time_human} · "
        f"**Длительность:** {duration_min} мин · "
        f"**Платформа:** {platform}\n"
        f"**Участники:** {names}\n\n"
        f"## Транскрипт\n\n"
        + "\n\n".join(transcript_lines)
        + "\n"
    )

    return frontmatter + body


# ─── RTMS path ────────────────────────────────────────────────────────────────


def _slug_meeting_uuid(uuid: str) -> str:
    """Sanitize Zoom meeting_uuid for filesystem use.

    Zoom UUIDs contain `/` and `=` (base64-like) which are invalid in paths
    and inconvenient in URLs. Replace runs of non-alphanumeric chars with _
    and trim edges.
    """
    return re.sub(r"[^a-zA-Z0-9-]+", "_", uuid).strip("_") or "unknown"


def rtms_source_filename(date_str: str, meeting_uuid: str) -> str:
    """RTMS path filename — always zoom (RTMS is Zoom-only)."""
    return f"{date_str}-zoom-{_slug_meeting_uuid(meeting_uuid)}.md"


def _speaker_for_segment(seg_start_abs: float, speakers: list[dict]) -> str:
    """Find most-recent speaker event with ts <= seg_start_abs.

    Speakers list must be sorted ascending by ts (RTMS callbacks fire in order).
    Returns '?' if no speaker event preceded the segment (e.g. host monologue
    before first speaker change, or speaker timeline empty).
    """
    name = "?"
    for sp in speakers:
        sp_ts = sp.get("ts")
        if sp_ts is None:
            continue
        if sp_ts <= seg_start_abs:
            name = sp.get("user_name") or sp.get("user_id") or "?"
        else:
            break
    return name


def render_rtms_source(
    rtms_stream_id: str,
    meeting_uuid: str,
    segments,
    speakers: list[dict],
    started_at_unix: float,
    duration_sec: float,
) -> str:
    """Render an RTMS-captured meeting as v2 source markdown.

    Frontmatter mirrors the Attendee path (`render_source`) so vault tooling
    treats them identically — except `bot_id` is replaced by `rtms_stream_id`
    and `meeting_uuid` is added. Transcript lines use real speaker names from
    the RTMS active-speaker timeline (vs `**Участник:**` placeholder in the
    Attendee path).
    """
    started_dt = datetime.fromtimestamp(started_at_unix, tz=timezone.utc)
    duration_min = max(1, round(duration_sec / 60))

    date_part = started_dt.strftime("%Y-%m-%d")
    time_human = started_dt.strftime("%H:%M")

    unique_names = []
    seen = set()
    for sp in speakers:
        n = sp.get("user_name")
        if n and n not in seen:
            seen.add(n)
            unique_names.append(n)
    participants_str = ", ".join(unique_names) if unique_names else "—"

    fm_lines = [
        "---",
        "type: source",
        "kind: zoom_transcript",
        f"date: {date_part}",
        "project: null",
        "people: []",
        f"duration_min: {duration_min}",
        "platform: zoom",
        f"rtms_stream_id: {rtms_stream_id}",
        f"meeting_uuid: {yaml_string(meeting_uuid)}",
        "source_pipeline: rtms",
        "---",
    ]
    frontmatter = "\n".join(fm_lines) + "\n\n"

    transcript_lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        seg_start = seg.get("start", 0)
        seg_start_abs = started_at_unix + seg_start
        speaker = _speaker_for_segment(seg_start_abs, speakers)
        transcript_lines.append(
            f"[{format_ts(seg_start)}] **{speaker}:** {text}"
        )

    body = (
        f"# Meeting {date_part} {time_human}\n\n"
        f"**Когда:** {date_part} {time_human} · "
        f"**Длительность:** {duration_min} мин · "
        f"**Платформа:** zoom (RTMS)\n"
        f"**Участники:** {participants_str}\n\n"
        f"## Транскрипт\n\n"
        + "\n\n".join(transcript_lines)
        + "\n"
    )

    return frontmatter + body
