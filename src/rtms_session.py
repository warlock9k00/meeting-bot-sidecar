"""RTMS session — capture per-participant audio from one Zoom RTMS stream.

Wraps native zoom/rtms SDK with synchronous join_and_capture() API.

Режим AUDIO_MULTI_STREAMS: каждый участник (включая авторизовавшего
приложение) приходит отдельным потоком. AUDIO_MIXED_STREAM давал тишину,
когда «слышно от других» было пусто — встреча один-на-один с тихим
собеседником или встреча, где говорит сам хост, выходили как −91 dBFS
(пустые фреймы) или вовсе без фреймов. Multi-stream не зависит от
active-speaker микса. См. Task #17 + research-отчёт 2026-06-13.

Аудио каждого участника стримится в свой PCM-файл на диске — краш
процесса теряет максимум несколько секунд. finalize() микширует все
потоки в один моно (ffmpeg amix) + loudnorm → Opus для Whisper.

SDK occasionally segfaults — callers should run each session in a subprocess
(see rtms_worker.py) for crash isolation.
"""
import json
import logging
import subprocess
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger(__name__)


AUDIO_SAMPLE_RATE = 16000
AUDIO_DURATION_MS = 20
# frame_size = число СЭМПЛОВ на кадр: 16000 Hz × 0.020 s = 320.
# ПРОВЕРЕНО эмпирически: с 320 фреймы приходят, с 640 (трактовка как байты)
# приём ломается полностью — «no audio frames within 30s» (тест 06-13 06:55).
AUDIO_FRAME_SIZE = AUDIO_SAMPLE_RATE * AUDIO_DURATION_MS // 1000  # 320

# Кадры приходят каждые 20 мс → 50 кадров/сек. Flush на диск каждые 250
# кадров (~5 сек): при краше процесса теряем максимум 5 секунд звука.
FLUSH_EVERY_FRAMES = 250

# Broadcast loudness normalization (EBU R128) — critically boosts quiet
# recordings before Whisper. PoC: avg volume 1.44% → 4.55% on test meeting.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"

# Opus 16 kbps mono ≈ 7 MB/час. Час несжатого PCM ≈ 115 MB и не влезает в
# лимит Groq на файл (25 MB) — исторический убийца длинных встреч (HTTP 413).
OPUS_BITRATE = "16k"

# Таймаут компрессии: база + длительность/фактор. loudnorm+opus работает
# ~30-60× realtime на cx23, фактор 10 даёт многократный запас.
COMPRESS_TIMEOUT_BASE_SEC = 120
COMPRESS_SPEED_FACTOR = 10

DEFAULT_TIMEOUT_SEC = 7200

# Fail-fast on connection failure: rtms SDK raises errors in its event loop
# thread, not in the calling thread — client.join() returns successfully even
# when alloc/join actually failed. onAudioData fires every 20ms while the
# stream is alive, so 30s without frames means the connection never established.
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


def build_mix_cmd(pcm_paths: list[Path], out_ogg: Path) -> list[str]:
    """ffmpeg: N raw-PCM потоков участников → amix → loudnorm → Opus mono.

    amix normalize=0 — не делить громкость на число входов (иначе на каждом
    добавленном участнике микс становится тише). При одном потоке amix не
    нужен, применяем только loudnorm."""
    cmd: list[str] = ["ffmpeg", "-y"]
    for p in pcm_paths:
        cmd += ["-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1", "-i", str(p)]
    n = len(pcm_paths)
    if n == 1:
        filt = LOUDNORM_FILTER
    else:
        filt = f"amix=inputs={n}:duration=longest:normalize=0,{LOUDNORM_FILTER}"
    cmd += ["-filter_complex", filt, "-c:a", "libopus", "-b:a", OPUS_BITRATE, str(out_ogg)]
    return cmd


