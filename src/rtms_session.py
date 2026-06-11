"""RTMS session — capture audio + speaker timeline from one Zoom RTMS stream.

Wraps native zoom/rtms SDK with synchronous join_and_capture() API. Audio
стримится на диск инкрементально (raw PCM 16kHz mono int16 LE) — краш
процесса посреди встречи теряет максимум несколько секунд звука, а не всю
встречу. Speaker events collected with absolute timestamps. On onLeave or
timeout, finalize() encodes WAV + runs ffmpeg loudnorm, returning paths +
speaker timeline for downstream alignment with Whisper segments.

SDK occasionally segfaults — callers should run each session in a subprocess
(see rtms_worker.py) for crash isolation.
"""
import json
import subprocess
import threading
import time
import wave
from pathlib import Path


AUDIO_SAMPLE_RATE = 16000
AUDIO_FRAME_SIZE = 320  # 16000 Hz × 20ms × mono
AUDIO_DURATION_MS = 20

# Кадры приходят каждые 20 мс → 50 кадров/сек. Flush на диск каждые 250
# кадров (~5 сек): при краше процесса теряем максимум 5 секунд звука.
FLUSH_EVERY_FRAMES = 250

# Broadcast loudness normalization (EBU R128) — critically boosts quiet
# recordings before Whisper. PoC: avg volume 1.44% → 4.55% on test meeting.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"

# Opus 16 kbps mono ≈ 7 MB/час. Час несжатого PCM ≈ 115 MB и не влезает в
# лимит Groq на файл (25 MB) — главный исторический убийца длинных встреч
# (HTTP 413 Payload Too Large, 26 потерянных записей за май–июнь 2026).
OPUS_BITRATE = "16k"

# Таймаут компрессии: база + длительность/фактор. loudnorm+opus работает
# ~30-60× realtime на cx23, фактор 10 даёт многократный запас.
COMPRESS_TIMEOUT_BASE_SEC = 120
COMPRESS_SPEED_FACTOR = 10

DEFAULT_TIMEOUT_SEC = 7200

# Fail-fast on connection failure: rtms SDK raises errors in its event loop
# thread, not in the calling thread — client.join() returns successfully even
# when alloc/join actually failed. onAudioData fires every 20ms while the
# stream is alive (regardless of silence vs speech), so 30s without frames
# means the connection never established.
INITIAL_AUDIO_TIMEOUT_SEC = 30


def pcm_to_wav(pcm_path: Path, wav_path: Path) -> None:
    """Stream-конвертация raw PCM → WAV чанками — часы аудио не грузим в RAM."""
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(AUDIO_SAMPLE_RATE)
        with pcm_path.open("rb") as src:
            while chunk := src.read(1 << 20):
                wf.writeframes(chunk)


def build_compress_cmd(pcm_path: Path, ogg_path: Path) -> list[str]:
    """ffmpeg: raw PCM → loudnorm → Opus 16 kbps mono, один проход."""
    return [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1",
        "-i", str(pcm_path),
        "-af", LOUDNORM_FILTER,
        "-c:a", "libopus", "-b:a", OPUS_BITRATE,
        str(ogg_path),
    ]


