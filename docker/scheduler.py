"""Scheduler loop for the containerised deployment.

Runs a daily cycle per configured channel: bounded backfill, then digests for the
last few days.

The loop is **enqueue-only**: it POSTs jobs to the app's own API
(`POST /api/jobs`, exactly like the UI does) and never touches Discord itself.
Everything that talks to Discord is executed by the single-worker queue in
`web/jobs.py`, which owns the pacing policy (small pages, jittered delays, a
per-run request budget, 429 backoff) and runs one job at a time. That is why no
CLI process is started here: shelling out would bypass the pacing and could open
a second Discord session alongside a job the queue is already running.

Delivery is the app's concern too - the scheduler never passes `--send`.
`SEND_TELEGRAM` is read by the queue/CLI side, not here, so the banner below
reports it for information only and nothing extra is POSTed for it.

Failure handling: a connection error or a non-2xx response is logged once and the
loop moves on to the next channel (the next cycle retries). A refusal from the
API itself - `{"ok": false, ...}`, e.g. the guild whitelist or a digest range
with no captured messages yet - is reported as *skipped*, not as a failure.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

INTERVAL = int(os.environ.get("SCHEDULE_INTERVAL_SECONDS", "21600"))  # 6h default
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "2"))
DIGEST_DAYS = int(os.environ.get("DIGEST_DAYS", "3"))
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "600"))
# Reported in the start banner only: delivery is the queue's concern, not ours.
SEND = os.environ.get("SEND_TELEGRAM", "true").lower() == "true"
# One cycle posts two jobs per channel (capture, then digest). The queue runs
# them one at a time, so a long channel list is legal - but a typo in
# DIGEST_CHANNELS must not turn a single cycle into an unbounded burst of API
# calls. The cap counts POST attempts, so queued jobs can never exceed it.
MAX_PER_CYCLE = int(os.environ.get("ENQUEUE_MAX_PER_CYCLE", "10"))
# A wedged API must not stop the loop from reaching the next cycle.
HTTP_TIMEOUT = float(os.environ.get("SCHEDULER_HTTP_TIMEOUT_SECONDS", "10"))


def api_base() -> str:
    """The app's API as the UI sees it: loopback inside the container."""
    return f"http://127.0.0.1:{os.environ.get('WEB_PORT', '9879')}"


def jobs_url() -> str:
    return f"{api_base()}/api/jobs"


def db_path() -> str:
    """Filesystem path of the SQLite store, from the DATABASE_URL SQLAlchemy URL.

    ``sqlite+aiosqlite:///rel.db`` is relative and ``sqlite+aiosqlite:////data/x.db``
    is absolute. Splitting on the *first* ``///`` keeps the leading slash of the
    absolute form; splitting on the last one silently turns the container's
    ``/data`` store into a relative path, and the whitelist gate then fails closed
    on every cycle.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "/data/discord_recall.db"
    return url.split("///", 1)[-1] if "///" in url else url


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
    db = db_path()
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


def _decode(raw: bytes) -> dict:
    """Best-effort JSON body; a non-JSON body becomes an empty dict."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def post_job(payload: dict) -> tuple[int, dict]:
    """HTTP seam: POST one job to the local API. Tests replace this function.

    Returns ``(status_code, decoded_body)``. A non-2xx response is *returned*, not
    raised, so a refusal is data the caller reports. Transport problems
    (connection refused, timeout, DNS) raise ``OSError``.
    """
    request = urllib.request.Request(
        jobs_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return int(response.status), _decode(response.read())
    except urllib.error.HTTPError as exc:  # a status code, with a body
        try:
            return int(exc.code), _decode(exc.read())
        finally:
            exc.close()


def _reason(body: dict) -> str:
    for key in ("status", "detail"):  # a FastAPI error body says `detail`
        value = body.get(key)
        if value:
            return str(value)
    return "no reason in the response body"


def enqueue(payload: dict, what: str) -> bool:
    """POST one job. True when the API queued it, False when it was skipped.

    Never raises: the loop must survive a restarting API.
    """
    print(f"[scheduler] POST {jobs_url()} {json.dumps(payload)}", flush=True)
    try:
        status, body = post_job(payload)
    except OSError as exc:  # connection refused, timeout, DNS
        print(f"[scheduler] {what}: API unreachable ({exc}); skipping", flush=True)
        return False
    if not 200 <= status < 300:
        print(
            f"[scheduler] {what}: API returned HTTP {status} ({_reason(body)}); skipping",
            flush=True,
        )
        return False
    if body.get("ok") is False:
        # Not a failure: the API refused for a policy reason (whitelist, or a
        # digest range with nothing captured yet). The next cycle tries again.
        print(f"[scheduler] {what}: skipped: {_reason(body)}", flush=True)
        return False
    print(f"[scheduler] {what}: queued job {body.get('jobId')}", flush=True)
    return True


def _jobs_for(channel: str, since: str, frm: str, to: str) -> list[tuple[dict, str]]:
    """The two enqueued jobs for one channel, in execution order."""
    return [
        (
            {
                "kind": "capture",
                "channelId": channel,
                "since": since,
                "maxMessages": MAX_MESSAGES,
            },
            f"channel {channel} capture",
        ),
        (
            {
                "kind": "digest",
                "channelId": channel,
                # `from` is the API's alias: `from` is a Python keyword.
                "from": frm,
                "to": to,
                "period": "daily",
            },
            f"channel {channel} digest",
        ),
    ]


def cycle(today: date | None = None) -> int:
    """Queue capture then digest for every allowed channel.

    Returns the number of POST attempts; ``today`` is injectable so tests are not
    date-dependent.
    """
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=BACKFILL_DAYS)).isoformat()
    frm = (today - timedelta(days=DIGEST_DAYS)).isoformat()
    to = (today - timedelta(days=1)).isoformat()

    posted = 0
    for channel in channels():
        for payload, what in _jobs_for(channel, since, frm, to):
            if posted >= MAX_PER_CYCLE:
                print(
                    f"[scheduler] per-cycle cap ENQUEUE_MAX_PER_CYCLE={MAX_PER_CYCLE} reached; "
                    f"{what} and the remaining channels are not enqueued "
                    "(raise the cap or trim DIGEST_CHANNELS)",
                    flush=True,
                )
                return posted
            posted += 1
            enqueue(payload, what)
    return posted


def main() -> None:
    print(
        f"[scheduler] start interval={INTERVAL}s channels={channels()} "
        f"backfill_days={BACKFILL_DAYS} digest_days={DIGEST_DAYS} "
        f"max_messages={MAX_MESSAGES} cap={MAX_PER_CYCLE}/cycle api={jobs_url()} "
        f"send_telegram={SEND} (applied by the queue)",
        flush=True,
    )
    if not channels():
        print("[scheduler] DIGEST_CHANNELS is empty - scheduler will idle", flush=True)
    while True:
        try:
            posted = cycle()
            if posted:
                print(f"[scheduler] cycle done: {posted} job(s) posted", flush=True)
        except Exception as exc:  # noqa: BLE001 - never exit the loop
            print(f"[scheduler] cycle failed: {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