def measure_mean_dbfs(pcm_path: Path) -> float | None:
    """Средняя громкость сырого PCM в dBFS через ffmpeg volumedetect.

    Диагностика silent-capture: −91 dB ≈ цифровая тишина, нормальная речь
    ≈ −30…−15 dB. Возвращает None если ffmpeg недоступен/упал."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-f", "s16le", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "1",
             "-i", str(pcm_path), "-af", "volumedetect", "-f", "null", "/dev/null"],
            capture_output=True, text=True, timeout=120,
        )
        for line in r.stderr.splitlines():
            if "mean_volume:" in line:
                return float(line.split("mean_volume:")[1].strip().split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError, FileNotFoundError):
        pass
    return None


SDK_LOG_DIR = Path("/app/logs")
# Маркеры отказа media-gateway в нативном логе SDK. reason: 960 = шлюз
# отверг handshake (несовпадение client_id/secret подписи, истёкший
# RTMS-доступ). Подробности: research-отчёт zoom-rtms-960-error.
_AUTH_FAIL_MARKERS = ("on_session_start, failed", "reason: 960")


def detect_media_auth_failure(since_ts: float, logs_dir: Path = SDK_LOG_DIR) -> str | None:
    """Просканировать свежие нативные SDK-логи на отказ авторизации media-сессии.

    Возвращает строку-причину если найден reason 960 / session_start failed
    (джойн прошёл, но шлюз отверг подпись — самый частый «Successfully joined
    но нет аудио»), иначе None. since_ts — отсекаем старые логи по mtime."""
    try:
        if not logs_dir.is_dir():
            return None
        recent = [p for p in logs_dir.glob("python_*.log")
                  if p.stat().st_mtime >= since_ts - 5]
        for p in sorted(recent, key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                blob = p.read_bytes()
            except OSError:
                continue
            for marker in _AUTH_FAIL_MARKERS:
                if marker.encode() in blob:
                    return (
                        "Zoom media-gateway отверг авторизацию RTMS "
                        "(on_session_start reason 960). Обычно причина — "
                        "несовпадение client_id/secret установленного app и "
                        "ZM_RTMS_* в .env, либо истёкший RTMS-доступ аккаунта."
                    )
    except OSError:
        pass
    return None


def _extract_participant_key(args: tuple) -> str:
    """Достать идентификатор участника из аргументов onAudioData.

    SDK варьирует форму callback между релизами и режимами; ищем user_id /
    user_name на объекте-метадате, в dict, либо целочисленный id среди
    позиционных. Fallback 'stream0' — всё в один поток (хуже, но не теряем)."""
    for a in args:
        uid = getattr(a, "user_id", None)
        if uid is not None:
            return f"u{uid}"
        uname = getattr(a, "user_name", None)
        if uname:
            return f"n{uname}"
    for a in args:
        if isinstance(a, dict):
            uid = a.get("user_id") or a.get("userId") or a.get("user_name") or a.get("userName")
            if uid:
                return f"u{uid}"
    # bytes — это аудио, не id; берём первый int как id канала (multi-stream).
    saw_bytes = False
    for a in args:
        if isinstance(a, (bytes, bytearray, memoryview)):
            saw_bytes = True
            continue
        if isinstance(a, int):
            return f"c{a}"
    return "stream0" if saw_bytes else "stream0"


class _ParticipantStream:
    """Один PCM-файл на участника, открытый в append для resume-устойчивости."""

    def __init__(self, output_dir: Path, key: str):
        # Имя файла безопасно для ФС: только хэш ключа.
        safe = str(abs(hash(key)) % (10 ** 12))
        self.path = output_dir / f"stream_{safe}.pcm"
        self.file = self.path.open("ab")
        self.bytes_count = 0


class RtmsSession:
    """One Zoom RTMS stream — per-participant audio capture стримом на диск."""

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

        self._streams: dict[str, _ParticipantStream] = {}
        self._streams_lock = threading.Lock()
        self._frames_since_flush = 0
        self._logged_shape = False

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
            data_opt=rtms.AudioDataOption["AUDIO_MULTI_STREAMS"],
            duration=AUDIO_DURATION_MS,
            frame_size=AUDIO_FRAME_SIZE,
        )
        self.client.setAudioParams(audio_params)

        @self.client.onAudioData
        def _on_audio(*args, **kwargs):
            # Один раз логируем форму callback'а — чтобы post-hoc свериться,
            # что participant-id извлекается правильно (формат SDK не в доках).
            if not self._logged_shape:
                self._logged_shape = True
                shape = [type(a).__name__ for a in args]
                log.info("rtms.audio.callback_shape args=%s kwargs=%s",
                         shape, list(kwargs.keys()))

            data = None
            for a in args:
                if isinstance(a, (bytes, bytearray, memoryview)):
                    data = bytes(a)
                    break
            if not data:
                return
            key = _extract_participant_key(args)

            with self._streams_lock:
                st = self._streams.get(key)
                if st is None:
                    st = _ParticipantStream(self.output_dir, key)
                    self._streams[key] = st
                if st.file.closed:
                    return
                if self._first_audio_at is None:
                    self._first_audio_at = time.time()
                st.file.write(data)
                st.bytes_count += len(data)
                self._frames_since_flush += 1
                if self._frames_since_flush >= FLUSH_EVERY_FRAMES:
                    st.file.flush()
                    self._frames_since_flush = 0

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
        don't propagate to caller)."""
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
        """Закрыть потоки участников, смикшировать в один моно Opus для Whisper.
        Fallback на конкатенацию PCM→WAV если ffmpeg недоступен или упал."""
        with self._streams_lock:
            for st in self._streams.values():
                if not st.file.closed:
                    st.file.flush()
                    st.file.close()

        pcm_paths = [st.path for st in self._streams.values() if st.path.stat().st_size > 0]
        total_bytes = sum(p.stat().st_size for p in pcm_paths)
        # Длительность по самому длинному потоку (участники говорят не подряд).
        max_bytes = max((p.stat().st_size for p in pcm_paths), default=0)
        duration_sec = max_bytes / (AUDIO_SAMPLE_RATE * 2)

        ogg_path = self.output_dir / "audio.ogg"
        audio_for_whisper: Path | None = None
        if pcm_paths:
            timeout = COMPRESS_TIMEOUT_BASE_SEC + int(duration_sec / COMPRESS_SPEED_FACTOR)
            try:
                subprocess.run(
                    build_mix_cmd(pcm_paths, ogg_path),
                    check=True, capture_output=True, timeout=timeout,
                )
                audio_for_whisper = ogg_path
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                log.warning("rtms.mix failed, fallback to first stream WAV: %s", e)
                wav_path = self.output_dir / "audio.wav"
                pcm_to_wav(pcm_paths[0], wav_path)
                audio_for_whisper = wav_path

        speakers_path = self.output_dir / "speaker-timeline.json"
        speakers_path.write_text(json.dumps(self.speakers, ensure_ascii=False, indent=2))

        # Диагностику меряем на СЫРЫХ потоках до loudnorm (иначе нормализация
        # замаскирует тишину) и берём самый громкий — если даже он почти
        # пуст, вся встреча пришла тишиной.
        per_stream_db = [d for d in (measure_mean_dbfs(p) for p in pcm_paths) if d is not None]
        mean_dbfs = max(per_stream_db) if per_stream_db else None

        return {
            "rtms_stream_id": self.rtms_stream_id,
            "audio_for_whisper": str(audio_for_whisper) if audio_for_whisper else "",
            "participant_streams": len(pcm_paths),
            "speakers_path": str(speakers_path),
            "speakers": self.speakers,
            "started_at": self.started_at,
            "audio_bytes_count": total_bytes,
            "duration_sec": duration_sec,
            "mean_dbfs": mean_dbfs,
        }
