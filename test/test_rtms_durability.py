"""Тесты durability-конвейера RTMS: helpers сессии и stage-логика worker'а."""
import wave
from pathlib import Path

import os
import time

import pytest

from src.groq_whisper import MAX_UPLOAD_BYTES, transcribe
from src.rtms_session import (
    AUDIO_SAMPLE_RATE,
    OPUS_BITRATE,
    build_compress_cmd,
    pcm_to_wav,
)
from src.rtms_worker import (
    GC_MAX_AGE_DAYS,
    _read_marker,
    _write_marker,
    gc_stale_artifacts,
)


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


def test_build_compress_cmd_flags(tmp_path: Path):
    cmd = build_compress_cmd(tmp_path / "raw.pcm", tmp_path / "audio.ogg")
    # raw PCM требует явного формата на входе
    assert ["-f", "s16le"] == cmd[cmd.index("-f"):cmd.index("-f") + 2]
    assert "libopus" in cmd
    assert OPUS_BITRATE in cmd
    assert "loudnorm" in " ".join(cmd)
    assert cmd[-1].endswith("audio.ogg")


def test_transcribe_rejects_oversized_file(tmp_path: Path):
    big = tmp_path / "big.ogg"
    # sparse-файл: размер больше лимита без записи реальных байтов
    with big.open("wb") as f:
        f.truncate(MAX_UPLOAD_BYTES + 1)

    with pytest.raises(ValueError, match="exceeds Groq upload limit"):
        transcribe(str(big))


def test_marker_roundtrip(tmp_path: Path):
    assert _read_marker(tmp_path, "capture") is None

    data = {"duration_sec": 12.5, "speakers": [{"user_name": "Алекс"}]}
    _write_marker(tmp_path, "capture", data)

    assert _read_marker(tmp_path, "capture") == data
    # tmp-файл атомарной записи не должен оставаться
    assert not (tmp_path / "capture.json.tmp").exists()


def test_corrupted_marker_means_stage_not_done(tmp_path: Path):
    (tmp_path / "transcript.json").write_text("{обрезанный json")
    assert _read_marker(tmp_path, "transcript") is None


def test_gc_removes_only_stale_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.rtms_worker.TMP_BASE", str(tmp_path))
    base = tmp_path / "rtms"

    stale = base / "old-stream"
    stale.mkdir(parents=True)
    (stale / "raw_audio.pcm").write_bytes(b"x")
    old = time.time() - (GC_MAX_AGE_DAYS + 1) * 86400
    os.utime(stale, (old, old))

    fresh = base / "fresh-stream"
    fresh.mkdir(parents=True)

    assert gc_stale_artifacts() == 1
    assert not stale.exists()
    assert fresh.exists()