class RtmsSession:
    """One Zoom RTMS stream — capture audio + speakers, стримя PCM на диск."""

    def __init__(self, payload: dict, output_dir: Path):
        # Lazy import: native SDK не нужен для импорта модуля (тесты helpers
        # гоняются без установленного rtms-пакета).
        import rtms

        self._rtms = rtms
        self.payload = payload
        self.rtms_stream_id = (
            payload.get("rtms_stream_id") or payload.get("meeting_uuid", "unknown")
        )
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.pcm_path = self.output_dir / "raw_audio.pcm"
        self._pcm_file = self.pcm_path.open("wb")
        self._pcm_lock = threading.Lock()
        self.audio_bytes_count = 0
        self._frames_since_flush = 0

        self.speakers: list[dict] = []
        self.started_at: float = 0.0
        self._first_audio_at: float | None = None

        self.client = rtms.Client()
        self._done = threading.Event()

    def _setup_callbacks(self):
        rtms = self._rtms
        audio_params = rtms.AudioParams(
            content_type=rtms.AudioContentType["RAW_AUDIO"],
            codec=rtms.AudioCodec["L16"],
            sample_rate=rtms.AudioSampleRate["SR_16K"],
            channel=rtms.AudioChannel["MONO"],
            data_opt=rtms.AudioDataOption["AUDIO_MIXED_STREAM"],
            duration=AUDIO_DURATION_MS,
            frame_size=AUDIO_FRAME_SIZE,
        )
        self.client.setAudioParams(audio_params)

        # SDK varies arg count/types between releases — universal *args
        # extraction is more robust than positional signature.
        @self.client.onAudioData
        def _on_audio(*args, **kwargs):
            for a in args:
                if isinstance(a, (bytes, bytearray, memoryview)):
                    data = bytes(a)
                    with self._pcm_lock:
                        if self._pcm_file.closed:
                            return
                        if self._first_audio_at is None:
                            self._first_audio_at = time.time()
                        self._pcm_file.write(data)
                        self.audio_bytes_count += len(data)
                        self._frames_since_flush += 1
                        if self._frames_since_flush >= FLUSH_EVERY_FRAMES:
                            self._pcm_file.flush()
                            self._frames_since_flush = 0
                    return

        @self.client.onActiveSpeakerEvent
        def _on_speaker(*args, **kwargs):
            entry: dict = {}
            for a in args:
                if hasattr(a, "user_name"):
                    entry["user_name"] = getattr(a, "user_name", None)
                    entry["user_id"] = getattr(a, "user_id", None)
                    entry["ts"] = getattr(a, "timestamp", None)
                    break
                if isinstance(a, dict):
                    entry["user_name"] = a.get("user_name") or a.get("userName")
                    entry["user_id"] = a.get("user_id") or a.get("userId")
                    entry["ts"] = a.get("timestamp")
                    break
            if entry:
                self.speakers.append(entry)

        @self.client.onLeave
        def _on_leave(reason):
            self._done.set()

    def join_and_capture(self, timeout: int = DEFAULT_TIMEOUT_SEC) -> None:
        """Join RTMS stream, block until onLeave or timeout.

        Raises RuntimeError if no audio arrives within INITIAL_AUDIO_TIMEOUT_SEC
        — protects against silent SDK join failures (errors in event loop thread
        don't propagate to caller). Without this check, a bad payload would
        block for the full DEFAULT_TIMEOUT_SEC (2 hours).
        """
        self._setup_callbacks()
        self.started_at = time.time()
        self.client.join(self.payload)

        # Phase 1: liveness check — wait for first audio frame, early leave,
        # or initial timeout. while-else: else runs only if loop exits via
        # the condition (deadline passed), not via break.
        deadline = self.started_at + INITIAL_AUDIO_TIMEOUT_SEC
        while time.time() < deadline:
            if self._first_audio_at is not None or self._done.is_set():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"no audio frames within {INITIAL_AUDIO_TIMEOUT_SEC}s after join "
                f"— likely join failure (check SDK logs above for cause)"
            )

        # Phase 2: connection is alive — wait for natural end (onLeave) or
        # absolute timeout for long meetings.
        self._done.wait(timeout=timeout)

    def finalize(self) -> dict:
        """Закрыть PCM-файл, сжать в Opus (loudnorm + 16k mono) для Whisper.
        Fallback на plain WAV если ffmpeg недоступен или упал."""
        with self._pcm_lock:
            if not self._pcm_file.closed:
                self._pcm_file.flush()
                self._pcm_file.close()

        audio_bytes_count = self.pcm_path.stat().st_size
        duration_sec = audio_bytes_count / (AUDIO_SAMPLE_RATE * 2)

        ogg_path = self.output_dir / "audio.ogg"
        timeout = COMPRESS_TIMEOUT_BASE_SEC + int(duration_sec / COMPRESS_SPEED_FACTOR)
        try:
            subprocess.run(
                build_compress_cmd(self.pcm_path, ogg_path),
                check=True, capture_output=True, timeout=timeout,
            )
            audio_for_whisper = ogg_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            # Лучше несжатый звук, чем потерянная встреча: короткие записи
            # пройдут в Groq и так, длинные отсечёт size guard в worker'е
            # с внятной ошибкой (а PCM-мастер останется для ручного спасения).
            wav_path = self.output_dir / "audio.wav"
            pcm_to_wav(self.pcm_path, wav_path)
            audio_for_whisper = wav_path

        speakers_path = self.output_dir / "speaker-timeline.json"
        speakers_path.write_text(json.dumps(self.speakers, ensure_ascii=False, indent=2))

        return {
            "rtms_stream_id": self.rtms_stream_id,
            "audio_for_whisper": str(audio_for_whisper),
            "pcm_path": str(self.pcm_path),
            "speakers_path": str(speakers_path),
            "speakers": self.speakers,
            "started_at": self.started_at,
            "audio_bytes_count": audio_bytes_count,
            "duration_sec": duration_sec,
        }
