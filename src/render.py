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
