"""RTMS session — capture audio + speaker timeline from one Zoom RTMS stream.

Wraps native zoom/rtms SDK with synchronous join_and_capture() API. Audio
buffered in memory as raw PCM 16kHz mono int16 LE; speaker events collected
with absolute timestamps. On onLeave or timeout, finalize() encodes WAV +
runs ffmpeg loudnorm, returning paths + speaker timeline for downstream
alignment with Whisper segments.

SDK occasionally segfaults — callers should run each session in a subprocess
(see rtms_worker.py) for crash isolation.
"""
import json
import subprocess
import threading
import time
import wave
from pathlib import Path

import rtms


AUDIO_SAMPLE_RATE = 16000
AUDIO_FRAME_SIZE = 320  # 16000 Hz × 20ms × mono
AUDIO_DURATION_MS = 20

# Broadcast loudness normalization (EBU R128) — critically boosts quiet
# recordings before Whisper. PoC: avg volume 1.44% → 4.55% on test meeting.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"

DEFAULT_TIMEOUT_SEC = 7200


class RtmsSession:
    """One Zoom RTMS stream — capture audio + speakers in memory."""

    def __init__(self, payload: dict, output_dir: Path):
        self.payload = payload
        self.rtms_stream_id = (
            payload.get("rtms_stream_id") or payload.get("meeting_uuid", "unknown")
        )
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.audio_chunks: list[bytes] = []
        self.speakers: list[dict] = []
        self.started_at: float = 0.0

        self.client = rtms.Client()
        self._done = threading.Event()

    def _setup_callbacks(self):
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
                    self.audio_chunks.append(bytes(a))
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
        """Join RTMS stream, block until onLeave or timeout."""
        self._setup_callbacks()
        self.started_at = time.time()
        self.client.join(self.payload)
        self._done.wait(timeout=timeout)

    def finalize(self) -> dict:
        """Encode captured PCM → WAV → loudnorm WAV. Falls back to raw WAV
        if ffmpeg is missing or fails."""
        audio_bytes = b"".join(self.audio_chunks)
        pcm_path = self.output_dir / "raw_audio.pcm"
        pcm_path.write_bytes(audio_bytes)

        wav_path = self.output_dir / "audio.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(AUDIO_SAMPLE_RATE)
            wf.writeframes(audio_bytes)

        normalized_wav_path = self.output_dir / "audio_normalized.wav"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(wav_path),
                 "-af", LOUDNORM_FILTER,
                 "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1",
                 str(normalized_wav_path)],
                check=True, capture_output=True, timeout=60,
            )
            wav_for_whisper = normalized_wav_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            wav_for_whisper = wav_path

        speakers_path = self.output_dir / "speaker-timeline.json"
        speakers_path.write_text(json.dumps(self.speakers, ensure_ascii=False, indent=2))

        return {
            "rtms_stream_id": self.rtms_stream_id,
            "wav_path": str(wav_path),
            "wav_for_whisper": str(wav_for_whisper),
            "pcm_path": str(pcm_path),
            "speakers_path": str(speakers_path),
            "speakers": self.speakers,
            "started_at": self.started_at,
            "audio_bytes_count": len(audio_bytes),
            "duration_sec": len(audio_bytes) / (AUDIO_SAMPLE_RATE * 2),
        }
