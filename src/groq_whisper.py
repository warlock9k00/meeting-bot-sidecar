"""Groq Whisper Large v3 — transcription."""
import os
import requests


def transcribe(audio_path: str, language: str = "ru") -> dict:
    """Return verbose_json with segments [{start, end, text}, ...]."""
    with open(audio_path, "rb") as f:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            files={"file": (os.path.basename(audio_path), f, "audio/ogg")},
            data={
                "model": "whisper-large-v3",
                "response_format": "verbose_json",
                "language": language,
            },
            timeout=600,
        )
    r.raise_for_status()
    return r.json()
