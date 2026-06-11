"""Entry point: HTTP server (push) + slow polling (safety net).

Architecture:
  - Worker on Cloudflare receives Attendee webhook → puts job in KV → POSTs
    /job to this sidecar with {bot_id} (Bearer SIDECAR_TOKEN).
  - This server queues the bot_id and processes in a worker thread.
  - A slow polling loop (default 5 min) catches anything the push missed
    (transient HTTP errors, Worker downtime, etc.). 288 list-ops/day fits
    well in CF KV free tier (1000/day limit).
"""
import os
import time
import logging
import traceback
import socket
import urllib3.util.connection as urllib3_cn

# Force IPv4 — наш CF API token имеет IP filter (только IPv4).
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

from . import kv, rtms_worker, server  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sidecar")

MAX_ATTEMPTS = 3


def safety_tick():
    """Polled fallback: re-queue any pending jobs the push pipeline missed."""
    try:
        jobs = kv.list_pending_jobs()
    except Exception as e:
        log.error("safety poll list failed: %s", e)
        return
    if not jobs:
        return
    log.info("safety poll: found %d pending job(s)", len(jobs))
    for job in jobs:
        bot_id = job["bot_id"]
        if job.get("attempts", 0) >= MAX_ATTEMPTS:
            log.warning("safety: skip %s — max attempts reached", bot_id)
            continue
        # Reuse the server's queue + dedup so a job won't be processed twice.
        enqueued = server.enqueue(bot_id)
        log.info("safety: enqueued bot_id=%s (was_inflight=%s)", bot_id, not enqueued)


def rtms_safety_tick():
    """Polled pickup for RTMS jobs.

    Unlike Attendee path (Worker → /job HTTP push), CF Worker writes RTMS
    jobs directly to KV on meeting.rtms_started. Polling is currently the
    sole pickup mechanism — POLL_INTERVAL_SEC controls webhook-to-join
    latency. Zoom RTMS buffers for late joiners so 15s delay is acceptable.
    """
    try:
        jobs = kv.list_pending_rtms_jobs()
    except Exception as e:
        log.error("rtms safety poll failed: %s", e)
        return
    if not jobs:
        return
    log.info("rtms poll: found %d pending job(s)", len(jobs))
    for job in jobs:
        stream_id = job.get("rtms_stream_id", "unknown")
        if job.get("attempts", 0) >= MAX_ATTEMPTS:
            log.warning("rtms: skip %s — max attempts reached", stream_id)
            continue
        enqueued = server.enqueue_rtms_job(job)
        log.info("rtms: enqueued stream=%s (was_inflight=%s)", stream_id, not enqueued)


def main():
    interval = int(os.environ.get("POLL_INTERVAL_SEC", "300"))  # 5 min default
    log.info("sidecar starting: HTTP push + safety polling every %d sec", interval)

    server.start_server_in_thread()

    while True:
        try:
            safety_tick()
            rtms_safety_tick()
            rtms_worker.gc_stale_artifacts()
        except Exception as e:
            log.error("safety tick failed: %s\n%s", e, traceback.format_exc())
        time.sleep(interval)


if __name__ == "__main__":
    main()
