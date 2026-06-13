"""Тесты durability-конвейера RTMS: helpers сессии и stage-логика worker'а."""
import wave
from pathlib import Path

import os
import time

import pytest

from src.groq_whisper import MAX_UPLOAD_BYTES, transcribe
from src.rtms_session import (
    AUDIO_FRAME_SIZE,
    AUDIO_SAMPLE_RATE,
    OPUS_BITRATE,
    _extract_participant_key,
    build_mix_cmd,
    measure_mean_dbfs,
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


def test_frame_size_is_samples():
    # SDK ждёт число сэмплов: 16000 Hz × 20ms = 320. Эмпирически с 640
    # приём фреймов ломается (no audio frames). См. Task #17.
    assert AUDIO_FRAME_SIZE == 320


def test_measure_mean_dbfs_silence_vs_signal(tmp_path: Path):
    # Цифровая тишина: все нули → очень низкий dBFS (или -inf → None при парсе)
    silent = tmp_path / "silent.pcm"
    silent.write_bytes(b"\x00\x00" * AUDIO_SAMPLE_RATE)  # 1 сек нулей
    db_silent = measure_mean_dbfs(silent)
    # ffmpeg на чистых нулях может вернуть -91 или -inf (None) — оба = тишина
    assert db_silent is None or db_silent < -80

    # Громкий сигнал: близкие к пику сэмплы → высокий dBFS
    import struct
    loud = tmp_path / "loud.pcm"
    loud.write_bytes(struct.pack("<" + "h" * AUDIO_SAMPLE_RATE, *([20000] * AUDIO_SAMPLE_RATE)))
    db_loud = measure_mean_dbfs(loud)
    assert db_loud is not None and db_loud > -20


def test_build_mix_cmd_single_stream(tmp_path: Path):
    # Один поток → amix не нужен, только loudnorm
    cmd = build_mix_cmd([tmp_path / "s1.pcm"], tmp_path / "audio.ogg")
    joined = " ".join(cmd)
    assert "amix" not in joined
    assert "loudnorm" in joined
    assert "libopus" in cmd and OPUS_BITRATE in cmd
    assert cmd[-1].endswith("audio.ogg")


def test_build_mix_cmd_multi_stream(tmp_path: Path):
    # Несколько потоков → amix с normalize=0, два входа -i
    cmd = build_mix_cmd([tmp_path / "s1.pcm", tmp_path / "s2.pcm"], tmp_path / "audio.ogg")
    joined = " ".join(cmd)
    assert "amix=inputs=2" in joined
    assert "normalize=0" in joined
    assert cmd.count("-i") == 2


def test_extract_participant_key():
    class Meta:
        user_id = 42
    # объект с user_id
    assert _extract_participant_key((b"audio", Meta())) == "u42"
    # dict с userId
    assert _extract_participant_key((b"audio", {"userId": 7})) == "u7"
    # только bytes — fallback в один поток
    assert _extract_participant_key((b"audio",)) == "stream0"
    # целочисленный канал среди args
    assert _extract_participant_key((b"audio", 3)) == "c3"


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


def test_r2_backup_key_naming():
    from src.r2_backup import backup_key

    assert backup_key("2026-06-11", "abc123", ".ogg") == "rtms/2026-06-11/abc123.ogg"
    # WAV-fallback сохраняет своё расширение
    assert backup_key("2026-06-11", "abc123", ".wav") == "rtms/2026-06-11/abc123.wav"


def test_r2_is_configured_requires_all_vars(monkeypatch):
    from src.r2_backup import _REQUIRED_ENV, is_configured

    for k in _REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    assert is_configured() is False

    for k in _REQUIRED_ENV:
        monkeypatch.setenv(k, "x")
    assert is_configured() is True

    monkeypatch.setenv("R2_BUCKET", "")
    assert is_configured() is False


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
