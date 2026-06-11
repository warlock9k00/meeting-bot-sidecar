"""Тесты durability-конвейера RTMS: helpers сессии и stage-логика worker'а."""
import wave
from pathlib import Path

from src.rtms_session import pcm_to_wav, AUDIO_SAMPLE_RATE


def test_pcm_to_wav_roundtrip(tmp_path: Path):
    # 1 секунда тишины: 16000 сэмплов × 2 байта
    pcm = tmp_path / "raw.pcm"
    pcm.write_bytes(b"\x00\x00" * AUDIO_SAMPLE_RATE)

    wav = tmp_path / "out.wav"
    pcm_to_wav(pcm, wav)

    with wave.open(str(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == AUDIO_SAMPLE_RATE
        assert wf.getnframes() == AUDIO_SAMPLE_RATE


def test_pcm_to_wav_empty(tmp_path: Path):
    pcm = tmp_path / "raw.pcm"
    pcm.write_bytes(b"")

    wav = tmp_path / "out.wav"
    pcm_to_wav(pcm, wav)

    with wave.open(str(wav), "rb") as wf:
        assert wf.getnframes() == 0
