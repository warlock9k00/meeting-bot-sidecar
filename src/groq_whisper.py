"""Groq Whisper Large v3 — transcription."""
import os
import requests

# Лимит Groq на размер загружаемого файла — 25 MB; держим 1 MB запаса
# на multipart-оверхед. Opus 16k mono достигает лимита только после
# ~3 часов записи; несжатый WAV — уже после ~13 минут (исторический
# источник HTTP 413 и потерянных встреч).
MAX_UPLOAD_BYTES = 24 * 1024 * 1024


def transcribe(
    audio_path: str,
    language: str = "ru",
    prompt: str | None = None,
) -> dict:
    """Return verbose_json with segments [{start, end, text}, ...].

    `prompt` grounds Whisper on proper names, technical terms, and punctuation
    style — significantly improves Russian-language recognition of names and
    project terminology. See default_meeting_prompt() for the standard context.

    Raises ValueError before upload if the file exceeds MAX_UPLOAD_BYTES —
    fail loud с понятной причиной вместо HTTP 413 от Groq.
    """
    size = os.path.getsize(audio_path)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"audio file is {size} bytes — exceeds Groq upload limit "
            f"({MAX_UPLOAD_BYTES} bytes). Запись длиннее ~3 часов в Opus 16k "
            f"или компрессия не отработала (WAV fallback?). Сырой PCM сохранён "
            f"рядом для ручного восстановления."
        )

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
