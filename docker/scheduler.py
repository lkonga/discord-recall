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


def _whitelist() -> set[int]:
    raw = os.environ.get("GUILD_WHITELIST", "").strip().strip("[]")
    return {int(p) for p in (x.strip() for x in raw.split(",")) if p.isdigit()}


def channels() -> list[str]:
    wanted = [c.strip() for c in os.environ.get("DIGEST_CHANNELS", "").split(",") if c.strip()]
    whitelist = _whitelist()
    if not whitelist or not wanted:
        return wanted
    # Belt and braces: even if DIGEST_CHANNELS is wrong, never touch a guild that
    # is not on the whitelist.
    import sqlite3

    db = os.environ.get("DATABASE_URL", "").rsplit("///", 1)[-1] or "/data/discord_recall.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        allowed = {
            str(cid) for (cid,) in con.execute(
                "select id from channels where server_id in (%s)"
                % ",".join(str(g) for g in whitelist)
            )
        }
        con.close()
    except Exception as exc:  # noqa: BLE001 - fail closed
        print(f"[scheduler] could not read the store ({exc}); skipping digests", flush=True)
        return []
    skipped = [c for c in wanted if c not in allowed]
    if skipped:
        print(f"[scheduler] skipping {len(skipped)} channel(s) outside GUILD_WHITELIST", flush=True)
    return [c for c in wanted if c in allowed]


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
