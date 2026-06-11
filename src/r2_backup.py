"""R2 backup — durable копия сжатого аудио до транскрипции.

Бакет meeting-audio-backup (Cloudflare R2, EU) с lifecycle 7 дней: если
VPS умирает целиком (Hetzner OOM 2026-05-05), запись доживает в R2 и
восстанавливается вручную. Upload best-effort: фейл бэкапа не блокирует
транскрипцию (локальный артефакт остаётся первичным).

Env: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
"""
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_REQUIRED_ENV = (
    "R2_ENDPOINT_URL",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
)


def is_configured() -> bool:
    return all(os.environ.get(k) for k in _REQUIRED_ENV)


def backup_key(date_str: str, rtms_stream_id: str, suffix: str) -> str:
    """Ключ в бакете: rtms/<дата>/<stream_id>.<ext> — группировка по дням
    упрощает ручной поиск записи за конкретный день."""
    return f"rtms/{date_str}/{rtms_stream_id}{suffix}"


def upload(local_path: str, key: str) -> dict:
    """Залить файл в R2. Возвращает {bucket, key, size_bytes}. Raises on failure."""
    # Lazy import: boto3 нужен только при реальном upload'е (тесты без него).
    import boto3

    bucket = os.environ["R2_BUCKET"]
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    client.upload_file(local_path, bucket, key)
    return {
        "bucket": bucket,
        "key": key,
        "size_bytes": Path(local_path).stat().st_size,
    }
