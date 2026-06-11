"""HTTP server for push-based job triggering.

Listens on port 8082 (or PORT env), authenticates via Bearer token, and
queues incoming jobs for async processing.

Endpoints:
  GET  /health        — liveness probe
  POST /job           — enqueue Attendee {bot_id} for processing (auth required)
  POST /job/rtms      — enqueue full RTMS job dict (auth required)
"""
import os
import threading
import logging
from datetime import datetime, timezone
from queue import Queue
from flask import Flask, request, jsonify

from . import kv, processor, rtms_worker

log = logging.getLogger("sidecar.server")

SIDECAR_TOKEN = os.environ.get("SIDECAR_TOKEN", "")
PORT = int(os.environ.get("PORT", "8082"))


# ─── Worker queue (single-threaded processing, FIFO) ─────────────────────────

_job_queue: Queue = Queue()
_inflight: set = set()
_inflight_lock = threading.Lock()


def _worker_loop():
    log.info("worker thread started")
    while True:
        bot_id = _job_queue.get()
        try:
            _process_one(bot_id)
        except Exception as e:
            log.error("worker exception bot_id=%s err=%s", bot_id, e)
        finally:
            with _inflight_lock:
                _inflight.discard(bot_id)
            _job_queue.task_done()


def _process_one(bot_id: str):
    """Re-pull job from KV (source of truth), then process."""
    try:
        job = kv.get_job(bot_id)
    except Exception as e:
        log.error("kv.get_job failed bot_id=%s err=%s", bot_id, e)
        return
    if not job:
        log.warning("no KV job for bot_id=%s — skip", bot_id)
        return
    if job.get("status") == "done":
        log.info("bot_id=%s already done — skip", bot_id)
        return
    if job.get("attempts", 0) >= kv.MAX_ATTEMPTS:
        log.warning("bot_id=%s max attempts reached — skip", bot_id)
        return
    try:
        kv.mark_processing(bot_id, job)
        result = processor.process_job(job)
        kv.mark_done(bot_id, job, result)
        log.info("job.done bot_id=%s sha=%s", bot_id, result.get("commit_sha"))
    except processor.EmptyRecordingError as e:
        log.warning("job.empty_recording bot_id=%s size=%d", bot_id, e.size_bytes)
        kv.mark_empty_recording(bot_id, job, e.size_bytes)
    except Exception as e:
        import traceback
        err = f"{type(e).__name__}: {e}"
        log.error("job.failed bot_id=%s err=%s\n%s", bot_id, err, traceback.format_exc())
        kv.mark_failed(bot_id, job, err)


def enqueue(bot_id: str) -> bool:
    """Return True if enqueued, False if already in flight."""
    with _inflight_lock:
        if bot_id in _inflight:
            return False
        _inflight.add(bot_id)
    _job_queue.put(bot_id)
    return True


# ─── RTMS dispatch ────────────────────────────────────────────────────────────
#
# Unlike Attendee jobs (single-threaded FIFO via _job_queue), each RTMS job
# runs in its own subprocess to isolate native SDK crashes. Concurrent
# meetings (different rtms_stream_ids) run truly in parallel — one daemon
# thread per stream manages its subprocess lifecycle.

_rtms_inflight: set = set()
_rtms_inflight_lock = threading.Lock()


def enqueue_rtms_job(job: dict) -> bool:
    """Spawn daemon thread to manage one RTMS subprocess.

    Return False if a job for the same rtms_stream_id is already inflight
    (idempotent — safety_tick may re-discover a job that's still processing).
    """
    stream_id = job.get("rtms_stream_id")
    if not stream_id:
        log.warning("rtms.enqueue.no_stream_id job=%s", job)
        return False

    with _rtms_inflight_lock:
        if stream_id in _rtms_inflight:
            return False
        _rtms_inflight.add(stream_id)

    def _runner():
        try:
            exit_code = rtms_worker.process_rtms_job_in_subprocess(job)
            log.info("rtms.runner.done stream=%s exit_code=%s", stream_id, exit_code)
        except Exception as e:
            log.error("rtms.runner.exception stream=%s err=%s", stream_id, e)
        finally:
            with _rtms_inflight_lock:
                _rtms_inflight.discard(stream_id)

    threading.Thread(
        target=_runner,
        daemon=True,
        name=f"rtms_runner:{stream_id}",
    ).start()
    return True


# ─── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "queue_size": _job_queue.qsize(),
        "inflight": len(_inflight),
        "rtms_inflight": len(_rtms_inflight),
        "ts": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/job", methods=["POST"])
def job():
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {SIDECAR_TOKEN}"
    if not SIDECAR_TOKEN or auth != expected:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    bot_id = body.get("bot_id")
    if not bot_id or not isinstance(bot_id, str):
        return jsonify({"error": "missing or invalid bot_id"}), 400

    enqueued = enqueue(bot_id)
    log.info("job.received bot_id=%s enqueued=%s", bot_id, enqueued)
    return jsonify({"ok": True, "bot_id": bot_id, "enqueued": enqueued}), 202


@app.route("/job/rtms", methods=["POST"])
def job_rtms():
    """Push endpoint для RTMS jobs от Worker.

    Body: полный job dict (то что Worker кладёт в KV) — содержит
    rtms_stream_id, meeting_uuid, payload (signature + server_urls),
    mirror_to_review. Принимаем целиком, не перечитываем из KV — это
    избавляет sidecar от list/get операций на CF KV REST API (которые
    имеют узкий free-tier лимит и однажды нас уже положили).

    Polling-fallback (main.rtms_safety_tick) остаётся как safety net,
    но при работающем push он почти всегда находит пустую очередь.
    """
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {SIDECAR_TOKEN}"
    if not SIDECAR_TOKEN or auth != expected:
        return jsonify({"error": "unauthorized"}), 401

    job = request.get_json(silent=True) or {}
    stream_id = job.get("rtms_stream_id")
    if not stream_id or not isinstance(stream_id, str):
        return jsonify({"error": "missing or invalid rtms_stream_id"}), 400
    if not job.get("meeting_uuid"):
        return jsonify({"error": "missing meeting_uuid"}), 400
    if not job.get("payload"):
        return jsonify({"error": "missing payload (need signature + server_urls)"}), 400

    enqueued = enqueue_rtms_job(job)
    log.info("rtms.job.received stream=%s enqueued=%s", stream_id, enqueued)
    return jsonify({
        "ok": True,
        "rtms_stream_id": stream_id,
        "enqueued": enqueued,
    }), 202


def start_server_in_thread():
    """Start Flask + worker in background threads. Returns immediately."""
    if not SIDECAR_TOKEN:
        log.warning("SIDECAR_TOKEN not set — /job endpoint will reject all requests")

    worker = threading.Thread(target=_worker_loop, daemon=True, name="worker")
    worker.start()

    server = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False),
        daemon=True,
        name="flask",
    )
    server.start()
    log.info("sidecar HTTP server listening on :%d", PORT)
