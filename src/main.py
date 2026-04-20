"""Entry point: poll loop. Pulls pending jobs from CF KV, processes each."""
import os
import time
import logging
import traceback
from . import kv, processor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sidecar")

MAX_ATTEMPTS = 3


def tick():
    jobs = kv.list_pending_jobs()
    if not jobs:
        return
    log.info("found %d pending jobs", len(jobs))
    for job in jobs:
        bot_id = job["bot_id"]
        attempts = job.get("attempts", 0)
        if attempts >= MAX_ATTEMPTS:
            log.warning("skip %s — max attempts (%d) reached", bot_id, attempts)
            continue
        try:
            kv.mark_processing(bot_id, job)
            result = processor.process_job(job)
            kv.mark_done(bot_id, job, result)
            log.info("job.done bot_id=%s sha=%s", bot_id, result.get("commit_sha"))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log.error("job.failed bot_id=%s err=%s\n%s", bot_id, err, traceback.format_exc())
            kv.mark_failed(bot_id, job, err)


def main():
    interval = int(os.environ.get("POLL_INTERVAL_SEC", "15"))
    log.info("sidecar started, polling every %d sec", interval)
    while True:
        try:
            tick()
        except Exception as e:
            log.error("tick failed: %s\n%s", e, traceback.format_exc())
        time.sleep(interval)


if __name__ == "__main__":
    main()
