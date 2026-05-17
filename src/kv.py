"""Cloudflare KV REST client.

Three key spaces share the MEETING_JOBS namespace:

  1. Attendee jobs (legacy mp4 path)
       key   = bot_<id>
       value = {"bot_id": ..., "received_at": ..., "status": ...}

  2. RTMS jobs (new realtime path; CF Worker writes these on
     meeting.rtms_started webhook)
       key   = job:rtms:<rtms_stream_id>
       value = {"type": "rtms", "rtms_stream_id": ..., "meeting_uuid": ...,
                "payload": <full Zoom payload for rtms.Client().join()>,
                "received_at": ..., "status": ...}

  3. Dedup keys (Worker-only; sidecar ignores)
       key   = webhook_dedup:<...>
       value = ISO timestamp string (TTL 300-600s)

list_pending_jobs() and list_pending_rtms_jobs() use prefix filters at the
API level so they only return their respective domain.
"""
import os
import json
import requests


def _base_url():
    acct = os.environ["CF_ACCOUNT_ID"]
    ns = os.environ["CF_KV_NAMESPACE_ID"]
    return f"https://api.cloudflare.com/client/v4/accounts/{acct}/storage/kv/namespaces/{ns}"


def _headers():
    return {"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"}


def list_pending_jobs(limit: int = 50) -> list[dict]:
    """Return list of pending Attendee job dicts (status=pending or failed).

    Filters at API level by prefix=bot_ so RTMS keys and dedup keys don't
    leak into the legacy path (where callers expect job["bot_id"] to exist).
    """
    url = f"{_base_url()}/keys?prefix=bot_&limit={limit}"
    r = requests.get(url, headers=_headers(), timeout=10)
    r.raise_for_status()
    keys = [k["name"] for k in r.json().get("result", [])]
    jobs = []
    for k in keys:
        v = get_job(k)
        if v and v.get("status", "pending") in ("pending", "failed"):
            jobs.append(v)
    return jobs


def list_pending_rtms_jobs(limit: int = 50) -> list[dict]:
    """Return list of pending RTMS job dicts (status=pending or failed).

    Each job dict carries rtms_stream_id; the KV key is f"job:rtms:{stream_id}".
    Reuse get_job/put_job/mark_* helpers with that key for state updates.
    """
    url = f"{_base_url()}/keys?prefix=job:rtms:&limit={limit}"
    r = requests.get(url, headers=_headers(), timeout=10)
    r.raise_for_status()
    keys = [k["name"] for k in r.json().get("result", [])]
    jobs = []
    for k in keys:
        v = get_job(k)
        if v and v.get("status", "pending") in ("pending", "failed"):
            jobs.append(v)
    return jobs


def get_job(bot_id: str) -> dict | None:
    url = f"{_base_url()}/values/{bot_id}"
    r = requests.get(url, headers=_headers(), timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    try:
        return json.loads(r.text)
    except json.JSONDecodeError:
        return None


def put_job(bot_id: str, job: dict) -> None:
    url = f"{_base_url()}/values/{bot_id}"
    r = requests.put(
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        data=json.dumps(job),
        timeout=10,
    )
    r.raise_for_status()


def mark_processing(bot_id: str, job: dict) -> None:
    job["status"] = "processing"
    job["processing_started_at"] = _now()
    put_job(bot_id, job)


def mark_done(bot_id: str, job: dict, result: dict) -> None:
    job["status"] = "done"
    job["completed_at"] = _now()
    job["result"] = result
    put_job(bot_id, job)


def mark_failed(bot_id: str, job: dict, error: str) -> None:
    job["status"] = "failed"
    job["last_error"] = error
    job["failed_at"] = _now()
    job["attempts"] = job.get("attempts", 0) + 1
    put_job(bot_id, job)


def mark_empty_recording(bot_id: str, job: dict, size_bytes: int) -> None:
    """Bot uploaded an empty placeholder mp4 (Attendee fail mode where the
    bot joined but never captured audio). Mark distinctly so we don't waste
    retries and can audit how often this happens."""
    job["status"] = "empty_recording"
    job["empty_size_bytes"] = size_bytes
    job["completed_at"] = _now()
    put_job(bot_id, job)


def delete_job(bot_id: str) -> None:
    url = f"{_base_url()}/values/{bot_id}"
    r = requests.delete(url, headers=_headers(), timeout=10)
    if r.status_code not in (200, 404):
        r.raise_for_status()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
