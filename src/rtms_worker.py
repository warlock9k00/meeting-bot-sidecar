"""RTMS job orchestrator — staged pipeline с durable artifacts.

Стадии: захват → транскрипция → render+commit. Каждая стадия оставляет
marker-файл (capture.json, transcript.json) в каталоге артефактов; при
ретрае или рестарте контейнера выполнение продолжается с первой
непройденной стадии. Захват НЕ повторяется если capture.json есть —
встреча уже закончилась, пере-джойн бессмысленен (исторически давал
«no audio frames within 30s» × 3 и терял запись навсегда).

Артефакты удаляются ТОЛЬКО при полном успехе. При фейле каталог остаётся
для resume/ручного спасения; gc_stale_artifacts() подчищает через 7 дней
(синхронно с lifecycle R2-бакета).

Native zoom/rtms SDK occasionally segfaults on edge cases. To prevent one
crash from taking down the whole sidecar, each RTMS job runs in its own
subprocess (multiprocessing.Process). The subprocess updates KV state
directly (done or failed). The parent only handles the timeout/kill case.

Public API:
  process_rtms_job_in_subprocess(job) -> int  # returns exit code
  gc_stale_artifacts() -> int                 # удалено каталогов
"""
import json
import logging
import multiprocessing
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import github_commit, kv, render
from .groq_whisper import default_meeting_prompt, transcribe

log = logging.getLogger(__name__)

# Session join timeout — covers any reasonable meeting length (>2h is rare).
DEFAULT_TIMEOUT_SEC = 7200

# Extra time on top of session timeout for finalize + Whisper + render +
# commit. Groq Whisper Large v3 processes ~100× realtime so even 2h audio
# transcribes in 1-2 min; commit is <5s. 600s is a generous buffer.
SUBPROCESS_OVERHEAD_SEC = 600

TMP_BASE = os.environ.get("TMP_DIR", "/tmp/sidecar")

# Каталоги фейлов живут неделю — достаточно для ручного спасения,
# синхронно с 7-дневным lifecycle R2-бакета meeting-audio-backup.
GC_MAX_AGE_DAYS = 7


def _marker_path(output_dir: Path, name: str) -> Path:
    return output_dir / f"{name}.json"


