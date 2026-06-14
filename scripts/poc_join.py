"""PoC: join a meeting via Attendee+OBF and process the recording.

Usage: python scripts/poc_join.py "<zoom_join_url>"
Env: ATTENDEE_BASE_URL, ATTENDEE_API_KEY, OBF_CONNECTION_USER_ID.
Reuses processor.process_job for recording -> Whisper -> render -> commit.
"""
import os
import sys
import time

# Allow `python scripts/poc_join.py` from the repo root: put the sidecar root
# (parent of scripts/) on sys.path so `from src import ...` resolves when run
# as a plain script (sys.path[0] would otherwise be scripts/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import attendee, processor  # noqa: E402

POLL_SECONDS = 15


def main(meeting_url: str) -> None:
    uid = os.environ["OBF_CONNECTION_USER_ID"]
    bot_id = attendee.create_bot(meeting_url, uid)
    print(f"bot created: {bot_id} — join the meeting now if you are the guest")

    while True:
        bot = attendee.get_bot(bot_id)
        print(f"state: {bot.get('state')}")
        if attendee.is_final_state(bot):
            break
        time.sleep(POLL_SECONDS)

    result = processor.process_job({"bot_id": bot_id})
    print(f"processed: {result} — check vault sources/")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('usage: python scripts/poc_join.py "<zoom_join_url>"', file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
