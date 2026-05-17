"""Groq Whisper Large v3 — transcription."""
import os
import requests


def transcribe(
    audio_path: str,
    language: str = "ru",
    prompt: str | None = None,
) -> dict:
    """Return verbose_json with segments [{start, end, text}, ...].

    `prompt` grounds Whisper on proper names, technical terms, and punctuation
    style — significantly improves Russian-language recognition of names and
    project terminology. See default_meeting_prompt() for the standard context.
    """
    data = {
        "model": "whisper-large-v3",
        "response_format": "verbose_json",
        "language": language,
    }
    if prompt is not None:
        data["prompt"] = prompt

    with open(audio_path, "rb") as f:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            files={"file": (os.path.basename(audio_path), f, "audio/ogg")},
            data=data,
            timeout=600,
        )
    r.raise_for_status()
    return r.json()


def default_meeting_prompt() -> str:
    """Standard context prompt for CULT-internal meetings.

    Update this when team composition or active projects change significantly
    (people leave/join, projects retire). Sidecar does not fetch this from the
    vault dynamically — that would add a GitHub round-trip on every transcribe
    and the prompt changes rarely (~once per 6 months).
    """
    return (
        "Это деловое совещание холдинга CULT. "
        "Участники: Александр Хомич, Костя Мельников, Дима, Таня, Надя, Денис. "
        "Проекты: ЦУП (центр управления проектами), Snoopdoc, Get Context, "
        "dashboard, hiring, registry, alias-second-brain, meeting-bot. "
        "Технологии: Claude, Cloudflare, Hetzner, Whisper, Groq, RTMS, Attendee, "
        "Zoom Marketplace, GitHub, Obsidian, PlanFact, 1С, Telegram. "
        "Записываются итоги встречи: задачи, решения, открытые вопросы. "
        "Используй полную пунктуацию: запятые, точки, кавычки."
    )
