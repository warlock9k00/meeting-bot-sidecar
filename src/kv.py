"""Cloudflare KV REST client.

Job format в KV:
  key   = bot_id  (строка)
  value = JSON: {"bot_id": "...", "received_at": "...", "status": "pending|processing|done|failed"}

Sidecar polls list_keys → читает каждый со status=pending → processing → done.
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
    """Return list of pending job dicts (status=pending or missing)."""
    url = f"{_base_url()}/keys?limit={limit}"
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


def delete_job(bot_id: str) -> None:
    url = f"{_base_url()}/values/{bot_id}"
    r = requests.delete(url, headers=_headers(), timeout=10)
    if r.status_code not in (200, 404):
        r.raise_for_status()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