def _read_marker(output_dir: Path, name: str) -> dict | None:
    p = _marker_path(output_dir, name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        # Битый маркер (краш посреди записи) = стадия не пройдена.
        return None


def _write_marker(output_dir: Path, name: str, data: dict) -> None:
    # Atomic write: tmp + rename — краш посреди записи не оставит битый маркер.
    p = _marker_path(output_dir, name)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(p)


def gc_stale_artifacts() -> int:
    """Удалить каталоги артефактов старше GC_MAX_AGE_DAYS. Возвращает число."""
    base = Path(TMP_BASE) / "rtms"
    if not base.exists():
        return 0
    cutoff = time.time() - GC_MAX_AGE_DAYS * 86400
    removed = 0
    for d in base.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("rtms.gc removed %d stale artifact dir(s)", removed)
    return removed


def _do_work(job: dict) -> dict:
    """Run the staged RTMS pipeline. Raises on any failure — caller catches.

    Returns result dict suitable for kv.mark_done().
    """
    rtms_stream_id = job["rtms_stream_id"]
    meeting_uuid = job["meeting_uuid"]

    output_dir = Path(TMP_BASE) / "rtms" / rtms_stream_id
    log.info("rtms.job.start stream=%s uuid=%s", rtms_stream_id, meeting_uuid)

    # ── Стадия 1: захват. При resume пропускается целиком — встреча уже
    # закончилась, пере-джойн даёт только «no audio frames within 30s». ──
    capture = _read_marker(output_dir, "capture")
    if capture is None:
        # Lazy import — native rtms package нужен только для живого захвата;
        # resume-путь и тесты работают без него.
        from .rtms_session import RtmsSession

        session = RtmsSession(job["payload"], output_dir)
        session.join_and_capture(timeout=DEFAULT_TIMEOUT_SEC)
        capture = session.finalize()
        log.info(
            "rtms.session.done bytes=%d duration=%.1fs speakers=%d",
            capture["audio_bytes_count"],
            capture["duration_sec"],
            len(capture["speakers"]),
        )
        if capture["audio_bytes_count"] == 0:
            raise RuntimeError("no audio captured (empty session)")
        _write_marker(output_dir, "capture", capture)
    else:
        log.info(
            "rtms.resume stream=%s — захват уже на диске (%.1fs), пропускаю join",
            rtms_stream_id, capture["duration_sec"],
        )

    # ── Стадия 2: транскрипция (результат кэшируется на диск) ──
    whisper = _read_marker(output_dir, "transcript")
    if whisper is None:
        whisper = transcribe(
            capture["audio_for_whisper"],
            language="ru",
            prompt=default_meeting_prompt(),
        )
        _write_marker(output_dir, "transcript", whisper)
    segments = whisper.get("segments", [])
    log.info(
        "rtms.transcribed segments=%d chars=%d",
        len(segments),
        len(whisper.get("text", "")),
    )

    # ── Стадия 3: render + commit. Идемпотентна — повторный commit того же
    # path перезаписывает файл тем же контентом, маркер не нужен. ──
    started_at_unix = capture["started_at"]
    date_str = datetime.fromtimestamp(started_at_unix, tz=timezone.utc).strftime("%Y-%m-%d")

    markdown = render.render_rtms_source(
        rtms_stream_id=rtms_stream_id,
        meeting_uuid=meeting_uuid,
        segments=segments,
        speakers=capture["speakers"],
        started_at_unix=started_at_unix,
        duration_sec=capture["duration_sec"],
    )
    filename = render.rtms_source_filename(date_str, meeting_uuid)
    sources_folder = os.environ.get("SOURCES_FOLDER", "sources")
    path_in_repo = f"{sources_folder}/{filename}"

    commit = github_commit.commit_file(
        path_in_repo, markdown, f"[meeting_ingest] {filename}"
    )
    sha = commit.get("commit", {}).get("sha", "")[:8]
    log.info("rtms.committed path=%s sha=%s", path_in_repo, sha)

    # Mirror commit для review-периода Zoom Marketplace. Worker устанавливает
    # job["mirror_to_review"]="true" через env RTMS_REVIEW_MODE, чтобы
    # reviewer мог видеть свои встречи в review repo. После approval —
    # выключить (RTMS_REVIEW_MODE=false), и mirror перестанет писаться.
    # Privacy gate identical to Attendee path (processor.py).
    mirror_repo = os.environ.get("MIRROR_REPO")
    mirror_to_review = (
        str(job.get("mirror_to_review", "")).lower() == "true"
    )
    mirror_sha = None
    if mirror_repo and mirror_to_review:
        try:
            mirror = github_commit.commit_file(
                path_in_repo,
                markdown,
                f"[meeting_ingest] {filename}",
                repo=mirror_repo,
            )
            mirror_sha = mirror.get("commit", {}).get("sha", "")[:8]
            log.info(
                "rtms.mirrored to %s: %s @ %s",
                mirror_repo, path_in_repo, mirror_sha,
            )
        except Exception as e:
            log.warning("rtms.mirror commit failed (non-fatal): %s", e)
    elif mirror_repo:
        log.info(
            "rtms.mirror skipped (mirror_to_review=false) stream=%s",
            rtms_stream_id,
        )

    unique_speakers = len({
        s.get("user_name")
        for s in capture["speakers"]
        if s.get("user_name")
    })
    result = {
        "filename": filename,
        "commit_sha": sha,
        "mirror_repo": mirror_repo,
        "mirror_sha": mirror_sha,
        "segments": len(segments),
        "duration_sec": round(capture["duration_sec"], 1),
        "audio_bytes": capture["audio_bytes_count"],
        "speakers_count": unique_speakers,
    }

    # Единственное место удаления артефактов — полный успех. При фейле
    # каталог остаётся для resume/ручного спасения (GC подчистит через
    # GC_MAX_AGE_DAYS). Disk economy: успешная встреча — это ~10-100 MB
    # PCM, которые больше не нужны (транскрипт в GitHub).
    shutil.rmtree(output_dir, ignore_errors=True)
    return result


def _worker_main(job: dict):
    """Subprocess entry: run pipeline, update KV, sys.exit.

    Logging is re-initialized here because the parent's basicConfig() does
    not propagate via spawn-mode multiprocessing on macOS, and behavior on
    Linux fork can be inconsistent depending on log handler types.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    rtms_stream_id = job.get("rtms_stream_id", "unknown")
    key = f"job:rtms:{rtms_stream_id}"

    try:
        kv.mark_processing(key, job)
        result = _do_work(job)
        kv.mark_done(key, job, result)
        log.info(
            "rtms.job.done stream=%s sha=%s",
            rtms_stream_id,
            result.get("commit_sha"),
        )
        sys.exit(0)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.error(
            "rtms.job.failed stream=%s err=%s\n%s",
            rtms_stream_id,
            err,
            traceback.format_exc(),
        )
        try:
            kv.mark_failed(key, job, err)
        except Exception as ke:
            log.error("rtms.kv.mark_failed_error stream=%s err=%s", rtms_stream_id, ke)
        sys.exit(1)


def process_rtms_job_in_subprocess(job: dict) -> int:
    """Spawn subprocess for crash isolation; return exit code.

    Native RTMS SDK can segfault; isolating each job means one crash != main
    crash. Subprocess updates KV state directly. Parent only handles the
    timeout-kill case (subprocess didn't reach mark_failed itself).

    Returns:
      0   on success
      1   on handled exception (KV already marked failed by subprocess)
      -1  on timeout-kill (KV marked failed here)
      -2  on subprocess died with no exitcode (race condition guard)
    """
    rtms_stream_id = job.get("rtms_stream_id", "unknown")
    p = multiprocessing.Process(
        target=_worker_main,
        args=(job,),
        name=f"rtms_worker:{rtms_stream_id}",
    )
    p.start()
    log.info(
        "rtms.subprocess.spawned stream=%s pid=%s",
        rtms_stream_id, p.pid,
    )

    timeout = DEFAULT_TIMEOUT_SEC + SUBPROCESS_OVERHEAD_SEC
    p.join(timeout=timeout)

    if p.is_alive():
        log.warning(
            "rtms.subprocess.timeout stream=%s pid=%s — SIGTERM",
            rtms_stream_id, p.pid,
        )
        p.terminate()
        p.join(timeout=10)
        if p.is_alive():
            log.warning(
                "rtms.subprocess.kill stream=%s pid=%s — SIGKILL",
                rtms_stream_id, p.pid,
            )
            p.kill()
            p.join(timeout=5)
        # Subprocess didn't reach mark_failed — do it here.
        try:
            kv.mark_failed(
                f"job:rtms:{rtms_stream_id}",
                job,
                f"subprocess timeout after {timeout}s",
            )
        except Exception as e:
            log.error("rtms.kv.mark_failed_after_timeout err=%s", e)
        return -1

    return p.exitcode if p.exitcode is not None else -2
