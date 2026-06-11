"""Алерты в Telegram через Worker-прокси /sidecar-alert.

Токен Telegram-бота живёт только в секретах CF Worker — sidecar шлёт
текст на Worker, аутентифицируясь тем же SIDECAR_TOKEN, которым Worker
аутентифицируется у sidecar'а. Fire-and-forget: фейл алерта логируется,
но не роняет pipeline.

Env: WORKER_URL (https://meetingbot.context.select), SIDECAR_TOKEN.
"""
import logging
import os

import requests

log = logging.getLogger(__name__)

ALERT_TIMEOUT_SEC = 10


def send_alert(text: str) -> bool:
    base = os.environ.get("WORKER_URL", "").rstrip("/")
    token = os.environ.get("SIDECAR_TOKEN", "")
    if not base or not token:
        log.warning("alert skipped (WORKER_URL/SIDECAR_TOKEN not set): %s", text)
        return False
    try:
        r = requests.post(
            f"{base}/sidecar-alert",
            json={"text": text},
            headers={"Authorization": f"Bearer {token}"},
            timeout=ALERT_TIMEOUT_SEC,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("alert send failed: %s | text=%s", e, text)
        return False
