"""RTMS job orchestrator — RtmsSession → Whisper → render → GitHub commit.

Native zoom/rtms SDK occasionally segfaults on edge cases. To prevent one
crash from taking down the whole sidecar, each RTMS job runs in its own
subprocess (multiprocessing.Process). The subprocess updates KV state
directly (done or failed). The parent only handles the timeout/kill case.

Public API:
  process_rtms_job_in_subprocess(job) -> int  # returns exit code
"""
import logging
import multiprocessing
import os
import shutil
import sys
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


def _do_work(job: dict) -> dict:
    """Run the full RTMS pipeline. Raises on any failure — caller catches.

    Returns result dict suitable for kv.mark_done().
    """
    # Lazy import — `rtms` native package not in requirements before C11,
    # and we want rtms_worker importable for testing/dispatch even without it.
    from .rtms_session import RtmsSession

    rtms_stream_id = job["rtms_stream_id"]
    meeting_uuid = job["meeting_uuid"]
    payload = job["payload"]

    output_dir = Path(TMP_BASE) / "rtms" / rtms_stream_id
    log.info("rtms.job.start stream=%s uuid=%s", rtms_stream_id, meeting_uuid)

    try:
        session = RtmsSession(payload, output_dir)
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

        whisper = transcribe(
            capture["audio_for_whisper"],
            language="ru",
            prompt=default_meeting_prompt(),
        )
        segments = whisper.get("segments", [])
        log.info(
            "rtms.transcribed segments=%d chars=%d",
            len(segments),
            len(whisper.get("text", "")),
        )

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
        return {
            "filename": filename,
            "commit_sha": sha,
            "mirror_repo": mirror_repo,
            "mirror_sha": mirror_sha,
            "segments": len(segments),
            "duration_sec": round(capture["duration_sec"], 1),
            "audio_bytes": capture["audio_bytes_count"],
            "speakers_count": unique_speakers,
        }
    finally:
        # Disk economy on Hetzner cx23 — meetings produce ~10-100 MB raw PCM.
        # Logs retain stream_id for post-mortem; transcript is in GitHub.
        shutil.rmtree(output_dir, ignore_errors=True)


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
