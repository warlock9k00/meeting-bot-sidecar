"""HTTP server for push-based job triggering.

Listens on port 8082 (or PORT env), authenticates via Bearer token, and
queues incoming jobs for async processing.

Endpoints:
  GET  /health        — liveness probe
  POST /job           — enqueue {bot_id} for processing (auth required)
"""
import os
import threading
import logging
from datetime import datetime, timezone
from queue import Queue
from flask import Flask, request, jsonify

from . import kv, processor

log = logging.getLogger("sidecar.server")

SIDECAR_TOKEN = os.environ.get("SIDECAR_TOKEN", "")
PORT = int(os.environ.get("PORT", "8082"))
MAX_ATTEMPTS = 3


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
    if job.get("attempts", 0) >= MAX_ATTEMPTS:
        log.warning("bot_id=%s max attempts reached — skip", bot_id)
        return
    try:
        kv.mark_processing(bot_id, job)
        result = processor.process_job(job)
        kv.mark_done(bot_id, job, result)
        log.info("job.done bot_id=%s sha=%s", bot_id, result.get("commit_sha"))
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


# ─── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "queue_size": _job_queue.qsize(),
        "inflight": len(_inflight),
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
