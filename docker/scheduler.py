"""Scheduler loop for the containerised deployment.

Runs a daily cycle per configured channel: bounded backfill, then digests for
the last few days (optionally pushed to Telegram). Each step shells out to the
CLI so every run is a fresh process and a failure cannot wedge the loop.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

INTERVAL = int(os.environ.get("SCHEDULE_INTERVAL_SECONDS", "21600"))  # 6h default
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "2"))
DIGEST_DAYS = int(os.environ.get("DIGEST_DAYS", "3"))
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "600"))
SEND = os.environ.get("SEND_TELEGRAM", "true").lower() == "true"


def channels() -> list[str]:
    return [c.strip() for c in os.environ.get("DIGEST_CHANNELS", "").split(",") if c.strip()]


def run(args: list[str]) -> None:
    print(f"[scheduler] $ discord-recall {' '.join(args)}", flush=True)
    try:
        proc = subprocess.run(["discord-recall", *args], timeout=3600)
        print(f"[scheduler] exit={proc.returncode}", flush=True)
    except Exception as exc:  # noqa: BLE001 - keep the loop alive
        print(f"[scheduler] step failed: {exc}", flush=True)


def cycle() -> None:
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=BACKFILL_DAYS)).isoformat()
    frm = (today - timedelta(days=DIGEST_DAYS)).isoformat()
    to = (today - timedelta(days=1)).isoformat()

    for channel in channels():
        run(["backfill", "-c", channel, "--since", since, "--max-messages", str(MAX_MESSAGES)])
        args = ["digest-range", "-c", channel, "--from", frm, "--to", to]
        if SEND:
            args.append("--send")
        run(args)


def main() -> None:
    print(
        f"[scheduler] start interval={INTERVAL}s channels={channels()} "
        f"backfill_days={BACKFILL_DAYS} digest_days={DIGEST_DAYS} send={SEND}",
        flush=True,
    )
    if not channels():
        print("[scheduler] DIGEST_CHANNELS is empty - scheduler will idle", flush=True)
    while True:
        try:
            if channels():
                cycle()
        except Exception as exc:  # noqa: BLE001 - never exit the loop
            print(f"[scheduler] cycle failed: {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
