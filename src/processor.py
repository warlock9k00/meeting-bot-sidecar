"""Per-job orchestration: download → ffmpeg → whisper → render → commit."""
import os
import subprocess
import logging
import requests
from . import attendee, groq_whisper, render, github_commit

log = logging.getLogger(__name__)


def process_job(job: dict) -> dict:
    """Process one job. Returns result dict for KV log. Raises on failure."""
    bot_id = job["bot_id"]
    log.info("job.start bot_id=%s", bot_id)

    bot = attendee.get_bot(bot_id)
    if not attendee.is_final_state(bot):
        raise RuntimeError(f"bot {bot_id} not in final state yet (state={bot.get('state')})")

    started_at, ended_at = attendee.event_timestamps(bot)
    participants = attendee.get_participants(bot_id)
    log.info("bot meta ok: state=%s, %d participants", bot.get("state"), len(participants))

    tmp = os.environ.get("TMP_DIR", "/tmp/sidecar")
    os.makedirs(tmp, exist_ok=True)
    mp4_path = os.path.join(tmp, f"{bot_id}.mp4")
    opus_path = os.path.join(tmp, f"{bot_id}.opus")

    try:
        # Download
        url = attendee.get_recording_url(bot_id)
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(mp4_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        size_mb = os.path.getsize(mp4_path) / 1024 / 1024
        log.info("downloaded mp4: %.1f MB", size_mb)

        # Convert
        subprocess.run(
            ["ffmpeg", "-y", "-i", mp4_path, "-vn", "-ac", "1",
             "-c:a", "libopus", "-b:a", "16k", opus_path],
            check=True, capture_output=True,
        )
        opus_mb = os.path.getsize(opus_path) / 1024 / 1024
        log.info("converted to opus: %.2f MB", opus_mb)

        # Transcribe
        transcript = groq_whisper.transcribe(opus_path)
        segments = transcript.get("segments", [])
        log.info("transcribed: %d segments, %d chars", len(segments), len(transcript.get("text", "")))

        # Render
        markdown = render.render_source(bot, participants, segments, started_at, ended_at)
        from datetime import datetime, timezone
        date_part = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d")
        filename = render.source_filename(date_part, bot)
        sources_folder = os.environ.get("SOURCES_FOLDER", "sources")
        path_in_repo = f"{sources_folder}/{filename}"

        # Commit
        commit = github_commit.commit_file(
            path_in_repo,
            markdown,
            f"[meeting_ingest] {filename}",
        )
        sha = commit.get("commit", {}).get("sha", "")[:8]
        log.info("committed: %s @ %s", path_in_repo, sha)

        # Mirror commit (used during Zoom Marketplace review so reviewers can
        # see real output without access to the operator's private vault).
        # Privacy gate: only mirror when bot metadata says so. Worker sets
        # mirror_to_review=true only when host_email matches the test account
        # (e.g. support@context.select). Operator's own meetings stay private.
        mirror_repo = os.environ.get("MIRROR_REPO")
        mirror_to_review = bool(bot.get("metadata", {}).get("mirror_to_review"))
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
                log.info("mirrored to %s: %s @ %s", mirror_repo, path_in_repo, mirror_sha)
            except Exception as e:
                log.warning("mirror commit failed (non-fatal): %s", e)
        elif mirror_repo:
            log.info("mirror skipped (mirror_to_review=false) bot_id=%s", bot_id)

        return {
            "filename": filename,
            "commit_sha": sha,
            "mirror_repo": mirror_repo,
            "mirror_sha": mirror_sha,
            "size_mb_mp4": round(size_mb, 1),
            "size_mb_opus": round(opus_mb, 2),
            "segments": len(segments),
        }
    finally:
        for p in (mp4_path, opus_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
